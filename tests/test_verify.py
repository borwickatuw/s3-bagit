"""Tests for BagIt verify against an S3 prefix."""

import hashlib

import pytest

from s3_bagit import verify as verify_mod
from s3_bagit.verify import verify_bag

from .conftest import make_bag_files, upload_bag_to_prefix


@pytest.fixture
def good_bag_payload():
    return {"a.txt": b"alpha\n", "sub/b.txt": b"beta\n"}


def _upload(s3, bucket, prefix, files):
    upload_bag_to_prefix(s3, bucket, prefix, files)


class TestValidBags:
    def test_minimal_bag_sha256(self, s3_client, good_bag_payload):
        files = make_bag_files(good_bag_payload)
        _upload(s3_client, "dest-bucket", "bag/", files)

        result = verify_bag(s3_client, "dest-bucket", "bag/")

        assert result.ok, result.errors
        assert result.declared_version == "1.0"
        assert result.manifest_algorithms == ["sha256"]
        assert result.tagmanifest_algorithms == ["sha256"]
        assert result.payload_file_count == 2
        assert result.payload_total_octets == len(b"alpha\n") + len(b"beta\n")

    def test_multi_algorithm_bag(self, s3_client, good_bag_payload):
        files = make_bag_files(good_bag_payload, algorithms=("md5", "sha256", "sha512"))
        _upload(s3_client, "dest-bucket", "bag/", files)
        result = verify_bag(s3_client, "dest-bucket", "bag/")
        assert result.ok, result.errors
        assert result.manifest_algorithms == ["md5", "sha256", "sha512"]


class TestSinglePassMultiHash:
    """Verify reads each payload file once regardless of manifest count."""

    def test_multi_algorithm_bag_reads_each_file_once(self, s3_client, good_bag_payload):
        files = make_bag_files(good_bag_payload, algorithms=("sha256", "sha512"))
        _upload(s3_client, "dest-bucket", "bag/", files)

        gets: dict[str, int] = {}
        real_get = s3_client.get_object

        def counting_get(**kwargs):
            gets[kwargs["Key"]] = gets.get(kwargs["Key"], 0) + 1
            return real_get(**kwargs)

        s3_client.get_object = counting_get
        result = verify_bag(s3_client, "dest-bucket", "bag/")

        assert result.ok, result.errors
        payload_keys = [k for k in gets if "/data/" in k]
        assert payload_keys, "expected at least one /data/ GET"
        for key in payload_keys:
            assert gets[key] == 1, f"{key} was GET {gets[key]} times, expected 1"

    def test_overlapping_but_not_identical_manifest_sets(self, s3_client, monkeypatch):
        """Per-file fan-out uses only the algorithms that listed each file.

        ``manifest-sha256.txt`` covers ``{a, b}``; ``manifest-sha512.txt`` covers
        ``{a, c}``. ``b`` must never be hashed with sha512 and ``c`` must
        never be hashed with sha256. (The bag itself is RFC-noncompliant
        because each payload file isn't in *every* manifest — covered by
        the existing "present but not listed" check; this test asserts
        the orthogonal per-file algorithm-subset invariant.)
        """
        payload = {
            "a.txt": b"alpha\n",
            "b.txt": b"beta\n",
            "c.txt": b"gamma\n",
        }
        files: dict[str, bytes] = {}
        for rel, content in payload.items():
            files["data/" + rel] = content
        files["bagit.txt"] = b"BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n"
        oxum_octets = sum(len(b) for b in payload.values())
        files["bag-info.txt"] = (
            f"Bagging-Date: 2026-05-22\nPayload-Oxum: {oxum_octets}.{len(payload)}\n"
        ).encode("utf-8")

        sha256_files = ("a.txt", "b.txt")
        sha512_files = ("a.txt", "c.txt")
        files["manifest-sha256.txt"] = "".join(
            f"{hashlib.sha256(payload[r]).hexdigest()}  data/{r}\n" for r in sha256_files
        ).encode("utf-8")
        files["manifest-sha512.txt"] = "".join(
            f"{hashlib.sha512(payload[r]).hexdigest()}  data/{r}\n" for r in sha512_files
        ).encode("utf-8")

        for algo in ("sha256", "sha512"):
            lines = []
            for rel, content in files.items():
                if rel.startswith("data/") or rel.startswith("tagmanifest-"):
                    continue
                hasher = hashlib.new(algo)
                hasher.update(content)
                lines.append(f"{hasher.hexdigest()}  {rel}\n")
            files[f"tagmanifest-{algo}.txt"] = "".join(lines).encode("utf-8")

        _upload(s3_client, "dest-bucket", "bag/", files)

        algorithms_used: dict[str, set[str]] = {}
        real_stream = verify_mod.stream_hash_object

        def recording_stream(client, bucket, key, algorithms, **kw):
            algorithms_used.setdefault(key, set()).update(algorithms)
            return real_stream(client, bucket, key, algorithms, **kw)

        monkeypatch.setattr(verify_mod, "stream_hash_object", recording_stream)
        result = verify_bag(s3_client, "dest-bucket", "bag/")

        assert algorithms_used["bag/data/a.txt"] == {"sha256", "sha512"}
        assert algorithms_used["bag/data/b.txt"] == {"sha256"}
        assert algorithms_used["bag/data/c.txt"] == {"sha512"}
        # Only errors should be the "missing from manifest X" ones — no
        # checksum mismatches, no "file listed but not present" errors.
        for err in result.errors:
            assert "present but not listed" in err, err


