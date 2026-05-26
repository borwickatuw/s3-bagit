# s3-bagit

Extract and verify [BagIt](https://www.rfc-editor.org/rfc/rfc8493) bags
that live in S3, streaming end-to-end so no local disk is required.
Works against AWS S3, UW Libraries' Kopah, MinIO, DigitalOcean Spaces,
Backblaze B2, Wasabi — anything that speaks S3.

```
s3-bagit config                                          # interactive credentials setup
s3-bagit extract        <archive_url> <dest_url>         # serialized bag in S3 → extracted bag in S3
s3-bagit create-bag     <src_url>     <dest_archive_url> # S3 prefix → BagIt .tar.gz in S3
s3-bagit verify         <bag_url>                        # check an already-extracted bag at an S3 prefix
s3-bagit verify-against <archive_url> <target_url>       # check files at a prefix vs. the manifests in a serialized bag
s3-bagit ls             <archive_url>                    # peek inside an archive without extracting
s3-bagit issue          ["short summary"]                # open a pre-filled GitHub issue
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

### 2. Make uv's tool directory available to your shell

```
uv tool update-shell
```

This adds uv's tool-binary directory (`~/.local/bin` on macOS/Linux,
`%USERPROFILE%\.local\bin` on Windows) to your `PATH`. **Close the
current terminal and open a fresh one** so the change takes effect —
shells, including PowerShell, only read `PATH` at launch.

### 3. Install s3-bagit

```
uv tool install git+https://github.com/borwickatuw/s3-bagit
```

### 4. Configure credentials

```
s3-bagit config
```

The interactive prompt asks for endpoint URL, access key, and secret
key, then writes them to `~/.s3cfg` (s3cmd-compatible). Press Enter on
the endpoint prompt if you're targeting AWS S3.

### 5. Verify the install

```
s3-bagit --version
```

You should see a version string like `s3-bagit 0.2.0`. If you see
`command not found` instead, step 2 didn't take effect — close every
terminal window and open a fresh one.

## Common tasks

The examples below all use the same fictional paths
(`s3://test-bucket/path/to/bag.tgz` for the serialized bag,
`s3://test-bucket/path/to/extracted/bag` for the extracted version) so
you can read them as a single end-to-end story.

### Configure credentials

```
s3-bagit config
```

Run this once on each workstation, then any time the endpoint or keys
change. If `~/.s3cfg` already exists, the prompt offers to keep it
unchanged instead of redoing everything.

### Extract a bag

```
s3-bagit extract s3://test-bucket/path/to/bag.tgz s3://test-bucket/path/to/extracted/bag
```

By default this verifies the extracted bag and exits 0 / `RESULT: VALID`
on success or 1 / `RESULT: INVALID` on failure. Pass `--no-verify` to
skip the post-extract check, or `--dry-run` to list members without
uploading anything.

### Create a bag from an S3 prefix

```
s3-bagit create-bag --bag-name my-bag \
    s3://test-bucket/incoming/source-dir/ \
    s3://test-bucket/bags/my-bag.tar.gz
```

Streams every object under the source prefix into a serialized BagIt
`.tar.gz` at the destination key. `--bag-name` is required and becomes
the top-level directory inside the archive (the bag root). Each
payload object is read from S3 exactly once: its bytes are hashed for
the manifest and pushed into the tar simultaneously, and the
compressed output streams through a pipe straight into a multipart
upload. The four tag files (`bagit.txt`, `bag-info.txt`,
`manifest-sha256.txt`, `tagmanifest-sha256.txt`) are appended to the
tar after the payload — see
[`docs/BAGIT-SPEC.md`](docs/BAGIT-SPEC.md#tag-file-ordering-in-create-bag-archives)
for why that's spec-conformant.

Options:

- `--algorithm sha256|sha512` (default `sha256`).
- `--bag-info "LABEL=VALUE"` (repeatable) adds a label to
  `bag-info.txt`. A user-supplied label overrides the default for
  `Bag-Software-Agent`, `Bagging-Date`, or `Payload-Oxum`.

The destination URL must end in `.tar.gz` or `.tgz` — `create-bag`
only produces gzip-compressed tar archives. An empty source prefix is
treated as an operator mistake and errors out (exit code 1) rather
than silently writing a zero-file bag.

### Verify a bag

```
s3-bagit verify s3://test-bucket/path/to/extracted/bag
```

Reports every problem in one pass — checksum mismatches, missing files,
manifest/oxum disagreements — instead of stopping at the first one.

### Verify a flat directory against a serialized bag

```
s3-bagit verify-against \
    s3://test-bucket/path/to/bag.tgz \
    s3://test-bucket/path/to/source-dir/
```

Use this when you want to confirm that a directory of files still
matches its archival `.tar.gz`, without extracting the bag. The target
prefix is treated as **flat** (the same shape `create-bag` consumes):
the bag's manifest entry `data/foo.txt` is checked against
`s3://test-bucket/path/to/source-dir/foo.txt`. Errors are collected
and reported together — missing files, mismatched checksums, target
files not listed in any manifest, Payload-Oxum disagreement — and
the exit code is 0 for `RESULT: VALID` and 1 for `RESULT: INVALID`,
same as `verify`.

**Cost:** the bag's `.tar.gz` is streamed end-to-end (tar has no
index, so we read the whole archive to find the manifest), and every
file under the target prefix is stream-hashed once. Multi-algorithm
bags (e.g. sha256 + sha512) still cost a single read per target file
because every hasher is fed in one pass.

If your target prefix's URL contains `/data/`, `verify-against` will
emit a warning — that shape almost always means you have an extracted
bag and should be using plain `verify` instead.

### Look inside an archive without extracting

```
s3-bagit ls s3://test-bucket/path/to/bag.tgz
```

Prints one line per file member plus a summary.

**Caveat — `ls` is not a cheap operation.** Tar archives have no
central index, so listing members means streaming the whole archive
just to read each header. Our zip path streams it too. For a 100 GB
archive, `ls` downloads 100 GB. To peek at just the top-level
structure, pipe to `head` and Ctrl-C once you've seen what you need:

```
s3-bagit ls s3://test-bucket/path/to/bag.tgz | head
```

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
names as-is; extract to `s3://test-bucket/path/to/extracted/BagName/`
and verify there. The other common cause is a `data/.DS_Store` or
`__MACOSX/` stowaway the manifest doesn't list — remove it from the
source archive. Still stuck? `s3-bagit issue`.

**Bytes look fine but the run hangs** — usually an endpoint mismatch
(pointing s3-bagit at AWS when the bag is on Kopah, or vice-versa).
Check `~/.s3cfg`'s `host_base` against the URL you're using.
`s3-bagit config` will rewrite it. Still stuck? `s3-bagit issue`.

## Reference

### Getting help on any command

The CLI itself is the authoritative reference. Every subcommand and
flag is documented there:

```
s3-bagit --help              # top-level: list subcommands + global flags
s3-bagit <subcommand> --help # per-subcommand: positional args + flags
```

For example, `s3-bagit extract --help` shows the exact form of
`--no-verify`, `--dry-run`, and the expected URLs.

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
| 130  | Cancelled by Ctrl-C.                                                          |

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
