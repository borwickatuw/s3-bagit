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
"""

import argparse
import logging
import sys
from pathlib import Path

from botocore.exceptions import ClientError
from dotenv import load_dotenv
from tqdm import tqdm

from s3_archive.config_cmd import validate_profile_name as _validate_profile_name
from s3_archive.exceptions import UnsupportedArchiveFormatError
from s3_archive.extract import ExtractEvent, extract
from s3_archive.ls import list_archive
from s3_archive.s3_client import client_for
from s3_archive.url import detect_format, looks_like_archive_url, parse_s3_prefix, parse_s3_url

from s3_bagit import REPO_URL, __version__
from s3_bagit.config_cmd import run_config
from s3_bagit.create_bag import create_bag
from s3_bagit.exceptions import BagError, ConfigError, S3OperationError
from s3_bagit.issue import open_issue
from s3_bagit.log_config import get_logger, setup_console
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


def _argparse_profile(value: str) -> str:
    """argparse `type=` for --profile that maps ConfigError → ArgumentTypeError.

    argparse handles `ArgumentTypeError` / `ValueError` / `TypeError`
    from a `type=` callable cleanly; anything else surfaces as a
    traceback. We catch the library-level ConfigError here and re-raise
    in argparse's preferred form so the user sees a clean message.
    """
    try:
        return _validate_profile_name(value)
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="s3-bagit",
        description=(
            "BagIt extract and verify operations against any S3-compatible storage. "
            "All operations stream end-to-end — nothing is staged on local disk."
        ),
        epilog=(
            "S3 credentials: run `s3-bagit config` for an interactive setup, "
            "or set $S3CMD_CONFIG (path to an s3cmd INI) or $S3_ENDPOINT_URL "
            "(used with the standard $AWS_ACCESS_KEY_ID / $AWS_SECRET_ACCESS_KEY). "
            f"Full resolution order: {REPO_URL}#credential-resolution-order"
        ),
    )
    parser.add_argument("--version", action="version", version=f"s3-bagit {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show per-file progress.")

    sub = parser.add_subparsers(dest="command", required=True)

    p_extract = sub.add_parser(
        "extract",
        help=(
            "Extract a BagIt archive (tar/tar.gz/tar.bz2/tar.xz/tar.zst/zip/7z) "
            "in S3 to a destination prefix."
        ),
    )
    p_extract.add_argument(
        "archive_url",
        help="Source archive URL, e.g. s3://my-bucket/incoming/bag.tar.gz",
    )
    p_extract.add_argument(
        "dest_url",
        help="Destination prefix URL, e.g. s3://my-bucket/extracted/bag/",
    )
    p_extract.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the post-extract bag verification.",
    )
    p_extract.add_argument(
        "--dry-run",
        action="store_true",
        help="List members that would be written without uploading anything.",
    )

    p_verify = sub.add_parser(
        "verify",
        help="Verify a BagIt bag whose contents already live at an S3 prefix.",
    )
    p_verify.add_argument(
        "bag_url",
        help="Bag root prefix URL, e.g. s3://my-bucket/extracted/bag/",
    )

    p_verify_against = sub.add_parser(
        "verify-against",
        help=(
            "Check the files under an S3 prefix against the manifests "
            "inside a serialized bag, without extracting the bag."
        ),
        description=(
            "Stream a serialized bag (tar/tar.gz/tar.bz2/tar.xz/tar.zst/zip/7z) "
            "once to read its manifest(s), then stream-hash each file under "
            "the target prefix and compare. The target is treated as "
            "flat — manifest entries 'data/<rel>' map to "
            "'<target_prefix>/<rel>'. To check an already-extracted bag "
            "(layout includes data/), use `verify` instead."
        ),
    )
    p_verify_against.add_argument(
        "archive_url",
        help="Source bag archive URL, e.g. s3://my-bucket/bags/bag.tar.gz",
    )
    p_verify_against.add_argument(
        "target_url",
        help="Target prefix URL whose files should match the bag's payload, e.g. s3://my-bucket/source-dir/",
    )

    p_create = sub.add_parser(
        "create-bag",
        help=(
            "Stream the objects under an S3 prefix into a BagIt .tar.gz at a destination S3 key."
        ),
        description=(
            "Stream the objects under an S3 prefix into a BagIt .tar.gz at "
            "a destination S3 key. Single-pass per payload object: each "
            "object is read once, hashed for the manifest and tar'd into "
            "the archive simultaneously. Tag files (bagit.txt, bag-info.txt, "
            "manifest, tagmanifest) are appended to the tar after the "
            "payload — RFC 8493 places no ordering requirement on "
            "serialized bags, so tag-files-trailing is conformant."
        ),
    )
    p_create.add_argument(
        "src_url",
        help="Source prefix URL whose objects become the bag payload, e.g. s3://my-bucket/source-dir/",
    )
    p_create.add_argument(
        "dest_url",
        help="Destination archive URL (must end in .tar.gz or .tgz), e.g. s3://my-bucket/bags/bag.tar.gz",
    )
    p_create.add_argument(
        "--bag-name",
        required=True,
        help=(
            "Top-level directory name inside the archive (becomes the bag "
            "root, e.g. --bag-name my-bag yields my-bag/bagit.txt). Required."
        ),
    )
    p_create.add_argument(
        "--algorithm",
        default="sha256",
        choices=["sha256", "sha512"],
        help="Hash algorithm for manifest-<algo>.txt / tagmanifest-<algo>.txt (default: sha256).",
    )
    p_create.add_argument(
        "--bag-info",
        action="append",
        default=[],
        metavar="LABEL=VALUE",
        help=(
            "Add a label to bag-info.txt; may be repeated. Example: "
            "--bag-info 'Source-Organization=UW Libraries'. A user-supplied "
            "label overrides s3-bagit's default for Bag-Software-Agent, "
            "Bagging-Date, or Payload-Oxum."
        ),
    )

    p_ls = sub.add_parser(
        "ls",
        help="List the contents of an archive in S3 without extracting it.",
        description=(
            "List the contents of an archive in S3 without extracting it. "
            "Reads the archive's own member listing (tar headers / zip local "
            "headers) — it does not consult bag-info.txt or any manifest, so "
            "the output reflects what is actually in the archive (bagit.txt, "
            "manifests, any wrapping top-level directory, and stowaways like "
            "__MACOSX/ or .DS_Store)."
        ),
    )
    p_ls.add_argument(
        "archive_url",
        help="Source archive URL, e.g. s3://my-bucket/incoming/bag.tar.gz",
    )

    p_config = sub.add_parser(
        "config",
        help=(
            "Interactively write an s3cmd-INI credentials file "
            "(~/.s3cfg by default; ~/.s3cfg-<name> with --profile)."
        ),
    )
    p_config.add_argument(
        "--profile",
        default="default",
        type=_argparse_profile,
        help=(
            "Profile name to configure. Default profile writes ~/.s3cfg; "
            "any other name writes ~/.s3cfg-<name>. Must match [A-Za-z0-9_-]+."
        ),
    )

    p_issue = sub.add_parser(
        "issue",
        help="Open a pre-filled GitHub new-issue page in your browser.",
    )
    p_issue.add_argument(
        "brief",
        nargs="?",
        default=None,
        help='Optional one-line summary, e.g. "extract hangs on big .tar.xz".',
    )

    return parser


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
            extract(
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

    if args.dry_run or args.no_verify:
        return _EXIT_OK

    log.info("Verifying extracted bag…")
    # Verify reads from where the bag now lives — that's the destination.
    with _progress_bar(desc="Verifying") as bar:
        result = verify_bag(
            dst_client, dst.bucket, dst.key, on_progress=_make_per_file_progress_cb(bar)
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


def main(argv: list[str] | None = None) -> int:
    # Load .env from CWD if present — operators frequently invoke from
    # the repo directory; CI / Docker should rely on the real environment.
    env_path = Path(".env")
    if env_path.exists():
        load_dotenv(env_path)

    parser = _build_parser()
    args = parser.parse_args(argv)
    setup_console(logging.DEBUG if args.verbose else logging.INFO)

    try:
        # `config` and `issue` don't need (and shouldn't require) S3 creds.
        if args.command == "config":
            return _cmd_config(args)
        if args.command == "issue":
            return _cmd_issue(args)
        # The rest each build their own client(s) via `client_for(profile)`
        # inside the dispatcher so a missing profile fails before any
        # stream opens.
        if args.command == "extract":
            return _cmd_extract(args)
        if args.command == "verify":
            return _cmd_verify(args)
        if args.command == "verify-against":
            return _cmd_verify_against(args)
        if args.command == "create-bag":
            return _cmd_create_bag(args)
        if args.command == "ls":
            return _cmd_ls(args)
    except KeyboardInterrupt:
        # Operator hit Ctrl-C — exit cleanly instead of dumping a traceback.
        print("\nCancelled.", file=sys.stderr)
        return _EXIT_INTERRUPTED
    except ConfigError as exc:
        # `s3_bagit.exceptions.ConfigError` is now an alias for
        # `s3_archive.exceptions.ConfigError`, so this single-catch
        # covers both s3-bagit and s3-archive raise sites.
        print(f"Configuration error: {exc}", file=sys.stderr)
        print(_ISSUE_HINT, file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    except UnsupportedArchiveFormatError as exc:
        # s3-archive raises this instead of ConfigError for unknown
        # archive extensions; map to the same operator-facing exit code
        # and hint so existing CLI behavior is preserved verbatim.
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

    # Unreachable; argparse already enforced a subcommand.
    parser.error("no subcommand")
    return _EXIT_CONFIG_ERROR


if __name__ == "__main__":
    sys.exit(main())
