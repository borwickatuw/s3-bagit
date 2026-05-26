# Operations guide

For operators using `s3-bagit` day-to-day.

## Setup (one-time)

1. **Install uv**, if you don't have it:

   ```
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Get your S3 credentials**. s3-bagit resolves them in this order:

   1. `$S3CMD_CONFIG` — explicit path to an s3cmd INI file. Reads
      access_key, secret_key, and host_base (the endpoint).
   2. `~/.s3cfg` — s3cmd's default config location. If you already
      have s3cmd configured against your S3 endpoint, s3-bagit will
      Just Work with zero extra setup.
   3. boto3's default credential chain: `~/.aws/credentials`,
      `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` env vars, IAM
      role, AWS SSO, etc. For a non-AWS endpoint (Kopah, MinIO,
      DigitalOcean Spaces, …), also set `S3_ENDPOINT_URL`.

   Set these via your shell rc file, a per-project `.env` (s3-bagit
   reads `./.env` if present), or your CI's secret store.

3. **Verify it works:**

   ```
   uvx --from git+https://github.com/borwickatuw/s3-bagit s3-bagit --version
   ```

## Extract a bag

```
s3-bagit extract s3://incoming/bag-001.tar.gz s3://preserved/bag-001/
```

- The destination prefix should be empty (or not exist yet). Existing
  objects under it will be **overwritten** without warning if they
  share a key with an archive member.
- The destination ends in `/` because the archive's members are
  expanded under it. `s3-bagit` normalizes a missing trailing slash.

By default, after the extract finishes, s3-bagit verifies the
extracted bag. If verification passes the command exits 0 and prints
`RESULT: VALID`. If it fails the command exits 1 and prints
`RESULT: INVALID` with the specific manifest mismatches.

Skip verification with `--no-verify` (faster, but you should still run
`verify` separately before considering the bag preserved).

## Verify a bag

```
s3-bagit verify s3://preserved/bag-001/
```

What it checks (per RFC 8493 §3):

- `bagit.txt` exists and declares a known BagIt-Version (0.97 or 1.0).
- At least one `manifest-<algorithm>.txt` exists.
- Every entry in every payload manifest points at an existing file
  under `data/` whose checksum matches.
- Every file under `data/` is listed in every payload manifest (no
  stowaways).
- Every `tagmanifest-<algorithm>.txt` entry points at an existing
  non-payload file whose checksum matches.
- `Payload-Oxum` in `bag-info.txt`, if present, equals
  `<total-bytes>.<file-count>` over `data/`.
- `fetch.txt`, if present and non-empty, causes a hard failure
  (s3-bagit does not yet handle fetched bags).

Each check is reported once on stdout; errors do not short-circuit, so
one run shows you everything that's wrong.

## Exit codes

| Code | Meaning                                                |
| ---- | ------------------------------------------------------ |
| 0    | Success.                                               |
| 1    | Bag failed verification (or `extract --no-verify` succeeded but a follow-up `verify` failed). |
| 2    | Configuration error (missing creds, bad S3 URL, unsupported format). |

## Troubleshooting

**`looks like an archive file, not an extracted-bag prefix`** — you
pointed `verify` at a `.tar.gz` / `.tgz` / `.zip` / `.7z` URL. `verify`
operates on an extracted bag (a directory of files at an S3 prefix),
not on the serialized archive. Run `s3-bagit extract <archive_url>
<dest_url>` first — that command auto-verifies the result.

**`No S3 credentials configured`** — none of the three sources
(`$S3CMD_CONFIG`, `~/.s3cfg`, boto3 default chain) was usable. The
error message names the `~/.s3cfg` path it actually looked at, which
is useful if `$HOME` is unexpected (containers, CI). See
`.env.example`.

**`Cannot detect archive format`** — s3-bagit only handles
`.tar.gz`, `.tgz`, and `.zip`. `.7z` raises a specific error pointing
at `docs/BAGIT-SPEC.md`.

**`XAmzContentSHA256Mismatch`** — should not occur (the client sets
`request_checksum_calculation="when_required"` unconditionally, which
satisfies both Ceph RadosGW and AWS S3). If you see this, the boto3
configuration in `src/s3_bagit/s3_client.py` regressed. Open an issue.

**A correct-looking bag verifies as INVALID** — check for:

- A wrapping top-level directory inside the archive (e.g. members
  named `BagName/data/...` instead of `data/...`). s3-bagit preserves
  member names as-is; if the archive wraps the bag, you'll need to
  extract to `s3://preserved/BagName/` and verify there.
- A `data/.DS_Store` or `__MACOSX/` stowaway that isn't in the
  manifest. Remove it from the source archive before extracting.

## Endpoint-specific notes

**AWS S3** — boto3's default credential chain handles this. No
`S3_ENDPOINT_URL` needed.

**UW Libraries' Kopah (Ceph RadosGW)** — easiest path: install
`s3cmd`, run `s3cmd --configure` to write `~/.s3cfg`, and s3-bagit
will pick up everything (credentials + endpoint) from there.
Alternatively, set `S3_ENDPOINT_URL=https://s3.kopah.uw.edu` plus
AWS-style `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.

**MinIO / DigitalOcean Spaces / Backblaze B2 / Wasabi** — same as
Kopah: either configure `~/.s3cfg` or set `S3_ENDPOINT_URL` plus
AWS-style credentials. These deployments all speak the S3 protocol
and the Ceph checksum workaround we apply is harmless for them.
