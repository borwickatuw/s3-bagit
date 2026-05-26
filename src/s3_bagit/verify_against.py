"""Verify the contents of an S3 prefix against the manifests inside a serialized bag.

Use case: you have a serialized BagIt ``.tar.gz`` in S3 and a separate
S3 prefix that holds the bag's payload "flat" (the way ``create-bag``
consumed it — ``data/`` stripped). This subcommand confirms the
prefix's bytes match the bag's manifest checksums without extracting
the bag.

Mechanics:

1. Stream the archive once. Capture every tag file (``bagit.txt``,
   ``bag-info.txt``, ``manifest-<algo>.txt``, ``tagmanifest-<algo>.txt``)
   into memory; drain payload-member bodies without storing them.
2. Find the bag root inside the archive (the directory containing
   ``bagit.txt``; ``""`` if at the archive top). Strip that prefix from
   the captured tag-file names so the manifests look the same as an
   extracted bag's would.
3. For every payload-manifest entry ``data/<rel>``, stream-hash
   ``s3://target_bucket/<target_prefix><rel>`` and compare. A single
   target read feeds every algorithm in use, so a multi-algorithm bag
   (sha256 + sha512) costs one egress per target file, not two.
4. Errors are collected and reported together (same model as
   ``verify``): missing files, mismatched checksums, target files not
   listed in any manifest, Payload-Oxum disagreement.

Tagmanifests are out of scope here: the target prefix is intentionally
"flat" with ``data/`` stripped, so it doesn't contain the bag's tag
files in the first place.
"""

import tarfile
import zipfile

from s3_archive.exceptions import UnsupportedArchiveFormatError
from s3_archive.hashing import stream_hash_object
from s3_archive.members import iter_archive_members

from s3_bagit.exceptions import BagError
from s3_bagit.log_config import get_logger
from s3_bagit.verify import (
    BagVerifyResult,
    _parse_bag_info,
    _parse_manifest_text,
)

log = get_logger(__name__)

_KNOWN_ALGOS = {"md5", "sha1", "sha256", "sha512"}


def _is_tag_file_name(name: str) -> bool:
    """True if *name* looks like a BagIt tag file (anywhere in the tree).

    Returns False for anything under a ``data/`` directory; we never want
    to materialize payload bytes in memory.
    """
    if "/data/" in name or name.startswith("data/") or name == "data":
        return False
    base = name.rsplit("/", 1)[-1]
    if base in ("bagit.txt", "bag-info.txt", "fetch.txt"):
        return True
    if base.startswith("manifest-") and base.endswith(".txt"):
        return True
    return base.startswith("tagmanifest-") and base.endswith(".txt")


def _read_tag_files(client, bucket: str, key: str, fmt: str) -> dict[str, bytes]:
    """Stream an archive once; capture tag-file bodies, drain payload bytes.

    Single loop over :func:`s3_archive.members.iter_archive_members`
    handles every supported archive format. A 100 GB payload member is
    drained without storing it in memory; only the (small) tag-file
    bodies are buffered.
    """
    captured: dict[str, bytes] = {}
    try:
        member_iter = iter_archive_members(client, bucket, key, fmt)
    except UnsupportedArchiveFormatError as exc:
        raise BagError(f"Unsupported archive format for verify-against: {fmt!r}") from exc
    for member in member_iter:
        if _is_tag_file_name(member.name):
            captured[member.name] = member.read_all()
        else:
            member.drain()
    return captured


def _identify_bag_root(captured: dict[str, bytes]) -> str:
    """Locate the ``bagit.txt`` and return everything before it (``""`` for unwrapped).

    Multiple ``bagit.txt`` files in different directories is a malformed
    archive (we don't know which one is "the" bag).
    """
    bagit_paths = [n for n in captured if n.rsplit("/", 1)[-1] == "bagit.txt"]
    if not bagit_paths:
        raise BagError("No bagit.txt found in archive — not a BagIt bag")
    if len(bagit_paths) > 1:
        raise BagError(
            f"Multiple bagit.txt found in archive: {sorted(bagit_paths)!r} — ambiguous bag root"
        )
    path = bagit_paths[0]
    if "/" not in path:
        return ""
    return path.rsplit("/", 1)[0] + "/"


def _strip_bag_root(captured: dict[str, bytes], bag_root: str) -> dict[str, bytes]:
    """Return only tag files under *bag_root*, with that prefix removed from keys."""
    out: dict[str, bytes] = {}
    for name, body in captured.items():
        if bag_root and not name.startswith(bag_root):
            continue
        rel = name[len(bag_root) :] if bag_root else name
        if "/" in rel:
            # A tag file in a subdirectory of the bag root — not standard BagIt.
            # We ignore it rather than treat it as bag metadata.
            continue
        out[rel] = body
    return out


def _list_target_objects(client, bucket: str, prefix: str) -> dict[str, int]:
    """Return ``{relative_path: size}`` for objects under *prefix*."""
    paginator = client.get_paginator("list_objects_v2")
    out: dict[str, int] = {}
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if obj["Size"] == 0 and key.endswith("/"):
                continue
            rel = key.removeprefix(prefix)
            if not rel:
                continue
            out[rel] = obj["Size"]
    return out


