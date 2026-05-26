"""Tests for the streaming `s3-bagit ls` subcommand."""

import pytest

from s3_bagit.ls import _format_size, list_archive

from .conftest import build_tar, build_zip, make_bag_files


@pytest.fixture
def sample_bag_bytes():
    return make_bag_files({"a.txt": b"hello\n", "sub/b.txt": b"world\n"})


class TestListTar:
    @pytest.mark.parametrize(
        "fmt,mode,suffix",
        [
            ("tar", "w", "tar"),
            ("tar.gz", "w:gz", "tar.gz"),
            ("tar.bz2", "w:bz2", "tar.bz2"),
        ],
    )
    def test_streams_member_names(self, s3_client, sample_bag_bytes, capsys, fmt, mode, suffix):
        archive = build_tar(sample_bag_bytes, mode=mode)
        s3_client.put_object(Bucket="src-bucket", Key=f"in/bag.{suffix}", Body=archive)

        count, total = list_archive(s3_client, "src-bucket", f"in/bag.{suffix}", fmt)

        out = capsys.readouterr().out
        assert "data/a.txt" in out
        assert "data/sub/b.txt" in out
        assert count == len(sample_bag_bytes)
        assert total >= sum(len(c) for c in sample_bag_bytes.values())
        assert " files, " in out


class TestListZip:
    def test_streams_member_names(self, s3_client, sample_bag_bytes, capsys):
        archive = build_zip(sample_bag_bytes)
        s3_client.put_object(Bucket="src-bucket", Key="in/bag.zip", Body=archive)

        count, total = list_archive(s3_client, "src-bucket", "in/bag.zip", "zip")

        out = capsys.readouterr().out
        assert "data/a.txt" in out
        assert count == len(sample_bag_bytes)
        assert total > 0


class TestUnsupportedFormat:
    def test_raises(self, s3_client):
        with pytest.raises(ValueError, match="Unsupported format"):
            list_archive(s3_client, "src-bucket", "x", "7z")


class TestFormatSize:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (0, "0 B"),
            (512, "512 B"),
            (1024, "1.0 KB"),
            (1536, "1.5 KB"),
            (1024 * 1024 + 500_000, "1.5 MB"),
        ],
    )
    def test_format_size(self, value, expected):
        assert _format_size(value) == expected
