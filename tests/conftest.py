"""Shared pytest fixtures: in-memory S3 (moto) + bag-builder helpers."""

import hashlib
import io
import tarfile
import zipfile

import boto3
import pytest
from moto import mock_aws


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


def build_tar_gz(files: dict[str, bytes], *, wrap_prefix: str = "") -> bytes:
    """Serialize *files* as a tar.gz archive in memory.

    If *wrap_prefix* is non-empty, every member name is prefixed with
    ``<wrap_prefix>/`` — used to test bags wrapped in a top-level dir.
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


def upload_bag_to_prefix(client, bucket: str, prefix: str, files: dict[str, bytes]) -> None:
    """Upload an already-built bag directly to S3 at *bucket/prefix*."""
    if prefix and not prefix.endswith("/"):
        prefix = prefix + "/"
    for rel, content in files.items():
        client.put_object(Bucket=bucket, Key=prefix + rel, Body=content)
