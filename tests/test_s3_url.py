"""Tests for s3_bagit.s3_url."""

import pytest

from s3_bagit.exceptions import ConfigError
from s3_bagit.s3_url import detect_format, parse_s3_prefix, parse_s3_url


class TestParseS3Url:
    def test_basic(self):
        assert parse_s3_url("s3://my-bucket/path/file.tar.gz") == (
            "my-bucket",
            "path/file.tar.gz",
        )

    def test_bucket_only(self):
        assert parse_s3_url("s3://my-bucket") == ("my-bucket", "")

    def test_bucket_with_trailing_slash(self):
        assert parse_s3_url("s3://my-bucket/") == ("my-bucket", "")

    def test_rejects_missing_scheme(self):
        with pytest.raises(ConfigError, match="must start with s3"):
            parse_s3_url("my-bucket/path")

    def test_rejects_empty_bucket(self):
        with pytest.raises(ConfigError, match="empty bucket"):
            parse_s3_url("s3:///path")


class TestParseS3Prefix:
    def test_appends_slash(self):
        assert parse_s3_prefix("s3://b/path") == ("b", "path/")

    def test_preserves_slash(self):
        assert parse_s3_prefix("s3://b/path/") == ("b", "path/")

    def test_empty_prefix(self):
        assert parse_s3_prefix("s3://b") == ("b", "")


class TestDetectFormat:
    @pytest.mark.parametrize("url", ["s3://b/x.tar.gz", "s3://b/X.TAR.GZ", "s3://b/x.tgz"])
    def test_tar_gz(self, url):
        assert detect_format(url) == "tar.gz"

    @pytest.mark.parametrize("url", ["s3://b/x.zip", "s3://b/X.ZIP"])
    def test_zip(self, url):
        assert detect_format(url) == "zip"

    def test_rejects_7z_with_helpful_message(self):
        with pytest.raises(ConfigError, match="7z is not supported"):
            detect_format("s3://b/x.7z")

    def test_rejects_unknown(self):
        with pytest.raises(ConfigError, match="Cannot detect archive format"):
            detect_format("s3://b/x.rar")
