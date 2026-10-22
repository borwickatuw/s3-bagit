"""``s3-bagit`` command-line entry point.

Subcommands:

  * ``extract`` — stream a BagIt archive (tar / tar.gz / tar.bz2 /
    tar.xz / tar.zst / zip / 7z) out of S3 and upload each member to a
    destination S3 prefix. By default also runs ``verify`` against the
    destination prefix; pass ``--no-verify`` to skip.

  * ``verify`` — verify an already-extracted bag at an S3 prefix.

  * ``verify-against`` — verify that the files under an S3 prefix
    match the manifests inside a serialized bag, without extracting
    the bag. Useful for confirming that a flat directory still
    matches its archival ``.tar.gz``.

  * ``create-bag`` — stream the objects under an S3 prefix into a
    serialized BagIt ``.tar.gz`` at a destination S3 key.

  * ``ls`` — stream-list an archive's members without extracting.

  * ``config`` — interactively write an s3cmd-INI credentials file.

  * ``issue`` — open a pre-filled GitHub new-issue page in a browser.

The CLI's job is to parse args, build the S3 client (where needed),
dispatch, and translate exceptions into clean stderr messages + exit
codes. All real work lives in the matching modules.

Parser construction lives next door in ``_cli_parsers``: declaring a
subcommand's flags and handling them change for different reasons, so
this module holds only the handlers, the progress/report presentation,
and the exception-to-exit-code translation.
"""

import argparse
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from botocore.exceptions import ClientError
from dotenv import load_dotenv
from tqdm import tqdm

from s3_archive.exceptions import UnsupportedArchiveFormatError
from s3_archive.extract import ExtractEvent, extract
from s3_archive.ls import list_archive
from s3_archive.s3_client import client_for
from s3_archive.url import detect_format, looks_like_archive_url, parse_s3_prefix, parse_s3_url

from s3_bagit._cli_parsers import _build_parser
from s3_bagit.config_cmd import run_config
from s3_bagit.create_bag import create_bag
from s3_bagit.exceptions import BagError, ConfigError, S3OperationError
from s3_bagit.issue import open_issue
from s3_bagit.log_config import get_logger, resolve_level, setup_console
from s3_bagit.verify import BagVerifyResult, verify_bag
from s3_bagit.verify_against import verify_against

log = get_logger(__name__)

# Exit codes.
_EXIT_OK = 0
_EXIT_VERIFY_FAILED = 1
_EXIT_CONFIG_ERROR = 2
_EXIT_S3_ERROR = 4
# 128 + SIGINT(2) — the conventional POSIX exit code for "killed by Ctrl-C".
_EXIT_INTERRUPTED = 130

_ISSUE_HINT = "For help, run: s3-bagit issue"


def _print_verify_report(result: BagVerifyResult) -> None:
    print()
    print(f"Bag: {result.bag_url}")
    if result.target_url:
        print(f"Target: {result.target_url}")
    if result.declared_version:
        print(f"  BagIt-Version: {result.declared_version}")
    if result.manifest_algorithms:
        print(f"  Payload manifests: {', '.join(result.manifest_algorithms)}")
    if result.tagmanifest_algorithms:
        print(f"  Tag manifests:     {', '.join(result.tagmanifest_algorithms)}")
    print(
        f"  Payload:           {result.payload_file_count} files, "
        f"{result.payload_total_octets} bytes"
    )
    if result.warnings:
        print(f"  Warnings ({len(result.warnings)}):")
        for warning in result.warnings:
            print(f"    - {warning}")
    if result.errors:
        print(f"  Errors ({len(result.errors)}):")
        for error in result.errors:
            print(f"    - {error}")
        print("RESULT: INVALID")
    else:
        print("RESULT: VALID")


