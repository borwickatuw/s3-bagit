"""Unit tests for the console logging setup.

``_TqdmLoggingHandler.emit`` used to be covered only by accident: every CLI
test attached the console handler, and botocore's INFO records ran through
it. Once ``conftest._quiet_logging`` stopped that noise the handler had no
test at all, so it gets one here — including the fallback path, which is the
part that only fires when something is already going wrong.
"""

import logging

import pytest

from s3_bagit.exceptions import ConfigError
from s3_bagit.log_config import _TqdmLoggingHandler, resolve_level, setup_console


def _record(msg: str, args: tuple = ()) -> logging.LogRecord:
    return logging.LogRecord(
        name="s3_bagit.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def _console_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if isinstance(h, _TqdmLoggingHandler)]


class TestTqdmLoggingHandler:
    """The handler that keeps log lines from clobbering an active progress bar."""

    def test_emit_writes_the_formatted_record_to_stderr(self, capsys):
        handler = _TqdmLoggingHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))

        handler.emit(_record("uploading manifest"))

        captured = capsys.readouterr()
        assert captured.err.strip() == "uploading manifest"
        # stdout stays clean: it carries the verify report, not log lines.
        assert captured.out == ""

    def test_emit_falls_back_to_handle_error_when_the_record_cannot_format(
        self, capsys, monkeypatch
    ):
        handler = _TqdmLoggingHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        errors: list[logging.LogRecord] = []
        monkeypatch.setattr(handler, "handleError", errors.append)

        # "%d" against a non-numeric arg raises inside record.getMessage().
        bad = _record("%d objects", args=("many",))
        handler.emit(bad)

        assert errors == [bad]
        assert capsys.readouterr().err == ""


class TestSetupConsole:
    """``setup_console`` is called by the CLI entry point and must latch once."""

    def test_attaches_one_handler_at_the_requested_level(self):
        root = logging.getLogger()
        before = len(root.handlers)

        setup_console(logging.DEBUG)

        handlers = _console_handlers()
        assert len(handlers) == 1
        assert handlers[0].level == logging.DEBUG
        assert root.level == logging.DEBUG
        assert len(root.handlers) == before + 1

    def test_second_call_is_a_no_op(self):
        setup_console(logging.INFO)
        setup_console(logging.DEBUG)

        handlers = _console_handlers()
        assert len(handlers) == 1
        # The first call's level survives: the second returns before touching
        # anything, so `--verbose` after a plain run does not re-level.
        assert handlers[0].level == logging.INFO
        assert logging.getLogger().level == logging.INFO


class TestResolveLevel:
    """``--verbose`` beats ``$LOG_LEVEL``; an unusable value is fatal."""

    def test_verbose_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "ERROR")

        assert resolve_level(verbose=True) == logging.DEBUG

    def test_env_var_sets_the_level(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "warning")

        assert resolve_level(verbose=False) == logging.WARNING

    def test_unset_env_var_means_info(self, monkeypatch):
        monkeypatch.delenv("LOG_LEVEL", raising=False)

        assert resolve_level(verbose=False) == logging.INFO

    def test_blank_env_var_means_info(self, monkeypatch):
        # An exported-but-empty LOG_LEVEL is how a shell leaves an unset
        # variable; treating it as a bad value would be hostile.
        monkeypatch.setenv("LOG_LEVEL", "  ")

        assert resolve_level(verbose=False) == logging.INFO

    def test_unknown_level_is_a_config_error(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "CHATTY")

        with pytest.raises(ConfigError, match="LOG_LEVEL"):
            resolve_level(verbose=False)

    def test_numeric_level_is_a_config_error(self, monkeypatch):
        # logging.getLevelName("10") returns the string "Level 10", not an
        # int, so digits fall through to the error path — which is what we
        # want: the documented spelling is the name.
        monkeypatch.setenv("LOG_LEVEL", "10")

        with pytest.raises(ConfigError, match="LOG_LEVEL"):
            resolve_level(verbose=False)
