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
