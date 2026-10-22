"""Tests for BagIt verify against an S3 prefix."""

import hashlib

import pytest

from s3_bagit import verify as verify_mod
from s3_bagit.verify import verify_bag

from .conftest import make_bag_files, rebuild_tagmanifests, upload_bag_to_prefix


@pytest.fixture
def good_bag_payload():
    return {"a.txt": b"alpha\n", "sub/b.txt": b"beta\n"}


def _upload(s3, bucket, prefix, files):
    upload_bag_to_prefix(s3, bucket, prefix, files)


# Four *independent* defects, each producing one error from a different
# check inside verify_bag. Keyed by a stable kind name so tests can
# assert the defect SET rather than a sequence.
_EXPECTED_DEFECTS = {
    "checksum-mismatch": ("checksum mismatch", "data/a.txt"),
    "listed-not-present": ("listed but not present", "data/b.txt"),
    "present-not-listed": ("present but not listed", "data/stowaway.txt"),
    "payload-oxum": ("Payload-Oxum mismatch", ""),
}


def _defect_kinds(errors: list[str]) -> set[str]:
    """Classify *errors* into defect kinds, ignoring the order they arrived in."""
    return {
        kind
        for kind, (needle, path) in _EXPECTED_DEFECTS.items()
        if any(needle in e and path in e for e in errors)
    }


def _bag_with_four_defects() -> dict[str, bytes]:
    """A bag carrying four independent defects simultaneously.

    Corrupt payload file, manifest entry with no file, payload file with
    no manifest entry, and a lying ``Payload-Oxum``. Each is caught by a
    different branch of ``verify_bag``, so a fail-fast implementation
    can only ever report one of them.
    """
    files = make_bag_files({"a.txt": b"alpha\n", "b.txt": b"beta\n", "c.txt": b"gamma\n"})
    files["data/a.txt"] = b"CORRUPTED\n"  # checksum mismatch
    del files["data/b.txt"]  # listed but not present
    files["data/stowaway.txt"] = b"stowaway\n"  # present but not listed
    files["bag-info.txt"] = b"Bagging-Date: 2026-05-22\nPayload-Oxum: 999.99\n"  # oxum lies
    rebuild_tagmanifests(files)
    return files


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
        ).encode()

        sha256_files = ("a.txt", "b.txt")
        sha512_files = ("a.txt", "c.txt")
        files["manifest-sha256.txt"] = "".join(
            f"{hashlib.sha256(payload[r]).hexdigest()}  data/{r}\n" for r in sha256_files
        ).encode("utf-8")
        files["manifest-sha512.txt"] = "".join(
            f"{hashlib.sha512(payload[r]).hexdigest()}  data/{r}\n" for r in sha512_files
        ).encode("utf-8")

        rebuild_tagmanifests(files, ("sha256", "sha512"))

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


def _drop_bagit_txt(files: dict[str, bytes]) -> None:
    files.pop("bagit.txt")


def _drop_payload_manifest(files: dict[str, bytes]) -> None:
    del files["manifest-sha256.txt"]


def _corrupt_a_payload_file(files: dict[str, bytes]) -> None:
    """Corrupt one payload file *after* its manifest entry was generated."""
    files["data/a.txt"] = b"CORRUPTED\n"


def _delete_a_listed_payload_file(files: dict[str, bytes]) -> None:
    del files["data/a.txt"]


def _add_an_unlisted_payload_file(files: dict[str, bytes]) -> None:
    """Add a file under ``data/`` that no manifest lists.

    Only ``data/`` changes, so the tag manifests stay consistent — this
    defect must not drag a tagmanifest error in with it.
    """
    files["data/stowaway.txt"] = b"stowaway\n"


def _edit_bag_info_without_rebuilding_tagmanifests(files: dict[str, bytes]) -> None:
    """Modify ``bag-info.txt`` and leave the tag manifest stale.

    ``Payload-Oxum`` stays numerically correct so the tagmanifest
    mismatch is unambiguously what trips the error.
    """
    files["bag-info.txt"] = b"Bagging-Date: 2026-05-22\nPayload-Oxum: 11.2\nExtra: x\n"


