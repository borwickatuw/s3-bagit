"""Tests for `verify-against`: bag-in-S3 + flat-target-in-S3 → manifest match."""

import pytest

from s3_bagit.create_bag import create_bag
from s3_bagit.verify_against import _is_tag_file_name, verify_against

from .conftest import SEVEN_Z_FLAVORS, build_7z, build_tar_gz, build_zip, make_bag_files


def _put_target_files(client, bucket: str, prefix: str, files: dict[str, bytes]) -> None:
    for rel, content in files.items():
        client.put_object(Bucket=bucket, Key=prefix + rel, Body=content)


def _put_archive(client, bucket: str, key: str, body: bytes) -> None:
    client.put_object(Bucket=bucket, Key=key, Body=body)


def _make_bag_from_payload(client, src_prefix: str, payload: dict[str, bytes]) -> str:
    """Use create_bag to lay down a bag in S3 from a freshly-uploaded payload prefix.

    Returns the destination archive key for the caller's convenience.
    """
    _put_target_files(client, "src-bucket", src_prefix, payload)
    archive_key = "bags/my-bag.tar.gz"
    create_bag(
        client,
        client,
        "src-bucket",
        src_prefix,
        "dest-bucket",
        archive_key,
        bag_name="my-bag",
    )
    return archive_key


class TestRoundTrip:
    """Bag a payload, then `verify-against` the same payload prefix."""

    def test_matching_target_is_valid(self, s3_client):
        payload = {"a.txt": b"alpha\n", "sub/b.txt": b"beta\n"}
        archive_key = _make_bag_from_payload(s3_client, "src/", payload)

        result = verify_against(
            s3_client,
            s3_client,
            "dest-bucket",
            archive_key,
            "tar.gz",
            "src-bucket",
            "src/",
            archive_url=f"s3://dest-bucket/{archive_key}",
            target_url="s3://src-bucket/src/",
        )
        assert result.ok, result.errors
        assert result.declared_version == "1.0"
        assert result.manifest_algorithms == ["sha256"]
        assert result.payload_file_count == 2

    def test_different_payload_prefix_works(self, s3_client):
        """The target prefix can be a different location from create-bag's source."""
        # Build the bag from one prefix...
        payload = {"a.txt": b"alpha\n"}
        archive_key = _make_bag_from_payload(s3_client, "src/", payload)
        # ...then upload the identical bytes to a different prefix and verify.
        _put_target_files(s3_client, "src-bucket", "elsewhere/", payload)

        result = verify_against(
            s3_client,
            s3_client,
            "dest-bucket",
            archive_key,
            "tar.gz",
            "src-bucket",
            "elsewhere/",
            archive_url=f"s3://dest-bucket/{archive_key}",
            target_url="s3://src-bucket/elsewhere/",
        )
        assert result.ok, result.errors


class TestMismatch:
    def test_checksum_mismatch_reported(self, s3_client):
        archive_key = _make_bag_from_payload(s3_client, "src/", {"a.txt": b"alpha\n"})
        # Overwrite the target file with different bytes.
        s3_client.put_object(Bucket="src-bucket", Key="src/a.txt", Body=b"corrupted\n")

        result = verify_against(
            s3_client,
            s3_client,
            "dest-bucket",
            archive_key,
            "tar.gz",
            "src-bucket",
            "src/",
            archive_url=f"s3://dest-bucket/{archive_key}",
            target_url="s3://src-bucket/src/",
        )
        assert not result.ok
        assert any("checksum mismatch" in e for e in result.errors)
        assert any("data/a.txt" in e for e in result.errors)

    def test_missing_target_file_reported(self, s3_client):
        archive_key = _make_bag_from_payload(
            s3_client, "src/", {"a.txt": b"alpha\n", "b.txt": b"beta\n"}
        )
        # Delete one file from the target.
        s3_client.delete_object(Bucket="src-bucket", Key="src/b.txt")

        result = verify_against(
            s3_client,
            s3_client,
            "dest-bucket",
            archive_key,
            "tar.gz",
            "src-bucket",
            "src/",
            archive_url=f"s3://dest-bucket/{archive_key}",
            target_url="s3://src-bucket/src/",
        )
        assert not result.ok
        assert any(
            "manifest lists data/b.txt but target has no such file" in e for e in result.errors
        )

    def test_extra_target_file_is_error(self, s3_client):
        archive_key = _make_bag_from_payload(s3_client, "src/", {"a.txt": b"alpha\n"})
        # Drop in an extra file the manifest doesn't know about.
        s3_client.put_object(Bucket="src-bucket", Key="src/extra.txt", Body=b"surprise")

        result = verify_against(
            s3_client,
            s3_client,
            "dest-bucket",
            archive_key,
            "tar.gz",
            "src-bucket",
            "src/",
            archive_url=f"s3://dest-bucket/{archive_key}",
            target_url="s3://src-bucket/src/",
        )
        assert not result.ok
        assert any("no manifest entry: 'extra.txt'" in e for e in result.errors)


