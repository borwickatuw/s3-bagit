# Architecture

How s3-bagit moves bytes around without ever touching local disk.

## The constraint

Preservation bags are large — frequently tens of GB, sometimes hundreds
— and they live in S3 (AWS or a compatible service like Kopah/Ceph).
Workstations don't reliably have the free disk space to download a
bag, operate on it, and re-upload. The whole tool only makes sense
if both operations stream **S3 → S3**.

That single requirement drives most of the code shape below.

## Extract

For both formats, the archive object is fetched from S3 with one
`get_object` call, whose `Body` is a `botocore.response.StreamingBody`.
That body is then handed to a streaming decoder, which yields one
member at a time, and each member is pushed to S3 via
`upload_fileobj`.

```
        ┌──────────────────────────┐
        │ S3                       │
        │  s3://src/bag.tar.gz     │
        └────────────┬─────────────┘
                     │  get_object().Body
                     ▼
        ┌──────────────────────────┐
        │ tarfile (or stream_unzip)│
        │   r|gz mode — no seek    │
        └────────────┬─────────────┘
                     │  member-by-member
                     ▼
        ┌──────────────────────────┐
        │ upload_fileobj()         │
        │   per-member multipart   │
        └────────────┬─────────────┘
                     │
                     ▼
        ┌──────────────────────────┐
        │ S3                       │
        │  s3://dest/bag/...       │
        └──────────────────────────┘
```

Nothing is buffered between the decoder and the uploader except the
chunk of the current member being passed through.

### Adapter for non-seekable sources

`boto3.upload_fileobj` dispatches its upload strategy on
`readable()` / `seekable()`. The streaming sources here — `tarfile`'s
`extractfile()` in `r|gz` mode, and `stream_unzip`'s per-member chunk
iterators — are read-once and not seekable, and they don't expose
those two methods at all. Calling `upload_fileobj` on them raw raises
`AttributeError`.

`extract._NonSeekableReader` and `extract._IterableFileobj` add the
two methods (returning `True` and `False` respectively), which steers
boto3 to its `UploadNonSeekableInputManager` path. That path uses
chunked single-part uploads, which is what we want for streaming.

The same shape is used in storage-scripts' `stream_archive` package;
the wrappers here are isolated copies so s3-bagit has no
storage-scripts runtime dependency.

## Create-bag

`create-bag` is the inverse of `extract`: it walks an S3 prefix and
emits a serialized BagIt `.tar.gz` at another S3 key. The same
"nothing on local disk" constraint applies. The challenge is that
BagIt manifests contain SHA-256 (or SHA-512) checksums of every
payload file, so we'd appear to need the bytes twice — once to hash,
once to write into the tar.

We avoid the second pass with two tricks:

1. **Tee the read**: each S3 GET is consumed by a
   `_HashingBody` wrapper whose `read()` updates a `hashlib` hasher
   on the way through. `tarfile.addfile(info, fileobj)` reads exactly
   `info.size` bytes from `fileobj`, so the single S3 GET produces
   both the tar member bytes and the manifest checksum.
2. **Tag files trailing**: RFC 8493 places no ordering requirement
   on members within a serialized bag, so after all `data/` members
   are in the tar we append `bagit.txt`, `bag-info.txt`,
   `manifest-<algo>.txt`, and `tagmanifest-<algo>.txt` (built in
   memory from the accumulated digests).

The compressed tar output goes through an `os.pipe()` to a worker
thread that runs `client.upload_fileobj` against the read end:

```
        ┌──────────────────────────┐
        │ S3 list_objects_v2 +     │
        │ get_object per payload   │
        └────────────┬─────────────┘
                     │  body chunks
                     ▼
        ┌──────────────────────────┐
        │ _HashingBody (tee)       │──► manifest digest
        └────────────┬─────────────┘
                     │  same bytes
                     ▼
        ┌──────────────────────────┐
        │ tarfile w|gz             │
        │  (data/* then tag files) │
        └────────────┬─────────────┘
                     │  os.pipe()
                     ▼
        ┌──────────────────────────┐
        │ worker thread:           │
        │ upload_fileobj(read_end) │
        └────────────┬─────────────┘
                     ▼
        ┌──────────────────────────┐
        │ S3                       │
        │  s3://dest/bag.tar.gz    │
        └──────────────────────────┘
```