def verify_against(
    client,
    archive_bucket: str,
    archive_key: str,
    archive_fmt: str,
    target_bucket: str,
    target_prefix: str,
    *,
    archive_url: str,
    target_url: str,
    verbose: bool = False,
) -> BagVerifyResult:
    """Verify *target_prefix*'s files against the manifests inside the bag at *archive_key*.

    The target is treated as "flat" — manifest entries ``data/<rel>``
    correspond to objects at ``s3://target_bucket/<target_prefix><rel>``.
    """
    result = BagVerifyResult(bag_url=archive_url, target_url=target_url)
    log.info("Verifying %s against manifests in %s", target_url, archive_url)

    # Heuristic: a target that itself has /data/ in its prefix is almost
    # always an operator who meant `verify` against the extracted bag,
    # not `verify-against`. Warn loudly rather than silently look for
    # `data/data/foo.txt`.
    if "/data/" in "/" + target_prefix:
        result.warn(
            f"Target prefix {target_url!r} contains '/data/' — verify-against expects a "
            f"flat target with data/ stripped (the same shape `create-bag` consumed). "
            f"If you meant to check an extracted bag, use `s3-bagit verify` instead."
        )

    try:
        captured = _read_tag_files(client, archive_bucket, archive_key, archive_fmt)
    except (tarfile.TarError, zipfile.BadZipFile) as exc:
        result.fail(f"Could not read archive: {exc}")
        return result

    try:
        bag_root = _identify_bag_root(captured)
    except BagError as exc:
        result.fail(str(exc))
        return result

    tag_files = _strip_bag_root(captured, bag_root)

    # bagit.txt → declared version.
    bagit_txt = tag_files.get("bagit.txt")
    if bagit_txt is not None:
        info = _parse_bag_info(bagit_txt.decode("utf-8"))
        result.declared_version = info.get("BagIt-Version")
        if result.declared_version not in {"0.97", "1.0"}:
            result.warn(f"BagIt-Version is {result.declared_version!r}; expected '0.97' or '1.0'")

    # Reject non-empty fetch.txt for the same reason verify does.
    fetch_txt = tag_files.get("fetch.txt")
    if fetch_txt is not None and fetch_txt.strip():
        result.fail(
            "fetch.txt is non-empty; s3-bagit does not yet handle remote-fetch bags. "
            "Resolve fetch.txt before verifying."
        )

    # Collect payload manifests by algorithm.
    manifests: dict[str, dict[str, str]] = {}
    for name, body in tag_files.items():
        if name.startswith("manifest-") and name.endswith(".txt"):
            algo = name[len("manifest-") : -len(".txt")]
            try:
                manifests[algo] = _parse_manifest_text(body.decode("utf-8"))
            except BagError as exc:
                result.fail(f"{name}: {exc}")

    if not manifests:
        result.fail("No payload manifest found in archive (need at least one manifest-<algo>.txt)")
        return result

    for algo in manifests:
        if algo not in _KNOWN_ALGOS:
            result.warn(f"Unknown payload manifest algorithm {algo!r} — attempting anyway")
    result.manifest_algorithms = sorted(manifests)

    # Build the union of manifest paths and the per-file (algo -> expected) map.
    expected_by_target: dict[str, dict[str, str]] = {}
    for algo, entries in manifests.items():
        for rel, expected in entries.items():
            if not rel.startswith("data/"):
                result.fail(f"manifest-{algo}.txt: entry outside data/: {rel!r}")
                continue
            target_rel = rel[len("data/") :]
            expected_by_target.setdefault(target_rel, {})[algo] = expected

    target_objects = _list_target_objects(client, target_bucket, target_prefix)
    if not target_objects:
        result.fail(f"No objects found at target {target_url}")
        return result

    # Single read per target file, fed to every hasher we need.
    algorithms_needed = sorted(manifests)
    for target_rel, expected_map in sorted(expected_by_target.items()):
        if target_rel not in target_objects:
            result.fail(f"manifest lists data/{target_rel} but target has no such file")
            continue
        if verbose:
            log.info("  %s", target_rel)
        actuals = stream_hash_object(
            client, target_bucket, target_prefix + target_rel, algorithms_needed
        )
        for algo, expected in expected_map.items():
            actual = actuals[algo].lower()
            if actual != expected.lower():
                result.fail(
                    f"manifest-{algo}.txt: checksum mismatch for data/{target_rel}: "
                    f"expected {expected}, got {actual}"
                )

    # Files in the target but not covered by any manifest — symmetric with
    # verify's "data file present but not listed in manifest" error.
    extras = sorted(set(target_objects) - set(expected_by_target))
    for extra in extras:
        result.fail(
            f"Target has file with no manifest entry: {extra!r} "
            f"(expected manifest entry data/{extra})"
        )

    # Payload-Oxum sanity check against target totals.
    result.payload_file_count = len(target_objects)
    result.payload_total_octets = sum(target_objects.values())
    bag_info_txt = tag_files.get("bag-info.txt")
    if bag_info_txt is not None:
        _check_payload_oxum_against_target(result, bag_info_txt.decode("utf-8"), target_objects)

    if result.ok:
        log.info(
            "Target matches bag: %d files, %d bytes, manifests=%s",
            result.payload_file_count,
            result.payload_total_octets,
            ",".join(result.manifest_algorithms),
        )
    else:
        log.error("Target does not match bag: %d error(s)", len(result.errors))
    return result


def _check_payload_oxum_against_target(
    result: BagVerifyResult,
    bag_info_text: str,
    target_objects: dict[str, int],
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
    actual_octets = sum(target_objects.values())
    actual_count = len(target_objects)
    if actual_octets != expected_octets or actual_count != expected_count:
        result.fail(
            f"Payload-Oxum mismatch: bag-info.txt says {expected_octets}.{expected_count} "
            f"but target has {actual_octets}.{actual_count}"
        )