def _wrap_client_error(
    exc: ClientError,
    *,
    archive_url: str,
    dest_url: str,
    operation_hint: str,
) -> S3OperationError:
    """Translate a botocore ``ClientError`` into an operator-facing message.

    Carries operation name, HTTP status, both URLs, and — when the server
    returned ``Content-Type: text/html`` — a hint that the failure was at
    an upstream proxy rather than S3 itself. The HTML hint exists because
    Apache-fronted RGW deployments can return a stock Apache 500 page for
    permission/proxy misconfigurations, which botocore can't parse as S3
    XML and surfaces as an opaque "An error occurred (500)".
    """
    resp = exc.response or {}
    err = resp.get("Error", {}) or {}
    meta = resp.get("ResponseMetadata", {}) or {}
    status = meta.get("HTTPStatusCode", "?")
    code = err.get("Code", "?")
    message = err.get("Message") or str(exc)
    headers = meta.get("HTTPHeaders", {}) or {}
    content_type = (headers.get("content-type") or "").lower()
    op_name = getattr(exc, "operation_name", None) or "?"

    lines = [
        f"S3 {op_name} failed during {operation_hint}.",
        f"  Source:      {archive_url}",
        f"  Destination: {dest_url}",
        f"  HTTP {status} ({code}): {message}",
    ]
    if "text/html" in content_type:
        lines.append(
            "  The server returned HTML, not S3 XML — this usually means "
            "an upstream proxy (e.g. Apache in front of Ceph RGW) rejected "
            "the request before it reached S3. Likely causes: missing write "
            "permission for this prefix, or a server-side proxy/ACL "
            "misconfiguration. Re-run with -v to see the response body, "
            "and contact your S3 administrator."
        )
    return S3OperationError("\n".join(lines))


def _progress_bar(*, desc: str) -> tqdm:
    """Construct the shared tqdm bar shape for long-running CLI operations.

    Single bar, bytes, IEC scale (KiB/MiB/GiB so an operator can mentally
    cross-check against S3 console sizes). Auto-disables when stderr
    isn't a TTY — keeps CI logs and shell redirects clean.
    """
    return tqdm(
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc=desc,
        miniters=1,
        disable=not sys.stderr.isatty(),
        leave=True,
    )


def _truncate_for_postfix(name: str, width: int = 40) -> str:
    """Trim *name* to the right-most *width* chars; postfix ellipsis if cut.

    Keeps the tail of the path because that's where the unique-per-file
    information lives (the bag prefix repeats across every member).
    """
    if len(name) <= width:
        return name
    return "…" + name[-(width - 1) :]


def _make_per_file_progress_cb(bar: tqdm):
    """Build a ``(rel, bytes_done)`` callback that advances *bar* per file."""

    def _cb(rel: str, bytes_done: int) -> None:
        bar.set_postfix_str(f"file={_truncate_for_postfix(rel)}", refresh=False)
        bar.update(bytes_done)

    return _cb


def _make_extract_progress_cb(bar: tqdm):
    """Build an ``ExtractEvent`` callback that advances *bar* on each event.

    Boundary events (``bytes_transferred == 0``) update the current-file
    postfix; byte-transfer events advance the bar. tqdm.update is
    thread-safe, which matters because boto3 dispatches the byte
    callbacks from its transfer threadpool.
    """

    def _cb(event: ExtractEvent) -> None:
        if event.bytes_transferred == 0:
            bar.set_postfix_str(f"file={_truncate_for_postfix(event.member)}", refresh=False)
        else:
            bar.update(event.bytes_transferred)

    return _cb


def _detect_archive_wrap(members: list[str]) -> str | None:
    """Return the single shared top-level directory across *members*, or None.

    "Single shared top-level directory" means every member's first path
    segment is the same and every member has at least one path
    separator (so no member sits at the archive root). Returns None for
    an empty list, mixed top-level segments, or any file at root —
    those cases are intentionally distinguished from "wrapped" because
    they need different handling.
    """
    top: str | None = None
    for raw in members:
        m = raw.strip("/")
        if not m:
            continue
        head, sep, _ = m.partition("/")
        if not sep:
            # File at root level — no consistent wrap.
            return None
        if top is None:
            top = head
        elif top != head:
            return None
    return top


def _summarize_archive_top_level(members: list[str], *, limit: int = 5) -> str:
    """Build a short human-readable description of distinct top-level entries.

    Used in the --strip-top-level error message so the operator can see
    why the strict check refused. Files at root are shown as ``<root>/<name>``
    to distinguish them from directories.
    """
    seen: set[str] = set()
    entries: list[str] = []
    for raw in members:
        m = raw.strip("/")
        if not m:
            continue
        head, sep, _ = m.partition("/")
        label = f"{head}/" if sep else f"<root>/{head}"
        if label in seen:
            continue
        seen.add(label)
        entries.append(label)
    if not entries:
        return "(empty archive)"
    if len(entries) > limit:
        return ", ".join(entries[:limit]) + f", … ({len(entries)} total)"
    return ", ".join(entries)


