# Single-pass multi-hash in verify

Status: **planning** — work has not started.

## Context

`verify_bag` (in `src/s3_bagit/verify.py`) reads every payload file
once per manifest algorithm. A bag with `manifest-sha256.txt` and
`manifest-sha512.txt` costs **two** S3 GETs per payload file. The
verify-against path (`src/s3_bagit/verify_against.py`) already does
this right — it uses `s3_archive.hashing.stream_hash_object(...,
all_algos)` once per target file and compares to every manifest's
expected hash in one pass.

The asymmetry is historical. `verify` was the original subcommand
and pre-dated the `stream_hash_object` primitive that consolidated
hashing in s3-archive v0.2.0. The work to align it was deferred from
the v0.2.0 plan as a "behavior change; defer" item, because changing
the read pattern is observable to operators monitoring egress / S3
read counts.

After this refactor:

- One S3 GET per payload file regardless of how many manifest
  algorithms the bag carries.
- One S3 GET per tag file regardless of how many tagmanifest
  algorithms the bag carries.
- For single-algorithm bags (the dominant case at UW Libraries),
  zero observable difference — same one GET per file.
- For multi-algorithm bags (e.g. APTrust deposits that carry both
  md5 and sha256), egress drops Nx; wall-clock drops correspondingly.

## Why bother

- **Cost.** UW Libraries' Kopah egress isn't free; multi-algorithm
  verifications today pay 2× or 4× what they need to.
- **Symmetry.** `verify` and `verify-against` should have the same
  read shape — they're verifying the same thing, just against
  different ground-truth sources. Two different read patterns is a
  smell.
- **Code clarity.** Today `_verify_manifest` couples "iterate manifest
  entries" with "hash this one file with this one algorithm." That
  coupling is awkward when you want to share the read cost. After the
  refactor the loop becomes "for each file, hash once, compare per
  manifest" — a more natural shape.

## Overall approach

- **One phase, one commit.** No tag coordination needed — the public
  CLI (`s3-bagit verify`) doesn't change shape, only its per-run S3
  read pattern.
- **Tests are the gate.** The current `tests/test_verify.py` covers
  the multi-algorithm case (`manifest-sha256.txt` + `manifest-sha512.txt`
  on the same bag). After the refactor, that test must still pass.
  Add a new assertion that counts S3 GET calls and confirms it equals
  the file count (not file count × algorithm count).
- **Release note in CHANGELOG.** "verify now reads each file once
  regardless of manifest count" — a one-liner so operators reviewing
  CloudTrail / S3 access logs aren't surprised by the drop in read
  count.

## Per-file changes

### `src/s3_bagit/verify.py`

Replace the per-manifest loop with a per-file loop that fans out to
every algorithm in use.

```python
# Before: _verify_manifest is called once per (manifest, algorithm)
#         and does a fresh _stream_hash inside its file loop.
# After: build a {rel: {algo: expected}} map across all manifests,
#        then loop files, single multi-hash, compare per algo.

from s3_archive.hashing import stream_hash_object

def _build_expected_map(
    manifests_by_algo: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    """{rel -> {algo -> expected}} union across all manifest files."""
    out: dict[str, dict[str, str]] = {}
    for algo, entries in manifests_by_algo.items():
        for rel, expected in entries.items():
            out.setdefault(rel, {})[algo] = expected.lower()
    return out

def _verify_files_single_pass(
    client, bucket, bag_prefix,
    expected_map: dict[str, dict[str, str]],
    objects: dict[str, dict],
    result: BagVerifyResult,
    *, payload: bool,
) -> set[str]:
    """Single-pass multi-hash for every file in expected_map."""
    covered: set[str] = set()
    label = "manifest" if payload else "tagmanifest"
    for rel, expected_by_algo in sorted(expected_map.items()):
        covered.add(rel)
        # Same scope checks as the old _verify_manifest.
        if payload and not rel.startswith("data/"):
            result.fail(...)
            continue
        if not payload and rel.startswith("data/"):
            result.fail(...)
            continue
        if rel not in objects:
            result.fail(f"{label}: file listed but not present in bag: {rel!r}")
            continue
        actuals = stream_hash_object(
            client, bucket, objects[rel]["Key"], expected_by_algo.keys()
        )
        for algo, expected in expected_by_algo.items():
            if actuals[algo].lower() != expected:
                result.fail(
                    f"manifest-{algo}.txt: checksum mismatch for {rel!r}: "
                    f"expected {expected}, got {actuals[algo]}"
                )
            else:
                log.debug("%s ok (%s) %s", label, algo, rel)
    return covered
```

