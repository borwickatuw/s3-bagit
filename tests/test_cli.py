"""End-to-end CLI smoke tests (with a patched S3 client + moto)."""

from unittest.mock import patch

import pytest

from s3_bagit.cli import main

from .conftest import build_tar_gz, build_zip, make_bag_files, upload_bag_to_prefix


@pytest.fixture
def patched_client(s3_client):
    """Make ``load_client()`` (called from the CLI) return the moto client."""
    with patch("s3_bagit.cli.load_client", return_value=s3_client):
        yield s3_client


def _put_archive(s3, bucket, key, body):
    s3.put_object(Bucket=bucket, Key=key, Body=body)


class TestExtractThenVerify:
    def test_extract_with_default_verify_passes_for_valid_bag(self, patched_client, capsys):
        files = make_bag_files({"a.txt": b"alpha\n", "b.txt": b"beta\n"})
        _put_archive(patched_client, "src-bucket", "in/bag.tar.gz", build_tar_gz(files))

        rc = main(
            [
                "extract",
                "s3://src-bucket/in/bag.tar.gz",
                "s3://dest-bucket/out/",
            ]
        )

        assert rc == 0
        captured = capsys.readouterr()
        assert "RESULT: VALID" in captured.out

    def test_extract_with_no_verify_skips_check(self, patched_client, capsys):
        files = make_bag_files({"a.txt": b"alpha\n"})
        _put_archive(patched_client, "src-bucket", "in/bag.zip", build_zip(files))

        rc = main(
            [
                "extract",
                "--no-verify",
                "s3://src-bucket/in/bag.zip",
                "s3://dest-bucket/out/",
            ]
        )

        assert rc == 0
        captured = capsys.readouterr()
        assert "RESULT:" not in captured.out

    def test_extract_then_verify_fails_when_archive_lacks_bagit_txt(self, patched_client, capsys):
        # An archive that isn't a bag at all.
        archive = build_tar_gz({"random.txt": b"not a bag"})
        _put_archive(patched_client, "src-bucket", "in/bag.tar.gz", archive)

        rc = main(["extract", "s3://src-bucket/in/bag.tar.gz", "s3://dest-bucket/out/"])

        assert rc == 1
        captured = capsys.readouterr()
        assert "RESULT: INVALID" in captured.out


class TestVerifyCommand:
    def test_verify_valid_bag(self, patched_client, capsys):
        files = make_bag_files({"a.txt": b"alpha\n"})
        upload_bag_to_prefix(patched_client, "dest-bucket", "bag/", files)

        rc = main(["verify", "s3://dest-bucket/bag/"])
        assert rc == 0
        assert "RESULT: VALID" in capsys.readouterr().out

    def test_verify_invalid_bag(self, patched_client, capsys):
        rc = main(["verify", "s3://dest-bucket/nothing-here/"])
        assert rc == 1
        assert "RESULT: INVALID" in capsys.readouterr().out


class TestConfigErrors:
    def test_missing_creds_exits_2(self, capsys, monkeypatch, tmp_path):
        # No patched_client — let load_client() run for real. Clear every
        # credential source AND point HOME at an empty dir, then stub
        # boto3.Session so the test doesn't accidentally succeed via
        # ~/.aws/credentials, an EC2 instance role, or AWS SSO.
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
        with patch("s3_bagit.s3_client.boto3.Session") as mock_session:
            mock_session.return_value.get_credentials.return_value = None
            rc = main(["verify", "s3://x/"])
        assert rc == 2
        assert "No S3 credentials configured" in capsys.readouterr().err

    def test_bad_archive_url_exits_2(self, patched_client, capsys):
        rc = main(["extract", "s3://src-bucket/file.rar", "s3://dest-bucket/out/"])
        assert rc == 2
        assert "Cannot detect archive format" in capsys.readouterr().err


class TestVerifyGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "s3://bucket/bags/foo.tar.gz",
            "s3://bucket/bags/foo.tgz",
            "s3://bucket/bags/foo.zip",
            "s3://bucket/bags/foo.7z",
            "s3://bucket/bags/FOO.TAR.GZ",
            "s3://bucket/bags/foo.zip/",  # trailing slash still detected
        ],
    )
    def test_archive_url_for_verify_exits_2_with_extract_hint(self, patched_client, capsys, url):
        rc = main(["verify", url])
        captured = capsys.readouterr()
        assert rc == 2, captured.err
        assert "RESULT:" not in captured.out  # No verify report printed.
        assert "looks like an archive file" in captured.err
        assert "s3-bagit extract" in captured.err

    def test_prefix_url_is_not_guarded(self, patched_client, capsys):
        # A normal prefix (no archive suffix) is allowed through to verify_bag.
        # The bucket is empty so verify will fail, but with the *correct* error.
        rc = main(["verify", "s3://dest-bucket/some-bag/"])
        assert rc == 1  # Verify failed, not config error.
        assert "RESULT: INVALID" in capsys.readouterr().out
