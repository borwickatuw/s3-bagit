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
from s3_bagit.s3_client import build_client

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


_URL_SCHEMES = {"http", "https"}


def _validate_endpoint(endpoint: str) -> str | None:
    """Return a one-line operator-facing error message, or ``None`` if OK.

    Blank input is allowed (= AWS S3 defaults). For non-blank input we
    require: no whitespace, and if an http/https scheme is given it
    must be followed by a host. We don't try to validate the host itself
    — boto3 will surface real-world failures (DNS, TLS) later, and the
    goal here is to catch the fat-finger cases ("blark dar dar") that
    otherwise produce a confusing
    ``ValueError: Invalid endpoint: https://blark dar dar`` crash from
    inside the smoke test.

    We only flag missing-host when the scheme is in ``_URL_SCHEMES``;
    ``urlparse`` parses bare ``host:port`` strings like ``localhost:9000``
    with ``scheme="localhost"``, which is fine for our purposes (not a
    URL scheme we recognise).
    """
    if not endpoint:
        return None
    if any(c.isspace() for c in endpoint):
        return "Endpoint must not contain spaces."
    parsed = urlparse(endpoint)
    if parsed.scheme in _URL_SCHEMES and not parsed.netloc:
        return f"Endpoint {endpoint!r} has a scheme but no host."
    return None


def _resolve_path(raw: str) -> Path:
    """Expand ``~`` and make absolute, but do **not** follow symlinks.

    Earlier versions used :meth:`Path.resolve` which dereferenced symlinks
    — that made the path s3-bagit displayed (e.g. an Ops-managed config
    target under ``/Users/foo/code/storage-scripts/secrets/x.cfg``)
    unfamiliar to operators who only know they have a ``~/.s3cfg``.
    Showing the path the operator typed is the friendlier default;
    ``Path.open``/``Path.exists`` follow the symlink at read/write time
    anyway, so behaviour is unchanged.
    """
    return Path(os.path.expanduser(raw)).absolute()


def _canonical_config_path() -> Path:
    """Return the path s3-bagit *would* use, mirroring runtime precedence.

    Matches the resolution order in :func:`s3_bagit.s3_client.load_client`:
    ``$S3CMD_CONFIG`` wins over ``~/.s3cfg``. Used for both the early-
    detect prompt and as the default of the "Config file path" prompt.
    """
    explicit = os.environ.get("S3CMD_CONFIG", "").strip()
    if explicit:
        return _resolve_path(explicit)
    return _resolve_path(_DEFAULT_PATH)


def _detect_existing_config() -> Path | None:
    """The canonical config path iff it exists on disk; otherwise ``None``.

    A set-but-missing ``$S3CMD_CONFIG`` returns ``None`` — that's the
    operator declaring "write a new config at this path", and the
    early-detect prompt would be noise.
    """
    canonical = _canonical_config_path()
    return canonical if canonical.exists() else None


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


def _try_connect(*, access_key: str, secret_key: str, host_base: str) -> str | None:
    """Run a ``list_buckets`` against the in-memory credentials.

    Returns ``None`` on success or a one-line error string on failure.
    Used *before* the config is written so a misconfigured set of values
    doesn't leave a broken file behind. boto3 raises ``ValueError`` for
    malformed endpoints; the rest of the tuple covers network and
    credential failures. We must never crash the CLI — this is
    diagnostic, not load-bearing.
    """
    endpoint_url = f"https://{host_base}" if host_base else None
    try:
        client = build_client(
            access_key=access_key,
            secret_key=secret_key,
            endpoint_url=endpoint_url,
        )
        client.list_buckets()
    except (BotoCoreError, ClientError, OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _non_default_path_note(cfg_path: Path) -> None:
    """Tell the operator the implications of choosing a non-default path.

    We deliberately don't generate shell-specific snippets or try to
    write to rc files — that requires guessing the right rc file
    (.bashrc vs .zshrc vs $PROFILE) and risks duplicate entries on
    re-run. Instead we explain the situation and offer the easy escape
    (use the default path).
    """
    print(
        f"Note: {cfg_path} is not the default location (~/.s3cfg). "
        f"Future runs of s3-bagit will only find it if $S3CMD_CONFIG "
        f"is set in your shell environment.\n"
        f"If you want this config picked up automatically (no env-var "
        f"needed), re-run `s3-bagit config` and accept the default "
        f"path `~/.s3cfg`."
    )


def run_config() -> int:
    """Drive the interactive prompts; return the CLI exit code."""
    print("Configure S3 credentials for s3-bagit.")

    existing = _detect_existing_config()
    if existing is not None:
        existing_host = _read_existing_host_base(existing)
        if existing_host:
            question = (
                f"An s3cmd config already exists at {existing} "
                f"pointing to {existing_host}. Replace it?"
            )
        else:
            question = f"An s3cmd config already exists at {existing}. Replace it?"
        if not _ask_confirm(question, default=False):
            print("Keeping existing configuration; no changes written.")
            return 0

    print(
        "These values will be tested with a list-buckets call before anything is written to disk."
    )
    print()
    # Gather inputs and test the connection BEFORE writing anything to
    # disk. A broken endpoint or expired key shouldn't leave a stale
    # config file behind that the operator then has to clean up.
    while True:
        while True:
            endpoint = _ask_text(
                "S3 endpoint URL (e.g. https://s3.kopah.uw.edu; blank for AWS S3)",
            )
            err = _validate_endpoint(endpoint)
            if err is None:
                break
            print(err)

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

        host_base = _endpoint_to_host_base(endpoint) if endpoint else ""

        print("Testing connection...", end=" ", flush=True)
        started = time.monotonic()
        error = _try_connect(access_key=access_key, secret_key=secret_key, host_base=host_base)
        elapsed = time.monotonic() - started
        if error is None:
            print(f"OK ({elapsed:.1f}s).")
            break
        print("FAILED.")
        print(f"  {error}")
        if _ask_confirm("Try again with different credentials?", default=True):
            continue
        # Decline-to-retry doesn't mean "save broken values" — that would
        # be a surprising default. Make the operator opt into saving
        # explicitly, with the safe choice (don't save) as the default.
        # The opt-in path covers the offline-configuration case: no
        # network, behind a firewall, configuring for a future endpoint.
        if not _ask_confirm("Save these values anyway (e.g. configuring offline)?", default=False):
            print("Cancelled; no changes written.")
            return 0
        print("Saving the values anyway.")
        break

    # Prefill the path prompt with $S3CMD_CONFIG if set; that's the
    # operator's declaration of "this is where my config goes."
    path_default = str(_canonical_config_path())
    raw_path = _ask_text("Config file path", default=path_default)
    cfg_path = _resolve_path(raw_path)

    if cfg_path.exists() and not _ask_confirm(
        f"File {cfg_path} already exists. Overwrite?", default=False
    ):
        print("Aborted; no changes written.")
        return 0

    _write_config(cfg_path, access_key=access_key, secret_key=secret_key, host_base=host_base)

    # End-of-run summary: one line confirming what was saved, then any
    # follow-up notes (AWS-defaults hint, non-default-path shell export).
    print(f"Configured. Settings saved to {cfg_path}.")
    if not host_base:
        print(
            "(No endpoint set; using AWS S3 defaults. To target a non-AWS endpoint "
            "later, edit `host_base` in the file or rerun `s3-bagit config`.)"
        )
    if cfg_path != _resolve_path(_DEFAULT_PATH):
        _non_default_path_note(cfg_path)
    return 0