class TestStructuralFailures:
    def test_missing_bagit_txt(self, s3_client, good_bag_payload):
        files = make_bag_files(good_bag_payload)
        files.pop("bagit.txt")
        _upload(s3_client, "dest-bucket", "bag/", files)
        result = verify_bag(s3_client, "dest-bucket", "bag/")
        assert not result.ok
        assert any("bagit.txt is missing" in e for e in result.errors)

    def test_no_manifest(self, s3_client, good_bag_payload):
        files = make_bag_files(good_bag_payload)
        del files["manifest-sha256.txt"]
        _upload(s3_client, "dest-bucket", "bag/", files)
        result = verify_bag(s3_client, "dest-bucket", "bag/")
        assert not result.ok
        assert any("No payload manifest" in e for e in result.errors)

    def test_no_data_files(self, s3_client):
        files = make_bag_files({})
        _upload(s3_client, "dest-bucket", "bag/", files)
        result = verify_bag(s3_client, "dest-bucket", "bag/")
        assert not result.ok
        assert any("No payload files" in e for e in result.errors)

    def test_empty_prefix(self, s3_client):
        result = verify_bag(s3_client, "dest-bucket", "nothing-here/")
        assert not result.ok
        assert any("No objects" in e for e in result.errors)


class TestChecksumFailures:
    def test_payload_checksum_mismatch(self, s3_client, good_bag_payload):
        files = make_bag_files(good_bag_payload)
        # Corrupt one payload file *after* the manifest was generated.
        files["data/a.txt"] = b"CORRUPTED\n"
        _upload(s3_client, "dest-bucket", "bag/", files)
        result = verify_bag(s3_client, "dest-bucket", "bag/")
        assert not result.ok
        assert any("checksum mismatch" in e and "a.txt" in e for e in result.errors)

    def test_missing_payload_file_listed_in_manifest(self, s3_client, good_bag_payload):
        files = make_bag_files(good_bag_payload)
        del files["data/a.txt"]
        _upload(s3_client, "dest-bucket", "bag/", files)
        result = verify_bag(s3_client, "dest-bucket", "bag/")
        assert not result.ok
        assert any("listed but not present" in e for e in result.errors)

    def test_unlisted_payload_file(self, s3_client, good_bag_payload):
        files = make_bag_files(good_bag_payload)
        # Add a file under data/ that's NOT in any manifest.
        files["data/stowaway.txt"] = b"stowaway\n"
        # Need to regenerate the tagmanifest because we're modifying tags too...
        # Actually we only added a data/ file so tagmanifest is still consistent.
        _upload(s3_client, "dest-bucket", "bag/", files)
        result = verify_bag(s3_client, "dest-bucket", "bag/")
        assert not result.ok
        assert any("present but not listed" in e and "stowaway" in e for e in result.errors)

    def test_tagmanifest_mismatch_when_bag_info_modified(self, s3_client, good_bag_payload):
        files = make_bag_files(good_bag_payload)
        # Modify bag-info.txt without updating tagmanifest. Also keep payload-oxum
        # numerically valid by replacing with another field instead of breaking oxum.
        files["bag-info.txt"] = b"Bagging-Date: 2026-05-22\nPayload-Oxum: 11.2\nExtra: x\n"
        _upload(s3_client, "dest-bucket", "bag/", files)
        result = verify_bag(s3_client, "dest-bucket", "bag/")
        assert not result.ok
        assert any("tagmanifest" in e and "bag-info.txt" in e for e in result.errors)


class TestPayloadOxum:
    def test_oxum_mismatch(self, s3_client, good_bag_payload):
        files = make_bag_files(good_bag_payload)
        # Recompute tagmanifest *with* the bogus bag-info so the tagmanifest
        # itself isn't what trips the error.
        files["bag-info.txt"] = b"Bagging-Date: 2026-05-22\nPayload-Oxum: 999.99\n"
        # Regenerate tagmanifest-sha256.txt to match.
        lines = []
        for rel, content in files.items():
            if rel.startswith("data/") or rel.startswith("tagmanifest-"):
                continue
            lines.append(f"{hashlib.sha256(content).hexdigest()}  {rel}\n")
        files["tagmanifest-sha256.txt"] = "".join(lines).encode("utf-8")

        _upload(s3_client, "dest-bucket", "bag/", files)
        result = verify_bag(s3_client, "dest-bucket", "bag/")
        assert not result.ok
        assert any("Payload-Oxum mismatch" in e for e in result.errors)

    def test_bag_without_bag_info_skips_oxum(self, s3_client, good_bag_payload):
        files = make_bag_files(good_bag_payload, include_bag_info=False)
        _upload(s3_client, "dest-bucket", "bag/", files)
        result = verify_bag(s3_client, "dest-bucket", "bag/")
        assert result.ok, result.errors


class TestFetchTxt:
    def test_non_empty_fetch_fails_loud(self, s3_client, good_bag_payload):
        files = make_bag_files(
            good_bag_payload, extra_tags={"fetch.txt": b"http://x  10  data/x\n"}
        )
        # Add fetch.txt to the tagmanifest manually so the tagmanifest itself passes.
        lines = []
        for rel, content in files.items():
            if rel.startswith("data/") or rel.startswith("tagmanifest-"):
                continue
            lines.append(f"{hashlib.sha256(content).hexdigest()}  {rel}\n")
        files["tagmanifest-sha256.txt"] = "".join(lines).encode("utf-8")

        _upload(s3_client, "dest-bucket", "bag/", files)
        result = verify_bag(s3_client, "dest-bucket", "bag/")
        assert not result.ok
        assert any("fetch.txt" in e for e in result.errors)
