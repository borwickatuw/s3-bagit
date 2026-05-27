"""Shared pytest fixtures: in-memory S3 (moto) + bag-builder helpers."""

import hashlib
import io
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
        ).encode("utf-8")

    for algo in algorithms:
        lines = []
        for rel, content in payload.items():
            hasher = hashlib.new(algo)
            hasher.update(content)
            lines.append(f"{hasher.hexdigest()}  data/{rel}\n")
        files[f"manifest-{algo}.txt"] = "".join(lines).encode("utf-8")

    # Tag manifests cover every non-payload file (including bagit.txt and bag-info.txt,
    # but NOT the tagmanifest file itself).
    for algo in algorithms:
        lines = []
        for rel, content in files.items():
            if rel.startswith("data/"):
                continue
            if rel.startswith("tagmanifest-"):
                continue
            hasher = hashlib.new(algo)
            hasher.update(content)
            lines.append(f"{hasher.hexdigest()}  {rel}\n")
        files[f"tagmanifest-{algo}.txt"] = "".join(lines).encode("utf-8")

    return files


def build_tar(
    files: dict[str, bytes],
    *,
    mode: str = "w:gz",
    wrap_prefix: str = "",
) -> bytes:
    """Serialize *files* as a tar archive in memory.

    *mode* is passed directly to :func:`tarfile.open` and selects the
    compression — ``"w"`` for plain tar, ``"w:gz"`` / ``"w:bz2"`` / ``"w:xz"``
    for the compressed variants. If *wrap_prefix* is non-empty, every
    member name is prefixed with ``<wrap_prefix>/`` — used to test bags
    wrapped in a top-level dir.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:
        for name, content in files.items():
            member_name = f"{wrap_prefix}/{name}" if wrap_prefix else name
            info = tarfile.TarInfo(name=member_name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def build_tar_gz(files: dict[str, bytes], *, wrap_prefix: str = "") -> bytes:
    """Shortcut for :func:`build_tar` with ``mode="w:gz"`` (legacy callers)."""
    return build_tar(files, mode="w:gz", wrap_prefix=wrap_prefix)


def build_zip(files: dict[str, bytes], *, wrap_prefix: str = "") -> bytes:
    """Serialize *files* as a zip archive in memory (stdlib zipfile is fine for tests)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            member_name = f"{wrap_prefix}/{name}" if wrap_prefix else name
            zf.writestr(member_name, content)
    return buf.getvalue()


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
    """Serialize *files* as a .7z archive by shelling out to the ``7z`` CLI.

    The 7z format can't be serialized in memory the way tar and zip can
    (the StartHeader at the front references a header at the end), so
    this helper writes the members to a temp dir, invokes ``7z a``, and
    returns the resulting archive bytes. Tests that call it are skipped
    if the ``7z`` CLI is not on ``PATH``.

    See s3-archive's tests/conftest.py for the flavor-by-flavor rationale.
    """
    if shutil.which("7z") is None:
        pytest.skip("7z CLI not installed; skipping .7z fixture")
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
        cmd = ["7z", "a", *SEVEN_Z_FLAVORS[flavor], str(archive), *member_names]
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
