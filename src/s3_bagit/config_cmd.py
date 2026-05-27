"""Thin shim that delegates to :func:`s3_archive.config_cmd.run_config`.

The full interactive bootstrap implementation now lives upstream in
s3-archive (see ``src/s3_archive/config_cmd.py``). This module exists
only so the s3-bagit CLI can keep its own banner ("Configure S3
credentials for s3-bagit.") and ``--profile`` plumbing.
"""

from s3_archive.config_cmd import run_config as _run


def run_config(profile: str = "default") -> int:
    """Run the interactive prompts with the s3-bagit banner."""
    return _run(tool_name="s3-bagit", profile=profile)
