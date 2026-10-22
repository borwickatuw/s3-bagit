"""End-to-end CLI smoke tests (with a patched S3 client + moto)."""

import argparse
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from s3_bagit._cli_parsers import _build_parser
from s3_bagit.cli import _COMMANDS, main
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


_ARCHIVE_URL = "s3://src-bucket/in/bag.tar.gz"
_DEST_URL = "s3://dest-bucket/out/"


def _run_extract(*flags: str, archive_url: str = _ARCHIVE_URL) -> int:
    """``main(["extract", *flags, <archive>, <dest>])`` with the standard URLs.

    The argv list is the noisiest part of these tests and the least
    interesting; keeping it in one place leaves each test showing only
    the flag under exercise.
    """
    return main(["extract", *flags, archive_url, _DEST_URL])


def _dest_keys(s3, prefix: str = "out/") -> set[str]:
    """Every key laid down under ``dest-bucket/<prefix>``."""
    objs = s3.list_objects_v2(Bucket="dest-bucket", Prefix=prefix)["Contents"]
    return {o["Key"] for o in objs}


def _assert_extract_report(
    capsys,
    rc: int,
    *,
    valid: bool,
    contains: tuple[str, ...] = (),
    absent: tuple[str, ...] = (),
) -> None:
    """Assert the verdict an ``extract`` run reported, plus this test's own expectations.

    ``valid`` is the whole verdict: rc 0 with ``RESULT: VALID``, or rc 1 with
    ``RESULT: INVALID``. ``contains`` / ``absent`` stay per-test — each caller
    names the report fragments its own case is about, so two cases asserting
    opposite outcomes still read as opposites at the call site.
    """
    expected_rc = 0 if valid else 1
    assert rc == expected_rc
    out = capsys.readouterr().out
    verdict = "RESULT: VALID" if valid else "RESULT: INVALID"
    assert verdict in out, out
    for fragment in contains:
        assert fragment in out, out
    for fragment in absent:
        assert fragment not in out, out


class TestExtractThenVerify:
    def test_extract_with_default_verify_passes_for_valid_bag(self, patched_client, capsys):
        files = make_bag_files({"a.txt": b"alpha\n", "b.txt": b"beta\n"})
        _put_archive(patched_client, "src-bucket", "in/bag.tar.gz", build_tar_gz(files))

        rc = _run_extract()

        _assert_extract_report(capsys, rc, valid=True)

    def test_extract_with_no_verify_skips_check(self, patched_client, capsys):
        files = make_bag_files({"a.txt": b"alpha\n"})
        _put_archive(patched_client, "src-bucket", "in/bag.zip", build_zip(files))

        rc = _run_extract("--no-verify", archive_url="s3://src-bucket/in/bag.zip")

        assert rc == 0
        captured = capsys.readouterr()
        assert "RESULT:" not in captured.out

    def test_extract_then_verify_fails_when_archive_lacks_bagit_txt(self, patched_client, capsys):
        # An archive that isn't a bag at all.
        archive = build_tar_gz({"random.txt": b"not a bag"})
        _put_archive(patched_client, "src-bucket", "in/bag.tar.gz", archive)

        rc = _run_extract()

        _assert_extract_report(capsys, rc, valid=False)

    def test_extract_with_default_verify_passes_for_7z_bag(self, patched_client, capsys):
        files = make_bag_files({"a.txt": b"alpha\n", "b.txt": b"beta\n"})
        _put_archive(patched_client, "src-bucket", "in/bag.7z", build_7z(files))

        rc = _run_extract(archive_url="s3://src-bucket/in/bag.7z")

        _assert_extract_report(capsys, rc, valid=True)


def _stage_wrapped_bag(s3, payload: dict[str, bytes] | None = None) -> None:
    """Put a valid bag wrapped in a single ``mybag/`` top-level dir at the archive URL."""
    files = make_bag_files(payload if payload is not None else {"a.txt": b"alpha\n"})
    _put_archive(s3, "src-bucket", "in/bag.tar.gz", build_tar_gz(files, wrap_prefix="mybag"))


