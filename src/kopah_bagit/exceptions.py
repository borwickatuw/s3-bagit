"""Exceptions raised by kopah-bagit.

Library code raises these instead of calling sys.exit(). The CLI entry
point catches them and maps to a non-zero exit code with a clean error
message on stderr.
"""


class ConfigError(Exception):
    """Missing or invalid configuration (env vars, paths, S3 URLs)."""


class BagError(Exception):
    """The bag failed structural or checksum verification."""
