"""End-to-end acceptance test for cross-endpoint bag workflows.

Builds a bag on one mock S3 endpoint, then runs ``verify_against`` with
the bag on that endpoint and the extracted target tree on the *other*
endpoint. This is the acceptance gate for the multi-config / cross-
environment refactor — the Preservation team's real workflow is bags
in AWS, extracted trees in Kopah (Ceph/RGW), and this test mirrors
that shape with two distinct ``ThreadedMotoServer`` instances.

The fixture ``cross_env_real_endpoints`` (in conftest.py) creates two
endpoints on ephemeral ports with *distinct* bucket names per side so
a cross-talk bug — uploading to or reading from the wrong endpoint —
fails loudly with NoSuchBucket instead of silently succeeding against
the wrong server.
"""

from s3_bagit.create_bag import create_bag
from s3_bagit.verify_against import verify_against

from .conftest import build_tar_gz, make_bag_files


class TestVerifyAgainstAcrossEndpoints:
    def test_bag_on_src_extracted_on_dst_verifies(self, cross_env_real_endpoints):
        """The canonical Preservation flow: bag in AWS, extracted tree in Kopah."""
        src = cross_env_real_endpoints["src"]
        dst = cross_env_real_endpoints["dst"]
        payload = {"a.txt": b"alpha\n", "sub/b.txt": b"beta\n"}

        # Stage the payload on the source endpoint, build the bag there.
        for rel, content in payload.items():
            src["client"].put_object(Bucket=src["bucket"], Key=f"payload/{rel}", Body=content)

        create_bag(
            src["client"],
            src["client"],  # bag lands on the same (src) endpoint as the payload
            src["bucket"],
            "payload/",
            src["bucket"],
            "bags/bag.tar.gz",
            bag_name="bag",
        )

        # The "extracted" flat tree lives on the destination endpoint.
        for rel, content in payload.items():
            dst["client"].put_object(Bucket=dst["bucket"], Key=f"extracted/{rel}", Body=content)

        result = verify_against(
            src["client"],  # read bag from src endpoint
            dst["client"],  # check extracted tree on dst endpoint
            src["bucket"],
            "bags/bag.tar.gz",
            "tar.gz",
            dst["bucket"],
            "extracted/",
            archive_url=f"s3://{src['bucket']}/bags/bag.tar.gz",
            target_url=f"s3://{dst['bucket']}/extracted/",
        )

        assert result.ok, result.errors
        assert result.payload_file_count == 2

    def test_target_mismatch_across_endpoints_fails(self, cross_env_real_endpoints):
        """A corrupted file on the destination endpoint is detected, not silently passed."""
        src = cross_env_real_endpoints["src"]
        dst = cross_env_real_endpoints["dst"]
        # The bag's manifest says a.txt has the alpha\n checksum…
        bag_files = make_bag_files({"a.txt": b"alpha\n"})
        src["client"].put_object(
            Bucket=src["bucket"], Key="bags/bag.tar.gz", Body=build_tar_gz(bag_files)
        )

        # ...but the destination tree holds corrupted bytes for the same key.
        dst["client"].put_object(Bucket=dst["bucket"], Key="extracted/a.txt", Body=b"CORRUPTED\n")

        result = verify_against(
            src["client"],
            dst["client"],
            src["bucket"],
            "bags/bag.tar.gz",
            "tar.gz",
            dst["bucket"],
            "extracted/",
            archive_url=f"s3://{src['bucket']}/bags/bag.tar.gz",
            target_url=f"s3://{dst['bucket']}/extracted/",
        )

        assert not result.ok
        assert any("checksum mismatch" in e for e in result.errors)
