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

from s3_archive.exceptions import ArchiveReadError, UnsupportedArchiveFormatError
from s3_archive.hashing import stream_hash_object
from s3_archive.members import iter_archive_members

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
from s3_bagit.verify import BagVerifyResult, ProgressCallback

log = get_logger(__name__)


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


def _read_tag_files(src_client, bucket: str, key: str, fmt: str) -> dict[str, bytes]:
    """Stream an archive once; capture tag-file bodies, drain payload bytes.

    Single loop over :func:`s3_archive.members.iter_archive_members`
    handles every supported archive format. A 100 GB payload member is
    drained without storing it in memory; only the (small) tag-file
    bodies are buffered.
    """
    captured: dict[str, bytes] = {}
    try:
        member_iter = iter_archive_members(src_client, bucket, key, fmt)
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


def _list_target_objects(dst_client, bucket: str, prefix: str) -> dict[str, int]:
    """Return ``{relative_path: size}`` for objects under *prefix*.

    The flat target has no ``Key`` of its own worth carrying — every
    object's key is ``prefix + rel`` — so this narrows the shared
    listing to the sizes ``verify_against`` actually reconciles against.
    """
    return {rel: meta["Size"] for rel, meta in _list_objects(dst_client, bucket, prefix).items()}


def _check_archive_tag_files(result: BagVerifyResult, tag_files: dict[str, bytes]) -> None:
    """Check the archive's own tag files: BagIt version, then fetch.txt.

    Both files are optional here — a bag missing either is a finding for
    the manifests to expose, not a reason to stop — so this collects and
    always returns ``None``.
    """
    bagit_txt = tag_files.get("bagit.txt")
    if bagit_txt is not None:
        _check_bagit_version(result, bagit_txt.decode("utf-8"))

    # Non-empty fetch.txt is rejected for the same reason verify rejects
    # it, but detected differently: captured bytes here, object size there.
    fetch_txt = tag_files.get("fetch.txt")
    if fetch_txt is not None and fetch_txt.strip():
        result.fail(_FETCH_TXT_NON_EMPTY)


def _load_archive_manifests(
    tag_files: dict[str, bytes],
    result: BagVerifyResult,
) -> dict[str, dict[str, str]]:
    """Parse the archive's payload manifests into ``{algorithm: {rel: expected}}``.

    Tagmanifests are out of scope: the flat target has ``data/`` stripped
    and carries no tag files to check them against.

    Keeps looping past a manifest that fails to parse — the offending
    file becomes an error and its algorithm never enters the map, so a
    multi-algorithm bag is still checked on the algorithms that did
    parse. An empty return is not an error signal; the caller decides
    what "no manifests" means, and on this side it means stop.

    Deliberately *not* shared with ``verify._parse_manifests``. That side
    reads each manifest from S3 by name, parses tagmanifests too, and
    diverges on what happens when none are found.
    """
    manifests: dict[str, dict[str, str]] = {}
    for name, body in tag_files.items():
        if name.startswith("manifest-") and name.endswith(".txt"):
            algo = name[len("manifest-") : -len(".txt")]
            try:
                manifests[algo] = _parse_manifest_text(body.decode("utf-8"))
            except BagError as exc:
                result.fail(f"{name}: {exc}")
    return manifests


def _map_expected_by_target(
    manifests: dict[str, dict[str, str]],
    result: BagVerifyResult,
) -> dict[str, dict[str, str]]:
    """Re-key the manifests onto the flat target: ``{target_rel: {algo: expected}}``.

    A manifest entry outside ``data/`` has no counterpart in a target
    whose ``data/`` was stripped. Each one is recorded and skipped, and
    the loop carries on to the next entry rather than abandoning the
    algorithm or the run.
    """
    expected_by_target: dict[str, dict[str, str]] = {}
    for algo, entries in manifests.items():
        for rel, expected in entries.items():
            if not rel.startswith("data/"):
                result.fail(f"manifest-{algo}.txt: entry outside data/: {rel!r}")
                continue
            expected_by_target.setdefault(rel[len("data/") :], {})[algo] = expected
    return expected_by_target