Caller becomes:

```python
manifests_by_algo = {algo: _parse_manifest_text(_read_text(...)) for algo, rel in manifests.items()}
payload_expected = _build_expected_map(manifests_by_algo)
_verify_files_single_pass(client, bucket, bag_prefix, payload_expected,
                          objects, result, payload=True)
# Same shape for tagmanifests.
```

`_verify_manifest` and `_stream_hash` are removed (or kept as a
deprecation shim for one release if there's any chance an external
caller imports them — they aren't in any public surface as of
today's grep, so removal is fine).

### `tests/test_verify.py`

Add (or extend an existing) test:

```python
def test_multi_algorithm_bag_reads_each_file_once(s3_client, multi_algo_bag):
    """A bag with sha256 + sha512 manifests must GET each file exactly once."""
    # Wrap s3_client.get_object to count calls per key.
    gets: dict[str, int] = {}
    real_get = s3_client.get_object
    def counting_get(**kwargs):
        gets[kwargs["Key"]] = gets.get(kwargs["Key"], 0) + 1
        return real_get(**kwargs)
    s3_client.get_object = counting_get
    verify_bag(s3_client, "bag-bucket", "mybag/")
    # Every payload file should appear exactly once.
    payload_keys = [k for k in gets if "/data/" in k]
    assert all(count == 1 for count in (gets[k] for k in payload_keys))
```

(Tag-file reads will still appear multiple times — `_read_text` for
the manifest/bag-info parsing is separate from the payload hash
read. That's fine and expected.)

## Behavior changes (the part the v0.2.0 plan deferred for)

- **S3 read count drops Nx for N-algorithm bags.** Operators watching
  CloudTrail or S3 access logs will see fewer GETs. Single-algorithm
  bags unchanged.
- **Order of operations.** Today: hash every file with algo A, then
  every file with algo B. After: hash file 1 with all algos, then
  file 2, etc. Error reporting groups by-file rather than by-
  algorithm; the error _messages_ are unchanged.
- **Error coupling for catastrophically broken files.** Today, a file
  with both sha256 and sha512 mismatches produces two errors from
  two reads. After: two errors from one read. Same error count,
  same messages, but the failure modes are coupled to one S3 GET
  rather than two — if the read itself raises (e.g. S3 timeout),
  you lose visibility into both algorithms at once. Acceptable: a
  failed read is a failed read; the operator retries.
- **Tagmanifest path.** Same refactor applied. Tag files are small;
  the egress saving is negligible, but the symmetry is worth it.

## Risks and watch-items

- **Algorithm subset per manifest.** RFC 8493 permits — and the wild
  has — bags where `manifest-sha256.txt` and `manifest-sha512.txt`
  cover overlapping but not identical file sets. The
  `{rel: {algo: expected}}` map preserves this: a file only listed
  in the sha256 manifest gets hashed with only sha256 (the
  algorithms passed to `stream_hash_object` come from
  `expected_by_algo.keys()`, not the global set). Easy to get
  wrong; cover with a test.
- **Empty algorithm set.** If a file ends up in `expected_map`
  with no algorithms (shouldn't happen, but defensive), `multi_hash`
  with an empty algorithm list returns `{}` and reads zero bytes.
  The caller's loop over `expected_by_algo.items()` is also empty,
  so nothing breaks. Still, assert this can't happen via the
  builder's structure.
- **`_stream_hash` removal.** Internal only; one grep across
  s3-bagit + storage-scripts to confirm no external caller.

## Verification

```bash
cd ~/code/s3-bagit && make check
# Plus: a multi-algorithm fixture round-trip:
uv run python -c "
from moto import mock_aws; import boto3
from s3_bagit.create_bag import create_bag
from s3_bagit.verify import verify_bag
# ... build a bag with manifest-sha256 + manifest-sha512 ...
# verify; assert ok and read-count-per-file == 1
"
```

## Things explicitly NOT in scope

- No public API change. `verify_bag` signature unchanged.
- No new manifest algorithm support — md5/sha1/sha256/sha512 only.
- No change to `verify-against` (already single-pass).
- No tag coordination with s3-archive — this is internal to s3-bagit.

## Cost estimate

~3 hours: refactor, one new test, CHANGELOG note. One commit, one PR.
