"""Tests for `verify-against`: bag-in-S3 + flat-target-in-S3 → manifest match."""

import hashlib

import pytest

from s3_bagit import verify_against as va_mod
from s3_bagit.create_bag import create_bag
from s3_bagit.verify_against import _is_tag_file_name, verify_against

from .conftest import (
    SEVEN_Z_FLAVORS,
    build_7z,
    build_tar_gz,
    build_zip,
    make_bag_files,
    rebuild_tagmanifests,
)


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


def _stage_bag_archive(
    s3, files: dict[str, bytes], *, key: str = "bag.tar.gz", wrap_prefix: str = "my-bag"
) -> None:
    """Serialize *files* as a tar.gz bag and put it at ``dest-bucket/<key>``."""
    _put_archive(s3, "dest-bucket", key, build_tar_gz(files, wrap_prefix=wrap_prefix))


def _run_verify_against(
    s3,
    *,
    archive_bucket: str = "dest-bucket",
    archive_key: str = "bag.tar.gz",
    fmt: str = "tar.gz",
    target_bucket: str = "src-bucket",
    target_prefix: str = "flat/",
):
    """Single-endpoint ``verify_against`` with the URLs derived, not repeated.

    ``verify_against`` takes seven positional arguments plus two derived
    URL keywords; spelled out at every call site that is ten lines of
    noise per test. Every knob a test actually varies is a keyword here.
    """
    return verify_against(
        s3,
        s3,
        archive_bucket,
        archive_key,
        fmt,
        target_bucket,
        target_prefix,
        archive_url=f"s3://{archive_bucket}/{archive_key}",
        target_url=f"s3://{target_bucket}/{target_prefix}",
    )


class TestRoundTrip:
    """Bag a payload, then `verify-against` the same payload prefix."""

    def test_matching_target_is_valid(self, s3_client):
        payload = {"a.txt": b"alpha\n", "sub/b.txt": b"beta\n"}
        archive_key = _make_bag_from_payload(s3_client, "src/", payload)

        result = _run_verify_against(s3_client, archive_key=archive_key, target_prefix="src/")
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

        result = _run_verify_against(s3_client, archive_key=archive_key, target_prefix="elsewhere/")
        assert result.ok, result.errors


_MISMATCH_PAYLOAD = {"a.txt": b"alpha\n", "b.txt": b"beta\n"}


def _corrupt_a_target_file(s3) -> None:
    """Overwrite one target file with different bytes."""
    s3.put_object(Bucket="src-bucket", Key="src/a.txt", Body=b"corrupted\n")


def _delete_a_target_file(s3) -> None:
    s3.delete_object(Bucket="src-bucket", Key="src/b.txt")


def _add_an_unlisted_target_file(s3) -> None:
    """Drop in an extra file the manifest doesn't know about."""
    s3.put_object(Bucket="src-bucket", Key="src/extra.txt", Body=b"surprise")


class TestMismatch:
    """Bag the payload, then damage the *target* one way at a time."""

    @pytest.mark.parametrize(
        "mutate_target,expected_fragments",
        [
            pytest.param(
                _corrupt_a_target_file,
                ("checksum mismatch", "data/a.txt"),
                id="target-checksum-mismatch",
            ),
            pytest.param(
                _delete_a_target_file,
                ("manifest lists data/b.txt but target has no such file",),
                id="target-missing-file",
            ),
            pytest.param(
                _add_an_unlisted_target_file,
                ("no manifest entry: 'extra.txt'",),
                id="target-extra-file",
            ),
        ],
    )
    def test_target_defect_is_reported(self, s3_client, mutate_target, expected_fragments):
        archive_key = _make_bag_from_payload(s3_client, "src/", _MISMATCH_PAYLOAD)
        mutate_target(s3_client)

        result = _run_verify_against(s3_client, archive_key=archive_key, target_prefix="src/")

        assert not result.ok
        assert any(all(f in e for f in expected_fragments) for e in result.errors), result.errors


class TestDataPrefixHeuristic:
    def test_warning_when_target_has_data_segment(self, s3_client):
        # Build a bag whose payload is at src/. We point verify-against at
        # an unrelated target that has /data/ in the URL — the verify
        # itself will fail for "no objects", but the warning is the point.
        archive_key = _make_bag_from_payload(s3_client, "src/", {"a.txt": b"alpha"})

        result = _run_verify_against(
            s3_client, archive_key=archive_key, target_prefix="some/bag/data/"
        )
        assert any("/data/" in w for w in result.warnings), result.warnings

    def test_no_warning_for_normal_prefix(self, s3_client):
        archive_key = _make_bag_from_payload(s3_client, "src/", {"a.txt": b"alpha\n"})

        result = _run_verify_against(s3_client, archive_key=archive_key, target_prefix="src/")
        assert not any("/data/" in w for w in result.warnings)


