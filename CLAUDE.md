# s3-bagit

## Project Overview

Two-operation CLI for BagIt (RFC 8493) workflows against any
S3-compatible object storage: `extract` a serialized bag (tar.gz / zip)
from S3 to an S3 destination prefix, and `verify` an already-extracted
bag at an S3 prefix. Everything streams — nothing is ever staged on
local disk.

Built initially for UW Libraries' Preservation team against Kopah
(Ceph RadosGW), but written to be S3-generic — AWS S3, MinIO,
DigitalOcean Spaces, etc. all work. Narrow scope: just these two
operations.

## Related Projects

- **storage-scripts** — the broader storage tooling suite. s3-bagit's
  streaming-extract code is adapted from its `stream_archive/` package.
  No runtime dependency on storage-scripts.
- **claude-meta** — cross-repo standards and best-practice guides.

## Coding Standards

Follow user preferences in `~/.claude/CLAUDE.md` and cross-repo
guides in `claude-meta/best-practices/`. Project-specific:

- **Credential resolution is the one place we allow multi-source
  fallback.** Order: `$S3CMD_CONFIG` → `~/.s3cfg` → boto3 default
  chain (with optional `$S3_ENDPOINT_URL`). This deliberately
  overrides the global "no fallback logic" preference because the
  chain mirrors s3cmd's own behavior. All other config values still
  follow the strict one-canonical-location rule.
- **Streaming model means single-pass.** Don't add code that requires
  re-reading the archive — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
- **BagIt verification collects all errors, then reports together** —
  don't fail-fast on the first checksum mismatch; operators want the
  full picture in one run.
- **Ceph workaround is always-on.** boto3's
  `request_checksum_calculation="when_required"` is required for Ceph
  RadosGW and harmless on AWS S3, so it's applied unconditionally —
  no flag, no conditional.

## Project Structure

```
src/s3_bagit/
    cli.py            argparse entry point (extract, verify)
    extract.py        streaming tar.gz + zip extract to S3
    verify.py         RFC 8493 manifest / tagmanifest / Payload-Oxum checks
    s3_client.py      boto3 client builder (s3cmd config or AWS chain)
    s3_url.py         parse_s3_url, parse_s3_prefix, detect_format
    exceptions.py     ConfigError, BagError
    log_config.py     tqdm-aware console logger
tests/                pytest + moto (no live S3 required)
docs/
    OPERATIONS.md     operator guide
    BAGIT-SPEC.md     conformance notes
    ARCHITECTURE.md   streaming model and S3-to-S3 design
```

## Commands

```
make install            # uv sync (dev + test deps)
make test               # uv run pytest
make test-cov           # tests + coverage report
make lint               # ruff check + ruff format --check
make format             # ruff format
make security           # bandit + pip-audit
make run ARGS='...'     # invoke s3-bagit
```

Direct invocation:

```
uv run s3-bagit extract s3://bucket/bag.tar.gz s3://bucket/extracted/
uv run s3-bagit verify s3://bucket/extracted/
```

Or once published, `uvx --from s3-bagit s3-bagit ...`.

## Security

`make security` runs:

- `bandit` against `src/`
- `pip-audit` against the lockfile

No secrets are stored in the repo. S3 credentials come from
`$S3CMD_CONFIG`, `~/.s3cfg`, or boto3's default chain (`AWS_*` env vars,
`~/.aws/credentials`, IAM role) — see `.env.example`.

## Cross-Repository Ideas

When you discover patterns, improvements, or ideas that might apply to
other repositories, capture them:

    claude-idea s3-bagit "Description of the pattern or improvement"