class TestDataPrefixHeuristic:
    def test_warning_when_target_has_data_segment(self, s3_client):
        # Build a bag whose payload is at src/. We point verify-against at
        # an unrelated target that has /data/ in the URL — the verify
        # itself will fail for "no objects", but the warning is the point.
        archive_key = _make_bag_from_payload(s3_client, "src/", {"a.txt": b"alpha"})

        result = verify_against(
            s3_client,
            s3_client,
            "dest-bucket",
            archive_key,
            "tar.gz",
            "src-bucket",
            "some/bag/data/",
            archive_url=f"s3://dest-bucket/{archive_key}",
            target_url="s3://src-bucket/some/bag/data/",
        )
        assert any("/data/" in w for w in result.warnings), result.warnings

    def test_no_warning_for_normal_prefix(self, s3_client):
        archive_key = _make_bag_from_payload(s3_client, "src/", {"a.txt": b"alpha\n"})

        result = verify_against(
            s3_client,
            s3_client,
            "dest-bucket",
            archive_key,
            "tar.gz",
            "src-bucket",
            "src/",
            archive_url=f"s3://dest-bucket/{archive_key}",
            target_url="s3://src-bucket/src/",
        )
        assert not any("/data/" in w for w in result.warnings)


class TestArchiveErrors:
    def test_archive_without_bagit_txt_fails(self, s3_client):
        # An archive that's a tarball but not a BagIt bag.
        files = {"random.txt": b"not a bag"}
        _put_archive(s3_client, "src-bucket", "in/junk.tar.gz", build_tar_gz(files))

        result = verify_against(
            s3_client,
            s3_client,
            "src-bucket",
            "in/junk.tar.gz",
            "tar.gz",
            "src-bucket",
            "src/",
            archive_url="s3://src-bucket/in/junk.tar.gz",
            target_url="s3://src-bucket/src/",
        )
        assert not result.ok
        assert any("No bagit.txt" in e for e in result.errors)

    def test_empty_target_fails(self, s3_client):
        archive_key = _make_bag_from_payload(s3_client, "src/", {"a.txt": b"alpha\n"})
        # `src-bucket` exists; `empty/` has no files.
        result = verify_against(
            s3_client,
            s3_client,
            "dest-bucket",
            archive_key,
            "tar.gz",
            "src-bucket",
            "empty/",
            archive_url=f"s3://dest-bucket/{archive_key}",
            target_url="s3://src-bucket/empty/",
        )
        assert not result.ok
        assert any("No objects found" in e for e in result.errors)


class TestMultiAlgorithmBag:
    def test_sha256_and_sha512_both_checked(self, s3_client):
        # Build a bag by hand with both manifests (create-bag only emits one).
        payload = {"a.txt": b"alpha\n", "b.txt": b"beta\n"}
        files = make_bag_files(payload, algorithms=("sha256", "sha512"))
        archive = build_tar_gz(files, wrap_prefix="my-bag")
        _put_archive(s3_client, "dest-bucket", "bag.tar.gz", archive)
        # Upload the matching flat payload to the target prefix.
        _put_target_files(s3_client, "src-bucket", "flat/", payload)

        result = verify_against(
            s3_client,
            s3_client,
            "dest-bucket",
            "bag.tar.gz",
            "tar.gz",
            "src-bucket",
            "flat/",
            archive_url="s3://dest-bucket/bag.tar.gz",
            target_url="s3://src-bucket/flat/",
        )
        assert result.ok, result.errors
        assert result.manifest_algorithms == ["sha256", "sha512"]