class TestArchiveErrors:
    def test_archive_without_bagit_txt_fails(self, s3_client):
        # An archive that's a tarball but not a BagIt bag.
        files = {"random.txt": b"not a bag"}
        _put_archive(s3_client, "src-bucket", "in/junk.tar.gz", build_tar_gz(files))

        result = _run_verify_against(
            s3_client,
            archive_bucket="src-bucket",
            archive_key="in/junk.tar.gz",
            target_prefix="src/",
        )
        assert not result.ok
        assert any("No bagit.txt" in e for e in result.errors)

    def test_empty_target_fails(self, s3_client):
        archive_key = _make_bag_from_payload(s3_client, "src/", {"a.txt": b"alpha\n"})
        # `src-bucket` exists; `empty/` has no files.
        result = _run_verify_against(s3_client, archive_key=archive_key, target_prefix="empty/")
        assert not result.ok
        assert any("No objects found" in e for e in result.errors)


class TestMultiAlgorithmBag:
    def test_sha256_and_sha512_both_checked(self, s3_client):
        # Build a bag by hand with both manifests (create-bag only emits one).
        payload = {"a.txt": b"alpha\n", "b.txt": b"beta\n"}
        _stage_bag_archive(s3_client, make_bag_files(payload, algorithms=("sha256", "sha512")))
        # Upload the matching flat payload to the target prefix.
        _put_target_files(s3_client, "src-bucket", "flat/", payload)

        result = _run_verify_against(s3_client)
        assert result.ok, result.errors
        assert result.manifest_algorithms == ["sha256", "sha512"]


class TestZipBag:
    def test_zip_archive(self, s3_client):
        payload = {"a.txt": b"alpha\n"}
        files = make_bag_files(payload)
        archive = build_zip(files, wrap_prefix="my-bag")
        _put_archive(s3_client, "dest-bucket", "bag.zip", archive)
        _put_target_files(s3_client, "src-bucket", "flat/", payload)

        result = _run_verify_against(s3_client, archive_key="bag.zip", fmt="zip")
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

        result = _run_verify_against(s3_client, archive_key="bag.7z", fmt="7z")
        assert result.ok, result.errors


class TestCorruptedArchive:
    def test_corrupted_archive_surfaces_as_result_fail(self, s3_client):
        """A truncated archive should produce ``Could not read archive: ...``,
        not propagate the underlying decoder exception."""
        # Truncate a valid tar.gz so the gzip stream is incomplete.
        files = make_bag_files({"a.txt": b"alpha\n"})
        archive = build_tar_gz(files, wrap_prefix="my-bag")
        _put_archive(s3_client, "src-bucket", "bag.tar.gz", archive[:64])

        result = _run_verify_against(s3_client, archive_bucket="src-bucket")
        assert not result.ok
        assert any("Could not read archive" in e for e in result.errors), result.errors


class TestPayloadOxum:
    def test_oxum_mismatch_reported(self, s3_client):
        # Hand-build a bag whose bag-info.txt lies about Payload-Oxum so we
        # can prove the check is wired up (create_bag's output is always
        # internally consistent).
        payload = {"a.txt": b"alpha\n"}
        # bag-info.txt goes in as an extra tag so the tagmanifests cover it;
        # only the Payload-Oxum is wrong.
        _stage_bag_archive(
            s3_client,
            make_bag_files(
                payload,
                include_bag_info=False,
                extra_tags={"bag-info.txt": b"Payload-Oxum: 999.42\n"},
            ),
        )
        _put_target_files(s3_client, "src-bucket", "flat/", payload)

        result = _run_verify_against(s3_client)
        assert not result.ok
        assert any("Payload-Oxum mismatch" in e for e in result.errors)


# Four independent defects, each from a different check inside
# verify_against. Keyed by kind so tests assert a SET, not a sequence.
_EXPECTED_DEFECTS = {
    "checksum-mismatch": ("checksum mismatch", "data/a.txt"),
    "target-missing-file": ("manifest lists data/b.txt but target has no such file", ""),
    "target-extra-file": ("no manifest entry", "stowaway.txt"),
    "payload-oxum": ("Payload-Oxum mismatch", ""),
}

