# Operations guide

For the Preservation team using `kopah-bagit` day-to-day.

## Setup (one-time)

1. **Install uv**, if you don't have it:

   ```
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Get your Kopah credentials**. Either:

   - You already use `s3cmd` against Kopah: set `S3CMD_CONFIG` to the
     path of your `.s3cfg` file. That's the canonical option.
   - You don't use `s3cmd`: set `KOPAH_ACCESS_KEY`, `KOPAH_SECRET_KEY`,
     and `KOPAH_ENDPOINT` directly.

   Either via your shell rc file, a per-project `.env` (kopah-bagit
   reads `./.env` if present), or your CI's secret store.

3. **Verify it works:**

   ```
   uvx --from git+https://github.com/uwlibrary/kopah-bagit kopah-bagit --version
   ```

## Extract a bag

```
kopah-bagit extract s3://incoming/bag-001.tar.gz s3://preserved/bag-001/
```

- The destination prefix should be empty (or not exist yet). Existing
  objects under it will be **overwritten** without warning if they
  share a key with an archive member.
- The destination ends in `/` because the archive's members are
  expanded under it. `kopah-bagit` normalizes a missing trailing slash.

By default, after the extract finishes, kopah-bagit verifies the
extracted bag. If verification passes the command exits 0 and prints
`RESULT: VALID`. If it fails the command exits 1 and prints
`RESULT: INVALID` with the specific manifest mismatches.

Skip verification with `--no-verify` (faster, but you should still run
`verify` separately before considering the bag preserved).

## Verify a bag

```
kopah-bagit verify s3://preserved/bag-001/
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
  (kopah-bagit does not yet handle fetched bags).

Each check is reported once on stdout; errors do not short-circuit, so
one run shows you everything that's wrong.

## Exit codes

| Code | Meaning                                                |
| ---- | ------------------------------------------------------ |
| 0    | Success.                                               |
| 1    | Bag failed verification (or `extract --no-verify` succeeded but a follow-up `verify` failed). |
| 2    | Configuration error (missing creds, bad S3 URL, unsupported format). |

## Troubleshooting

**`No Kopah credentials configured`** — neither `S3CMD_CONFIG` nor the
three `KOPAH_*` env vars are set. See `.env.example`.

**`Cannot detect archive format`** — kopah-bagit only handles
`.tar.gz`, `.tgz`, and `.zip`. `.7z` raises a specific error pointing
at `docs/BAGIT-SPEC.md`.

**`XAmzContentSHA256Mismatch`** — should not occur (the client is
configured for Ceph). If you see this, the boto3 configuration in
`src/kopah_bagit/kopah_client.py` regressed. Open an issue.

**A correct-looking bag verifies as INVALID** — check for:

- A wrapping top-level directory inside the archive (e.g. members
  named `BagName/data/...` instead of `data/...`). kopah-bagit
  preserves member names as-is; if the archive wraps the bag, you'll
  need to extract to `s3://preserved/BagName/` and verify there.
- A `data/.DS_Store` or `__MACOSX/` stowaway that isn't in the
  manifest. Remove it from the source archive before extracting.
