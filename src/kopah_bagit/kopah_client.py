"""Build a boto3 S3 client for Kopah (Ceph RadosGW).

Two credential sources are supported, checked in this order:

1. ``S3CMD_CONFIG`` — path to an s3cmd INI file. Read ``access_key``,
   ``secret_key``, and ``host_base`` from its ``[default]`` section.
2. Direct env vars: ``KOPAH_ACCESS_KEY``, ``KOPAH_SECRET_KEY``,
   ``KOPAH_ENDPOINT``.

If neither is fully configured, :class:`ConfigError` is raised with a
message listing both options. The two-source policy is documented in
``.env.example`` and ``docs/OPERATIONS.md``.
"""

import configparser
import os
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig

from kopah_bagit.exceptions import ConfigError

_DEFAULT_POOL = 32


def _from_s3cmd_config(cfg_path: str) -> tuple[str, str, str]:
    if not Path(cfg_path).exists():
        raise ConfigError(f"S3CMD_CONFIG path does not exist: {cfg_path}")
    parser = configparser.ConfigParser()
    parser.read(cfg_path)
    if "default" not in parser:
        raise ConfigError(f"{cfg_path}: missing [default] section")
    section = parser["default"]
    for key in ("access_key", "secret_key", "host_base"):
        if not section.get(key):
            raise ConfigError(f"{cfg_path}: [default] missing required key {key!r}")
    endpoint = f"https://{section['host_base']}"
    return section["access_key"], section["secret_key"], endpoint


def _from_direct_env() -> tuple[str, str, str]:
    access = os.environ.get("KOPAH_ACCESS_KEY", "")
    secret = os.environ.get("KOPAH_SECRET_KEY", "")
    endpoint = os.environ.get("KOPAH_ENDPOINT", "")
    missing = [
        name
        for name, value in (
            ("KOPAH_ACCESS_KEY", access),
            ("KOPAH_SECRET_KEY", secret),
            ("KOPAH_ENDPOINT", endpoint),
        )
        if not value
    ]
    if missing:
        raise ConfigError("Direct Kopah env vars are incomplete: missing " + ", ".join(missing))
    return access, secret, endpoint


def _resolve_credentials() -> tuple[str, str, str]:
    cfg_path = os.environ.get("S3CMD_CONFIG", "").strip()
    if cfg_path:
        return _from_s3cmd_config(cfg_path)
    if any(os.environ.get(k) for k in ("KOPAH_ACCESS_KEY", "KOPAH_SECRET_KEY", "KOPAH_ENDPOINT")):
        return _from_direct_env()
    raise ConfigError(
        "No Kopah credentials configured. Set one of:\n"
        "  • S3CMD_CONFIG=/path/to/.s3cfg (recommended)\n"
        "  • KOPAH_ACCESS_KEY + KOPAH_SECRET_KEY + KOPAH_ENDPOINT\n"
        "See .env.example for details."
    )


def load_client(max_pool_connections: int = _DEFAULT_POOL):
    """Return a configured boto3 S3 client pointing at Kopah.

    ``request_checksum_calculation="when_required"`` matches the
    storage-scripts setting: Ceph RadosGW rejects PutObject under the
    default SigV4 content-SHA256 handling. The flag tells boto3 to skip
    that header except where the API explicitly requires it.
    """
    access, secret, endpoint = _resolve_credentials()
    return boto3.client(
        "s3",
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        endpoint_url=endpoint,
        config=BotoConfig(
            request_checksum_calculation="when_required",
            max_pool_connections=max_pool_connections,
        ),
    )
