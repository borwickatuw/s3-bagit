"""Interactive ``s3-bagit config`` subcommand.

Prompts for endpoint, access key, and secret key, then writes an
s3cmd-compatible INI file. The same ``[default]`` section is what
:func:`s3_bagit.s3_client._from_s3cmd_config` already consumes — so
running ``s3-bagit config`` is enough to bootstrap the tool on a fresh
workstation without installing s3cmd.

Uses `questionary <https://github.com/tmbo/questionary>`_ for prompts so
the experience is consistent across terminals (arrow-key Y/N, masked
password entry, clean Ctrl-C cancellation).
"""

import configparser
import contextlib
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import questionary
from botocore.exceptions import BotoCoreError, ClientError

from s3_bagit.log_config import get_logger
from s3_bagit.s3_client import load_client

log = get_logger(__name__)

_DEFAULT_PATH = "~/.s3cfg"


def _ask_text(question: str, default: str = "") -> str:
    """Ask for free-text input. Ctrl-C surfaces as ``KeyboardInterrupt``."""
    answer = questionary.text(question, default=default).ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer.strip()


def _ask_password(question: str) -> str:
    """Ask for a hidden-input secret. Ctrl-C surfaces as ``KeyboardInterrupt``."""
    answer = questionary.password(question).ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer.strip()


def _ask_confirm(question: str, *, default: bool = False) -> bool:
    """Ask a Y/N question. Ctrl-C surfaces as ``KeyboardInterrupt``."""
    answer = questionary.confirm(question, default=default).ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer


def _endpoint_to_host_base(endpoint: str) -> str:
    """Strip scheme/path off *endpoint* — s3cmd's ``host_base`` is host[:port] only."""
    parsed = urlparse(endpoint)
    if parsed.netloc:
        return parsed.netloc
    # User typed bare host like "s3.kopah.uw.edu" — urlparse puts it in path.
    return parsed.path


def _resolve_path(raw: str) -> Path:
    return Path(os.path.expanduser(raw)).resolve()


def _read_existing_host_base(cfg_path: Path) -> str | None:
    """Best-effort: read ``host_base`` from an existing s3cmd INI.

    Returns ``None`` if the file isn't parseable as an s3cmd config or
    doesn't carry a ``host_base`` — both are fine, the caller still
    surfaces "config exists" to the operator.
    """
    try:
        parser = configparser.ConfigParser()
        parser.read(cfg_path, encoding="utf-8")
        return parser["default"].get("host_base") or None
    except (configparser.Error, KeyError, OSError, UnicodeDecodeError):
        return None


def _write_config(path: Path, *, access_key: str, secret_key: str, host_base: str) -> None:
    parser = configparser.ConfigParser()
    parser["default"] = {"access_key": access_key, "secret_key": secret_key}
    if host_base:
        parser["default"]["host_base"] = host_base
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        parser.write(fh)
    # s3cmd writes 0600 — credentials belong to the user. On platforms
    # where chmod is a no-op (Windows), the OSError is harmless.
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def _smoke_test(cfg_path: Path) -> None:
    """Best-effort ``list_buckets`` against the freshly-written config.

    Failures print a one-line note and return — the config file is still
    on disk and the operator can fix it (wrong endpoint, expired key,
    network) without redoing the prompts.
    """
    print("Testing connection...", end=" ", flush=True)
    original = os.environ.get("S3CMD_CONFIG")
    os.environ["S3CMD_CONFIG"] = str(cfg_path)
    try:
        started = time.monotonic()
        client = load_client()
        client.list_buckets()
        elapsed = time.monotonic() - started
        print(f"OK ({elapsed:.1f}s).")
    except (BotoCoreError, ClientError, OSError) as exc:
        print("FAILED.")
        print(f"  {type(exc).__name__}: {exc}")
        print("  The config file was written; fix the issue and re-run.")
    finally:
        if original is None:
            os.environ.pop("S3CMD_CONFIG", None)
        else:
            os.environ["S3CMD_CONFIG"] = original


def _shell_export_hint(cfg_path: Path) -> None:
    """Print platform-appropriate ``S3CMD_CONFIG`` export hints.

    Operators on Windows/PowerShell hit a different syntax than mac/Linux
    operators do, so we print both — the goal is "copy-pasteable", not
    auto-detected.
    """
    print(
        "This file is not at the default location (~/.s3cfg). "
        "To make future runs find it, add this to your shell profile:"
    )
    print(f"  bash/zsh:    export S3CMD_CONFIG={cfg_path}")
    print(f"  PowerShell:  $env:S3CMD_CONFIG = '{cfg_path}'")


def run_config() -> int:
    """Drive the interactive prompts; return the CLI exit code."""
    print("Configure S3 credentials for s3-bagit.")

    default_path = _resolve_path(_DEFAULT_PATH)
    if default_path.exists():
        existing_host = _read_existing_host_base(default_path)
        if existing_host:
            question = (
                f"An s3cmd config already exists at {default_path} "
                f"pointing to {existing_host}. Replace it?"
            )
        else:
            question = f"An s3cmd config already exists at {default_path}. Replace it?"
        if not _ask_confirm(question, default=False):
            print("Keeping existing configuration; no changes written.")
            return 0

    endpoint = _ask_text(
        "S3 endpoint URL (e.g. https://s3.kopah.uw.edu; blank for AWS S3)",
    )

    access_key = ""
    while not access_key:
        access_key = _ask_text("Access key")
        if not access_key:
            print("Access key is required.")
    secret_key = ""
    while not secret_key:
        secret_key = _ask_password("Secret key")
        if not secret_key:
            print("Secret key is required.")

    raw_path = _ask_text("Config file path", default=_DEFAULT_PATH)
    cfg_path = _resolve_path(raw_path)

    if cfg_path.exists() and not _ask_confirm(
        f"File {cfg_path} already exists. Overwrite?", default=False
    ):
        print("Aborted; no changes written.")
        return 0

    host_base = _endpoint_to_host_base(endpoint) if endpoint else ""
    _write_config(cfg_path, access_key=access_key, secret_key=secret_key, host_base=host_base)
    print(f"Wrote {cfg_path}.")

    if not host_base:
        print(
            "No endpoint set; using AWS S3 defaults. To target a non-AWS endpoint "
            "(Kopah, MinIO, DigitalOcean Spaces, …), set `host_base` in the file "
            "or rerun `s3-bagit config`."
        )

    if cfg_path != _resolve_path(_DEFAULT_PATH):
        _shell_export_hint(cfg_path)

    _smoke_test(cfg_path)
    return 0
