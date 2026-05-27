# BagIt conformance notes

s3-bagit implements the verification checks from [RFC 8493](https://www.rfc-editor.org/rfc/rfc8493)
(BagIt File Packaging Format v1.0). This document records the design
decisions where the spec leaves room or where s3-bagit makes
deliberate scope choices.

## Supported BagIt versions

`bagit.txt`'s `BagIt-Version` is checked against the set `{"0.97", "1.0"}`.
Anything else triggers a warning (not a hard error). The two known
versions differ in encoding and manifest-completeness language but
share the structural shape s3-bagit verifies.

## Supported serialization formats

RFC 8493 §4.1.2 names **TAR**, **ZIP**, and **TGZ**:

> Common serialization formats include TAR, ZIP, and TAR with GZIP
> compression (TGZ).

s3-bagit handles all of those plus several tar-with-compression
variants and (BagIt-non-standard) `.7z`:

| Extension(s)            | Notes                                                |
| ----------------------- | ---------------------------------------------------- |
| `.tar`                  | uncompressed                                         |
| `.tar.gz`, `.tgz`       | gzip — the format named in RFC 8493 §4.1.2 as "TGZ"  |
| `.tar.bz2`, `.tbz2`     | bzip2                                                |
| `.tar.xz`, `.txz`       | xz / lzma                                            |
| `.tar.zst`              | zstandard                                            |
| `.zip`                  | streaming via `stream-unzip`                         |
| `.7z`                   | seekable-S3 + py7zr; see note below                  |

All formats share the same streaming dispatch in
[`s3_archive.members.iter_archive_members`](https://github.com/borwickatuw/s3-archive) —
tar variants via `tarfile.open(fileobj=..., mode=m)`, zip via
`stream-unzip`, 7z via py7zr driven by a seekable-S3 adapter (range
GETs + tail prefetch, no local disk).

### A note on `.7z`

`.7z` is **not** a BagIt-standard serialization. RFC 8493 §4.1.2's
list of TAR / ZIP / TGZ is informative, not normative, so a `.7z` bag
is not RFC-non-conformant — but a downstream tool that only knows the
three named formats will not accept it. Use `.7z` knowingly.

The implementation reaches into the underlying s3-archive library,
which handles the "7z's index lives at the tail" problem with a
seekable-S3 adapter (ranged GETs + a one-time tail prefetch); the
"no local disk" promise still holds. See
[`s3-archive/docs/ARCHITECTURE.md`](https://github.com/borwickatuw/s3-archive/blob/main/docs/ARCHITECTURE.md)
§ ".7z — the exception that proves the rule" for the design.

`create-bag` does not emit `.7z` — the SignatureHeader at byte 0
references metadata at the tail, which is incompatible with streaming
multipart uploads. Bag creation stays `.tar.gz`-only.

## Manifest algorithms

`manifest-<algorithm>.txt` and `tagmanifest-<algorithm>.txt` are
parsed for any algorithm name `hashlib.new()` accepts. The four
common BagIt choices — `md5`, `sha1`, `sha256`, `sha512` — pass
without warning. Anything else issues a warning but is still
attempted; if `hashlib.new()` rejects the name, the run fails with a
descriptive error.

Bandit's `B324` warning about MD5/SHA1 is suppressed for `verify.py`
because BagIt expressly allows both for backward compatibility with
older bags.

## Tag-file ordering in `create-bag` archives

Bags emitted by `s3-bagit create-bag` place all `data/` payload
members first, then `bagit.txt`, `bag-info.txt`,
`manifest-<algo>.txt`, and `tagmanifest-<algo>.txt`. This is
deliberate — keeping tag files trailing means `create-bag` can be
single-pass over each payload object (the manifest is built from
hashes computed on the way through). RFC 8493 §4 describes a
serialized bag as a packaged form of the bag directory but places
no ordering requirement on members within the serialization, so
tag-files-trailing is spec-conformant. Tools that walk the archive
in order (`tar tvf`, `s3-bagit ls`) will list payload first.

## What s3-bagit verifies

Per RFC 8493 §3:

- ✅ `bagit.txt` declares a known version.
- ✅ At least one payload manifest exists.
- ✅ Every payload manifest entry exists under `data/` and matches its
  checksum.
- ✅ Every file under `data/` is listed in **every** payload manifest
  (RFC 8493 §3 invariant).
- ✅ Every tag manifest entry exists outside `data/` and matches its
  checksum.
- ✅ `Payload-Oxum` in `bag-info.txt`, if present, equals
  `<total-octets>.<file-count>` over `data/`.

## What s3-bagit does NOT verify

- **`fetch.txt`** with non-empty content. RFC 8493 §2.2.3 allows bags
  to defer some payload files to URLs in `fetch.txt`. s3-bagit
  treats a non-empty `fetch.txt` as a hard error rather than silently
  reporting "missing files" or attempting to fetch from arbitrary
  URLs (out of scope, security-sensitive).

- **Tag-file character encoding.** RFC 8493 §2.1.2 specifies
  `Tag-File-Character-Encoding`. s3-bagit assumes UTF-8 for all tag
  files (the only encoding any current bag uses in practice). A
  non-UTF-8 tag file would surface as a `UnicodeDecodeError` from
  `_read_text`.

- **`bag-info.txt` semantics** beyond `Payload-Oxum`. Standard fields
  like `Source-Organization`, `Bagging-Date`, `External-Identifier`
  are parsed but their *values* aren't validated. Adding validation
  for any of them is straightforward in `verify._check_payload_oxum`
  if the Preservation team wants it.

## References

- [RFC 8493 — The BagIt File Packaging Format (V1.0)](https://www.rfc-editor.org/rfc/rfc8493)
- [Library of Congress `bagit-python`](https://github.com/LibraryOfCongress/bagit-python) — reference
  implementation for the on-disk case (s3-bagit borrows none of
  its code but does follow its interpretation of ambiguous spec lines).