def _hash_and_compare_targets(
    dst_client,
    target_bucket: str,
    target_prefix: str,
    expected_by_target: dict[str, dict[str, str]],
    target_objects: dict[str, int],
    result: BagVerifyResult,
    *,
    verbose: bool,
    on_progress: ProgressCallback | None,
) -> None:
    """Hash each manifest-listed target file once and compare every algorithm.

    A single read feeds every hasher, so a sha256 + sha512 bag costs one
    egress per target file rather than two. A file the manifests list but
    the target lacks is recorded and skipped; the loop always runs to the
    end so one missing file never hides the next mismatch.

    Pinned divergence from ``verify``: this hashes every target file with
    *every* algorithm the bag uses (``result.manifest_algorithms``) and
    then compares only the subset that listed the file, whereas ``verify``
    hashes each file with just the algorithms that listed it. Real, known,
    and not this function's to reconcile.
    """
    for target_rel, expected_map in sorted(expected_by_target.items()):
        if target_rel not in target_objects:
            result.fail(f"manifest lists data/{target_rel} but target has no such file")
            continue
        if verbose:
            log.info("  %s", target_rel)
        actuals = stream_hash_object(
            dst_client, target_bucket, target_prefix + target_rel, result.manifest_algorithms
        )
        for algo, expected in expected_map.items():
            actual = actuals[algo].lower()
            if actual != expected.lower():
                result.fail(
                    f"manifest-{algo}.txt: checksum mismatch for data/{target_rel}: "
                    f"expected {expected}, got {actual}"
                )
        if on_progress is not None:
            on_progress(target_rel, target_objects[target_rel])


def _reconcile_target_totals(
    tag_files: dict[str, bytes],
    target_objects: dict[str, int],
    expected_by_target: dict[str, dict[str, str]],
    result: BagVerifyResult,
) -> None:
    """Report target files no manifest covers, then check Payload-Oxum.

    The extras check is symmetric with ``verify``'s "payload file present
    but not listed". The oxum check runs second because it compares
    against the counts recorded here, and both collect rather than raise —
    an absent or lying Payload-Oxum is a finding, not a reason to abandon
    the run.
    """
    for extra in sorted(set(target_objects) - set(expected_by_target)):
        result.fail(
            f"Target has file with no manifest entry: {extra!r} "
            f"(expected manifest entry data/{extra})"
        )

    result.payload_file_count = len(target_objects)
    result.payload_total_octets = sum(target_objects.values())
    bag_info_txt = tag_files.get("bag-info.txt")
    if bag_info_txt is not None:
        _check_payload_oxum(
            result,
            bag_info_txt.decode("utf-8"),
            result.payload_total_octets,
            result.payload_file_count,
            subject="target",
        )


def _log_target_verdict(result: BagVerifyResult) -> None:
    """Log the one-line summary that closes a verify-against run."""
    if result.ok:
        log.info(
            "Target matches bag: %d files, %d bytes, manifests=%s",
            result.payload_file_count,
            result.payload_total_octets,
            ",".join(result.manifest_algorithms),
        )
    else:
        log.error("Target does not match bag: %d error(s)", len(result.errors))


def verify_against(
    src_client,
    dst_client,
    archive_bucket: str,
    archive_key: str,
    archive_fmt: str,
    target_bucket: str,
    target_prefix: str,
    *,
    archive_url: str,
    target_url: str,
    verbose: bool = False,
    on_progress: ProgressCallback | None = None,
) -> BagVerifyResult:
    """Verify *target_prefix*'s files against the manifests inside the bag at *archive_key*.

    *src_client* reads the archive object; *dst_client* lists and
    hashes the target prefix. They may be the same boto3 client
    (single-endpoint workflows) or two clients pointed at different
    endpoints — see s3-archive's ``client_for`` resolver.

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
        captured = _read_tag_files(src_client, archive_bucket, archive_key, archive_fmt)
    except ArchiveReadError as exc:
        result.fail(f"Could not read archive: {exc}")
        return result

    try:
        bag_root = _identify_bag_root(captured)
    except BagError as exc:
        result.fail(str(exc))
        return result

    tag_files = _strip_bag_root(captured, bag_root)

    _check_archive_tag_files(result, tag_files)

    # A bag with no payload manifest is the one no-manifest case that
    # stops this run: with nothing to compare the target against there is
    # no further work to do. `verify` fails and *continues* at the same
    # point, because it still has structural checks left. Pinned on both
    # sides; do not harmonize.
    manifests = _load_archive_manifests(tag_files, result)
    if not manifests:
        result.fail("No payload manifest found in archive (need at least one manifest-<algo>.txt)")
        return result

    _warn_unknown_algorithms(result, manifests, label="payload manifest")
    result.manifest_algorithms = sorted(manifests)

    expected_by_target = _map_expected_by_target(manifests, result)

    target_objects = _list_target_objects(dst_client, target_bucket, target_prefix)
    if not target_objects:
        result.fail(f"No objects found at target {target_url}")
        return result

    _hash_and_compare_targets(
        dst_client,
        target_bucket,
        target_prefix,
        expected_by_target,
        target_objects,
        result,
        verbose=verbose,
        on_progress=on_progress,
    )

    _reconcile_target_totals(tag_files, target_objects, expected_by_target, result)

    _log_target_verdict(result)
    return result
