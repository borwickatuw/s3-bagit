"""End-to-end CLI smoke tests (with a patched S3 client + moto)."""

from unittest.mock import patch

import pytest

from s3_bagit.cli import main
from s3_bagit.create_bag import create_bag

from .conftest import build_7z, build_tar_gz, build_zip, make_bag_files, upload_bag_to_prefix


@pytest.fixture
def patched_client(s3_client):
    """Make every `client_for(profile)` call in the CLI return the moto client.

    Profile-aware tests (and the cross-endpoint acceptance test) override
    this; the default here serves single-endpoint tests where source and
    destination share one client.
    """
    with patch("s3_bagit.cli.client_for", return_value=s3_client):
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

    def test_extract_with_default_verify_passes_for_7z_bag(self, patched_client, capsys):
        files = make_bag_files({"a.txt": b"alpha\n", "b.txt": b"beta\n"})
        _put_archive(patched_client, "src-bucket", "in/bag.7z", build_7z(files))

        rc = main(
            [
                "extract",
                "s3://src-bucket/in/bag.7z",
                "s3://dest-bucket/out/",
            ]
        )

        assert rc == 0
        captured = capsys.readouterr()
        assert "RESULT: VALID" in captured.out


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
        # No patched_client — let the real resolver run via s3-archive.
        # Clear every credential source AND point HOME at an empty dir,
        # then stub boto3.Session so the test doesn't accidentally succeed
        # via ~/.aws/credentials, an EC2 instance role, or AWS SSO.
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
        with patch("s3_archive.s3_client.boto3.Session") as mock_session:
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
            "s3://bucket/bags/foo.tar",
            "s3://bucket/bags/foo.tar.gz",
            "s3://bucket/bags/foo.tgz",
            "s3://bucket/bags/foo.tar.bz2",
            "s3://bucket/bags/foo.tar.xz",
            "s3://bucket/bags/foo.tar.zst",
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


