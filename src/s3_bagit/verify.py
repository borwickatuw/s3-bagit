"""Verify a BagIt bag whose contents live at an S3 prefix.

Implements the structural and checksum checks from RFC 8493 §3:

  * ``bagit.txt`` declares the BagIt version (0.97 or 1.0).
  * Every payload file under ``data/`` MUST be listed in every payload
    manifest (``manifest-<algorithm>.txt``).
  * Every payload manifest entry MUST point at an existing payload file
    whose checksum matches.
  * Tag manifests (``tagmanifest-<algorithm>.txt``) are verified the same
    way against every non-payload file.
  * ``Payload-Oxum`` in ``bag-info.txt``, if present, MUST equal
    ``<total-octets>.<file-count>`` over ``data/``.

Verification streams each file in 64 KiB chunks; nothing is staged on
local disk. Errors are collected and reported together so an operator
sees the full picture rather than a single fail-fast surface.

This module is written from the RFC.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from s3_archive.hashing import stream_hash_object

from s3_bagit._bagit_checks import (
    _FETCH_TXT_NON_EMPTY,
    _check_bagit_version,
    _check_payload_oxum,
    _list_objects,
    _parse_manifest_text,
    _warn_unknown_algorithms,
)
from s3_bagit.exceptions import BagError
from s3_bagit.log_config import get_logger

log = get_logger(__name__)


# Per-file progress callback: ``on_progress(rel_path, bytes_done)``.
# Called once per file after its hash is computed. ``bytes_done`` is the
# file's size (one event per file, suitable for tqdm.update). None means
# no progress reporting — preserves the previous library behavior.
ProgressCallback = Callable[[str, int], None]


@dataclass
class BagVerifyResult:
    """Outcome of a verify run. Empty error lists mean the bag is valid."""

    bag_url: str
    target_url: str | None = None
    declared_version: str | None = None
    manifest_algorithms: list[str] = field(default_factory=list)
    tagmanifest_algorithms: list[str] = field(default_factory=list)
    payload_file_count: int = 0
    payload_total_octets: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def fail(self, msg: str) -> None:
        self.errors.append(msg)
        log.error("FAIL: %s", msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        log.warning("WARN: %s", msg)


def _read_text(client, bucket: str, key: str) -> str:
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    return body.decode("utf-8")


def _build_expected_map(
    manifests_by_algo: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Union the per-algorithm manifests into ``{rel: {algo: expected}}``.

    Preserves the RFC 8493 case where different manifests cover
    overlapping-but-not-identical file sets: a file only listed in
    ``manifest-sha256.txt`` ends up with ``{"sha256": ...}`` and is
    hashed with sha256 only, never sha512.
    """
    out: dict[str, dict[str, str]] = {}
    for algo, entries in manifests_by_algo.items():
        for rel, expected in entries.items():
            out.setdefault(rel, {})[algo] = expected.lower()
    return out


def _reject_misplaced_entry(
    rel: str,
    expected_by_algo: dict[str, str],
    result: BagVerifyResult,
    *,
    payload: bool,
) -> bool:
    """Record an error if *rel* sits on the wrong side of the payload boundary.

    RFC 8493: payload manifests list only ``data/`` files, tag manifests
    list only files outside it. Returns True when the entry was rejected
    so the caller can skip to the next entry — this collects into
    *result* rather than raising, keeping the "one run, full picture"
    discipline the rest of the verifier follows.
    """
    if payload and not rel.startswith("data/"):
        for algo in expected_by_algo:
            result.fail(f"manifest-{algo}.txt: payload manifest entry outside data/: {rel!r}")
        return True
    if not payload and rel.startswith("data/"):
        for algo in expected_by_algo:
            result.fail(
                f"tagmanifest-{algo}.txt: tag manifest must not list payload files: {rel!r}"
            )
        return True
    return False