# The bag says a/b/c; the target corrupts a, omits b, and adds a stowaway.
_BAG_PAYLOAD = {"a.txt": b"alpha\n", "b.txt": b"beta\n", "c.txt": b"gamma\n"}
_DEFECTIVE_TARGET = {"a.txt": b"CORRUPTED\n", "c.txt": b"gamma\n", "stowaway.txt": b"stowaway\n"}


def _defect_kinds(errors: list[str]) -> set[str]:
    """Classify *errors* into defect kinds, ignoring the order they arrived in."""
    return {
        kind
        for kind, (needle, path) in _EXPECTED_DEFECTS.items()
        if any(needle in e and path in e for e in errors)
    }


def _bag_files_with_lying_oxum() -> dict[str, bytes]:
    """A well-formed bag for ``_BAG_PAYLOAD`` whose ``bag-info.txt`` lies."""
    return make_bag_files(
        _BAG_PAYLOAD,
        include_bag_info=False,
        extra_tags={"bag-info.txt": b"Payload-Oxum: 999.42\n"},
    )


@pytest.fixture
def target_listing_spy(monkeypatch):
    """Record every call to ``_list_target_objects``.

    ``verify_against``'s legitimate early returns all fire *before* the
    target prefix is listed. An empty call list is the evidence that a
    can't-continue return really did stop work, as opposed to a
    fail-fast regression that also happens to return early.
    """
    calls: list[tuple[str, str]] = []
    real = va_mod._list_target_objects

    def spy(client, bucket, prefix):
        calls.append((bucket, prefix))
        return real(client, bucket, prefix)

    monkeypatch.setattr(va_mod, "_list_target_objects", spy)
    return calls


@pytest.fixture
def target_hash_spy(monkeypatch):
    """Record every target object that gets stream-hashed."""
    hashed: list[str] = []
    real = va_mod.stream_hash_object

    def spy(client, bucket, key, algorithms, **kw):
        hashed.append(key)
        return real(client, bucket, key, algorithms, **kw)

    monkeypatch.setattr(va_mod, "stream_hash_object", spy)
    return hashed


