"""Shared pytest fixtures: in-memory S3 (moto) + bag-builder helpers."""

import hashlib
import io
import logging
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from moto.server import ThreadedMotoServer

from s3_archive.s3_client import _reset_client_cache

from s3_bagit import log_config


@pytest.fixture(autouse=True)
def _clear_client_cache():
    """Drop any cached boto3 clients between tests.

    `s3_archive.s3_client` keeps a module-level dict of profile → client
    for the lifetime of the process. Without this fixture a client
    built against one test's moto context would leak into the next,
    pointing at a torn-down endpoint and producing baffling failures.
    """
    _reset_client_cache()
    yield
    _reset_client_cache()


@pytest.fixture(autouse=True)
def _quiet_logging():
    """Keep CLI logging out of the test run -- and out of the next test.

    ``cli.main()`` calls ``log_config.setup_console()``, which sets the root
    logger's level, attaches a handler, and latches a module-level once-flag.
    Left alone, the first CLI test configures logging for the whole session:
    botocore's INFO records ("Found credentials in environment variables")
    reach stderr, and a ``--verbose`` test leaves the root logger at DEBUG for
    every test after it. No test asserts on log records -- the CLI's operator
    output is ``print``, not logging -- so suppress outright per
    PYTHON-TESTING.md #5 and hand the root logger back as it was found.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        # Re-arm the once-flag so the next test reaches setup_console's real
        # body instead of its early return.
        log_config._console_initialized = False


@pytest.fixture
def aws_creds(monkeypatch):
    """Set bogus AWS credentials so moto + boto3 don't try the real chain."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def s3_client(aws_creds):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="src-bucket")
        client.create_bucket(Bucket="dest-bucket")
        yield client


@pytest.fixture
def cross_env_real_endpoints(tmp_path, monkeypatch):
    """Two real moto-server endpoints + two `~/.s3cfg-*` files.

    Mirrors s3-archive's fixture of the same name. Exercises the real
    two-client path end-to-end: each profile resolves to a distinct
    boto3 client pointed at a different `ThreadedMotoServer` instance.
    Use sparingly — the end-to-end acceptance test in
    ``tests/test_cross_endpoint.py`` is the primary consumer.

    Yields a dict::

        {
            "src": {"profile": "src-env", "client": <boto3>, "bucket": "src-bucket"},
            "dst": {"profile": "dst-env", "client": <boto3>, "bucket": "dst-bucket"},
        }
    """
    src_server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    dst_server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    src_server.start()
    dst_server.start()
    try:
        src_port = src_server._server.socket.getsockname()[1]
        dst_port = dst_server._server.socket.getsockname()[1]
        src_endpoint = f"http://127.0.0.1:{src_port}"
        dst_endpoint = f"http://127.0.0.1:{dst_port}"

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("S3CMD_CONFIG", raising=False)

        src_client = boto3.client(
            "s3",
            endpoint_url=src_endpoint,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            region_name="us-east-1",
        )
        dst_client = boto3.client(
            "s3",
            endpoint_url=dst_endpoint,
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
            region_name="us-east-1",
        )

        # Distinct bucket names per side so cross-talk fails with NoSuchBucket.
        src_client.create_bucket(Bucket="src-bucket")
        dst_client.create_bucket(Bucket="dst-bucket")

        _reset_client_cache()
        clients_by_profile = {"src-env": src_client, "dst-env": dst_client}

        def _client_for(profile):
            key = profile if profile is not None else "default"
            if key not in clients_by_profile:
                raise KeyError(f"unexpected profile {key!r} in test")
            return clients_by_profile[key]

        monkeypatch.setattr("s3_archive.s3_client.client_for", _client_for)
        monkeypatch.setattr("s3_bagit.cli.client_for", _client_for)

        yield {
            "src": {"profile": "src-env", "client": src_client, "bucket": "src-bucket"},
            "dst": {"profile": "dst-env", "client": dst_client, "bucket": "dst-bucket"},
        }
    finally:
        src_server.stop()
        dst_server.stop()


# ---------------------------------------------------------------------------
# Bag construction helpers
# ---------------------------------------------------------------------------


def rebuild_tagmanifests(
    files: dict[str, bytes], algorithms: tuple[str, ...] = ("sha256",)
) -> None:
    """Recompute every ``tagmanifest-<algo>.txt`` in *files*, in place.

    Tag manifests cover every non-payload file (including ``bagit.txt``
    and ``bag-info.txt``, but NOT the tagmanifest files themselves).

    ``make_bag_files`` calls this once, at build time. A test that mutates
    a tag file afterwards — ``bagit.txt``, ``bag-info.txt``, a payload
    manifest — must call it again, or an incidental tagmanifest mismatch
    joins the defect set under test and muddies the count. This is that
    entry point: tests must never re-implement the loop.
    """
    for algo in algorithms:
        files[f"tagmanifest-{algo}.txt"] = "".join(
            f"{hashlib.new(algo, content).hexdigest()}  {rel}\n"
            for rel, content in files.items()
            if not rel.startswith(("data/", "tagmanifest-"))
        ).encode("utf-8")


