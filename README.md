# s3-bagit

[BagIt](https://www.rfc-editor.org/rfc/rfc8493) extract and verify
operations against any S3-compatible object storage (AWS S3, Ceph
RadosGW like UW Libraries' Kopah, MinIO, DigitalOcean Spaces,
Backblaze B2, Wasabi, …), streaming end-to-end so no local disk is
required.

Two subcommands:

```
s3-bagit extract <archive_url> <dest_url>   # extract a bag (tar.gz/zip) in S3 to S3
s3-bagit verify  <bag_url>                  # verify an already-extracted bag at an S3 prefix
```

`extract` runs `verify` against the destination by default; pass
`--no-verify` to skip.

## Quick start

```bash
# One-shot via uvx (no clone needed):
uvx --from git+https://github.com/borwickatuw/s3-bagit s3-bagit --help

# Or from a clone:
make install
uv run s3-bagit extract \
    s3://incoming/bag-001.tar.gz \
    s3://preserved/bag-001/
```

S3 credentials are resolved in this order:

1. `$S3CMD_CONFIG` — explicit path to an s3cmd INI file.
2. `~/.s3cfg` — s3cmd's default config location.
3. boto3's default credential chain (`~/.aws/credentials`, `AWS_*` env
   vars, IAM role, AWS SSO, …). For a non-AWS endpoint, also set
   `$S3_ENDPOINT_URL`.

See [`.env.example`](.env.example) for details.

## Documentation

- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — operator guide
- [`docs/BAGIT-SPEC.md`](docs/BAGIT-SPEC.md) — RFC 8493 conformance notes
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — streaming model and S3-to-S3 design

## Status

v0.1.0 — initial release. Supports `tar.gz` and `zip` archives. 7z is
not supported (see [`docs/BAGIT-SPEC.md`](docs/BAGIT-SPEC.md)).