class TestExtractArchiveWrap:
    """Archives that wrap the bag in a single top-level directory."""

    def test_default_follows_wrap_to_nested_bag_root(self, patched_client, capsys):
        # Archive contains mybag/bagit.txt, mybag/data/..., etc.
        _stage_wrapped_bag(patched_client)

        rc = _run_extract()

        # Verify ran against the nested bag root, not the dest prefix.
        _assert_extract_report(capsys, rc, valid=True, contains=("s3://dest-bucket/out/mybag/",))
        # Files were NOT moved — the wrap remains in place.
        keys = _dest_keys(patched_client)
        assert "out/mybag/bagit.txt" in keys
        assert "out/bagit.txt" not in keys

    def test_default_does_not_follow_when_top_level_is_not_a_bag(self, patched_client, capsys):
        # Single top-level dir but no bagit.txt inside it — verify should
        # still target the dest prefix and report the failure as today.
        archive = build_tar_gz({"random.txt": b"not a bag"}, wrap_prefix="something")
        _put_archive(patched_client, "src-bucket", "in/bag.tar.gz", archive)

        rc = _run_extract()

        # Bag header in the report should be the dest prefix (not the wrap).
        _assert_extract_report(
            capsys,
            rc,
            valid=False,
            contains=("Bag: s3://dest-bucket/out/",),
            absent=("s3://dest-bucket/out/something/",),
        )


class TestExtractStripTopLevel:
    """``--strip-top-level`` reorganizes a single-wrap archive into the dest."""

    def test_strip_top_level_lands_bag_at_dest_and_verifies(self, patched_client, capsys):
        _stage_wrapped_bag(patched_client, {"a.txt": b"alpha\n", "b.txt": b"beta\n"})

        rc = _run_extract("--strip-top-level")

        _assert_extract_report(capsys, rc, valid=True, contains=("Bag: s3://dest-bucket/out/",))

        # Wrap dir is gone; bag files now sit directly under the dest.
        keys = _dest_keys(patched_client)
        assert "out/bagit.txt" in keys
        assert "out/data/a.txt" in keys
        assert "out/mybag/bagit.txt" not in keys

    @pytest.mark.parametrize(
        "members,expected_stderr,expected_keys",
        [
            pytest.param(
                # bagit.txt at root and a file under mybag/ — no single wrap.
                {"bagit.txt": b"x", "mybag/data/foo": b"y"},
                ("--strip-top-level requires", "s3://dest-bucket/out/"),
                {"out/bagit.txt", "out/mybag/data/foo"},
                id="file-at-archive-root",
            ),
            pytest.param(
                {"a/x.txt": b"1", "b/y.txt": b"2"},
                ("--strip-top-level requires", "a/", "b/"),
                {"out/a/x.txt", "out/b/y.txt"},
                id="multiple-top-level-dirs",
            ),
        ],
    )
    def test_strip_top_level_errors_without_a_single_wrap(
        self, patched_client, capsys, members, expected_stderr, expected_keys
    ):
        _put_archive(patched_client, "src-bucket", "in/bag.tar.gz", build_tar_gz(members))

        rc = _run_extract("--strip-top-level")

        assert rc == 1
        stderr = capsys.readouterr().err
        # The message names the problem and gives cleanup guidance.
        for fragment in expected_stderr:
            assert fragment in stderr, stderr
        # Files were extracted (and left in place per spec).
        assert expected_keys <= _dest_keys(patched_client)


