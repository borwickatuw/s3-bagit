# s3-bagit

## Project Overview

Narrow CLI for BagIt (RFC 8493) workflows against any S3-compatible
object storage:

- `extract` — serialized bag (tar / tar.gz / tar.bz2 / tar.xz /
  tar.zst / zip / 7z) in S3 → extracted bag at an S3 prefix.
- `verify` — check an already-extracted bag at an S3 prefix.
- `verify-against` — check the files at an S3 prefix against the
  manifests inside a serialized bag (without extracting the bag).
- `create-bag` — S3 prefix → serialized BagIt `.tar.gz` at an S3 key.
  (Only `.tar.gz` is emitted; `.7z` create is not supported.)

Everything streams — nothing is ever staged on local disk.

Built initially for UW Libraries' Preservation team against Kopah
(Ceph RadosGW), but written to be S3-generic — AWS S3, MinIO,
DigitalOcean Spaces, etc. all work.

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
    cli.py            argparse entry point (extract, verify, verify-against, create-bag, ls, config, issue)
    verify.py         RFC 8493 manifest / tagmanifest / Payload-Oxum checks
    verify_against.py stream serialized bag once, hash target prefix once-per-file (multi-hasher)
    create_bag.py     streaming S3-prefix → BagIt .tar.gz (single-pass, tag files trailing)
    config_cmd.py     interactive `s3-bagit config` for ~/.s3cfg
    issue.py          open a pre-filled GitHub new-issue URL
    s3_client.py      boto3 client builder (s3cmd config or AWS chain)
    exceptions.py     ConfigError, BagError
    log_config.py     tqdm-aware console logger
tests/                pytest + moto (no live S3 required)
docs/
    BAGIT-SPEC.md     conformance notes (including the .7z BagIt-non-standard note)
    ARCHITECTURE.md   streaming model and S3-to-S3 design
```

Note: `extract`, `ls`, archive-member iteration, S3 URL parsing, and
format detection all live in [`s3-archive`](https://github.com/borwickatuw/s3-archive)
— s3-bagit imports them rather than reimplementing.

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
uv run s3-bagit create-bag --bag-name my-bag s3://bucket/src/ s3://bucket/my-bag.tar.gz
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
