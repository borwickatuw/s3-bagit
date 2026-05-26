"""s3-bagit: BagIt extract and verify operations against any S3-compatible storage."""

try:
    # Written by hatch-vcs on every build from `git describe --tags`.
    from s3_bagit._version import __version__
except ImportError:
    # Working from a source tree where the build hook hasn't run yet.
    __version__ = "0.0.0+unknown"

REPO_URL = "https://github.com/borwickatuw/s3-bagit"
