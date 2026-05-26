"""Tests for s3_bagit.s3_client credential resolution.

We don't actually let boto3 make API calls here — these tests only
exercise the resolution-order logic (which path produces which set of
client kwargs / raises which ConfigError).
"""

import textwrap
from unittest.mock import patch

import pytest

from s3_bagit.exceptions import ConfigError
from s3_bagit.s3_client import load_client


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Clear env vars AND point HOME at an empty dir.

    Otherwise the developer's own ~/.s3cfg, ~/.aws/credentials, or AWS_*
    env vars could silently satisfy what should be a 'no credentials'
    test.
    """
    for k in (
        "S3CMD_CONFIG",
        "S3_ENDPOINT_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))


def _write_s3cfg(path, *, access="AK", secret="SK", host="s3.example.test"):
    path.write_text(
        textwrap.dedent(
            f"""\
            [default]
            access_key = {access}
            secret_key = {secret}
            host_base = {host}
            """
        )
    )


def _patched_boto_client():
    """Patch boto3.client so we can inspect kwargs without making API calls."""
    return patch("s3_bagit.s3_client.boto3.client")


class TestExplicitS3cmdConfig:
    def test_uses_explicit_config(self, monkeypatch, tmp_path):
        cfg = tmp_path / "explicit.s3cfg"
        _write_s3cfg(cfg, access="EX", secret="EX_SECRET", host="explicit.test")
        monkeypatch.setenv("S3CMD_CONFIG", str(cfg))
        with _patched_boto_client() as mock:
            load_client()
        kwargs = mock.call_args.kwargs
        assert kwargs["aws_access_key_id"] == "EX"
        assert kwargs["aws_secret_access_key"] == "EX_SECRET"
        assert kwargs["endpoint_url"] == "https://explicit.test"

    def test_missing_file_raises(self, monkeypatch, tmp_path):
        monkeypatch.setenv("S3CMD_CONFIG", str(tmp_path / "nope"))
        with pytest.raises(ConfigError, match="does not exist"):
            load_client()

    def test_missing_section_raises(self, monkeypatch, tmp_path):
        cfg = tmp_path / "explicit.s3cfg"
        cfg.write_text("[other]\naccess_key=x\n")
        monkeypatch.setenv("S3CMD_CONFIG", str(cfg))
        with pytest.raises(ConfigError, match="missing \\[default\\] section"):
            load_client()

    def test_missing_key_raises(self, monkeypatch, tmp_path):
        cfg = tmp_path / "explicit.s3cfg"
        cfg.write_text("[default]\naccess_key=x\nsecret_key=y\n")  # no host_base
        monkeypatch.setenv("S3CMD_CONFIG", str(cfg))
        with pytest.raises(ConfigError, match="host_base"):
            load_client()


class TestDefaultS3cfg:
    def test_picks_up_home_s3cfg(self, tmp_path):
        cfg = tmp_path / ".s3cfg"
        _write_s3cfg(cfg, access="HOME", secret="HOME_SK", host="home.test")
        with _patched_boto_client() as mock:
            load_client()
        kwargs = mock.call_args.kwargs
        assert kwargs["aws_access_key_id"] == "HOME"
        assert kwargs["endpoint_url"] == "https://home.test"

    def test_explicit_beats_default(self, monkeypatch, tmp_path):
        home_cfg = tmp_path / ".s3cfg"
        _write_s3cfg(home_cfg, access="HOME", host="home.test")
        explicit = tmp_path / "explicit.s3cfg"
        _write_s3cfg(explicit, access="EXPLICIT", host="explicit.test")
        monkeypatch.setenv("S3CMD_CONFIG", str(explicit))
        with _patched_boto_client() as mock:
            load_client()
        kwargs = mock.call_args.kwargs
        assert kwargs["aws_access_key_id"] == "EXPLICIT"
        assert kwargs["endpoint_url"] == "https://explicit.test"

    def test_s3cmd_beats_aws_env_vars(self, monkeypatch, tmp_path):
        """When ~/.s3cfg is present, it wins over AWS_* env vars."""
        cfg = tmp_path / ".s3cfg"
        _write_s3cfg(cfg, access="FROM_S3CMD", host="s3cmd.test")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "FROM_AWS")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "FROM_AWS")
        with _patched_boto_client() as mock:
            load_client()
        kwargs = mock.call_args.kwargs
        assert kwargs["aws_access_key_id"] == "FROM_S3CMD"


class TestBoto3DefaultChain:
    def test_aws_env_vars_used_when_no_s3cfg(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AWS_AK")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "AWS_SK")
        with patch("s3_bagit.s3_client.boto3.Session") as mock_session:
            mock_session.return_value.get_credentials.return_value = object()
            load_client()
            mock_session.return_value.client.assert_called_once()
            kwargs = mock_session.return_value.client.call_args.kwargs
            assert kwargs.get("endpoint_url") is None  # AWS default.

    def test_s3_endpoint_url_used(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AWS_AK")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "AWS_SK")
        monkeypatch.setenv("S3_ENDPOINT_URL", "https://minio.example.com")
        with patch("s3_bagit.s3_client.boto3.Session") as mock_session:
            mock_session.return_value.get_credentials.return_value = object()
            load_client()
            kwargs = mock_session.return_value.client.call_args.kwargs
            assert kwargs["endpoint_url"] == "https://minio.example.com"


class TestNoCredentials:
    def test_no_creds_anywhere_raises(self):
        with patch("s3_bagit.s3_client.boto3.Session") as mock_session:
            mock_session.return_value.get_credentials.return_value = None
            with pytest.raises(ConfigError, match="No S3 credentials configured"):
                load_client()

    def test_error_names_default_s3cfg_path(self, tmp_path):
        """The 'no credentials' message names the ~/.s3cfg path it checked."""
        with patch("s3_bagit.s3_client.boto3.Session") as mock_session:
            mock_session.return_value.get_credentials.return_value = None
            with pytest.raises(ConfigError) as exc_info:
                load_client()
        assert str(tmp_path / ".s3cfg") in str(exc_info.value)


class TestBotoConfig:
    def test_ceph_workaround_applied_unconditionally(self, tmp_path):
        """The request_checksum_calculation flag is set on every path."""
        cfg = tmp_path / ".s3cfg"
        _write_s3cfg(cfg)
        with _patched_boto_client() as mock:
            load_client()
        boto_config = mock.call_args.kwargs["config"]
        assert boto_config.request_checksum_calculation == "when_required"
