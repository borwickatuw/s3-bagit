"""``s3-bagit`` command-line entry point.

Subcommands:

  * ``extract`` — stream a BagIt archive (tar / tar.gz / tar.bz2 /
    tar.xz / zip) out of S3 and upload each member to a destination S3
    prefix. By default also runs ``verify`` against the destination
    prefix; pass ``--no-verify`` to skip.

  * ``verify`` — verify an already-extracted bag at an S3 prefix.

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

from dotenv import load_dotenv

from s3_bagit import __version__
from s3_bagit.config_cmd import run_config
from s3_bagit.exceptions import BagError, ConfigError
from s3_bagit.extract import extract
from s3_bagit.issue import open_issue
from s3_bagit.log_config import get_logger, setup_console
from s3_bagit.ls import list_archive
from s3_bagit.s3_client import load_client
from s3_bagit.s3_url import detect_format, parse_s3_prefix, parse_s3_url
from s3_bagit.verify import BagVerifyResult, verify_bag

log = get_logger(__name__)

# Exit codes.
_EXIT_OK = 0
_EXIT_VERIFY_FAILED = 1
_EXIT_CONFIG_ERROR = 2
# 128 + SIGINT(2) — the conventional POSIX exit code for "killed by Ctrl-C".
_EXIT_INTERRUPTED = 130

_ISSUE_HINT = "For help, run: s3-bagit issue"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="s3-bagit",
        description=(
            "BagIt extract and verify operations against any S3-compatible storage. "
            "All operations stream end-to-end — nothing is staged on local disk."
        ),
    )
    parser.add_argument("--version", action="version", version=f"s3-bagit {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show per-file progress.")

    sub = parser.add_subparsers(dest="command", required=True)

    p_extract = sub.add_parser(
        "extract",
        help=(
            "Extract a BagIt archive (tar/tar.gz/tar.bz2/tar.xz/zip) in S3 to a destination prefix."
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

    sub.add_parser(
        "config",
        help="Interactively write an s3cmd-INI credentials file (~/.s3cfg by default).",
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


def _cmd_extract(args: argparse.Namespace, client) -> int:
    archive_bucket, archive_key = parse_s3_url(args.archive_url)
    if not archive_key:
        raise ConfigError(f"Archive URL needs a key: {args.archive_url!r}")
    dest_bucket, dest_prefix = parse_s3_prefix(args.dest_url)
    fmt = detect_format(args.archive_url)

    extract(
        client,
        archive_bucket,
        archive_key,
        dest_bucket,
        dest_prefix,
        fmt,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    if args.dry_run or args.no_verify:
        return _EXIT_OK

    log.info("Verifying extracted bag…")
    result = verify_bag(client, dest_bucket, dest_prefix)
    _print_verify_report(result)
    return _EXIT_OK if result.ok else _EXIT_VERIFY_FAILED


_ARCHIVE_SUFFIXES = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".zip",
    ".7z",
)


def _guard_against_archive_url(bag_url: str) -> None:
    """Reject `verify <archive>` with a message pointing at `extract` instead.

    A common operator mistake is to point ``verify`` at a serialized
    bag (``...bag.tar.gz``) rather than an extracted-bag prefix. Without
    this guard the symptom is ``No objects found`` followed by
    ``RESULT: INVALID``, which implies we checked a bag and it failed —
    misleading. Fail fast with a clear ConfigError instead.
    """
    if bag_url.lower().rstrip("/").endswith(_ARCHIVE_SUFFIXES):
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


def _cmd_verify(args: argparse.Namespace, client) -> int:
    _guard_against_archive_url(args.bag_url)
    bucket, prefix = parse_s3_prefix(args.bag_url)
    result = verify_bag(client, bucket, prefix)
    _print_verify_report(result)
    return _EXIT_OK if result.ok else _EXIT_VERIFY_FAILED


def _cmd_ls(args: argparse.Namespace, client) -> int:
    archive_bucket, archive_key = parse_s3_url(args.archive_url)
    if not archive_key:
        raise ConfigError(f"Archive URL needs a key: {args.archive_url!r}")
    fmt = detect_format(args.archive_url)
    list_archive(client, archive_bucket, archive_key, fmt)
    return _EXIT_OK


def _cmd_config(_args: argparse.Namespace) -> int:
    return run_config()


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

        client = load_client()
        if args.command == "extract":
            return _cmd_extract(args, client)
        if args.command == "verify":
            return _cmd_verify(args, client)
        if args.command == "ls":
            return _cmd_ls(args, client)
    except KeyboardInterrupt:
        # Operator hit Ctrl-C — exit cleanly instead of dumping a traceback.
        print("\nCancelled.", file=sys.stderr)
        return _EXIT_INTERRUPTED
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print(_ISSUE_HINT, file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    except BagError as exc:
        print(f"Bag error: {exc}", file=sys.stderr)
        print(_ISSUE_HINT, file=sys.stderr)
        return _EXIT_VERIFY_FAILED

    # Unreachable; argparse already enforced a subcommand.
    parser.error("no subcommand")
    return _EXIT_CONFIG_ERROR


if __name__ == "__main__":
    sys.exit(main())
