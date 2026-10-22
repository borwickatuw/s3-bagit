"""argparse construction for the ``s3-bagit`` CLI.

Holds the whole parser-building layer — the ``--profile`` ``type=``
callable, one ``_add_*_parser`` helper per subcommand, and the
``_build_parser`` entry point that assembles them. ``cli.py`` imports
``_build_parser`` and owns everything downstream of parsing: the
command handlers, the progress/report presentation, and the
exception-to-exit-code translation.

Splitting here follows the seam argparse already draws. Declaring a flag
and handling it are different jobs that change for different reasons —
a new ``--flag`` touches only this module, while a behavior change
touches only ``cli.py`` — and keeping them in one file is what drove
that file's growth.

**Every name here is ``_``-prefixed, and that is a design constraint,
not a style choice** — the same rule ``_bagit_checks.py`` carries (see
``docs/PYSMELLY.md``, Phase 68g). pysmelly's ``build_function_index``
skips private names, so the checks that key on public shape cannot fire
on helpers whose shape is dictated by argparse rather than by us. Any
member added here inherits the underscore.
"""

# argparse is deliberate: the paired repo s3-archive also builds its CLI
# on argparse and the two ship together, so cross-repo consistency
# outranks a per-repo preference for click/typer (docs/DECISIONS.md 006).
# re-evaluate-by: 2026-11
# The directive sits here, not in `cli.py`: `stdlib-alternatives` lists
# both files and suppresses only at its first-listed (anchor) location,
# which this split moved to this module.
# pysmelly: ignore[stdlib-alternatives]  # noqa: ERA001
import argparse

from s3_archive.config_cmd import validate_profile_name as _validate_profile_name

from s3_bagit import REPO_URL, __version__
from s3_bagit.exceptions import ConfigError


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


# argparse publishes no name for the object `add_subparsers()` returns;
# the private class is the only handle on it. Aliased once here so the
# seven `_add_*_parser` helpers below share one annotation.
_SubParsers = argparse._SubParsersAction


def _add_extract_parser(sub: _SubParsers) -> None:
    p = sub.add_parser(
        "extract",
        help=(
            "Extract a BagIt archive (tar/tar.gz/tar.bz2/tar.xz/tar.zst/zip/7z) "
            "in S3 to a destination prefix."
        ),
    )
    p.add_argument(
        "archive_url",
        help="Source archive URL, e.g. s3://my-bucket/incoming/bag.tar.gz",
    )
    p.add_argument(
        "dest_url",
        help="Destination prefix URL, e.g. s3://my-bucket/extracted/bag/",
    )
    p.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the post-extract bag verification.",
    )
    p.add_argument(
        "--strip-top-level",
        action="store_true",
        help=(
            "Require the archive to contain exactly one top-level directory "
            "and strip it after extracting, so the bag lands directly at the "
            "destination prefix. Errors out if the archive has a file at root "
            "or more than one top-level directory. Without this flag, a single "
            "wrapping directory is left in place and verify follows it."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List members that would be written without uploading anything.",
    )


def _add_verify_parser(sub: _SubParsers) -> None:
    p = sub.add_parser(
        "verify",
        help="Verify a BagIt bag whose contents already live at an S3 prefix.",
    )
    p.add_argument(
        "bag_url",
        help="Bag root prefix URL, e.g. s3://my-bucket/extracted/bag/",
    )


def _add_verify_against_parser(sub: _SubParsers) -> None:
    p = sub.add_parser(
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
    p.add_argument(
        "archive_url",
        help="Source bag archive URL, e.g. s3://my-bucket/bags/bag.tar.gz",
    )
    p.add_argument(
        "target_url",
        help=(
            "Target prefix URL whose files should match the bag's payload, "
            "e.g. s3://my-bucket/source-dir/"
        ),
    )


def _add_create_bag_parser(sub: _SubParsers) -> None:
    p = sub.add_parser(
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
    p.add_argument(
        "src_url",
        help=(
            "Source prefix URL whose objects become the bag payload, "
            "e.g. s3://my-bucket/source-dir/"
        ),
    )
    p.add_argument(
        "dest_url",
        help=(
            "Destination archive URL (must end in .tar.gz or .tgz), "
            "e.g. s3://my-bucket/bags/bag.tar.gz"
        ),
    )
    p.add_argument(
        "--bag-name",
        required=True,
        help=(
            "Top-level directory name inside the archive (becomes the bag "
            "root, e.g. --bag-name my-bag yields my-bag/bagit.txt). Required."
        ),
    )
    p.add_argument(
        "--algorithm",
        default="sha256",
        choices=["sha256", "sha512"],
        help="Hash algorithm for manifest-<algo>.txt / tagmanifest-<algo>.txt (default: sha256).",
    )
    p.add_argument(
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


def _add_ls_parser(sub: _SubParsers) -> None:
    p = sub.add_parser(
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
    p.add_argument(
        "archive_url",
        help="Source archive URL, e.g. s3://my-bucket/incoming/bag.tar.gz",
    )


def _add_config_parser(sub: _SubParsers) -> None:
    p = sub.add_parser(
        "config",
        help=(
            "Interactively write an s3cmd-INI credentials file "
            "(~/.s3cfg by default; ~/.s3cfg-<name> with --profile)."
        ),
    )
    p.add_argument(
        "--profile",
        default="default",
        type=_argparse_profile,
        help=(
            "Profile name to configure. Default profile writes ~/.s3cfg; "
            "any other name writes ~/.s3cfg-<name>. Must match [A-Za-z0-9_-]+."
        ),
    )


def _add_issue_parser(sub: _SubParsers) -> None:
    p = sub.add_parser(
        "issue",
        help="Open a pre-filled GitHub new-issue page in your browser.",
    )
    p.add_argument(
        "brief",
        nargs="?",
        default=None,
        help='Optional one-line summary, e.g. "extract hangs on big .tar.xz".',
    )


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

    _add_extract_parser(sub)
    _add_verify_parser(sub)
    _add_verify_against_parser(sub)
    _add_create_bag_parser(sub)
    _add_ls_parser(sub)
    _add_config_parser(sub)
    _add_issue_parser(sub)
    return parser
