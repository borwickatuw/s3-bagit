"""Tests for streaming bag creation (S3 prefix → BagIt .tar.gz in S3)."""

import hashlib
import io
import tarfile

import pytest

from s3_archive.extract import extract

from s3_bagit.create_bag import _encode_manifest_path, create_bag
from s3_bagit.exceptions import BagError
from s3_bagit.verify import verify_bag


def _put_source_files(client, bucket: str, prefix: str, files: dict[str, bytes]) -> None:
    for rel, content in files.items():
        client.put_object(Bucket=bucket, Key=prefix + rel, Body=content)


def _download_tar_gz(client, bucket: str, key: str) -> bytes:
    return client.get_object(Bucket=bucket, Key=key)["Body"].read()


def _read_tar_members(archive: bytes) -> dict[str, bytes]:
    """Return ``{member_name: body}`` from a .tar.gz archive's full bytes."""
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            fobj = tar.extractfile(member)
            assert fobj is not None
            out[member.name] = fobj.read()
    return out


class TestCreateBagRoundTrip:
    """The strongest correctness check: produced bags pass `verify_bag` after re-extraction."""

    def test_basic_round_trip(self, s3_client):
        files = {"a.txt": b"alpha\n", "sub/b.txt": b"beta\n"}
        _put_source_files(s3_client, "src-bucket", "incoming/source-dir/", files)

        count, octets = create_bag(
            s3_client,
            "src-bucket",
            "incoming/source-dir/",
            "dest-bucket",
            "bags/my-bag.tar.gz",
            bag_name="my-bag",
        )

        assert count == 2
        assert octets == len(b"alpha\n") + len(b"beta\n")

        # Re-extract the produced archive into a fresh prefix and verify.
        extract(
            s3_client,
            "dest-bucket",
            "bags/my-bag.tar.gz",
            "dest-bucket",
            "extracted/",
            "tar.gz",
        )
        result = verify_bag(s3_client, "dest-bucket", "extracted/my-bag/")
        assert result.ok, f"verify failed: {result.errors}"
        assert result.payload_file_count == 2
        assert result.declared_version == "1.0"

    def test_manifest_checksums_match_payload(self, s3_client):
        files = {"a.txt": b"alpha\n", "b.txt": b"beta\n"}
        _put_source_files(s3_client, "src-bucket", "src/", files)

        create_bag(
            s3_client,
            "src-bucket",
            "src/",
            "dest-bucket",
            "out.tar.gz",
            bag_name="bag",
        )

        members = _read_tar_members(_download_tar_gz(s3_client, "dest-bucket", "out.tar.gz"))
        manifest = members["bag/manifest-sha256.txt"].decode()

        for rel, content in files.items():
            expected = hashlib.sha256(content).hexdigest()
            assert f"{expected}  data/{rel}" in manifest

    def test_tag_files_trailing_in_tar(self, s3_client):
        """The whole point of single-pass — tag files MUST appear after data/ members."""
        files = {"a.txt": b"x" * 1000, "b.txt": b"y" * 1000}
        _put_source_files(s3_client, "src-bucket", "src/", files)
        create_bag(
            s3_client,
            "src-bucket",
            "src/",
            "dest-bucket",
            "out.tar.gz",
            bag_name="bag",
        )

        # Read the tar in order; record the positions of data/ vs tag members.
        with tarfile.open(
            fileobj=io.BytesIO(_download_tar_gz(s3_client, "dest-bucket", "out.tar.gz")),
            mode="r:gz",
        ) as tar:
            order = [m.name for m in tar]

        last_data_idx = max(i for i, n in enumerate(order) if "/data/" in n)
        first_tag_idx = min(
            i
            for i, n in enumerate(order)
            if n.endswith("bagit.txt")
            or n.endswith("bag-info.txt")
            or "/manifest-" in n
            or "/tagmanifest-" in n
        )
        assert last_data_idx < first_tag_idx, f"tag files must come after data/, got order: {order}"