def _rewrite_strip_top_level(
    client,
    bucket: str,
    dest_prefix: str,
    wrap: str,
    members: list[str],
) -> None:
    """Server-side move every ``<dest>/<wrap>/<rel>`` object to ``<dest>/<rel>``.

    Uses ``client.copy`` (multipart-aware, no data flows through us)
    plus ``delete_object``, run across a small threadpool. The post-
    extract verify pass is the integrity check — if a copy went wrong,
    the SHA mismatch surfaces there.
    """
    dest_prefix = dest_prefix.rstrip("/")
    wrap_slash = f"{wrap}/"

    pairs: list[tuple[str, str]] = []
    for raw in members:
        m = raw.strip("/")
        if not m or not m.startswith(wrap_slash):
            continue
        rel = m[len(wrap_slash) :]
        old_key = f"{dest_prefix}/{m}" if dest_prefix else m
        new_key = f"{dest_prefix}/{rel}" if dest_prefix else rel
        pairs.append((old_key, new_key))

    def _move(pair: tuple[str, str]) -> None:
        old_key, new_key = pair
        client.copy(
            CopySource={"Bucket": bucket, "Key": old_key},
            Bucket=bucket,
            Key=new_key,
        )
        client.delete_object(Bucket=bucket, Key=old_key)

    bar = tqdm(
        total=len(pairs),
        unit="file",
        desc="Reorganizing",
        disable=not sys.stderr.isatty(),
        leave=True,
    )
    with bar, ThreadPoolExecutor(max_workers=16) as pool:
        for _ in pool.map(_move, pairs):
            bar.update(1)


def _cmd_extract(args: argparse.Namespace) -> int:
    src = parse_s3_url(args.archive_url)
    if not src.key:
        raise ConfigError(f"Archive URL needs a key: {args.archive_url!r}")
    dst = parse_s3_prefix(args.dest_url)
    fmt = detect_format(args.archive_url)

    # Resolve both clients up-front so a missing profile fails fast,
    # before any archive stream is opened.
    src_client = client_for(src.profile)
    dst_client = client_for(dst.profile)

    try:
        with _progress_bar(desc="Extracting") as bar:
            members = extract(
                src_client,
                dst_client,
                src.bucket,
                src.key,
                dst.bucket,
                dst.key,
                fmt,
                dry_run=args.dry_run,
                verbose=args.verbose,
                on_progress=_make_extract_progress_cb(bar),
            )
    except ClientError as exc:
        raise _wrap_client_error(
            exc,
            archive_url=args.archive_url,
            dest_url=args.dest_url,
            operation_hint="extract",
        ) from exc

    if args.dry_run:
        return _EXIT_OK

    # Decide where the bag actually landed and whether to reorganize.
    wrap = _detect_archive_wrap(members)
    dest_prefix_norm = dst.key.rstrip("/")
    bag_prefix = dst.key  # default: bag is at the destination prefix as given

    if args.strip_top_level:
        if wrap is None:
            summary = _summarize_archive_top_level(members)
            raise BagError(
                f"--strip-top-level requires the archive to contain exactly "
                f"one top-level directory. Found: {summary}.\n"
                f"Extracted files remain at {args.dest_url} for inspection.\n"
                f"Clean up with:  s3cmd rm -r {args.dest_url}"
            )
        log.info(
            "Stripping top-level directory %r from %s",
            wrap,
            args.dest_url,
        )
        _rewrite_strip_top_level(dst_client, dst.bucket, dst.key, wrap, members)
        # After the rewrite, the bag is at the destination prefix.
        bag_prefix = dst.key
    elif wrap is not None and f"{wrap}/bagit.txt" in members:
        # Archive wraps a bag in a top-level directory. Leave the layout
        # alone but point auto-verify at the nested bag root so the
        # operator gets a useful verify result instead of "bagit.txt missing".
        bag_prefix = f"{dest_prefix_norm}/{wrap}/" if dest_prefix_norm else f"{wrap}/"
        log.info(
            "Archive wraps bag in top-level directory '%s/'; "
            "bag is at s3://%s/%s (use --strip-top-level to flatten on extract)",
            wrap,
            dst.bucket,
            bag_prefix,
        )

    if args.no_verify:
        return _EXIT_OK

    log.info("Verifying extracted bag…")
    with _progress_bar(desc="Verifying") as bar:
        result = verify_bag(
            dst_client, dst.bucket, bag_prefix, on_progress=_make_per_file_progress_cb(bar)
        )
    _print_verify_report(result)
    return _EXIT_OK if result.ok else _EXIT_VERIFY_FAILED


