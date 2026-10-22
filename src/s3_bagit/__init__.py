"""s3-bagit: BagIt extract and verify operations against any S3-compatible storage."""

# Not a requires-python compat shim: `src/s3_bagit/_version.py` is
# gitignored (.gitignore:16) and is written by hatch-vcs at build time
# from `git describe --tags`, so the ImportError branch is live on every
# fresh clone, on every supported Python. re-evaluate-by: 2026-11
# pysmelly: ignore[compat-shims]  # noqa: ERA001
try:
    from s3_bagit._version import __version__
except ImportError:
    # Working from a source tree where the build hook hasn't run yet.
    __version__ = "0.0.0+unknown"

REPO_URL = "https://github.com/borwickatuw/s3-bagit"
