"""Exceptions raised by s3-bagit.

``ConfigError`` is re-exported from :mod:`s3_archive.exceptions` so the
two packages share a single exception type — the s3-bagit CLI catches
``ConfigError`` and traffic raised from either side surfaces with the
same exit code and message format.

``BagError`` is local to s3-bagit (BagIt structural / checksum failure
is a BagIt-specific concept; no s3-archive equivalent).

``S3OperationError`` wraps a botocore ``ClientError`` raised mid-operation
with operator-facing context (which archive, which destination, whether
the server returned a non-S3 HTML response).
"""

from s3_archive.exceptions import ConfigError

__all__ = ["BagError", "ConfigError", "S3OperationError"]


class BagError(Exception):
    """The bag failed structural or checksum verification."""


class S3OperationError(Exception):
    """An S3 API call failed in a way that's outside our control.

    Distinct from ``BagError`` (the bag itself is broken) and
    ``ConfigError`` (the user's setup is wrong) — this means the server
    returned an error during a streaming operation. Carries the formatted
    operator-facing message; the underlying ``ClientError`` is the
    ``__cause__``.
    """
