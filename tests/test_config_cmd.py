"""Tests for the interactive `s3-bagit config` subcommand.

The production prompts go through `questionary`, which talks directly to
the controlling TTY. Tests patch the thin `_ask_text` / `_ask_password`
/ `_ask_confirm` helpers in `config_cmd` so we can script answers and
make assertions on the prompt text without spinning up a fake TTY.
"""

import configparser

import pytest

from s3_bagit import config_cmd


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    # Point HOME at tmp_path so the early-detect check for an existing
    # `~/.s3cfg` never sees the developer's real config. We deliberately
    # pick a *non*-`.s3cfg` filename so writing to `cfg_path` doesn't
    # collide with the default-path early-detect check; tests that
    # exercise the early-detect prompt explicitly create `tmp_path / ".s3cfg"`.
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / "custom-s3cfg"


def _drive_prompts(
    monkeypatch,
    *,
    text: list[str] = (),
    passwords: list[str] = (),
    confirms: list[bool] = (),
) -> None:
    """Patch the three `_ask_*` helpers to consume scripted answers.

    Each fake echoes the question to stdout the way a real questionary
    prompt would render it, so capsys-based assertions on the prompt
    text (e.g. "already exists") continue to work.
    """
    text_iter = iter(text)
    password_iter = iter(passwords)
    confirm_iter = iter(confirms)

    def fake_text(question: str, default: str = "") -> str:
        print(question)
        return next(text_iter)

    def fake_password(question: str) -> str:
        print(question)
        return next(password_iter)

    def fake_confirm(question: str, *, default: bool = False) -> bool:
        print(question)
        return next(confirm_iter)

    monkeypatch.setattr(config_cmd, "_ask_text", fake_text)
    monkeypatch.setattr(config_cmd, "_ask_password", fake_password)
    monkeypatch.setattr(config_cmd, "_ask_confirm", fake_confirm)


def _disable_smoke(monkeypatch) -> None:
    """The smoke test reaches into boto3; bypass it for unit tests."""
    monkeypatch.setattr(config_cmd, "_smoke_test", lambda _path: None)


class TestRunConfigHappyPath:
    def test_writes_full_s3cmd_ini_with_endpoint(self, monkeypatch, cfg_path, capsys):
        _drive_prompts(
            monkeypatch,
            text=[
                "https://s3.kopah.uw.edu",
                "ACCESS123",
                str(cfg_path),
            ],
            passwords=["SECRET456"],
        )
        _disable_smoke(monkeypatch)

        rc = config_cmd.run_config()

        assert rc == 0
        assert cfg_path.exists()
        parser = configparser.ConfigParser()
        parser.read(cfg_path)
        assert parser["default"]["access_key"] == "ACCESS123"
        assert parser["default"]["secret_key"] == "SECRET456"
        assert parser["default"]["host_base"] == "s3.kopah.uw.edu"

    def test_blank_endpoint_omits_host_base(self, monkeypatch, cfg_path, capsys):
        _drive_prompts(
            monkeypatch,
            text=[
                "",  # endpoint left blank → AWS S3
                "ACCESS123",
                str(cfg_path),
            ],
            passwords=["SECRET456"],
        )
        _disable_smoke(monkeypatch)

        rc = config_cmd.run_config()

        assert rc == 0
        parser = configparser.ConfigParser()
        parser.read(cfg_path)
        assert "host_base" not in parser["default"]
        out = capsys.readouterr().out
        assert "AWS S3 defaults" in out


class TestRunConfigOverwriteGuard:
    def test_existing_file_overwrite_declined(self, monkeypatch, cfg_path, capsys):
        cfg_path.write_text("# original content\n")
        original_mtime = cfg_path.stat().st_mtime

        _drive_prompts(
            monkeypatch,
            text=[
                "https://s3.kopah.uw.edu",
                "ACCESS",
                str(cfg_path),
            ],
            passwords=["SECRET"],
            confirms=[False],  # declines the overwrite prompt
        )
        _disable_smoke(monkeypatch)

        rc = config_cmd.run_config()

        assert rc == 0
        # File untouched.
        assert cfg_path.read_text() == "# original content\n"
        assert cfg_path.stat().st_mtime == original_mtime
        assert "Aborted" in capsys.readouterr().out

    def test_existing_file_overwrite_accepted(self, monkeypatch, cfg_path):
        cfg_path.write_text("# original content\n")
        _drive_prompts(
            monkeypatch,
            text=[
                "https://s3.kopah.uw.edu",
                "ACCESS",
                str(cfg_path),
            ],
            passwords=["SECRET"],
            confirms=[True],  # accepts overwrite
        )
        _disable_smoke(monkeypatch)

        rc = config_cmd.run_config()

        assert rc == 0
        parser = configparser.ConfigParser()
        parser.read(cfg_path)
        assert parser["default"]["access_key"] == "ACCESS"


