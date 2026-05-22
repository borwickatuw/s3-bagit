"""Tests for kopah_bagit.kopah_client credential resolution."""

import textwrap

import pytest

from kopah_bagit.exceptions import ConfigError
from kopah_bagit.kopah_client import _resolve_credentials


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in ("S3CMD_CONFIG", "KOPAH_ACCESS_KEY", "KOPAH_SECRET_KEY", "KOPAH_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)


def test_no_creds_set_raises(monkeypatch):
    with pytest.raises(ConfigError, match="No Kopah credentials"):
        _resolve_credentials()


def test_direct_env_vars(monkeypatch):
    monkeypatch.setenv("KOPAH_ACCESS_KEY", "a")
    monkeypatch.setenv("KOPAH_SECRET_KEY", "s")
    monkeypatch.setenv("KOPAH_ENDPOINT", "https://kopah.test")
    assert _resolve_credentials() == ("a", "s", "https://kopah.test")


def test_direct_env_partial_raises(monkeypatch):
    monkeypatch.setenv("KOPAH_ACCESS_KEY", "a")
    # Missing secret + endpoint.
    with pytest.raises(ConfigError, match="incomplete"):
        _resolve_credentials()


def test_s3cmd_config(tmp_path, monkeypatch):
    cfg = tmp_path / ".s3cfg"
    cfg.write_text(
        textwrap.dedent(
            """\
            [default]
            access_key = AK
            secret_key = SK
            host_base = s3.kopah.test
            """
        )
    )
    monkeypatch.setenv("S3CMD_CONFIG", str(cfg))
    assert _resolve_credentials() == ("AK", "SK", "https://s3.kopah.test")


def test_s3cmd_config_wins_over_direct(tmp_path, monkeypatch):
    """If both are set, S3CMD_CONFIG takes precedence (documented in .env.example)."""
    cfg = tmp_path / ".s3cfg"
    cfg.write_text(
        textwrap.dedent(
            """\
            [default]
            access_key = FROM_CFG
            secret_key = FROM_CFG
            host_base = cfg.test
            """
        )
    )
    monkeypatch.setenv("S3CMD_CONFIG", str(cfg))
    monkeypatch.setenv("KOPAH_ACCESS_KEY", "FROM_ENV")
    monkeypatch.setenv("KOPAH_SECRET_KEY", "FROM_ENV")
    monkeypatch.setenv("KOPAH_ENDPOINT", "https://env.test")
    access, _secret, endpoint = _resolve_credentials()
    assert access == "FROM_CFG"
    assert endpoint == "https://cfg.test"


def test_s3cmd_missing_file_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("S3CMD_CONFIG", str(tmp_path / "nope"))
    with pytest.raises(ConfigError, match="does not exist"):
        _resolve_credentials()


def test_s3cmd_missing_section_raises(monkeypatch, tmp_path):
    cfg = tmp_path / ".s3cfg"
    cfg.write_text("[other]\naccess_key=x\n")
    monkeypatch.setenv("S3CMD_CONFIG", str(cfg))
    with pytest.raises(ConfigError, match="missing \\[default\\] section"):
        _resolve_credentials()


def test_s3cmd_missing_key_raises(monkeypatch, tmp_path):
    cfg = tmp_path / ".s3cfg"
    cfg.write_text("[default]\naccess_key=x\nsecret_key=y\n")  # no host_base
    monkeypatch.setenv("S3CMD_CONFIG", str(cfg))
    with pytest.raises(ConfigError, match="host_base"):
        _resolve_credentials()