class TestCollectAllErrors:
    """Pins the repo's headline design commitment (CLAUDE.md) for ``verify_against``.

    > BagIt verification collects all errors, then reports together --
    > don't fail-fast on the first checksum mismatch; operators want the
    > full picture in one run.

    Every other failure test in this file injects **exactly one** defect
    and asserts ``any(substring in e for e in result.errors)``. That
    shape cannot detect a fail-fast regression: inserting
    ``return result`` right after the first ``result.fail(...)`` still
    satisfies every one of them and still passes ``make check``, while
    destroying the behaviour three documents promise.

    ``verify_against`` is complexity 26 -- the highest in either s3 repo
    -- precisely because every check falls through to the next instead
    of returning. The rule for any future extraction: **an extracted
    helper takes ``result: BagVerifyResult`` and returns ``None`` or a
    data value -- never a bool the caller branches on to ``return``.**
    """

    def test_four_independent_defects_are_all_reported(self, s3_client):
        """The essential test: one run, four defects, four errors."""
        _stage_bag_archive(s3_client, _bag_files_with_lying_oxum())
        _put_target_files(s3_client, "src-bucket", "flat/", _DEFECTIVE_TARGET)

        result = _run_verify_against(s3_client)

        assert not result.ok
        # A fail-fast implementation reports 1. Collect-all reports 4.
        assert len(result.errors) >= 4, result.errors
        assert any("checksum mismatch" in e and "data/a.txt" in e for e in result.errors), (
            result.errors
        )
        assert any(
            "manifest lists data/b.txt but target has no such file" in e for e in result.errors
        ), result.errors
        assert any("no manifest entry" in e and "stowaway.txt" in e for e in result.errors), (
            result.errors
        )
        assert any("Payload-Oxum mismatch" in e for e in result.errors), result.errors

    def test_defect_set_is_order_independent(self, s3_client):
        """Assert the defect *set*, never a sequence.

        Two archives carry identical tag files written in opposite
        member order -- which changes the order tag files are captured
        and manifests are parsed. Both must yield the same defect set.
        A later extraction is free to reorder the checks, and must be,
        so long as it still reports all of them.
        """
        files = _bag_files_with_lying_oxum()
        reversed_files = dict(reversed(list(files.items())))
        _stage_bag_archive(s3_client, files, key="fwd.tar.gz", wrap_prefix="b")
        _stage_bag_archive(s3_client, reversed_files, key="rev.tar.gz", wrap_prefix="b")
        _put_target_files(s3_client, "src-bucket", "flat/", _DEFECTIVE_TARGET)

        fwd = _run_verify_against(s3_client, archive_key="fwd.tar.gz")
        rev = _run_verify_against(s3_client, archive_key="rev.tar.gz")

        assert _defect_kinds(fwd.errors) == set(_EXPECTED_DEFECTS), fwd.errors
        assert _defect_kinds(rev.errors) == _defect_kinds(fwd.errors)

    def test_malformed_manifest_does_not_stop_other_algorithms(self, s3_client):
        """A manifest that fails to parse must not abort the manifest loop.

        The ``sha512`` manifest is still parsed and still reports its
        checksum mismatch. An extraction that propagates the ``BagError``
        instead of looping would lose the second error entirely.
        """
        payload = {"a.txt": b"alpha\n"}
        files = make_bag_files(payload, algorithms=("sha256", "sha512"))
        files["manifest-sha256.txt"] = b"deadbeef-with-no-path\n"
        _stage_bag_archive(s3_client, files)
        # Same byte length, different bytes: mismatch without an oxum error.
        _put_target_files(s3_client, "src-bucket", "flat/", {"a.txt": b"ALPHA\n"})

        result = _run_verify_against(s3_client)

        assert not result.ok
        assert len(result.errors) >= 2, result.errors
        assert any("manifest-sha256.txt" in e and "malformed" in e for e in result.errors), (
            result.errors
        )
        assert any(
            "manifest-sha512.txt" in e and "checksum mismatch" in e for e in result.errors
        ), result.errors

    def test_two_manifest_entries_outside_data_are_both_reported(self, s3_client):
        """Two misplaced entries, two errors: the walk must not stop at the first.

        ``_map_expected_by_target`` skips a manifest entry that is not
        under ``data/`` -- a flat target with ``data/`` stripped has no
        counterpart for it -- and carries on. Until this test, every case
        reaching that branch carried **exactly one** misplaced entry, so
        "abandon the walk after the first" and "collect them all" were
        indistinguishable: an injected ``return`` there passed the whole
        suite. Two entries are the minimum that tells them apart.

        The corrupted target file is a third, independent defect that
        only lands if the walk ran to the end of the manifest.
        """
        payload = {"a.txt": b"alpha\n", "b.txt": b"beta\n"}
        files = make_bag_files(payload)
        # Two entries with no data/ prefix. The checksums are never
        # compared -- the entries are rejected before any target read --
        # so any well-formed hex stands in.
        files["manifest-sha256.txt"] += (
            b"0" * 64 + b"  loose.txt\n" + b"1" * 64 + b"  extras/stray.txt\n"
        )
        rebuild_tagmanifests(files)
        _stage_bag_archive(s3_client, files)
        # Same byte length, different bytes: mismatch without an oxum error.
        _put_target_files(
            s3_client, "src-bucket", "flat/", {"a.txt": b"ALPHA\n", "b.txt": b"beta\n"}
        )

        result = _run_verify_against(s3_client)

        assert not result.ok
        # A walk that stops at the first misplaced entry reports 2. Collect-all reports 3.
        assert len(result.errors) >= 3, result.errors
        assert any("entry outside data/" in e and "loose.txt" in e for e in result.errors), (
            result.errors
        )
        assert any("entry outside data/" in e and "extras/stray.txt" in e for e in result.errors), (
            result.errors
        )
        assert any("checksum mismatch" in e and "data/a.txt" in e for e in result.errors), (
            result.errors
        )