class TestEndpointToHostBase:
    @pytest.mark.parametrize(
        "endpoint,expected",
        [
            ("https://s3.kopah.uw.edu", "s3.kopah.uw.edu"),
            ("http://localhost:9000", "localhost:9000"),
            ("s3.kopah.uw.edu", "s3.kopah.uw.edu"),
            ("https://s3.kopah.uw.edu/", "s3.kopah.uw.edu"),
        ],
    )
    def test_strips_scheme_and_path(self, endpoint, expected):
        assert config_cmd._endpoint_to_host_base(endpoint) == expected


class TestEarlyDetectExistingConfig:
    """The fast-path: detect an existing ~/.s3cfg before prompting for anything."""

    def test_decline_keeps_existing_file(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        default = tmp_path / ".s3cfg"
        default.write_text(
            "[default]\naccess_key = OLD\nsecret_key = OLD\nhost_base = s3.kopah.uw.edu\n"
        )
        original_mtime = default.stat().st_mtime

        _drive_prompts(monkeypatch, confirms=[False])
        _disable_smoke(monkeypatch)

        rc = config_cmd.run_config()

        assert rc == 0
        assert default.read_text().startswith("[default]")
        assert default.stat().st_mtime == original_mtime
        out = capsys.readouterr().out
        assert "already exists" in out
        assert "s3.kopah.uw.edu" in out  # URL hint is shown.
        assert "Keeping existing configuration" in out

    def test_decline_when_host_base_missing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        default = tmp_path / ".s3cfg"
        # Malformed-but-parseable INI: no [default] section.
        default.write_text("# half-broken file\n")

        _drive_prompts(monkeypatch, confirms=[False])
        _disable_smoke(monkeypatch)

        rc = config_cmd.run_config()

        assert rc == 0
        out = capsys.readouterr().out
        assert "already exists" in out
        # No URL claim made when we couldn't read host_base.
        assert "pointing to" not in out

    def test_accept_continues_to_full_prompts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        default = tmp_path / ".s3cfg"
        default.write_text(
            "[default]\naccess_key = OLD\nsecret_key = OLD\nhost_base = s3.kopah.uw.edu\n"
        )

        _drive_prompts(
            monkeypatch,
            text=[
                "https://s3.new-endpoint.example",
                "NEWACCESS",
                str(default),
            ],
            passwords=["NEWSECRET"],
            confirms=[True, True],  # accept early-detect, then accept overwrite
        )
        _disable_smoke(monkeypatch)

        rc = config_cmd.run_config()

        assert rc == 0
        parser = configparser.ConfigParser()
        parser.read(default)
        assert parser["default"]["access_key"] == "NEWACCESS"
        assert parser["default"]["host_base"] == "s3.new-endpoint.example"


class TestReprompts:
    def test_blank_access_key_reprompts(self, monkeypatch, cfg_path):
        _drive_prompts(
            monkeypatch,
            text=[
                "https://s3.kopah.uw.edu",
                "",  # first access-key attempt: blank, reprompts
                "ACCESS",
                str(cfg_path),
            ],
            passwords=["SECRET"],
        )
        _disable_smoke(monkeypatch)

        rc = config_cmd.run_config()
        assert rc == 0
        parser = configparser.ConfigParser()
        parser.read(cfg_path)
        assert parser["default"]["access_key"] == "ACCESS"


class TestCtrlCSurfacesKeyboardInterrupt:
    """questionary returns None on Ctrl-C — the helpers must raise KeyboardInterrupt."""

    def test_text_helper_raises(self, monkeypatch):
        monkeypatch.setattr(
            config_cmd.questionary,
            "text",
            lambda *_a, **_kw: _StubAsk(None),
        )
        with pytest.raises(KeyboardInterrupt):
            config_cmd._ask_text("anything")

    def test_password_helper_raises(self, monkeypatch):
        monkeypatch.setattr(
            config_cmd.questionary,
            "password",
            lambda *_a, **_kw: _StubAsk(None),
        )
        with pytest.raises(KeyboardInterrupt):
            config_cmd._ask_password("anything")

    def test_confirm_helper_raises(self, monkeypatch):
        monkeypatch.setattr(
            config_cmd.questionary,
            "confirm",
            lambda *_a, **_kw: _StubAsk(None),
        )
        with pytest.raises(KeyboardInterrupt):
            config_cmd._ask_confirm("anything")


class _StubAsk:
    """Mimic the `questionary.Question` interface our helpers consume."""

    def __init__(self, value):
        self._value = value

    def ask(self):
        return self._value
