"""Logging setup for s3-bagit.

One module-level logger per file. Console handler is attached lazily by
the CLI entry point so library callers can configure logging themselves.

Verbosity has exactly two inputs, in this order: the ``--verbose`` flag
(an operator asking for detail on this one run) and ``$LOG_LEVEL`` (the
fleet-wide level variable — LOGGING.md, "CLI / batch-runner logging").
Absent both, INFO.
"""

import logging
import os
import sys

from tqdm import tqdm

from s3_bagit.exceptions import ConfigError

_console_initialized = False

_LOG_LEVEL_ENV = "LOG_LEVEL"


class _TqdmLoggingHandler(logging.Handler):
    """Route log records through tqdm.write so they don't clobber an active progress bar."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            tqdm.write(msg, file=sys.stderr)
        except Exception:  # noqa: BLE001 - defensive, matches stdlib StreamHandler
            self.handleError(record)


def resolve_level(*, verbose: bool) -> int:
    """Return the console log level for this run.

    ``--verbose`` is the operator overriding the environment for one
    invocation, so it wins outright. Otherwise ``$LOG_LEVEL`` decides,
    and an unusable value is a ``ConfigError`` rather than a silent
    fall back to INFO: a level that quietly does nothing is worse than
    one that says why.
    """
    if verbose:
        return logging.DEBUG

    raw = os.environ.get(_LOG_LEVEL_ENV)
    if raw is None or not raw.strip():
        return logging.INFO

    level = logging.getLevelName(raw.strip().upper())
    if not isinstance(level, int):
        raise ConfigError(
            f"${_LOG_LEVEL_ENV} must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL (got {raw!r})"
        )
    return level


def setup_console(level: int = logging.INFO) -> None:
    """Attach a console handler at *level*. Idempotent."""
    global _console_initialized  # noqa: PLW0603 - module-level once-flag
    if _console_initialized:
        return
    _console_initialized = True

    handler = _TqdmLoggingHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