class TestZipBag:
    def test_zip_archive(self, s3_client):
        payload = {"a.txt": b"alpha\n"}
        files = make_bag_files(payload)
        archive = build_zip(files, wrap_prefix="my-bag")
        _put_archive(s3_client, "dest-bucket", "bag.zip", archive)
        _put_target_files(s3_client, "src-bucket", "flat/", payload)

        result = verify_against(
            s3_client,
            s3_client,
            "dest-bucket",
            "bag.zip",
            "zip",
            "src-bucket",
            "flat/",
            archive_url="s3://dest-bucket/bag.zip",
            target_url="s3://src-bucket/flat/",
        )
        assert result.ok, result.errors


class TestSevenZBag:
    @pytest.mark.parametrize("flavor", sorted(SEVEN_Z_FLAVORS))
    def test_seven_z_archive(self, s3_client, flavor):
        """verify-against should treat .7z bags the same as tar.gz / zip."""
        payload = {"a.txt": b"alpha\n", "sub/b.txt": b"beta\n"}
        files = make_bag_files(payload)
        archive = build_7z(files, flavor=flavor, wrap_prefix="my-bag")
        _put_archive(s3_client, "dest-bucket", "bag.7z", archive)
        _put_target_files(s3_client, "src-bucket", "flat/", payload)

        result = verify_against(
            s3_client,
            s3_client,
            "dest-bucket",
            "bag.7z",
            "7z",
            "src-bucket",
            "flat/",
            archive_url="s3://dest-bucket/bag.7z",
            target_url="s3://src-bucket/flat/",
        )
        assert result.ok, result.errors


class TestCorruptedArchive:
    def test_corrupted_archive_surfaces_as_result_fail(self, s3_client):
        """A truncated archive should produce ``Could not read archive: ...``,
        not propagate the underlying decoder exception."""
        # Truncate a valid tar.gz so the gzip stream is incomplete.
        files = make_bag_files({"a.txt": b"alpha\n"})
        archive = build_tar_gz(files, wrap_prefix="my-bag")
        _put_archive(s3_client, "src-bucket", "bag.tar.gz", archive[:64])

        result = verify_against(
            s3_client,
            s3_client,
            "src-bucket",
            "bag.tar.gz",
            "tar.gz",
            "src-bucket",
            "flat/",
            archive_url="s3://src-bucket/bag.tar.gz",
            target_url="s3://src-bucket/flat/",
        )
        assert not result.ok
        assert any("Could not read archive" in e for e in result.errors), result.errors


class TestPayloadOxum:
    def test_oxum_mismatch_reported(self, s3_client):
        # Hand-build a bag whose bag-info.txt lies about Payload-Oxum so we
        # can prove the check is wired up (create_bag's output is always
        # internally consistent).
        payload = {"a.txt": b"alpha\n"}
        files = make_bag_files(payload, include_bag_info=False)
        files["bag-info.txt"] = b"Payload-Oxum: 999.42\n"
        # Tagmanifests must cover bag-info.txt — rebuild them.
        files = make_bag_files(
            payload,
            include_bag_info=False,
            extra_tags={"bag-info.txt": b"Payload-Oxum: 999.42\n"},
        )
        archive = build_tar_gz(files, wrap_prefix="my-bag")
        _put_archive(s3_client, "dest-bucket", "bag.tar.gz", archive)
        _put_target_files(s3_client, "src-bucket", "flat/", payload)

        result = verify_against(
            s3_client,
            s3_client,
            "dest-bucket",
            "bag.tar.gz",
            "tar.gz",
            "src-bucket",
            "flat/",
            archive_url="s3://dest-bucket/bag.tar.gz",
            target_url="s3://src-bucket/flat/",
        )
        assert not result.ok
        assert any("Payload-Oxum mismatch" in e for e in result.errors)


class TestTagFileNameDetection:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("bagit.txt", True),
            ("my-bag/bagit.txt", True),
            ("my-bag/bag-info.txt", True),
            ("my-bag/manifest-sha256.txt", True),
            ("my-bag/tagmanifest-sha512.txt", True),
            ("my-bag/fetch.txt", True),
            ("my-bag/data/foo.txt", False),
            ("data/bagit.txt", False),  # under data/ — payload-shaped
            ("my-bag/data/sub/manifest-fake.txt", False),  # nested under data/
            ("my-bag/some-other-tag.txt", False),  # doesn't match known names
        ],
    )
    def test_is_tag_file_name(self, name, expected):
        assert _is_tag_file_name(name) is expected
