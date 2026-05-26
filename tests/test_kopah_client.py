"""Tests for kopah_bagit.kopah_client credential resolution."""

import textwrap

import pytest

from kopah_bagit.exceptions import ConfigError
from kopah_bagit.kopah_client import _resolve_credentials


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Clear env vars AND point HOME at an empty dir.

    Otherwise the developer's own ~/.s3cfg would silently make every
    "no credentials configured" test pass for the wrong reason.
    """
    for k in ("S3CMD_CONFIG", "KOPAH_ACCESS_KEY", "KOPAH_SECRET_KEY", "KOPAH_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))


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


def test_default_s3cfg_used_when_env_unset(tmp_path):
    """If ~/.s3cfg exists and $S3CMD_CONFIG is unset, the default is picked up."""
    cfg = tmp_path / ".s3cfg"  # HOME is monkeypatched to tmp_path above.
    cfg.write_text(
        textwrap.dedent(
            """\
            [default]
            access_key = HOME_AK
            secret_key = HOME_SK
            host_base = home.kopah.test
            """
        )
    )
    assert _resolve_credentials() == ("HOME_AK", "HOME_SK", "https://home.kopah.test")


def test_env_var_wins_over_default_s3cfg(tmp_path, monkeypatch):
    """An explicit $S3CMD_CONFIG overrides ~/.s3cfg (s3cmd's own semantics)."""
    home_cfg = tmp_path / ".s3cfg"
    home_cfg.write_text(
        textwrap.dedent(
            """\
            [default]
            access_key = HOME
            secret_key = HOME
            host_base = home.test
            """
        )
    )
    explicit = tmp_path / "explicit.s3cfg"
    explicit.write_text(
        textwrap.dedent(
            """\
            [default]
            access_key = EXPLICIT
            secret_key = EXPLICIT
            host_base = explicit.test
            """
        )
    )
    monkeypatch.setenv("S3CMD_CONFIG", str(explicit))
    access, _secret, endpoint = _resolve_credentials()
    assert access == "EXPLICIT"
    assert endpoint == "https://explicit.test"


def test_default_s3cfg_wins_over_direct_env(tmp_path, monkeypatch):
    """When ~/.s3cfg is present, it beats the KOPAH_* fallback."""
    cfg = tmp_path / ".s3cfg"
    cfg.write_text(
        textwrap.dedent(
            """\
            [default]
            access_key = HOME
            secret_key = HOME
            host_base = home.test
            """
        )
    )
    monkeypatch.setenv("KOPAH_ACCESS_KEY", "FROM_ENV")
    monkeypatch.setenv("KOPAH_SECRET_KEY", "FROM_ENV")
    monkeypatch.setenv("KOPAH_ENDPOINT", "https://env.test")
    access, _secret, endpoint = _resolve_credentials()
    assert access == "HOME"
    assert endpoint == "https://home.test"


def test_error_message_lists_default_s3cfg_path(tmp_path):
    """The 'no credentials' error names the ~/.s3cfg path it actually checked."""
    with pytest.raises(ConfigError) as exc_info:
        _resolve_credentials()
    # tmp_path is the monkeypatched HOME, so default path should be tmp_path/.s3cfg.
    assert str(tmp_path / ".s3cfg") in str(exc_info.value)