class TestEmptyPrefix:
    def test_empty_source_raises_bag_error(self, s3_client):
        # src-bucket exists from the fixture but has no objects.
        with pytest.raises(BagError, match="empty; nothing to bag"):
            create_bag(
                s3_client,
                "src-bucket",
                "empty-dir/",
                "dest-bucket",
                "out.tar.gz",
                bag_name="bag",
            )


class TestBagInfoOverrides:
    def test_default_bag_info_fields_present(self, s3_client):
        _put_source_files(s3_client, "src-bucket", "src/", {"x.txt": b"data"})
        create_bag(
            s3_client,
            "src-bucket",
            "src/",
            "dest-bucket",
            "out.tar.gz",
            bag_name="bag",
        )
        members = _read_tar_members(_download_tar_gz(s3_client, "dest-bucket", "out.tar.gz"))
        bag_info = members["bag/bag-info.txt"].decode()

        assert "Bag-Software-Agent: s3-bagit " in bag_info
        assert "Bagging-Date: " in bag_info
        assert "Payload-Oxum: 4.1" in bag_info

    def test_user_bag_info_overrides_default_and_appends(self, s3_client):
        _put_source_files(s3_client, "src-bucket", "src/", {"x.txt": b"data"})
        create_bag(
            s3_client,
            "src-bucket",
            "src/",
            "dest-bucket",
            "out.tar.gz",
            bag_name="bag",
            bag_info=[
                ("Bagging-Date", "1999-01-01"),
                ("Source-Organization", "UW Libraries"),
            ],
        )
        members = _read_tar_members(_download_tar_gz(s3_client, "dest-bucket", "out.tar.gz"))
        bag_info = members["bag/bag-info.txt"].decode()

        # User Bagging-Date replaced the default; default is NOT also emitted.
        assert "Bagging-Date: 1999-01-01" in bag_info
        assert bag_info.count("Bagging-Date:") == 1
        # New labels are appended.
        assert "Source-Organization: UW Libraries" in bag_info
        # Untouched defaults still emitted.
        assert "Payload-Oxum: 4.1" in bag_info
        assert "Bag-Software-Agent: s3-bagit " in bag_info


class TestSha512:
    def test_sha512_algorithm(self, s3_client):
        _put_source_files(s3_client, "src-bucket", "src/", {"x.txt": b"data"})
        create_bag(
            s3_client,
            "src-bucket",
            "src/",
            "dest-bucket",
            "out.tar.gz",
            bag_name="bag",
            algorithm="sha512",
        )
        extract(s3_client, "dest-bucket", "out.tar.gz", "dest-bucket", "ext/", "tar.gz")
        result = verify_bag(s3_client, "dest-bucket", "ext/bag/")
        assert result.ok, result.errors
        assert result.manifest_algorithms == ["sha512"]


class TestBagNameValidation:
    @pytest.mark.parametrize("bad", ["", "with/slash", "leading/slash/"])
    def test_invalid_bag_name(self, s3_client, bad):
        _put_source_files(s3_client, "src-bucket", "src/", {"x.txt": b"data"})
        with pytest.raises(BagError, match="single path segment"):
            create_bag(
                s3_client,
                "src-bucket",
                "src/",
                "dest-bucket",
                "out.tar.gz",
                bag_name=bad,
            )


class TestUnknownAlgorithm:
    def test_unknown_algorithm_fails_fast(self, s3_client):
        # No source files needed — algorithm is validated before listing.
        with pytest.raises(BagError, match="Unknown hash algorithm"):
            create_bag(
                s3_client,
                "src-bucket",
                "src/",
                "dest-bucket",
                "out.tar.gz",
                bag_name="bag",
                algorithm="not-a-real-algo",
            )


class TestManifestPathEncoding:
    def test_percent_encoding(self):
        assert _encode_manifest_path("simple.txt") == "simple.txt"
        assert _encode_manifest_path("with space.txt") == "with space.txt"
        # Order matters: "%" must be encoded first so we don't double-encode
        # the "%" we just introduced for the LF.
        assert _encode_manifest_path("a\nb") == "a%0Ab"
        assert _encode_manifest_path("a%b") == "a%25b"
        assert _encode_manifest_path("a\r\nb") == "a%0D%0Ab"
