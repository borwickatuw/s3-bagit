"""Exceptions raised by s3-bagit.

``ConfigError`` is re-exported from :mod:`s3_archive.exceptions` so the
two packages share a single exception type — the s3-bagit CLI catches
``ConfigError`` and traffic raised from either side surfaces with the
same exit code and message format.

``BagError`` is local to s3-bagit (BagIt structural / checksum failure
is a BagIt-specific concept; no s3-archive equivalent).
"""

from s3_archive.exceptions import ConfigError

__all__ = ["BagError", "ConfigError"]


class BagError(Exception):
    """The bag failed structural or checksum verification."""