def _guard_against_archive_url(bag_url: str) -> None:
    """Reject `verify <archive>` with a message pointing at `extract` instead.

    A common operator mistake is to point ``verify`` at a serialized
    bag (``...bag.tar.gz``) rather than an extracted-bag prefix. Without
    this guard the symptom is ``No objects found`` followed by
    ``RESULT: INVALID``, which implies we checked a bag and it failed —
    misleading. Fail fast with a clear ConfigError instead.

    The extension list is sourced from s3-archive's ``looks_like_archive_url``
    so this guard stays in lockstep with the formats s3-archive can read.
    """
    if looks_like_archive_url(bag_url):
        raise ConfigError(
            f"{bag_url} looks like an archive file, not an extracted-bag prefix.\n"
            f"`verify` operates on an already-extracted bag whose files live "
            f"at an S3 prefix.\n"
            f"To check this archive's contents, extract it first "
            f"(extract auto-verifies):\n"
            f"    s3-bagit extract {bag_url} s3://<bucket>/<dest-prefix>/\n"
            f"Verifying a serialized bag without extracting it is not "
            f"implemented in v1 — see docs/BAGIT-SPEC.md."
        )


def _cmd_verify(args: argparse.Namespace) -> int:
    _guard_against_archive_url(args.bag_url)
    bag = parse_s3_prefix(args.bag_url)
    with _progress_bar(desc="Verifying") as bar:
        result = verify_bag(
            client_for(bag.profile),
            bag.bucket,
            bag.key,
            on_progress=_make_per_file_progress_cb(bar),
        )
    _print_verify_report(result)
    return _EXIT_OK if result.ok else _EXIT_VERIFY_FAILED


def _cmd_verify_against(args: argparse.Namespace) -> int:
    src = parse_s3_url(args.archive_url)
    if not src.key:
        raise ConfigError(f"Archive URL needs a key: {args.archive_url!r}")
    archive_fmt = detect_format(args.archive_url)
    dst = parse_s3_prefix(args.target_url)

    # Resolve both clients up-front so a missing profile fails fast,
    # before any archive stream is opened.
    src_client = client_for(src.profile)
    dst_client = client_for(dst.profile)

    with _progress_bar(desc="Verifying against") as bar:
        result = verify_against(
            src_client,
            dst_client,
            src.bucket,
            src.key,
            archive_fmt,
            dst.bucket,
            dst.key,
            archive_url=args.archive_url,
            target_url=args.target_url,
            verbose=args.verbose,
            on_progress=_make_per_file_progress_cb(bar),
        )
    _print_verify_report(result)
    return _EXIT_OK if result.ok else _EXIT_VERIFY_FAILED


_BAG_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz")


def _parse_bag_info_args(items: list[str]) -> list[tuple[str, str]]:
    """Split ``--bag-info LABEL=VALUE`` strings into ``(label, value)`` pairs.

    Empty labels and missing ``=`` are rejected here so the operator gets
    a clean ConfigError rather than a confusing bag-info.txt.
    """
    out: list[tuple[str, str]] = []
    for raw in items:
        if "=" not in raw:
            raise ConfigError(f"--bag-info expects LABEL=VALUE, got {raw!r} (no '=' found)")
        label, value = raw.split("=", 1)
        label = label.strip()
        if not label:
            raise ConfigError(f"--bag-info has an empty LABEL: {raw!r}")
        out.append((label, value))
    return out


