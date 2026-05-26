"""S3 URL parsing and archive-format detection."""

from s3_bagit.exceptions import ConfigError


def parse_s3_url(url: str) -> tuple[str, str]:
    """Parse ``s3://bucket/key`` into ``(bucket, key)``.

    The key may be empty for prefix-style URLs that end in ``/`` — callers
    that need a non-empty key should validate separately.
    """
    if not url.startswith("s3://"):
        raise ConfigError(f"Not an S3 URL: {url!r} (must start with s3://)")
    rest = url[5:]
    if "/" not in rest:
        # Bucket-only URL like s3://bucket — treat as empty key/prefix.
        bucket, key = rest, ""
    else:
        bucket, key = rest.split("/", 1)
    if not bucket:
        raise ConfigError(f"S3 URL has empty bucket: {url!r}")
    return bucket, key


def parse_s3_prefix(url: str) -> tuple[str, str]:
    """Like :func:`parse_s3_url` but normalizes the prefix to end with ``/``.

    Empty prefix is allowed (whole-bucket). Used for ``extract`` destinations
    and ``verify`` bag roots.
    """
    bucket, prefix = parse_s3_url(url)
    if prefix and not prefix.endswith("/"):
        prefix = prefix + "/"
    return bucket, prefix


# Order matters: longer suffixes (e.g. ".tar.gz") must be checked before
# their bare-extension counterparts (".tar") to avoid misclassifying a
# compressed tar as an uncompressed one.
_EXTENSION_FORMATS: tuple[tuple[tuple[str, ...], str], ...] = (
    ((".tar.gz", ".tgz"), "tar.gz"),
    ((".tar.bz2", ".tbz2"), "tar.bz2"),
    ((".tar.xz", ".txz"), "tar.xz"),
    ((".tar",), "tar"),
    ((".zip",), "zip"),
)


def detect_format(url: str) -> str:
    """Detect archive format from URL extension.

    Returns one of ``"tar"``, ``"tar.gz"``, ``"tar.bz2"``, ``"tar.xz"``,
    or ``"zip"``. Raises :class:`ConfigError` for unrecognized extensions.
    ``.7z`` is rejected with a specific message because Preservation teams
    sometimes use it and the spec doesn't require it; see
    docs/BAGIT-SPEC.md.
    """
    lower = url.lower()
    for suffixes, fmt in _EXTENSION_FORMATS:
        if lower.endswith(suffixes):
            return fmt
    if lower.endswith(".7z"):
        raise ConfigError(
            f"7z is not supported (see docs/BAGIT-SPEC.md). RFC 8493 §4.1.2 names "
            f"TAR, ZIP, and TGZ; 7z requires non-streaming seek access. URL: {url!r}"
        )
    raise ConfigError(
        f"Cannot detect archive format from: {url!r} "
        f"(expected .tar, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz, or .zip)"
    )