def _lie_about_payload_oxum(files: dict[str, bytes]) -> None:
    """Write a bogus ``Payload-Oxum``, then rebuild so only the oxum is wrong."""
    files["bag-info.txt"] = b"Bagging-Date: 2026-05-22\nPayload-Oxum: 999.99\n"
    rebuild_tagmanifests(files)


def _add_a_non_empty_fetch_txt(files: dict[str, bytes]) -> None:
    """Add a populated ``fetch.txt``, keeping it covered by the tag manifest."""
    files["fetch.txt"] = b"http://x  10  data/x\n"
    rebuild_tagmanifests(files)


class TestSingleDefectBags:
    """One defect per bag: inject it, verify, assert the matching error.

    Deliberately *not* a fail-fast detector — a single-defect bag plus
    ``any(substring in e ...)`` passes just as happily against an
    implementation that returns after the first ``result.fail``. That is
    ``TestCollectAllErrors``'s job; these cases only pin that each
    individual check fires at all, and each defect earns a parametrize
    row rather than a hand-copied arrange/act/assert block.
    """

    @pytest.mark.parametrize(
        "mutate,expected_fragments",
        [
            pytest.param(_drop_bagit_txt, ("bagit.txt is missing",), id="missing-bagit-txt"),
            pytest.param(
                _drop_payload_manifest, ("No payload manifest",), id="no-payload-manifest"
            ),
            pytest.param(
                _corrupt_a_payload_file,
                ("checksum mismatch", "a.txt"),
                id="payload-checksum-mismatch",
            ),
            pytest.param(
                _delete_a_listed_payload_file,
                ("listed but not present",),
                id="manifest-entry-with-no-file",
            ),
            pytest.param(
                _add_an_unlisted_payload_file,
                ("present but not listed", "stowaway"),
                id="payload-file-with-no-manifest-entry",
            ),
            pytest.param(
                _edit_bag_info_without_rebuilding_tagmanifests,
                ("tagmanifest", "bag-info.txt"),
                id="stale-tagmanifest",
            ),
            pytest.param(
                _lie_about_payload_oxum, ("Payload-Oxum mismatch",), id="payload-oxum-mismatch"
            ),
            pytest.param(_add_a_non_empty_fetch_txt, ("fetch.txt",), id="non-empty-fetch-txt"),
        ],
    )
    def test_defect_is_reported(self, s3_client, good_bag_payload, mutate, expected_fragments):
        files = make_bag_files(good_bag_payload)
        mutate(files)
        _upload(s3_client, "dest-bucket", "bag/", files)

        result = verify_bag(s3_client, "dest-bucket", "bag/")

        assert not result.ok
        assert any(all(f in e for f in expected_fragments) for e in result.errors), result.errors


class TestStructuralFailures:
    """Bags too degenerate to express as a mutation of a good bag."""

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


class TestPayloadOxum:
    def test_bag_without_bag_info_skips_oxum(self, s3_client, good_bag_payload):
        files = make_bag_files(good_bag_payload, include_bag_info=False)
        _upload(s3_client, "dest-bucket", "bag/", files)
        result = verify_bag(s3_client, "dest-bucket", "bag/")
        assert result.ok, result.errors