class TestVerifyAgainstCommand:
    def _put_payload_and_bag(self, client, payload: dict[str, bytes]) -> None:
        """Stage matching payload at src/ + a bag.tar.gz at bags/bag.tar.gz."""
        for rel, content in payload.items():
            client.put_object(Bucket="src-bucket", Key=f"src/{rel}", Body=content)
        create_bag(
            client,
            client,
            "src-bucket",
            "src/",
            "dest-bucket",
            "bags/bag.tar.gz",
            bag_name="my-bag",
        )

    def test_matching_target_returns_valid(self, patched_client, capsys):
        self._put_payload_and_bag(patched_client, {"a.txt": b"alpha\n"})

        rc = main(
            [
                "verify-against",
                "s3://dest-bucket/bags/bag.tar.gz",
                "s3://src-bucket/src/",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "RESULT: VALID" in out
        assert "Target: s3://src-bucket/src/" in out

    def test_mismatching_target_returns_invalid(self, patched_client, capsys):
        self._put_payload_and_bag(patched_client, {"a.txt": b"alpha\n"})
        # Corrupt the target.
        patched_client.put_object(Bucket="src-bucket", Key="src/a.txt", Body=b"corrupted")

        rc = main(
            [
                "verify-against",
                "s3://dest-bucket/bags/bag.tar.gz",
                "s3://src-bucket/src/",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 1, out
        assert "RESULT: INVALID" in out

    def test_data_segment_in_target_emits_warning(self, patched_client, capsys):
        self._put_payload_and_bag(patched_client, {"a.txt": b"alpha\n"})
        # Re-upload the file under a /data/-shaped prefix.
        patched_client.put_object(Bucket="src-bucket", Key="bag/data/a.txt", Body=b"alpha\n")

        # rc may be 0 or 1 depending on file alignment; the point is the warning.
        main(
            [
                "verify-against",
                "s3://dest-bucket/bags/bag.tar.gz",
                "s3://src-bucket/bag/data/",
            ]
        )
        out = capsys.readouterr().out
        assert "Warnings" in out
        assert "/data/" in out

    def test_bad_archive_url_exits_2(self, patched_client, capsys):
        rc = main(
            [
                "verify-against",
                "s3://dest-bucket/bags/bag.rar",
                "s3://src-bucket/src/",
            ]
        )
        assert rc == 2
        assert "Cannot detect archive format" in capsys.readouterr().err


class TestCreateBagCommand:
    def _put_source(self, client, files: dict[str, bytes]) -> None:
        for rel, content in files.items():
            client.put_object(Bucket="src-bucket", Key=f"src/{rel}", Body=content)

    def test_create_bag_round_trip(self, patched_client, capsys):
        self._put_source(patched_client, {"a.txt": b"alpha\n"})

        rc = main(
            [
                "create-bag",
                "--bag-name",
                "my-bag",
                "s3://src-bucket/src/",
                "s3://dest-bucket/bags/my-bag.tar.gz",
            ]
        )
        assert rc == 0, capsys.readouterr().err

        # The bag's root inside the archive is `my-bag/`, so re-extract to
        # the parent prefix and verify at `extracted/my-bag/`.
        rc = main(
            [
                "extract",
                "--no-verify",
                "s3://dest-bucket/bags/my-bag.tar.gz",
                "s3://dest-bucket/extracted/",
            ]
        )
        assert rc == 0
        rc = main(["verify", "s3://dest-bucket/extracted/my-bag/"])
        assert rc == 0
        assert "RESULT: VALID" in capsys.readouterr().out

    def test_create_bag_rejects_non_targz_dest(self, patched_client, capsys):
        self._put_source(patched_client, {"a.txt": b"alpha\n"})

        rc = main(
            [
                "create-bag",
                "--bag-name",
                "my-bag",
                "s3://src-bucket/src/",
                "s3://dest-bucket/bags/my-bag.zip",
            ]
        )
        assert rc == 2
        assert "must end with .tar.gz" in capsys.readouterr().err

    def test_create_bag_rejects_dest_without_key(self, patched_client, capsys):
        rc = main(
            [
                "create-bag",
                "--bag-name",
                "my-bag",
                "s3://src-bucket/src/",
                "s3://dest-bucket/",
            ]
        )
        assert rc == 2
        assert "needs a key" in capsys.readouterr().err

    def test_create_bag_rejects_bad_bag_info(self, patched_client, capsys):
        self._put_source(patched_client, {"a.txt": b"alpha\n"})
        rc = main(
            [
                "create-bag",
                "--bag-name",
                "my-bag",
                "--bag-info",
                "no-equals-sign",
                "s3://src-bucket/src/",
                "s3://dest-bucket/bag.tar.gz",
            ]
        )
        assert rc == 2
        assert "LABEL=VALUE" in capsys.readouterr().err

    def test_create_bag_empty_source_exits_1(self, patched_client, capsys):
        # Empty prefix → BagError → exit 1 via the BagError handler.
        rc = main(
            [
                "create-bag",
                "--bag-name",
                "my-bag",
                "s3://src-bucket/empty/",
                "s3://dest-bucket/bag.tar.gz",
            ]
        )
        assert rc == 1
        assert "empty" in capsys.readouterr().err


class TestLsCommand:
    def test_ls_tar_gz_prints_members_and_summary(self, patched_client, capsys):
        files = make_bag_files({"a.txt": b"alpha\n", "b.txt": b"beta\n"})
        _put_archive(patched_client, "src-bucket", "in/bag.tar.gz", build_tar_gz(files))

        rc = main(["ls", "s3://src-bucket/in/bag.tar.gz"])

        out = capsys.readouterr().out
        assert rc == 0
        assert "data/a.txt" in out
        assert "data/b.txt" in out
        # Summary line shape: "<n> files, <size> <unit>"
        assert " files, " in out

    def test_ls_zip_prints_members(self, patched_client, capsys):
        files = make_bag_files({"a.txt": b"alpha\n"})
        _put_archive(patched_client, "src-bucket", "in/bag.zip", build_zip(files))

        rc = main(["ls", "s3://src-bucket/in/bag.zip"])

        out = capsys.readouterr().out
        assert rc == 0
        assert "data/a.txt" in out
        assert " files, " in out

    def test_ls_7z_prints_members(self, patched_client, capsys):
        files = make_bag_files({"a.txt": b"alpha\n"})
        _put_archive(patched_client, "src-bucket", "in/bag.7z", build_7z(files))

        rc = main(["ls", "s3://src-bucket/in/bag.7z"])

        out = capsys.readouterr().out
        assert rc == 0
        assert "data/a.txt" in out
        assert " files, " in out

    def test_ls_rejects_bad_url(self, patched_client, capsys):
        rc = main(["ls", "s3://src-bucket/file.rar"])
        assert rc == 2
        assert "Cannot detect archive format" in capsys.readouterr().err


class TestIssueCommand:
    def test_issue_prints_url_and_opens_browser(self, capsys, monkeypatch):
        opened: list[str] = []
        monkeypatch.setattr(
            "s3_bagit.issue.webbrowser.open", lambda url: opened.append(url) or True
        )

        rc = main(["issue", "something broke"])

        captured = capsys.readouterr()
        assert rc == 0
        assert len(opened) == 1
        assert opened[0].startswith("https://github.com/borwickatuw/s3-bagit/issues/new?")
        # The brief travels through the URL.
        assert "something" in opened[0]
        # And the URL is also printed for copy-paste.
        assert "github.com/borwickatuw/s3-bagit/issues/new" in captured.out

    def test_issue_falls_back_when_no_browser(self, capsys, monkeypatch):
        monkeypatch.setattr("s3_bagit.issue.webbrowser.open", lambda url: False)

        rc = main(["issue"])

        captured = capsys.readouterr()
        assert rc == 0
        assert "No browser available" in captured.out


class TestConfigCommandDispatch:
    """Smoke check that `s3-bagit config` reaches `run_config` without needing S3 creds."""

    def test_config_does_not_call_client_for(self, monkeypatch, capsys):
        called = {"run_config": False}

        def fake_run_config(profile: str = "default") -> int:
            called["run_config"] = True
            return 0

        # `client_for` must NOT be called for `config` — operators run this
        # *before* they have credentials.
        monkeypatch.setattr(
            "s3_bagit.cli.client_for",
            lambda *a, **kw: pytest.fail("client_for should not be called for `config`"),
        )
        # `cli.py` does `from s3_bagit.config_cmd import run_config`, so the
        # binding to patch lives in the cli module's namespace.
        monkeypatch.setattr("s3_bagit.cli.run_config", fake_run_config)

        rc = main(["config"])

        assert rc == 0
        assert called["run_config"] is True


class TestKeyboardInterruptHandling:
    """Ctrl-C inside a subcommand should exit cleanly, not dump a traceback."""

    def test_config_ctrl_c_exits_130(self, monkeypatch, capsys):
        def _raise(*_a, **_kw):
            raise KeyboardInterrupt

        monkeypatch.setattr("s3_bagit.cli.run_config", _raise)

        rc = main(["config"])

        captured = capsys.readouterr()
        assert rc == 130
        assert "Cancelled." in captured.err


class TestErrorHintAppended:
    def test_config_error_includes_issue_hint(self, patched_client, capsys):
        rc = main(["extract", "s3://src-bucket/file.rar", "s3://dest-bucket/out/"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "Cannot detect archive format" in err
        assert "s3-bagit issue" in err