def _cmd_create_bag(args: argparse.Namespace) -> int:
    src = parse_s3_prefix(args.src_url)
    dst = parse_s3_url(args.dest_url)
    if not dst.key:
        raise ConfigError(f"Destination URL needs a key: {args.dest_url!r}")
    if not dst.key.lower().endswith(_BAG_ARCHIVE_SUFFIXES):
        raise ConfigError(
            f"Destination URL must end with .tar.gz or .tgz (got {args.dest_url!r}). "
            "create-bag only produces gzip-compressed tar archives in v1."
        )

    bag_info = _parse_bag_info_args(args.bag_info)
    # Resolve both clients up-front so a missing profile fails fast,
    # before any source object is fetched.
    src_client = client_for(src.profile)
    dst_client = client_for(dst.profile)

    with _progress_bar(desc="Creating bag") as bar:
        create_bag(
            src_client,
            dst_client,
            src.bucket,
            src.key,
            dst.bucket,
            dst.key,
            bag_name=args.bag_name,
            algorithm=args.algorithm,
            bag_info=bag_info,
            verbose=args.verbose,
            on_progress=_make_per_file_progress_cb(bar),
        )
    return _EXIT_OK


def _cmd_ls(args: argparse.Namespace) -> int:
    src = parse_s3_url(args.archive_url)
    if not src.key:
        raise ConfigError(f"Archive URL needs a key: {args.archive_url!r}")
    fmt = detect_format(args.archive_url)
    list_archive(client_for(src.profile), src.bucket, src.key, fmt)
    return _EXIT_OK


def _cmd_config(args: argparse.Namespace) -> int:
    return run_config(profile=args.profile)


def _cmd_issue(args: argparse.Namespace) -> int:
    return open_issue(args.brief)


# Subcommand -> handler. `config` and `issue` don't need (and shouldn't
# require) S3 creds; the rest each build their own client(s) via
# `client_for(profile)` inside the handler, so a missing profile fails
# before any stream opens. Keys must match the `sub.add_parser(...)` names
# in `_cli_parsers`; argparse's `required=True` guarantees `args.command`
# is one of those names, and tests/test_cli.py drives all seven through
# `main()`, so a subparser added without a handler here fails the suite.
_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "config": _cmd_config,
    "issue": _cmd_issue,
    "extract": _cmd_extract,
    "verify": _cmd_verify,
    "verify-against": _cmd_verify_against,
    "create-bag": _cmd_create_bag,
    "ls": _cmd_ls,
}


def main(argv: list[str] | None = None) -> int:
    # Load .env from CWD if present — operators frequently invoke from
    # the repo directory; CI / Docker should rely on the real environment.
    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(env_path)

    args = _build_parser().parse_args(argv)

    try:
        # Inside the try: resolve_level raises ConfigError on an unusable
        # $LOG_LEVEL, and that deserves the same operator-facing message
        # and exit code as any other configuration mistake.
        setup_console(resolve_level(verbose=args.verbose))
        return _COMMANDS[args.command](args)
    except KeyboardInterrupt:
        # Operator hit Ctrl-C — exit cleanly instead of dumping a traceback.
        print("\nCancelled.", file=sys.stderr)
        return _EXIT_INTERRUPTED
    except (ConfigError, UnsupportedArchiveFormatError) as exc:
        # `s3_bagit.exceptions.ConfigError` is now an alias for
        # `s3_archive.exceptions.ConfigError`, so this catch covers both
        # s3-bagit and s3-archive raise sites. s3-archive raises
        # `UnsupportedArchiveFormatError` instead of ConfigError for
        # unknown archive extensions; it maps to the same operator-facing
        # message and exit code, so the two share one handler.
        print(f"Configuration error: {exc}", file=sys.stderr)
        print(_ISSUE_HINT, file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    except BagError as exc:
        print(f"Bag error: {exc}", file=sys.stderr)
        print(_ISSUE_HINT, file=sys.stderr)
        return _EXIT_VERIFY_FAILED
    except S3OperationError as exc:
        print(str(exc), file=sys.stderr)
        print(_ISSUE_HINT, file=sys.stderr)
        return _EXIT_S3_ERROR


if __name__ == "__main__":
    sys.exit(main())
