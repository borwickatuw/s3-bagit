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

This module is written from the RFC; no code is copied from
storage-scripts, since storage-scripts does not implement BagIt
verification.
"""

from dataclasses import dataclass, field
from typing import Any

from s3_archive.hashing import stream_hash_object

from s3_bagit.exceptions import BagError
from s3_bagit.log_config import get_logger

log = get_logger(__name__)

# Supported hash algorithms — anything ``hashlib.new()`` accepts works at
# runtime, but we hard-list the four standard BagIt choices for clearer
# error messages.
_KNOWN_ALGOS = {"md5", "sha1", "sha256", "sha512"}


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


def _decode_manifest_path(raw: str) -> str:
    """Decode the limited percent-encoding allowed by RFC 8493 §2.1.3."""
    return (
        raw.replace("%0A", "\n")
        .replace("%0D", "\r")
        .replace("%0a", "\n")
        .replace("%0d", "\r")
        .replace("%25", "%")
    )


def _parse_manifest_text(text: str) -> dict[str, str]:
    """Parse a manifest file body into ``{relative_path: checksum}``.

    RFC 8493 §3: ``<checksum> <whitespace> <filepath>`` per line. The
    filepath may contain spaces, so we split only on the first run of
    whitespace.
    """
    entries: dict[str, str] = {}
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise BagError(f"manifest line {lineno} is malformed: {raw_line!r}")
        checksum, path = parts
        entries[_decode_manifest_path(path.strip())] = checksum.lower()
    return entries


def _list_bag_objects(client, bucket: str, prefix: str) -> dict[str, dict[str, Any]]:
    """List objects at *prefix*, keyed by their bag-relative path."""
    paginator = client.get_paginator("list_objects_v2")
    out: dict[str, dict[str, Any]] = {}
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Skip directory markers.
            if obj["Size"] == 0 and key.endswith("/"):
                continue
            rel = key.removeprefix(prefix)
            if not rel:
                continue
            out[rel] = {"Key": key, "Size": obj["Size"]}
    return out


def _stream_hash(client, bucket: str, key: str, algorithm: str) -> str:
    """Stream-download s3://bucket/key and return its lowercase hex digest.

    One-algorithm-at-a-time wrapper around
    :func:`s3_archive.hashing.stream_hash_object`. Matches the
    per-manifest call shape used by :func:`_verify_manifest`: each
    payload manifest re-reads the file with its own algorithm. That's
    intentionally not optimized — a multi-algorithm bag's verify cost
    scales linearly with manifest count, but the read code path stays
    simple. See verify_against for the single-pass multi-hash variant.
    """
    return stream_hash_object(client, bucket, key, (algorithm,))[algorithm]


def _read_text(client, bucket: str, key: str) -> str:
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    return body.decode("utf-8")


def _parse_bag_info(text: str) -> dict[str, str]:
    """Parse a bag-info.txt body. Handles RFC 8493 continuation lines.

    Returns the last value for any repeated label. Verification only
    needs Payload-Oxum, so this lossy approach is fine.
    """
    info: dict[str, str] = {}
    current_label: str | None = None
    for raw in text.splitlines():
        if raw and raw[0] in (" ", "\t"):
            if current_label is not None:
                info[current_label] = info[current_label] + " " + raw.strip()
            continue
        if ":" not in raw:
            continue
        label, value = raw.split(":", 1)
        current_label = label.strip()
        info[current_label] = value.strip()
    return info


def _check_payload_oxum(
    result: BagVerifyResult,
    bag_info_text: str,
    sizes: dict[str, int],
) -> None:
    info = _parse_bag_info(bag_info_text)
    oxum = info.get("Payload-Oxum")
    if not oxum:
        result.warn("bag-info.txt present but no Payload-Oxum — skipping oxum check")
        return
    try:
        octets_str, count_str = oxum.split(".", 1)
        expected_octets = int(octets_str)
        expected_count = int(count_str)
    except (ValueError, AttributeError):
        result.fail(f"bag-info.txt Payload-Oxum is malformed: {oxum!r}")
        return

    actual_octets = sum(size for rel, size in sizes.items() if rel.startswith("data/"))
    actual_count = sum(1 for rel in sizes if rel.startswith("data/"))
    if actual_octets != expected_octets or actual_count != expected_count:
        result.fail(
            f"Payload-Oxum mismatch: bag-info.txt says {expected_octets}.{expected_count} "
            f"but data/ has {actual_octets}.{actual_count}"
        )


def _verify_manifest(
    client,
    bucket: str,
    bag_prefix: str,
    manifest_rel: str,
    algorithm: str,
    objects: dict[str, dict[str, Any]],
    result: BagVerifyResult,
    *,
    payload: bool,
) -> set[str]:
    """Verify a single manifest (or tagmanifest) file. Returns the set of paths it covers."""
    text = _read_text(client, bucket, bag_prefix + manifest_rel)
    entries = _parse_manifest_text(text)
    covered: set[str] = set()
    label = "manifest" if payload else "tagmanifest"

    for rel, expected in entries.items():
        covered.add(rel)
        if payload and not rel.startswith("data/"):
            result.fail(f"{manifest_rel}: payload manifest entry outside data/: {rel!r}")
            continue
        if not payload and rel.startswith("data/"):
            result.fail(f"{manifest_rel}: tag manifest must not list payload files: {rel!r}")
            continue
        if rel not in objects:
            result.fail(f"{manifest_rel}: file listed but not present in bag: {rel!r}")
            continue
        actual = _stream_hash(client, bucket, objects[rel]["Key"], algorithm)
        if actual.lower() != expected.lower():
            result.fail(
                f"{manifest_rel}: checksum mismatch for {rel!r}: expected {expected}, got {actual}"
            )
        else:
            log.debug("%s ok (%s) %s", label, algorithm, rel)
    return covered


def verify_bag(client, bucket: str, bag_prefix: str) -> BagVerifyResult:
    """Verify the BagIt bag rooted at ``s3://bucket/bag_prefix``.

    ``bag_prefix`` MUST end with ``/`` (the caller normalizes via
    :func:`s3_bagit.s3_url.parse_s3_prefix`).
    """
    bag_url = f"s3://{bucket}/{bag_prefix}"
    result = BagVerifyResult(bag_url=bag_url)
    log.info("Verifying bag at %s", bag_url)

    objects = _list_bag_objects(client, bucket, bag_prefix)
    if not objects:
        result.fail(f"No objects found under {bag_url}")
        return result

    # bagit.txt must exist.
    if "bagit.txt" not in objects:
        result.fail("bagit.txt is missing — not a valid BagIt bag")
    else:
        info = _parse_bag_info(_read_text(client, bucket, bag_prefix + "bagit.txt"))
        result.declared_version = info.get("BagIt-Version")
        if result.declared_version not in {"0.97", "1.0"}:
            result.warn(f"BagIt-Version is {result.declared_version!r}; expected '0.97' or '1.0'")

    # fetch.txt is out of scope — fail loud rather than silently miss files.
    if "fetch.txt" in objects and objects["fetch.txt"]["Size"] > 0:
        result.fail(
            "fetch.txt is non-empty; s3-bagit does not yet handle remote-fetch bags. "
            "Resolve fetch.txt before verifying."
        )

    # Collect manifests and tagmanifests by algorithm.
    manifests: dict[str, str] = {}
    tagmanifests: dict[str, str] = {}
    for rel in objects:
        if rel.startswith("manifest-") and rel.endswith(".txt") and "/" not in rel:
            algo = rel[len("manifest-") : -len(".txt")]
            manifests[algo] = rel
        elif rel.startswith("tagmanifest-") and rel.endswith(".txt") and "/" not in rel:
            algo = rel[len("tagmanifest-") : -len(".txt")]
            tagmanifests[algo] = rel

    if not manifests:
        result.fail("No payload manifest found (need at least one manifest-<algo>.txt)")

    for algo in manifests:
        if algo not in _KNOWN_ALGOS:
            result.warn(f"Unknown payload manifest algorithm {algo!r} — attempting anyway")
    for algo in tagmanifests:
        if algo not in _KNOWN_ALGOS:
            result.warn(f"Unknown tag manifest algorithm {algo!r} — attempting anyway")

    result.manifest_algorithms = sorted(manifests)
    result.tagmanifest_algorithms = sorted(tagmanifests)

    # Verify each payload manifest.
    payload_covered: set[str] = set()
    for algo, rel in manifests.items():
        try:
            covered = _verify_manifest(
                client, bucket, bag_prefix, rel, algo, objects, result, payload=True
            )
        except BagError as exc:
            result.fail(f"{rel}: {exc}")
            continue
        payload_covered |= covered

    # Every data/ file must be covered by every payload manifest.
    data_files = {rel for rel in objects if rel.startswith("data/")}
    if not data_files:
        result.fail("No payload files found under data/ — bag has no content")
    for rel in manifests.values():
        try:
            entries_text = _read_text(client, bucket, bag_prefix + rel)
            entries = set(_parse_manifest_text(entries_text))
        except BagError:
            continue
        missing_in_manifest = data_files - entries
        for path in sorted(missing_in_manifest):
            result.fail(f"{rel}: payload file present but not listed: {path!r}")

    # Verify each tagmanifest.
    for algo, rel in tagmanifests.items():
        try:
            _verify_manifest(client, bucket, bag_prefix, rel, algo, objects, result, payload=False)
        except BagError as exc:
            result.fail(f"{rel}: {exc}")

    # Payload-Oxum from bag-info.txt.
    sizes = {rel: meta["Size"] for rel, meta in objects.items()}
    result.payload_file_count = sum(1 for rel in sizes if rel.startswith("data/"))
    result.payload_total_octets = sum(s for r, s in sizes.items() if r.startswith("data/"))
    if "bag-info.txt" in objects:
        _check_payload_oxum(result, _read_text(client, bucket, bag_prefix + "bag-info.txt"), sizes)

    if result.ok:
        log.info(
            "Bag valid: %d payload files, %d bytes, manifests=%s",
            result.payload_file_count,
            result.payload_total_octets,
            ",".join(result.manifest_algorithms) or "(none)",
        )
    else:
        log.error("Bag invalid: %d error(s)", len(result.errors))
    return result