class TestCollectAllErrors:
    """Pins the repo's headline design commitment (CLAUDE.md).

    > BagIt verification collects all errors, then reports together --
    > don't fail-fast on the first checksum mismatch; operators want the
    > full picture in one run.

    Every other failure test in this file injects **exactly one** defect
    and asserts ``any(substring in e for e in result.errors)``. That
    shape cannot detect a fail-fast regression: inserting
    ``return result`` right after the first ``result.fail(...)`` still
    satisfies every one of them, keeps coverage flat, and passes
    ``make check`` -- while silently destroying the behaviour CLAUDE.md,
    docs/HOWTO-SIMPLIFY.md and the module docstring all promise.

    The tests below are the detector. They exist because "extract a
    check, have it return early" is the single most natural move a
    complexity-reducing decomposition of ``verify_bag`` makes, and
    ``verify_bag`` is complexity 22 precisely because it refuses to.

    The rule they encode, for any future extraction: **an extracted
    helper takes ``result: BagVerifyResult`` and returns ``None`` or a
    data value -- never a bool the caller branches on to ``return``.**
    The moment a helper's return value gates the caller, fail-fast is
    back.
    """

    def test_four_independent_defects_are_all_reported(self, s3_client):
        """The essential test: one run, four defects, four errors."""
        _upload(s3_client, "dest-bucket", "bag/", _bag_with_four_defects())

        result = verify_bag(s3_client, "dest-bucket", "bag/")

        assert not result.ok
        # A fail-fast implementation reports 1. Collect-all reports 4.
        assert len(result.errors) >= 4, result.errors
        assert any("checksum mismatch" in e and "data/a.txt" in e for e in result.errors), (
            result.errors
        )
        assert any("listed but not present" in e and "data/b.txt" in e for e in result.errors), (
            result.errors
        )
        assert any(
            "present but not listed" in e and "data/stowaway.txt" in e for e in result.errors
        ), result.errors
        assert any("Payload-Oxum mismatch" in e for e in result.errors), result.errors

    def test_defect_set_is_order_independent(self, s3_client):
        """Assert the defect *set*, never a sequence.

        Two bags carry the same four defects but lay their objects down
        in opposite order. Both must yield the same set of defect kinds.
        Pinning the set rather than ``errors[0]`` is deliberate: a later
        extraction is free to reorder the checks, and must be, so long
        as it still reports all of them.
        """
        forward = _bag_with_four_defects()
        reverse = dict(reversed(list(_bag_with_four_defects().items())))
        _upload(s3_client, "dest-bucket", "fwd/", forward)
        _upload(s3_client, "dest-bucket", "rev/", reverse)

        fwd_result = verify_bag(s3_client, "dest-bucket", "fwd/")
        rev_result = verify_bag(s3_client, "dest-bucket", "rev/")

        assert _defect_kinds(fwd_result.errors) == set(_EXPECTED_DEFECTS), fwd_result.errors
        assert _defect_kinds(rev_result.errors) == _defect_kinds(fwd_result.errors)

    def test_structural_failures_do_not_stop_the_remaining_checks(self, s3_client):
        """A missing ``bagit.txt`` and a non-empty ``fetch.txt`` are NOT
        can't-continue conditions.

        These are the first two fall-through ``result.fail`` calls in
        ``verify_bag``, and they sit *before* every checksum check. No
        other test in this file combines either of them with a second
        defect, so an extraction that returned right after the
        ``bagit.txt`` check -- the single most literal reading of
        "extract a check, have it return early" -- would otherwise go
        completely undetected.
        """
        files = make_bag_files(
            {"a.txt": b"alpha\n"}, extra_tags={"fetch.txt": b"http://x  10  data/x\n"}
        )
        del files["bagit.txt"]
        # Same byte length, different bytes: mismatch without an oxum error.
        files["data/a.txt"] = b"ALPHA\n"
        _upload(s3_client, "dest-bucket", "bag/", files)

        result = verify_bag(s3_client, "dest-bucket", "bag/")

        assert not result.ok
        assert len(result.errors) >= 4, result.errors
        assert any("bagit.txt is missing" in e for e in result.errors), result.errors
        assert any("fetch.txt is non-empty" in e for e in result.errors), result.errors
        assert any("checksum mismatch" in e and "data/a.txt" in e for e in result.errors), (
            result.errors
        )
        assert any(
            "tagmanifest-sha256.txt" in e and "listed but not present" in e and "bagit.txt" in e
            for e in result.errors
        ), result.errors

    def test_malformed_payload_manifest_does_not_stop_other_algorithms(self, s3_client):
        """A manifest that fails to parse must not abort the manifest loop.

        Pins ``verify.py`` "except BagError -> result.fail" inside the
        payload-manifest parse loop: the ``sha512`` manifest is still
        parsed and still reports its checksum mismatch. A ``_load_manifests``
        extraction that propagates the ``BagError`` instead of looping
        would lose the second error entirely.
        """
        files = make_bag_files({"a.txt": b"alpha\n"}, algorithms=("sha256", "sha512"))
        files["manifest-sha256.txt"] = b"deadbeef-with-no-path\n"
        # Same byte length, different bytes: corrupts the checksum without
        # also tripping the Payload-Oxum check.
        files["data/a.txt"] = b"ALPHA\n"
        rebuild_tagmanifests(files, ("sha256", "sha512"))
        _upload(s3_client, "dest-bucket", "bag/", files)

        result = verify_bag(s3_client, "dest-bucket", "bag/")

        assert not result.ok
        assert len(result.errors) >= 2, result.errors
        assert any("manifest-sha256.txt" in e and "malformed" in e for e in result.errors), (
            result.errors
        )
        assert any(
            "manifest-sha512.txt" in e and "checksum mismatch" in e for e in result.errors
        ), result.errors

    def test_malformed_tagmanifest_does_not_stop_other_algorithms(self, s3_client):
        """Same guarantee for the tagmanifest parse loop."""
        files = make_bag_files({"a.txt": b"alpha\n"}, algorithms=("sha256", "sha512"))
        files["tagmanifest-sha256.txt"] = b"deadbeef-with-no-path\n"
        # Mutate a tag file *without* rebuilding, so tagmanifest-sha512
        # has something to report. Same length keeps Payload-Oxum clean.
        files["bag-info.txt"] = files["bag-info.txt"].replace(b"2026-05-22", b"2026-05-23")
        _upload(s3_client, "dest-bucket", "bag/", files)

        result = verify_bag(s3_client, "dest-bucket", "bag/")

        assert not result.ok
        assert len(result.errors) >= 2, result.errors
        assert any("tagmanifest-sha256.txt" in e and "malformed" in e for e in result.errors), (
            result.errors
        )
        assert any(
            "tagmanifest-sha512.txt" in e and "checksum mismatch" in e and "bag-info.txt" in e
            for e in result.errors
        ), result.errors

    def test_two_misplaced_payload_entries_are_both_reported(self, s3_client):
        """Two payload-manifest entries outside ``data/``, two errors.

        ``_reject_misplaced_entry`` returns True so the caller can
        ``continue`` -- the one place in this file where a helper's
        return value gates the loop. Until this test, no case carried
        **two** misplaced entries, so a ``break``/``return`` in that
        caller was indistinguishable from the ``continue`` that is
        actually there. The sibling gap to ``verify_against``'s R7.

        The corrupted payload file is a third, independent defect that
        only lands if the pass ran to the end.
        """
        files = make_bag_files({"a.txt": b"alpha\n", "b.txt": b"beta\n"})
        # Two entries with no data/ prefix. The checksums are never
        # compared -- the entries are rejected before any object read --
        # so any well-formed hex stands in.
        files["manifest-sha256.txt"] += (
            b"0" * 64 + b"  loose.txt\n" + b"1" * 64 + b"  extras/stray.txt\n"
        )
        # Same byte length, different bytes: mismatch without an oxum error.
        files["data/a.txt"] = b"ALPHA\n"
        rebuild_tagmanifests(files)
        _upload(s3_client, "dest-bucket", "bag/", files)

        result = verify_bag(s3_client, "dest-bucket", "bag/")

        assert not result.ok
        # A pass that stops at the first misplaced entry reports 2. Collect-all reports 3.
        assert len(result.errors) >= 3, result.errors
        assert any(
            "payload manifest entry outside data/" in e and "loose.txt" in e for e in result.errors
        ), result.errors
        assert any(
            "payload manifest entry outside data/" in e and "extras/stray.txt" in e
            for e in result.errors
        ), result.errors
        assert any("checksum mismatch" in e and "data/a.txt" in e for e in result.errors), (
            result.errors
        )