def _verify_files_single_pass(
    client,
    bucket: str,
    expected_map: dict[str, dict[str, str]],
    objects: dict[str, dict[str, Any]],
    result: BagVerifyResult,
    *,
    payload: bool,
    on_progress: ProgressCallback | None = None,
) -> set[str]:
    """Hash each file once, fanning out to every algorithm that listed it.

    One S3 GET per file regardless of how many manifest algorithms the
    bag carries.
    """
    covered: set[str] = set()
    label = "manifest" if payload else "tagmanifest"
    for rel, expected_by_algo in sorted(expected_map.items()):
        covered.add(rel)
        if _reject_misplaced_entry(rel, expected_by_algo, result, payload=payload):
            continue
        if rel not in objects:
            for algo in expected_by_algo:
                result.fail(f"{label}-{algo}.txt: file listed but not present in bag: {rel!r}")
            continue
        actuals = stream_hash_object(client, bucket, objects[rel]["Key"], expected_by_algo.keys())
        for algo, expected in expected_by_algo.items():
            actual = actuals[algo].lower()
            if actual != expected:
                result.fail(
                    f"{label}-{algo}.txt: checksum mismatch for {rel!r}: "
                    f"expected {expected}, got {actual}"
                )
            else:
                log.debug("%s ok (%s) %s", label, algo, rel)
        if on_progress is not None:
            on_progress(rel, objects[rel]["Size"])
    return covered


