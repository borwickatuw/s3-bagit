"""Tests for the interactive `s3-bagit config` subcommand."""

import configparser

import pytest

from s3_bagit import config_cmd


@pytest.fixture
def cfg_path(tmp_path):
    return tmp_path / ".s3cfg"


def _drive_prompts(monkeypatch, answers: list[str], passwords: list[str]) -> None:
    """Wire ``input`` and ``getpass`` to consume scripted answers in order."""
    answer_iter = iter(answers)
    password_iter = iter(passwords)
    # Some tests don't supply enough — fail loudly if a prompt overruns.
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answer_iter))
    monkeypatch.setattr(config_cmd.getpass, "getpass", lambda prompt="": next(password_iter))


def _disable_smoke(monkeypatch) -> None:
    """The smoke test reaches into boto3; bypass it for unit tests."""
    monkeypatch.setattr(config_cmd, "_smoke_test", lambda _path: None)


class TestRunConfigHappyPath:
    def test_writes_full_s3cmd_ini_with_endpoint(self, monkeypatch, cfg_path, capsys):
        _drive_prompts(
            monkeypatch,
            answers=[
                "https://s3.kopah.uw.edu",  # endpoint
                "ACCESS123",  # access key
                str(cfg_path),  # config path
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
            answers=[
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
            answers=[
                "https://s3.kopah.uw.edu",
                "ACCESS",
                str(cfg_path),
                "n",  # declines the overwrite prompt
            ],
            passwords=["SECRET"],
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
            answers=[
                "https://s3.kopah.uw.edu",
                "ACCESS",
                str(cfg_path),
                "y",  # accepts overwrite
            ],
            passwords=["SECRET"],
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


class TestReprompts:
    def test_blank_access_key_reprompts(self, monkeypatch, cfg_path):
        _drive_prompts(
            monkeypatch,
            answers=[
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
