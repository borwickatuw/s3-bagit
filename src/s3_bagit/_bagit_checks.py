"""BagIt primitives and checks shared by ``verify`` and ``verify_against``.

Both verifiers walk different sources — ``verify`` lists an extracted bag
at an S3 prefix, ``verify_against`` streams a serialized bag and hashes a
flat target prefix — but a handful of RFC 8493 checks are identical on
both sides. Those live here so the wording of an error appears once.

**Every name in this module is ``_``-prefixed, and that is load-bearing.**
pysmelly's ``build_function_index`` skips private names, so eight of its
checks cannot fire here. The one that matters is
``return-none-instead-of-raise``: a *public* ``check_payload_oxum(result,
...) -> None`` is precisely that check's target shape, and "fixing" it by
raising would convert an oxum problem into a fail-fast — destroying the
collect-all-errors commitment this repo is built around. Cross-module
``from x import _y`` is legal Python; the underscore is a design
constraint, not a style choice.

Rules every helper here obeys, because ``verify``/``verify_against``
collect all errors and report them together (CLAUDE.md: "operators want
the full picture in one run"):

1. A check takes ``result: BagVerifyResult`` and returns ``None`` or a
   data value — never a bool the caller branches on to ``return``. The
   moment a return value gates the caller, fail-fast is back.
2. A check may ``return`` early only to skip its *own* remaining work,
   never the caller's. The legitimate can't-continue returns stay in the
   two parent functions.
3. Nothing here raises on a bag defect. ``result.fail(...)`` then fall
   through (or return from the helper alone) is the only shape.
4. Anything looping over algorithms keeps looping past a per-item
   problem and returns a partial result rather than propagating.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from s3_bagit.exceptions import BagError

# Annotation-only import. ``verify`` imports this module at runtime, so a
# runtime import back would be a cycle; ``BagVerifyResult`` stays in
# ``verify`` because it is public API (``cli`` imports it from there) and
# everything in this module is deliberately private.
if TYPE_CHECKING:
    from s3_bagit.verify import BagVerifyResult

# Supported hash algorithms — anything ``hashlib.new()`` accepts works at
# runtime, but we hard-list the four standard BagIt choices for clearer
# error messages.
_KNOWN_ALGOS = {"md5", "sha1", "sha256", "sha512"}

# fetch.txt is out of scope for both verifiers — fail loud rather than
# silently miss files. The two call sites detect "non-empty" differently
# (object size vs. captured bytes), so only the wording is shared.
_FETCH_TXT_NON_EMPTY = (
    "fetch.txt is non-empty; s3-bagit does not yet handle remote-fetch bags. "
    "Resolve fetch.txt before verifying."
)


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


def _list_objects(client, bucket: str, prefix: str) -> dict[str, dict[str, Any]]:
    """List objects at *prefix*, keyed by their path relative to it."""
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


def _check_bagit_version(result: BagVerifyResult, bagit_txt_text: str) -> None:
    """Record the declared BagIt version and warn if it is not 0.97 / 1.0.

    Warn-only: an unexpected version is not a reason to stop verifying,
    and neither caller treats it as one.
    """
    info = _parse_bag_info(bagit_txt_text)
    result.declared_version = info.get("BagIt-Version")
    if result.declared_version not in {"0.97", "1.0"}:
        result.warn(f"BagIt-Version is {result.declared_version!r}; expected '0.97' or '1.0'")


def _warn_unknown_algorithms(
    result: BagVerifyResult,
    algorithms: Iterable[str],
    *,
    label: str,
) -> None:
    """Warn once per algorithm outside :data:`_KNOWN_ALGOS`, then carry on.

    *label* is the manifest kind as it appears in the message — either
    ``"payload manifest"`` or ``"tag manifest"``. Loops to the end: an
    unknown algorithm is attempted anyway, so every one gets its warning.
    """
    for algo in algorithms:
        if algo not in _KNOWN_ALGOS:
            result.warn(f"Unknown {label} algorithm {algo!r} — attempting anyway")


def _check_payload_oxum(
    result: BagVerifyResult,
    bag_info_text: str,
    actual_octets: int,
    actual_count: int,
    *,
    subject: str,
) -> None:
    """Compare ``Payload-Oxum`` in *bag_info_text* against the actual totals.

    The callers differ only in how they arrive at *actual_octets* /
    *actual_count* (``verify`` sums the ``data/`` objects in the bag,
    ``verify_against`` sums the target prefix), and in *subject* — the
    noun the mismatch message uses for the thing that was measured.

    Returns ``None`` in every case, including the two "cannot check"
    ones. It must never raise: an absent or malformed Payload-Oxum is a
    finding to collect, not a reason to abandon the rest of the run.
    The two ``return`` statements below skip only this function's own
    remaining work.
    """
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

    if actual_octets != expected_octets or actual_count != expected_count:
        result.fail(
            f"Payload-Oxum mismatch: bag-info.txt says {expected_octets}.{expected_count} "
            f"but {subject} has {actual_octets}.{actual_count}"
        )
