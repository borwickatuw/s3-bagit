"""Tests for streaming extract from S3 to S3."""

import pytest

from kopah_bagit.extract import extract

from .conftest import build_tar_gz, build_zip, make_bag_files


@pytest.fixture
def sample_bag_bytes():
    return make_bag_files({"a.txt": b"hello\n", "sub/b.txt": b"world\n"})


def _extracted_keys(s3, bucket, prefix):
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    return sorted(keys)


def _body(s3, bucket, key):
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


class TestExtractTarGz:
    def test_round_trip(self, s3_client, sample_bag_bytes):
        archive = build_tar_gz(sample_bag_bytes)
        s3_client.put_object(Bucket="src-bucket", Key="in/bag.tar.gz", Body=archive)

        members = extract(s3_client, "src-bucket", "in/bag.tar.gz", "dest-bucket", "out/", "tar.gz")

        # Every input file landed at dest-bucket/out/<name>.
        assert set(members) == set(sample_bag_bytes)
        keys = _extracted_keys(s3_client, "dest-bucket", "out/")
        assert "out/data/a.txt" in keys
        assert "out/data/sub/b.txt" in keys
        assert _body(s3_client, "dest-bucket", "out/data/a.txt") == b"hello\n"

    def test_dry_run_uploads_nothing(self, s3_client, sample_bag_bytes):
        archive = build_tar_gz(sample_bag_bytes)
        s3_client.put_object(Bucket="src-bucket", Key="in/bag.tar.gz", Body=archive)

        members = extract(
            s3_client,
            "src-bucket",
            "in/bag.tar.gz",
            "dest-bucket",
            "out/",
            "tar.gz",
            dry_run=True,
        )

        assert set(members) == set(sample_bag_bytes)
        assert _extracted_keys(s3_client, "dest-bucket", "out/") == []

    def test_empty_prefix(self, s3_client, sample_bag_bytes):
        archive = build_tar_gz(sample_bag_bytes)
        s3_client.put_object(Bucket="src-bucket", Key="bag.tar.gz", Body=archive)

        extract(s3_client, "src-bucket", "bag.tar.gz", "dest-bucket", "", "tar.gz")
        keys = _extracted_keys(s3_client, "dest-bucket", "")
        assert "data/a.txt" in keys


class TestExtractZip:
    def test_round_trip(self, s3_client, sample_bag_bytes):
        archive = build_zip(sample_bag_bytes)
        s3_client.put_object(Bucket="src-bucket", Key="in/bag.zip", Body=archive)

        members = extract(s3_client, "src-bucket", "in/bag.zip", "dest-bucket", "out/", "zip")

        assert set(members) == set(sample_bag_bytes)
        keys = _extracted_keys(s3_client, "dest-bucket", "out/")
        assert "out/data/a.txt" in keys
        assert "out/data/sub/b.txt" in keys
        assert _body(s3_client, "dest-bucket", "out/data/a.txt") == b"hello\n"

    def test_dry_run_uploads_nothing(self, s3_client, sample_bag_bytes):
        archive = build_zip(sample_bag_bytes)
        s3_client.put_object(Bucket="src-bucket", Key="in/bag.zip", Body=archive)
        members = extract(
            s3_client,
            "src-bucket",
            "in/bag.zip",
            "dest-bucket",
            "out/",
            "zip",
            dry_run=True,
        )
        assert set(members) == set(sample_bag_bytes)
        assert _extracted_keys(s3_client, "dest-bucket", "out/") == []


def test_unsupported_format_raises(s3_client):
    with pytest.raises(ValueError, match="Unsupported format"):
        extract(s3_client, "src-bucket", "x", "dest-bucket", "", "7z")