def _discover_manifest_names(
    objects: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Split the bag's manifest files into ``{algorithm: relative_path}`` maps.

    Returns ``(payload_manifests, tag_manifests)``. RFC 8493 places both
    kinds at the bag root only, so anything with a ``/`` in its relative
    path is not a manifest however it is named.
    """
    manifests: dict[str, str] = {}
    tagmanifests: dict[str, str] = {}
    for rel in objects:
        if "/" in rel or not rel.endswith(".txt"):
            continue
        if rel.startswith("manifest-"):
            manifests[rel[len("manifest-") : -len(".txt")]] = rel
        elif rel.startswith("tagmanifest-"):
            tagmanifests[rel[len("tagmanifest-") : -len(".txt")]] = rel
    return manifests, tagmanifests


def _parse_manifests(
    client,
    bucket: str,
    bag_prefix: str,
    manifest_names: dict[str, str],
    result: BagVerifyResult,
) -> dict[str, dict[str, str]]:
    """Read and parse each named manifest into ``{algorithm: {rel: expected}}``.

    Collects rather than raises, and keeps looping past a bad manifest:
    the offending file becomes an error and its algorithm simply never
    enters the returned map, so the caller still gets a full result for
    every algorithm that did parse.

    That omission is also why a malformed manifest silently drops its
    algorithm's present-but-not-listed coverage check (see
    :func:`_check_payload_coverage`) — pinned behaviour, not an
    oversight. This helper is shared by the payload and tag manifests of
    *this* module only; ``verify_against`` keeps its own parse loop,
    because that side diverges on what happens next.
    """
    by_algo: dict[str, dict[str, str]] = {}
    for algo, rel in manifest_names.items():
        try:
            by_algo[algo] = _parse_manifest_text(_read_text(client, bucket, bag_prefix + rel))
        except BagError as exc:
            result.fail(f"{rel}: {exc}")
    return by_algo


def _check_payload_coverage(
    objects: dict[str, dict[str, Any]],
    manifest_names: dict[str, str],
    manifests_by_algo: dict[str, dict[str, str]],
    result: BagVerifyResult,
) -> None:
    """Fail for every ``data/`` file that a parsed payload manifest omits.

    RFC 8493: every payload file MUST appear in every payload manifest.
    Only algorithms present in *manifests_by_algo* are checked — an
    algorithm whose manifest failed to parse contributes no coverage
    errors at all.
    """
    data_files = {rel for rel in objects if rel.startswith("data/")}
    if not data_files:
        result.fail("No payload files found under data/ — bag has no content")
    for algo, entries in manifests_by_algo.items():
        for path in sorted(data_files - set(entries)):
            result.fail(f"{manifest_names[algo]}: payload file present but not listed: {path!r}")


def _log_bag_verdict(result: BagVerifyResult) -> None:
    """Log the one-line summary that closes a verify run."""
    if result.ok:
        log.info(
            "Bag valid: %d payload files, %d bytes, manifests=%s",
            result.payload_file_count,
            result.payload_total_octets,
            ",".join(result.manifest_algorithms) or "(none)",
        )
    else:
        log.error("Bag invalid: %d error(s)", len(result.errors))


def verify_bag(
    client,
    bucket: str,
    bag_prefix: str,
    *,
    on_progress: ProgressCallback | None = None,
) -> BagVerifyResult:
    """Verify the BagIt bag rooted at ``s3://bucket/bag_prefix``.

    ``bag_prefix`` MUST end with ``/`` (the caller normalizes via
    :func:`s3_archive.url.parse_s3_prefix`).

    *on_progress*, if supplied, is invoked once per verified file with
    ``(rel_path, file_size)`` — designed to map onto a ``tqdm.update``
    in the CLI without the library knowing about tqdm. See
    :data:`ProgressCallback`.
    """
    bag_url = f"s3://{bucket}/{bag_prefix}"
    result = BagVerifyResult(bag_url=bag_url)
    log.info("Verifying bag at %s", bag_url)

    objects = _list_objects(client, bucket, bag_prefix)
    if not objects:
        result.fail(f"No objects found under {bag_url}")
        return result

    # bagit.txt must exist.
    if "bagit.txt" not in objects:
        result.fail("bagit.txt is missing — not a valid BagIt bag")
    else:
        _check_bagit_version(result, _read_text(client, bucket, bag_prefix + "bagit.txt"))

    # fetch.txt is out of scope — fail loud rather than silently miss files.
    if "fetch.txt" in objects and objects["fetch.txt"]["Size"] > 0:
        result.fail(_FETCH_TXT_NON_EMPTY)

    # A missing payload manifest is fatal to the bag but not to this run:
    # verify records it and carries on so the operator sees everything
    # else too. verify_against returns at the same point — a deliberate
    # divergence, pinned on both sides.
    manifests, tagmanifests = _discover_manifest_names(objects)
    if not manifests:
        result.fail("No payload manifest found (need at least one manifest-<algo>.txt)")
    _warn_unknown_algorithms(result, manifests, label="payload manifest")
    _warn_unknown_algorithms(result, tagmanifests, label="tag manifest")
    result.manifest_algorithms = sorted(manifests)
    result.tagmanifest_algorithms = sorted(tagmanifests)

    manifests_by_algo = _parse_manifests(client, bucket, bag_prefix, manifests, result)
    tagmanifests_by_algo = _parse_manifests(client, bucket, bag_prefix, tagmanifests, result)

    # Verify all payload files in a single pass per file, then confirm no
    # data/ file was left out of a manifest.
    _verify_files_single_pass(
        client,
        bucket,
        _build_expected_map(manifests_by_algo),
        objects,
        result,
        payload=True,
        on_progress=on_progress,
    )
    _check_payload_coverage(objects, manifests, manifests_by_algo, result)

    # Verify all tag files in a single pass per file.
    _verify_files_single_pass(
        client,
        bucket,
        _build_expected_map(tagmanifests_by_algo),
        objects,
        result,
        payload=False,
        on_progress=on_progress,
    )

    # Payload-Oxum from bag-info.txt.
    sizes = {rel: meta["Size"] for rel, meta in objects.items()}
    result.payload_file_count = sum(1 for rel in sizes if rel.startswith("data/"))
    result.payload_total_octets = sum(s for r, s in sizes.items() if r.startswith("data/"))
    if "bag-info.txt" in objects:
        _check_payload_oxum(
            result,
            _read_text(client, bucket, bag_prefix + "bag-info.txt"),
            result.payload_total_octets,
            result.payload_file_count,
            subject="data/",
        )

    _log_bag_verdict(result)
    return result