class TestWarningsAreNotErrors:
    """Warn-path pins: ``result.warn`` must never become ``result.fail``.

    These branches are currently uncovered, so a decomposition could
    drop or upgrade them without any test noticing. Each asserts both
    that the warning fires *and* that ``result.ok`` still reflects only
    real errors.
    """

    def test_unexpected_bagit_version_warns_but_bag_stays_valid(self, s3_client):
        files = make_bag_files({"a.txt": b"alpha\n"})
        files["bagit.txt"] = b"BagIt-Version: 2.0\nTag-File-Character-Encoding: UTF-8\n"
        rebuild_tagmanifests(files)
        _upload(s3_client, "dest-bucket", "bag/", files)

        result = verify_bag(s3_client, "dest-bucket", "bag/")

        assert result.ok, result.errors
        assert result.declared_version == "2.0"
        assert any("BagIt-Version" in w for w in result.warnings), result.warnings

    def test_unknown_payload_manifest_algorithm_warns_but_bag_stays_valid(self, s3_client):
        payload = {"a.txt": b"alpha\n"}
        files = make_bag_files(payload)
        files["manifest-blake2b.txt"] = "".join(
            f"{hashlib.new('blake2b', content).hexdigest()}  data/{rel}\n"
            for rel, content in payload.items()
        ).encode("utf-8")
        rebuild_tagmanifests(files)
        _upload(s3_client, "dest-bucket", "bag/", files)

        result = verify_bag(s3_client, "dest-bucket", "bag/")

        assert result.ok, result.errors
        assert result.manifest_algorithms == ["blake2b", "sha256"]
        assert any(
            "Unknown payload manifest algorithm" in w and "blake2b" in w for w in result.warnings
        ), result.warnings

    def test_unknown_tag_manifest_algorithm_warns_but_bag_stays_valid(self, s3_client):
        files = make_bag_files({"a.txt": b"alpha\n"})
        rebuild_tagmanifests(files, ("sha256", "blake2b"))
        _upload(s3_client, "dest-bucket", "bag/", files)

        result = verify_bag(s3_client, "dest-bucket", "bag/")

        assert result.ok, result.errors
        assert result.tagmanifest_algorithms == ["blake2b", "sha256"]
        assert any(
            "Unknown tag manifest algorithm" in w and "blake2b" in w for w in result.warnings
        ), result.warnings


class TestLegitimateEarlyReturn:
    """The ONE place in ``verify_bag`` where returning early is correct.

    An empty prefix genuinely cannot be verified further -- there is no
    bag to check. Pinning it (exactly one error, nothing else attempted)
    is what lets a reviewer tell a legitimate can't-continue return from
    a fail-fast regression: everywhere else, ``result.fail`` must fall
    through.
    """

    def test_empty_prefix_reports_one_error_and_does_no_further_work(self, s3_client, monkeypatch):
        hashed: list[str] = []
        monkeypatch.setattr(
            verify_mod,
            "stream_hash_object",
            lambda client, bucket, key, algorithms, **kw: hashed.append(key),
        )

        result = verify_bag(s3_client, "dest-bucket", "nothing-here/")

        assert len(result.errors) == 1, result.errors
        assert "No objects found" in result.errors[0]
        assert result.warnings == []
        assert result.declared_version is None
        assert result.manifest_algorithms == []
        assert result.tagmanifest_algorithms == []
        assert result.payload_file_count == 0
        assert result.payload_total_octets == 0
        assert hashed == [], "nothing should be hashed for an empty prefix"
