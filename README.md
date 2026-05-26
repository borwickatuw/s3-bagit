# s3-bagit

Extract and verify [BagIt](https://www.rfc-editor.org/rfc/rfc8493) bags
that live in S3, streaming end-to-end so no local disk is required.
Works against AWS S3, UW Libraries' Kopah, MinIO, DigitalOcean Spaces,
Backblaze B2, Wasabi — anything that speaks S3.

```
s3-bagit extract <archive_url> <dest_url>   # serialized bag in S3 → extracted bag in S3
s3-bagit verify  <bag_url>                  # check an already-extracted bag at an S3 prefix
s3-bagit ls      <archive_url>              # peek inside an archive without extracting
s3-bagit config                             # interactive credentials setup
s3-bagit issue   ["short summary"]          # open a pre-filled GitHub issue
```

> Bookmark `https://github.com/borwickatuw/s3-bagit#readme` — this
> README is the canonical user guide.

## Quick start

### 1. Install [uv](https://docs.astral.sh/uv/)

uv is a Python installer-and-runner; one command, no virtualenv
gymnastics.

- **Windows** (PowerShell):

  ```powershell
  irm https://astral.sh/uv/install.ps1 | iex
  ```

- **macOS / Linux**:

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

### 2. Install s3-bagit

```
uv tool install git+https://github.com/borwickatuw/s3-bagit
```

This drops the `s3-bagit` executable into uv's tool-binary directory —
`~/.local/bin` on macOS/Linux, `%USERPROFILE%\.local\bin` on Windows —
so you can run it as `s3-bagit` from any terminal.

If your shell can't find `s3-bagit` after install, that directory
isn't on your `PATH` yet. Fix it once with:

```
uv tool update-shell
```

then **open a new terminal window** so the updated `PATH` takes
effect. The same step is needed on PowerShell, which inherits its
`PATH` from the user environment at launch — re-running `uv tool
update-shell` and starting a fresh PowerShell session does the trick.
(`uv tool dir --bin` prints the exact directory if you'd rather add it
to `PATH` by hand.)

### 3. Configure credentials

```
s3-bagit config
```

The interactive prompt asks for endpoint URL, access key, and secret
key, then writes them to `~/.s3cfg` (s3cmd-compatible). Press Enter on
the endpoint prompt if you're targeting AWS S3.

### 4. Verify the install

```
s3-bagit --version
```

## Common tasks

### Extract a bag

```
s3-bagit extract \
    s3://incoming/bag-2026-05-01.tar.gz \
    s3://preserved/bag-2026-05-01/
```

By default this verifies the extracted bag and exits 0 / `RESULT: VALID`
on success or 1 / `RESULT: INVALID` on failure. Pass `--no-verify` to
skip the post-extract check, or `--dry-run` to list members without
uploading anything.

### Verify a bag

```
s3-bagit verify s3://preserved/bag-2026-05-01/
```

Reports every problem in one pass — checksum mismatches, missing files,
manifest/oxum disagreements — instead of stopping at the first one.

### Look inside an archive without extracting

```
s3-bagit ls s3://incoming/bag-2026-05-01.tar.gz
```

Prints one line per file member plus a summary. Useful as a sanity
check before kicking off a multi-GB extract job ("is this archive
actually a bag? what's the top-level directory called?").

### Report a problem

```
s3-bagit issue "extract hangs on a 50 GB .tar.xz"
```

Opens a GitHub issue page in your browser, pre-filled with your OS,
Python version, and s3-bagit version. If you're in a terminal without a
browser, the URL is also printed for copy-paste.

## Supported archive formats

| Extension(s)            | Notes                                |
| ----------------------- | ------------------------------------ |
| `.tar`                  | uncompressed                         |
| `.tar.gz`, `.tgz`       | gzip                                 |
| `.tar.bz2`, `.tbz2`     | bzip2                                |
| `.tar.xz`, `.txz`       | xz / lzma                            |
| `.zip`                  | streaming via `stream-unzip`         |

`.7z` is **not** supported (see [`docs/BAGIT-SPEC.md`](docs/BAGIT-SPEC.md)
for why). Convert to `.tar.gz` outside s3-bagit if you need to ingest a
`.7z` bag.

## Troubleshooting

**`looks like an archive file, not an extracted-bag prefix`** — you
pointed `verify` at an archive URL (`.tar.gz`, `.zip`, …). `verify`
operates on an already-extracted bag at an S3 prefix. Run
`s3-bagit extract <archive_url> <dest_url>` first — that command
auto-verifies. Still stuck? `s3-bagit issue`.

**`No S3 credentials configured`** — none of the three sources
(`$S3CMD_CONFIG`, `~/.s3cfg`, AWS-style env vars / `~/.aws/credentials`)
was usable. The fastest fix is `s3-bagit config`. The error message
names the `~/.s3cfg` path it actually looked at, which is useful if
`$HOME` is unexpected (containers, CI). Still stuck? `s3-bagit issue`.

**`Cannot detect archive format`** — see "Supported archive formats"
above. If your bag uses a non-listed extension (e.g. `.tar.lz4`), open
an issue. Still stuck? `s3-bagit issue`.

**`RESULT: INVALID` on a bag that looks correct** — most often this is
a wrapping top-level directory inside the archive (members named
`BagName/data/...` instead of `data/...`). s3-bagit preserves member
names as-is; extract to `s3://preserved/BagName/` and verify there.
The other common cause is a `data/.DS_Store` or `__MACOSX/` stowaway
the manifest doesn't list — remove it from the source archive. Still
stuck? `s3-bagit issue`.

**Bytes look fine but the run hangs** — usually an endpoint mismatch
(pointing s3-bagit at AWS when the bag is on Kopah, or vice-versa).
Check `~/.s3cfg`'s `host_base` against the URL you're using.
`s3-bagit config` will rewrite it. Still stuck? `s3-bagit issue`.

## Reference

### Subcommands and flags

| Command                                          | What it does                                                                       |
| ------------------------------------------------ | ---------------------------------------------------------------------------------- |
| `s3-bagit extract <archive_url> <dest_url>`      | Stream-extract a serialized bag from S3 to an S3 prefix. Auto-verifies by default. |
| `s3-bagit extract … --no-verify`                 | Skip the post-extract bag verification.                                            |
| `s3-bagit extract … --dry-run`                   | List members that would be written without uploading anything.                     |
| `s3-bagit verify <bag_url>`                      | Verify an already-extracted bag at an S3 prefix.                                   |
| `s3-bagit ls <archive_url>`                      | Stream-list members of an archive without extracting.                              |
| `s3-bagit config`                                | Interactive credentials setup.                                                     |
| `s3-bagit issue ["brief"]`                       | Open a pre-filled GitHub issue page.                                               |
| `-v`, `--verbose`                                | Show per-file progress.                                                            |
| `--version`                                      | Print the s3-bagit version.                                                        |

### Credential resolution order

s3-bagit looks for S3 credentials in this order:

1. `$S3CMD_CONFIG` — explicit path to an s3cmd INI file. Reads
   `access_key`, `secret_key`, and `host_base` (the endpoint).
2. `~/.s3cfg` — s3cmd's default config location. If you already have
   s3cmd configured against your endpoint, s3-bagit Just Works.
3. boto3's default chain: `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`
   env vars, `~/.aws/credentials`, IAM role, AWS SSO. For a non-AWS
   endpoint, also set `$S3_ENDPOINT_URL`.

(See [`.env.example`](.env.example) for the env-var form.)

### Endpoint-specific notes

**AWS S3** — boto3's default credential chain handles this. No
`$S3_ENDPOINT_URL` needed.

**UW Libraries' Kopah (Ceph RadosGW)** — easiest path: run
`s3-bagit config` and supply `https://s3.kopah.uw.edu` as the
endpoint. Alternative: install `s3cmd` and `s3cmd --configure` —
s3-bagit reads the same `~/.s3cfg`.

**MinIO / DigitalOcean Spaces / Backblaze B2 / Wasabi** — same as
Kopah: `s3-bagit config` with the provider's endpoint URL. The Ceph
content-checksum workaround we apply unconditionally is harmless for
all of them.

### Exit codes

| Code | Meaning                                                                       |
| ---- | ----------------------------------------------------------------------------- |
| 0    | Success.                                                                      |
| 1    | Bag failed verification (or `extract --no-verify` succeeded but `verify` later failed). |
| 2    | Configuration error (missing creds, bad S3 URL, unsupported format).          |

### Running from a clone (contributors)

```
git clone https://github.com/borwickatuw/s3-bagit
cd s3-bagit
make install      # uv sync (dev + test deps)
make test         # uv run pytest
make lint         # ruff check + format --check
make security     # bandit + pip-audit
make run ARGS='extract s3://… s3://…'
```

## Architecture and BagIt conformance

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — streaming model and
  S3-to-S3 design (why a 500 GB bag doesn't need 500 GB of free space
  anywhere).
- [`docs/BAGIT-SPEC.md`](docs/BAGIT-SPEC.md) — RFC 8493 conformance
  notes and scope decisions (including the `.7z` non-support
  rationale).