def _make_client_error(
    *, status: int, code: str, message: str, content_type: str, body: str | None = None
) -> ClientError:
    """Mirror what botocore builds when the server returns *status*.

    With an HTML body, XML parsing fails and botocore falls back to
    ``Code`` = the HTTP status as a string plus generic ``Message`` text;
    a proper S3 XML error carries the real code and message and no body.
    """
    response = {
        "Error": {"Code": code, "Message": message},
        "ResponseMetadata": {
            "HTTPStatusCode": status,
            "HTTPHeaders": {"content-type": content_type},
        },
    }
    if body is not None:
        response["Body"] = body
    return ClientError(response, "PutObject")


class TestExtractClientErrorWrap:
    """The CLI wraps mid-stream botocore ClientErrors with operator context."""

    @pytest.mark.parametrize(
        "error_kwargs,archive_url,expected,absent",
        [
            pytest.param(
                {
                    "status": 500,
                    "code": "500",
                    "message": "Internal Server Error",
                    "content_type": "text/html; charset=iso-8859-1",
                    "body": "<html><title>500 Internal Server Error</title></html>",
                },
                "s3://src-bucket/in/bag.7z",
                (
                    "S3 PutObject failed during extract",
                    "s3://src-bucket/in/bag.7z",
                    _DEST_URL,
                    "HTTP 500",
                    "HTML",  # proxy/server hint fired
                    "upstream proxy",
                ),
                (),
                id="html-500-hints-at-a-proxy",
            ),
            pytest.param(
                {
                    "status": 403,
                    "code": "AccessDenied",
                    "message": "Access Denied",
                    "content_type": "application/xml",
                },
                _ARCHIVE_URL,
                ("HTTP 403 (AccessDenied): Access Denied", _ARCHIVE_URL, _DEST_URL),
                # No proxy hint for a proper S3 XML error.
                ("upstream proxy",),
                id="xml-403-omits-the-proxy-hint",
            ),
        ],
    )
    def test_client_error_is_wrapped(
        self, patched_client, capsys, error_kwargs, archive_url, expected, absent
    ):
        with patch("s3_bagit.cli.extract", side_effect=_make_client_error(**error_kwargs)):
            rc = _run_extract(archive_url=archive_url)

        assert rc == 4
        stderr = capsys.readouterr().err
        for fragment in expected:
            assert fragment in stderr, stderr
        for fragment in absent:
            assert fragment not in stderr, stderr


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
    @pytest.mark.parametrize(
        "key,build",
        [
            pytest.param("in/bag.tar.gz", build_tar_gz, id="tar.gz"),
            pytest.param("in/bag.zip", build_zip, id="zip"),
            pytest.param("in/bag.7z", build_7z, id="7z"),
        ],
    )
    def test_ls_prints_members_and_summary(self, patched_client, capsys, key, build):
        files = make_bag_files({"a.txt": b"alpha\n", "b.txt": b"beta\n"})
        _put_archive(patched_client, "src-bucket", key, build(files))

        rc = main(["ls", f"s3://src-bucket/{key}"])

        out = capsys.readouterr().out
        assert rc == 0
        assert "data/a.txt" in out
        assert "data/b.txt" in out
        # Summary line shape: "<n> files, <size> <unit>"
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


class TestParserHandlerSeam:
    """Every subcommand `_cli_parsers` declares has a handler in `cli._COMMANDS`.

    Parser construction and command handling live in different modules, so
    nothing syntactic ties a new `sub.add_parser("foo")` to a `"foo"` entry
    in the dispatch table. `argparse`'s `required=True` turns a missing
    handler into a `KeyError` at run time on that subcommand only — which
    the end-to-end tests below catch for the seven that exist today, and
    would not catch for an eighth. Assert the two sets match directly.
    """

    @staticmethod
    def _declared_subcommands() -> set[str]:
        parser = _build_parser()
        actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        assert len(actions) == 1, "expected exactly one subparsers action"
        return set(actions[0].choices)

    def test_declared_subcommands_match_handler_table(self):
        assert self._declared_subcommands() == set(_COMMANDS)

    def test_every_subcommand_is_reachable_by_name(self):
        for name in sorted(self._declared_subcommands()):
            assert callable(_COMMANDS[name])
