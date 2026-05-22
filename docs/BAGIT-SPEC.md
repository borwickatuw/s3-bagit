# BagIt conformance notes

kopah-bagit implements the verification checks from [RFC 8493](https://www.rfc-editor.org/rfc/rfc8493)
(BagIt File Packaging Format v1.0). This document records the design
decisions where the spec leaves room or where kopah-bagit makes
deliberate scope choices.

## Supported BagIt versions

`bagit.txt`'s `BagIt-Version` is checked against the set `{"0.97", "1.0"}`.
Anything else triggers a warning (not a hard error). The two known
versions differ in encoding and manifest-completeness language but
share the structural shape kopah-bagit verifies.

## Supported serialization formats

RFC 8493 §4.1.2 names **TAR**, **ZIP**, and **TGZ**:

> Common serialization formats include TAR, ZIP, and TAR with GZIP
> compression (TGZ).

kopah-bagit handles `.tar.gz`, `.tgz`, and `.zip`. Plain uncompressed
`.tar` is not currently in scope — adding it would be a small change to
`src/kopah_bagit/s3_url.detect_format` and a new `extract_tar`
function (the standard library's `tarfile.open(..., mode="r|")`
streams uncompressed tar the same way the gzip variant does).

### Why not 7z?

The Preservation team sometimes uses `.7z` for compression-ratio
reasons. **kopah-bagit does not support it** for v1, and the CLI
raises a specific error pointing here.

Two reasons:

1. **It's not a BagIt-standard format.** RFC 8493 §4.1.2's list is
   informative, not normative, but the working group's deliberate
   choice was the trio of widely-deployed formats.
2. **7z is not stream-friendly.** Like ZIP, 7z stores its index at the
   end of the file — but unlike streaming-ZIP tooling, no mature
   Python library can stream-extract 7z without a seekable input.
   Supporting it would require either downloading the whole archive
   to local disk (against the project's "no local disk" promise) or
   implementing seekable-S3-reads via range requests under py7zr.

If Preservation needs 7z, an option is to do a `.7z` → `.tar.gz`
conversion step outside kopah-bagit on a workstation with disk space,
then run `kopah-bagit extract` on the tar.gz. Or open an issue to
revisit the range-read approach.

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

## What kopah-bagit verifies

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

## What kopah-bagit does NOT verify

- **`fetch.txt`** with non-empty content. RFC 8493 §2.2.3 allows bags
  to defer some payload files to URLs in `fetch.txt`. kopah-bagit
  treats a non-empty `fetch.txt` as a hard error rather than silently
  reporting "missing files" or attempting to fetch from arbitrary
  URLs (out of scope, security-sensitive).

- **Tag-file character encoding.** RFC 8493 §2.1.2 specifies
  `Tag-File-Character-Encoding`. kopah-bagit assumes UTF-8 for all tag
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
  implementation for the on-disk case (kopah-bagit borrows none of
  its code but does follow its interpretation of ambiguous spec lines).
