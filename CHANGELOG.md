# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.5.1] - 2026-10-22

### Changed

- Packaging only: the public repository's test suite leaves out tests
  that check this project's internal development tooling. No code
  changes.

## [1.5.0] - 2026-10-22

### Security

- Upgraded `certifi` 2026.7.22 → 2026.10.19 (2 advisories) and
  `werkzeug` 3.1.8 → 3.1.9 (2, test-only via `moto`).
- Upgraded four locked dependencies past published advisories:
  `cryptography` 48.0.0 → 50.0.0 (7 advisories), `py7zr` 1.1.0 → 1.1.3
  (6), `pydantic-settings` 2.14.1 → 2.15.0 (1), `setuptools` 82.0.1 →
  84.0.0 (2). All transitive. `uv audit` now reports clean.
- Added a `detect-secrets` gate (`make security-secrets`, backed by a
  committed `.secrets.baseline`) to `make security`.

### Fixed

- Bumped the pinned `s3-archive` dependency to `v1.12.0`: `extract` /
  `verify-against` / `ls` now handle zips whose stored members hide
  sizes behind data descriptors (SwissTransfer / Drive-style
  generators) transparently — the walk streams first and continues
  via a ranged-GET central-directory walk at the first unstreamable
  member. Previously these valid zips failed with a misleading
  `zip decode failed` error. `ls` on zip/7z also now reads only the
  archive's index (central directory / trailing header) instead of
  streaming the whole archive — a 1 TB zip lists in seconds.

### Changed

- Bumped the pinned `s3-archive` dependency to `v1.16.1` (from `v1.14.0`).
  No s3-bagit code changes were needed.
- Docs and examples name the backend technology (Ceph RadosGW,
  `--profile ceph-server`) instead of one deployment of it. No
  configuration keys or CLI options changed.
- Bumped the pinned `s3-archive` dependency from `v1.8.0` to `v1.11.0`
  (constraint now `>=1.11.0`). No s3-bagit code changes were needed.
  Notable upstream additions: opt-in resumable `extract` (`--resume`)
  for per-member-seekable formats; `If-Match`-pinned seekable reads (a
  source object overwritten mid-read fails fast instead of mixing
  bytes from two generations); a seekable-zip manifest fallback for
  SwissTransfer/Drive-style zips whose stored members hide sizes
  behind data descriptors; and `open_seekable()`, the canonical 1 MiB
  buffered ranged-GET open.

## [1.4.1] - 2026-07-01

### Fixed

- Bumped the pinned `s3-archive` dependency to `v1.8.0`, which adds:
  resumable per-source-object reads on `create-bag` (a transient
  mid-object connection drop resumes from the byte offset already read
  instead of failing the whole archive); normalized zip member paths
  (backslash → slash, drive stripped, path-traversal guarded), so
  Windows-authored zips extract/verify with correct forward-slash
  member names; and a byte progress bar (%, ETA, speed) on the s3-archive
  CLI. `create-bag`'s tar.gz path also no longer buffers whole source
  objects in memory.

## [1.4.0] - 2026-07-01

### Fixed

- Bumped the pinned `s3-archive` dependency to `v1.7.0`, which makes
  tar/zip `extract` / `verify-against` / `ls` survive transient
  mid-stream S3 connection drops (`ResponseStreamingError` /
  `IncompleteRead`) by resuming via a ranged GET from the byte offset
  already consumed. Previously an isolated hiccup part-way through a
  large archive aborted the whole operation and discarded all progress.

### Changed

- `verify` now reads each payload/tag file once regardless of how many
  manifest algorithms the bag carries (was Nx GETs for N-algorithm
  bags). Single-algorithm bags unchanged. Operators watching
  CloudTrail / S3 access logs will see the GET count drop for
  multi-algorithm bags.

### Documentation

- README and `docs/BAGIT-SPEC.md` now reflect that `.7z` is supported
  for `verify-against` in addition to `extract` and `ls` (only
  `create-bag` rejects `.7z`). The capability was already present via
  `s3-archive>=1.6.0`'s `iter_archive_members` dispatcher and covered
  by tests; only the docs lagged.