class TestWarningsAreNotErrors:
    """Warn-path pins: ``result.warn`` must never become ``result.fail``.

    Both branches are currently uncovered, so a decomposition could drop
    or upgrade them unnoticed.
    """

    def test_unexpected_bagit_version_warns_but_target_stays_valid(self, s3_client):
        payload = {"a.txt": b"alpha\n"}
        files = make_bag_files(payload)
        files["bagit.txt"] = b"BagIt-Version: 2.0\nTag-File-Character-Encoding: UTF-8\n"
        _stage_bag_archive(s3_client, files)
        _put_target_files(s3_client, "src-bucket", "flat/", payload)

        result = _run_verify_against(s3_client)

        assert result.ok, result.errors
        assert result.declared_version == "2.0"
        assert any("BagIt-Version" in w for w in result.warnings), result.warnings

    def test_unknown_manifest_algorithm_warns_but_target_stays_valid(self, s3_client):
        payload = {"a.txt": b"alpha\n"}
        files = make_bag_files(payload)
        files["manifest-blake2b.txt"] = "".join(
            f"{hashlib.new('blake2b', content).hexdigest()}  data/{rel}\n"
            for rel, content in payload.items()
        ).encode("utf-8")
        _stage_bag_archive(s3_client, files)
        _put_target_files(s3_client, "src-bucket", "flat/", payload)

        result = _run_verify_against(s3_client)

        assert result.ok, result.errors
        assert result.manifest_algorithms == ["blake2b", "sha256"]
        assert any(
            "Unknown payload manifest algorithm" in w and "blake2b" in w for w in result.warnings
        ), result.warnings


class TestLegitimateEarlyReturns:
    """The FOUR places in ``verify_against`` where returning early is correct.

    Unreadable archive, no bag root, no manifests, no target objects --
    each genuinely cannot continue. Pinning them (exactly one error, and
    provably no further work) is what lets a reviewer tell a legitimate
    can't-continue return from a fail-fast regression. Everywhere else,
    ``result.fail`` must fall through.
    """

    def test_unreadable_archive_returns_before_listing_target(self, s3_client, target_listing_spy):
        files = make_bag_files({"a.txt": b"alpha\n"})
        truncated = build_tar_gz(files, wrap_prefix="my-bag")[:64]
        _put_archive(s3_client, "dest-bucket", "bag.tar.gz", truncated)
        # Populate the target so an absent listing is meaningful.
        _put_target_files(s3_client, "src-bucket", "flat/", {"a.txt": b"alpha\n"})

        result = _run_verify_against(s3_client)

        assert len(result.errors) == 1, result.errors
        assert "Could not read archive" in result.errors[0]
        assert target_listing_spy == []
        assert result.payload_file_count == 0

    def test_missing_bagit_txt_returns_before_listing_target(self, s3_client, target_listing_spy):
        _stage_bag_archive(s3_client, {"random.txt": b"not a bag"}, wrap_prefix="")
        _put_target_files(s3_client, "src-bucket", "flat/", {"a.txt": b"alpha\n"})

        result = _run_verify_against(s3_client)

        assert len(result.errors) == 1, result.errors
        assert "No bagit.txt" in result.errors[0]
        assert target_listing_spy == []
        assert result.manifest_algorithms == []

    def test_no_manifests_returns_before_listing_target(self, s3_client, target_listing_spy):
        tag_only = {
            "bagit.txt": b"BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n",
            "bag-info.txt": b"Payload-Oxum: 6.1\n",
        }
        _stage_bag_archive(s3_client, tag_only)
        _put_target_files(s3_client, "src-bucket", "flat/", {"a.txt": b"alpha\n"})

        result = _run_verify_against(s3_client)

        assert len(result.errors) == 1, result.errors
        assert "No payload manifest found in archive" in result.errors[0]
        assert target_listing_spy == []

    def test_empty_target_returns_before_hashing(
        self, s3_client, target_listing_spy, target_hash_spy
    ):
        files = make_bag_files({"a.txt": b"alpha\n"})
        _stage_bag_archive(s3_client, files)
        # `empty/` has no objects at all.

        result = _run_verify_against(s3_client, target_prefix="empty/")

        assert len(result.errors) == 1, result.errors
        assert "No objects found at target" in result.errors[0]
        assert target_listing_spy == [("src-bucket", "empty/")]
        assert target_hash_spy == [], "nothing should be hashed when the target is empty"
        assert result.payload_file_count == 0


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


class TestNonAsciiPayloadNames:
    """Character-set support: streamed manifest and hashed target agree on non-ASCII keys."""

    def test_non_ascii_target_matches(self, s3_client):
        payload = {"한국어/기록.txt": b"korean payload\n", "résumé-café.txt": b"accented\n"}
        archive_key = _make_bag_from_payload(s3_client, "src/", payload)

        result = _run_verify_against(s3_client, archive_key=archive_key, target_prefix="src/")
        assert result.ok, result.errors
        assert result.payload_file_count == len(payload)