Broken-pipe semantics make error propagation clean: if the uploader
dies mid-stream, the writer side sees `BrokenPipeError` on its next
flush. The `create_bag` function joins the worker thread before
returning and re-raises whichever exception fired.

## Verify

Verification is unavoidably **download + hash + compare** for every
file in the bag, because RFC 8493's correctness statement is "every
listed checksum matches the bytes that are there." There is no
metadata-only shortcut that gives the same correctness guarantee.

But "download + hash" is still streaming: each S3 object is read in
64 KiB chunks into the hasher, and the chunks are released immediately
after being hashed. The hasher itself holds only its algorithm-specific
state (~kilobytes), not the file's contents.

```
        ┌──────────────────────────┐
        │ S3                       │
        │  s3://bag/data/x.tif     │
        └────────────┬─────────────┘
                     │  get_object().Body
                     ▼
        ┌──────────────────────────┐
        │ while chunk = read(64K): │
        │   hasher.update(chunk)   │
        └────────────┬─────────────┘
                     │  hexdigest()
                     ▼
        ┌──────────────────────────┐
        │ compare to manifest line │
        │ collect mismatches       │
        └──────────────────────────┘
```

Verify is single-pass per file but **multi-pass over the bag** when
multiple manifest algorithms are present — each manifest is checked
in its own pass, because the algorithm is fixed per pass. For the
typical case of one manifest (`sha256`), that's one pass.

## Verify-against

`verify-against` compares the files under an S3 prefix to the
manifests inside a *serialized* bag, without ever extracting it. It's
the cheapest way to confirm that a flat directory still matches its
archival `.tar.gz` — you avoid paying for a second copy on S3.

Mechanically it stitches together the streaming patterns from
`extract` and `verify`:

```
        ┌──────────────────────────┐
        │ S3 bag.tar.gz            │
        └────────────┬─────────────┘
                     │  one full stream
                     ▼
        ┌──────────────────────────┐
        │ tarfile r|gz             │
        │  capture tag-file bytes  │
        │  drain payload bytes     │
        └────────────┬─────────────┘
                     │  manifest text(s) + bag-info.txt
                     ▼
        ┌──────────────────────────┐
        │ S3 target_prefix/        │
        │  list_objects_v2         │
        └────────────┬─────────────┘
                     │  per file
                     ▼
        ┌──────────────────────────┐
        │ get_object().Body        │
        │  → all required hashers  │
        │    (one read per file)   │
        └────────────┬─────────────┘
                     │  hexdigests
                     ▼
        ┌──────────────────────────┐
        │ compare to manifest line │
        │ collect mismatches       │
        └──────────────────────────┘
```

Two structural notes:

- **Bag root detection.** `verify-against` finds the single
  `bagit.txt` member in the archive and treats everything before its
  final segment as the wrapping directory (`""` if `bagit.txt` is at
  the archive top). Tag-file names are normalized against that root
  so a wrapped bag (`my-bag/manifest-sha256.txt`, what `create-bag`
  produces) and an unwrapped one parse identically.
- **Multi-algorithm bags are still single-pass per file.** For a bag
  with both `manifest-sha256.txt` and `manifest-sha512.txt`, each
  target file is read once and fed through both hashers via
  `_stream_hash_multi`. We do *not* re-issue `get_object` per
  algorithm.

## Why "collect all errors" instead of fail-fast

When Preservation has a bag that doesn't verify, they want to know
*all* the things wrong with it in one run, not one mismatch per
invocation. The `BagVerifyResult` dataclass accumulates errors and
warnings; the CLI prints the full list at the end and exits 1 if
any errors were collected.

Within a single verify pass we still bail early on truly fatal
structural problems (no `bagit.txt`, malformed manifest line) because
continuing past those would produce confusing cascade errors.

## What's deliberately NOT here

- **No local-disk fallback.** If you find yourself wanting one, the
  bag is probably small enough to use plain `aws s3 cp` + local
  `bagit-python`.
- **No multi-bag batching.** One bag per invocation. Operators who
  need batch behavior can wrap s3-bagit in a shell loop;
  parallelizing inside a single process would add complexity without
  buying much, because the bottleneck is S3 throughput.
- **No subprocess shelling out to s3cmd.** Everything goes through
  boto3 against the same endpoint. Operators inherit s3cmd's INI
  file for credentials, but s3cmd itself doesn't run.
