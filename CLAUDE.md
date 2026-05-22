# kopah-bagit

## Project Overview

Two-operation CLI for BagIt (RFC 8493) workflows against UW Libraries'
Kopah (Ceph S3): `extract` a serialized bag (tar.gz / zip) in S3 to an
S3 destination prefix, and `verify` an already-extracted bag at an S3
prefix. Everything streams — nothing is ever staged on local disk.

Built for the Preservation team. Narrow scope: just these two operations.

## Related Projects

- **storage-scripts** — the broader storage tooling suite. kopah-bagit's
  streaming-extract code is adapted from its `stream_archive/` package;
  the Kopah client pattern (`S3CMD_CONFIG` → boto3) is copied from its
  `shared/kopah.py`. kopah-bagit does NOT depend on storage-scripts at
  runtime — it's a standalone repo.
- **claude-meta** — cross-repo standards and best-practice guides.

## Coding Standards

Follow user preferences in `~/.claude/CLAUDE.md` and cross-repo
guides in `claude-meta/best-practices/` (`PYTHON.md`, `GIT.md`,
`CLAUDE-FILES.md` apply here). Project-specific:

- Two credential sources are intentionally supported. `S3CMD_CONFIG`
  wins if set; otherwise direct `KOPAH_*` env vars. See
  [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for the rationale (CI
  vs. operator workstations).
- Streaming model means **single-pass**. Don't add code that requires
  re-reading the archive — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
- BagIt verification collects all errors, then reports together —
  don't fail-fast on the first checksum mismatch; operators want the
  full picture in one run.

## Project Structure

```
src/kopah_bagit/
    cli.py            argparse entry point (extract, verify)
    extract.py        streaming tar.gz + zip extract to S3
    verify.py         RFC 8493 manifest / tagmanifest / Payload-Oxum checks
    kopah_client.py   boto3 client builder (S3CMD_CONFIG or KOPAH_* env)
    s3_url.py         parse_s3_url, parse_s3_prefix, detect_format
    exceptions.py     ConfigError, BagError
    log_config.py     tqdm-aware console logger
tests/                pytest + moto (no live S3 required)
docs/
    OPERATIONS.md     operator guide (Preservation)
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
make run ARGS='...'     # invoke kopah-bagit
```

Direct invocation:

```
uv run kopah-bagit extract s3://bucket/bag.tar.gz s3://bucket/extracted/
uv run kopah-bagit verify s3://bucket/extracted/
```

Or once published, `uvx --from kopah-bagit kopah-bagit ...`.

## Security

`make security` runs:

- `bandit` against `src/`
- `pip-audit` against the lockfile

No secrets are stored in the repo. Kopah credentials come from
`S3CMD_CONFIG` (an s3cmd INI file) or `KOPAH_*` env vars — see
`.env.example`.

## Cross-Repository Ideas

When you discover patterns, improvements, or ideas that might apply to
other repositories, capture them:

    claude-idea kopah-bagit "Description of the pattern or improvement"
