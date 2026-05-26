# kopah-bagit

[BagIt](https://www.rfc-editor.org/rfc/rfc8493) extract and verify operations
against [Kopah](https://itconnect.uw.edu/) (UW's Ceph S3-compatible object
storage), streaming end-to-end so no local disk is required.

Two subcommands:

```
kopah-bagit extract <archive_url> <dest_url>   # extract a bag (tar.gz/zip) in S3 to S3
kopah-bagit verify  <bag_url>                  # verify an already-extracted bag at an S3 prefix
```

`extract` runs `verify` against the destination by default; pass `--no-verify`
to skip.

## Quick start

```bash
# One-shot via uvx (no clone needed):
uvx --from git+https://github.com/uwlibrary/kopah-bagit kopah-bagit --help

# Or from a clone:
make install
uv run kopah-bagit extract \
    s3://incoming/bag-001.tar.gz \
    s3://preserved/bag-001/
```

Kopah credentials are resolved in s3cmd's own order: `$S3CMD_CONFIG`
if set, then `~/.s3cfg`, then the `KOPAH_ACCESS_KEY` /
`KOPAH_SECRET_KEY` / `KOPAH_ENDPOINT` env vars. If you already have
s3cmd working against Kopah, there's nothing to configure. See
`.env.example`.

## Documentation

- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — operator guide (Preservation team)
- [`docs/BAGIT-SPEC.md`](docs/BAGIT-SPEC.md) — conformance notes (RFC 8493)
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — streaming model and S3-to-S3 design

## Status

v0.1.0 — initial release. Supports `tar.gz` and `zip` archives. 7z is
not supported (see [`docs/BAGIT-SPEC.md`](docs/BAGIT-SPEC.md)).