def make_bag_files(
    payload: dict[str, bytes],
    *,
    extra_tags: dict[str, bytes] | None = None,
    include_bag_info: bool = True,
    algorithms: tuple[str, ...] = ("sha256",),
) -> dict[str, bytes]:
    """Build a dict of ``relative_path -> bytes`` representing a valid BagIt bag.

    Caller specifies the *payload* (without the ``data/`` prefix). The
    helper writes ``bagit.txt``, the requested manifest(s), and (by
    default) ``bag-info.txt`` with a correct ``Payload-Oxum``.
    """
    files: dict[str, bytes] = {}
    for rel, content in payload.items():
        files["data/" + rel] = content

    files["bagit.txt"] = b"BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n"

    if extra_tags:
        for rel, content in extra_tags.items():
            files[rel] = content

    if include_bag_info:
        oxum_octets = sum(len(b) for b in payload.values())
        oxum_count = len(payload)
        files["bag-info.txt"] = (
            f"Bagging-Date: 2026-05-22\nPayload-Oxum: {oxum_octets}.{oxum_count}\n"
        ).encode()

    for algo in algorithms:
        files[f"manifest-{algo}.txt"] = "".join(
            f"{hashlib.new(algo, content).hexdigest()}  data/{rel}\n"
            for rel, content in payload.items()
        ).encode("utf-8")

    rebuild_tagmanifests(files, algorithms)

    return files


def build_tar_gz(files: dict[str, bytes], *, wrap_prefix: str = "") -> bytes:
    """Serialize *files* as a gzipped tar archive in memory.

    If *wrap_prefix* is non-empty, every member name is prefixed with
    ``<wrap_prefix>/`` — used to test bags wrapped in a top-level dir.

    s3-archive's conftest also carries a general ``build_tar(mode=...)``
    because it exercises plain/bz2/xz tars; s3-bagit's fixtures are
    gzip-only, so the mode knob is not carried over.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            member_name = f"{wrap_prefix}/{name}" if wrap_prefix else name
            info = tarfile.TarInfo(name=member_name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def build_zip(files: dict[str, bytes], *, wrap_prefix: str = "") -> bytes:
    """Serialize *files* as a zip archive in memory (stdlib zipfile is fine for tests)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            member_name = f"{wrap_prefix}/{name}" if wrap_prefix else name
            zf.writestr(member_name, content)
    return buf.getvalue()


# The 7-Zip CLI ships under two names: Homebrew's `sevenzip` formula and
# 7-Zip's own Linux/macOS builds install `7zz`; p7zip and most Linux distro
# packages install `7z`. Prefer `7zz` — it is the current upstream binary —
# and accept `7z`. Resolved once at import; mirrors s3-archive's conftest.
SEVEN_Z_CLI_NAMES: tuple[str, ...] = ("7zz", "7z")
SEVEN_Z_CLI: str | None = next(
    (name for name in SEVEN_Z_CLI_NAMES if shutil.which(name) is not None), None
)


# 7z archive flavors exercising different code paths in py7zr / SeekableS3Object.
# Mirrors s3-archive's tests/conftest.py:SEVEN_Z_FLAVORS so behavior is identical
# across the two test suites.
SEVEN_Z_FLAVORS: dict[str, list[str]] = {
    "solid": [],
    "nonsolid": ["-ms=off"],
    "plain_header": ["-mhc=off"],
    "solid_bcj": ["-m0=BCJ", "-m1=LZMA2"],
}


def build_7z(
    files: dict[str, bytes],
    *,
    flavor: str = "solid",
    wrap_prefix: str = "",
) -> bytes:
    """Serialize *files* as a .7z archive by shelling out to the 7-Zip CLI.

    The 7z format can't be serialized in memory the way tar and zip can
    (the StartHeader at the front references a header at the end), so
    this helper writes the members to a temp dir, invokes ``7zz a`` (or
    ``7z a`` — see :data:`SEVEN_Z_CLI`), and returns the resulting
    archive bytes. Tests that call it are skipped if neither CLI name is
    on ``PATH``.

    See s3-archive's tests/conftest.py for the flavor-by-flavor rationale.
    """
    if SEVEN_Z_CLI is None:
        pytest.skip("7-Zip CLI (7zz or 7z) not installed; skipping .7z fixture")
    if flavor not in SEVEN_Z_FLAVORS:
        raise ValueError(f"Unknown 7z flavor {flavor!r}; expected one of {sorted(SEVEN_Z_FLAVORS)}")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        src = tmpdir / "src"
        src.mkdir()
        member_names: list[str] = []
        for name, content in files.items():
            member_name = f"{wrap_prefix}/{name}" if wrap_prefix else name
            target = src / member_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            member_names.append(member_name)

        archive = tmpdir / "out.7z"
        cmd = [SEVEN_Z_CLI, "a", *SEVEN_Z_FLAVORS[flavor], str(archive), *member_names]
        # cmd is built from constants and dict keys controlled by the test;
        # not user input.
        subprocess.run(cmd, check=True, capture_output=True, cwd=src)  # noqa: S603
        return archive.read_bytes()


def upload_bag_to_prefix(client, bucket: str, prefix: str, files: dict[str, bytes]) -> None:
    """Upload an already-built bag directly to S3 at *bucket/prefix*."""
    if prefix and not prefix.endswith("/"):
        prefix = prefix + "/"
    for rel, content in files.items():
        client.put_object(Bucket=bucket, Key=prefix + rel, Body=content)
