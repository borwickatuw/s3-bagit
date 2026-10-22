"""Stream a directory at an S3 prefix into a serialized BagIt ``.tar.gz`` at another S3 key.

The flow is single-pass per payload object:

1. Walk the source prefix with ``list_objects_v2``.
2. For each object, GET its body once, tee the bytes into both a
   ``hashlib`` hasher and ``tarfile.addfile`` for a ``<bag>/data/<rel>``
   member. After all payload objects are written, append the four tag
   files (``bagit.txt``, ``bag-info.txt``, ``manifest-<algo>.txt``,
   ``tagmanifest-<algo>.txt``) to the same tar — RFC 8493 places no
   ordering requirement on serialized bags, so tag-files-trailing is
   conformant and lets us avoid re-reading any payload object.
3. The tar's compressed output goes through an OS pipe; a worker thread
   ``upload_fileobj``s the read end to S3 in parallel. Nothing is staged
   on local disk.

See ``docs/ARCHITECTURE.md`` for the diagram.
"""

import contextlib
import datetime
import hashlib
import io
import os
import tarfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from s3_archive.hashing import HashingTap, body_chunks
from s3_archive.iter import PipeReader
from s3_archive.list import list_objects

from s3_bagit import __version__
from s3_bagit.exceptions import BagError
from s3_bagit.log_config import get_logger
from s3_bagit.verify import ProgressCallback

log = get_logger(__name__)

_PIPE_READ_CHUNK = 65536

# RFC 8493 §2.1.3: only LF, CR, and the percent escape itself are
# percent-encoded in manifest paths. Order matters — encode "%" first
# so we don't double-encode the "%0A"/"%0D" we just introduced.
_PATH_ESCAPES = (("%", "%25"), ("\n", "%0A"), ("\r", "%0D"))


def _encode_manifest_path(path: str) -> str:
    for char, escape in _PATH_ESCAPES:
        path = path.replace(char, escape)
    return path


def _make_tarinfo(name: str, size: int, mtime: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = size
    info.mtime = mtime
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _build_bagit_txt() -> bytes:
    return b"BagIt-Version: 1.0\nTag-File-Character-Encoding: UTF-8\n"


def _build_bag_info_txt(
    *,
    total_octets: int,
    file_count: int,
    extra: list[tuple[str, str]],
) -> bytes:
    """Build ``bag-info.txt``. *extra* takes precedence over our defaults for the same label."""
    extra_labels = {label for label, _ in extra}
    lines: list[str] = []
    if "Bag-Software-Agent" not in extra_labels:
        lines.append(f"Bag-Software-Agent: s3-bagit {__version__}")
    if "Bagging-Date" not in extra_labels:
        lines.append(f"Bagging-Date: {datetime.date.today().isoformat()}")
    if "Payload-Oxum" not in extra_labels:
        lines.append(f"Payload-Oxum: {total_octets}.{file_count}")
    for label, value in extra:
        lines.append(f"{label}: {value}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _build_manifest_txt(entries: list[tuple[str, str]]) -> bytes:
    """Each entry is ``(hex_digest, bag_relative_path)``."""
    return "".join(f"{digest}  {_encode_manifest_path(path)}\n" for digest, path in entries).encode(
        "utf-8"
    )


def _build_tagmanifest_txt(
    algorithm: str,
    tag_files: list[tuple[str, bytes]],
) -> bytes:
    """Hash each tag-file body and emit a manifest line per file."""
    entries: list[tuple[str, str]] = []
    for name, body in tag_files:
        # usedforsecurity=False: BagIt manifests are fixity checksums, and
        # md5/sha1 are legal RFC 8493 algorithms. The keyword records that in
        # Python (and keeps the call working in FIPS mode) instead of taking a
        # bandit suppression — SECURITY.md §4a.
        hasher = hashlib.new(algorithm, usedforsecurity=False)
        hasher.update(body)
        entries.append((hasher.hexdigest(), name))
    return _build_manifest_txt(entries)


def _add_payload_members(
    tar: tarfile.TarFile,
    src_client,
    src_bucket: str,
    objects: list[dict[str, Any]],
    *,
    bag_name: str,
    algorithm: str,
    mtime: int,
    verbose: bool,
    on_progress: ProgressCallback | None,
) -> tuple[list[tuple[str, str]], int, int]:
    """Tee every source object into *tar* and into a manifest checksum.

    Returns ``(manifest_entries, total_octets, file_count)``. One S3 GET
    per object: :class:`HashingTap` feeds ``tar.addfile`` and the hasher
    from the same byte stream.
    """
    manifest_entries: list[tuple[str, str]] = []
    total_octets = 0
    file_count = 0
    for obj in objects:
        rel = obj["RelativePath"]
        size = obj["Size"]
        if verbose:
            log.info("  %s (%d bytes)", rel, size)
        body = src_client.get_object(Bucket=src_bucket, Key=obj["Key"])["Body"]
        tap = HashingTap(body_chunks(body), algorithms=(algorithm,))
        tarinfo = _make_tarinfo(f"{bag_name}/data/{rel}", size, mtime)
        tar.addfile(tarinfo, tap)
        manifest_entries.append((tap.hexdigests()[algorithm], f"data/{rel}"))
        total_octets += size
        file_count += 1
        if on_progress is not None:
            on_progress(rel, size)
    return manifest_entries, total_octets, file_count


def _write_tag_members(
    tar: tarfile.TarFile,
    manifest_entries: list[tuple[str, str]],
    *,
    bag_name: str,
    algorithm: str,
    mtime: int,
    total_octets: int,
    file_count: int,
    bag_info: list[tuple[str, str]],
) -> None:
    """Append the four BagIt tag files to *tar*, built in memory from accumulated state.

    RFC 8493 places no ordering requirement on serialized bags, so writing
    the tag files after the payload is conformant — and it is what lets us
    hash every payload object exactly once.
    """
    bagit_body = _build_bagit_txt()
    bag_info_body = _build_bag_info_txt(
        total_octets=total_octets,
        file_count=file_count,
        extra=bag_info,
    )
    manifest_body = _build_manifest_txt(manifest_entries)
    tag_files = [
        ("bagit.txt", bagit_body),
        ("bag-info.txt", bag_info_body),
        (f"manifest-{algorithm}.txt", manifest_body),
    ]
    tagmanifest_body = _build_tagmanifest_txt(algorithm, tag_files)
    tag_files.append((f"tagmanifest-{algorithm}.txt", tagmanifest_body))

    for name, body_bytes in tag_files:
        tarinfo = _make_tarinfo(f"{bag_name}/{name}", len(body_bytes), mtime)
        tar.addfile(tarinfo, io.BytesIO(body_bytes))


@contextlib.contextmanager
def _tar_streamed_to_s3(dst_client, dest_bucket: str, dest_key: str) -> Iterator[tarfile.TarFile]:
    """Yield a tar whose compressed bytes upload to S3 as they are produced.

    The tar's output goes through an OS pipe; a worker thread
    ``upload_fileobj``s the read end. Nothing is staged on local disk.
    ``w|gz`` is tarfile's streaming-write mode: header + data emitted
    sequentially, no seeking, which pairs naturally with the pipe.
    """
    rfd, wfd = os.pipe()

    def _upload() -> None:
        with os.fdopen(rfd, "rb", buffering=_PIPE_READ_CHUNK) as r:
            dst_client.upload_fileobj(PipeReader(r), dest_bucket, dest_key)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="s3-bagit-upload") as pool:
        upload = pool.submit(_upload)
        try:
            with (
                os.fdopen(wfd, "wb") as wfile,
                tarfile.open(fileobj=wfile, mode="w|gz") as tar,
            ):
                yield tar
        finally:
            # `Future.result()` joins the uploader and re-raises whatever
            # the upload thread raised, on this thread. Running it in
            # `finally` keeps the old precedence: when the writer dies of
            # BrokenPipeError because the reader-end went away, the
            # upload's real cause is raised and the write-side symptom
            # rides along as its `__context__`.
            upload.result()


def _validate_bag_args(bag_name: str, algorithm: str) -> None:
    """Reject a bad bag name or hash algorithm before any S3 traffic happens."""
    if not bag_name or "/" in bag_name:
        raise BagError(f"--bag-name must be a single path segment, got {bag_name!r}")
    try:
        hashlib.new(algorithm, usedforsecurity=False)
    except (ValueError, TypeError) as exc:
        raise BagError(f"Unknown hash algorithm {algorithm!r}: {exc}") from exc


def create_bag(
    src_client,
    dst_client,
    src_bucket: str,
    src_prefix: str,
    dest_bucket: str,
    dest_key: str,
    *,
    bag_name: str,
    algorithm: str = "sha256",
    bag_info: list[tuple[str, str]] | None = None,
    verbose: bool = False,
    on_progress: ProgressCallback | None = None,
) -> tuple[int, int]:
    """Stream ``s3://src_bucket/src_prefix/`` into a BagIt ``.tar.gz`` at ``s3://dest_bucket/dest_key``.

    *src_client* reads the per-object source bodies; *dst_client*
    writes the single archive object. They may be the same boto3
    client (single-endpoint workflows) or two clients pointed at
    different endpoints — see s3-archive's ``client_for`` resolver.

    Returns ``(payload_file_count, payload_total_octets)``.

    The function raises :class:`BagError` if the source prefix is empty —
    callers expect "create-bag of nothing" to be an operator mistake, not
    a silent zero-file bag.
    """
    _validate_bag_args(bag_name, algorithm)

    bag_info = bag_info or []
    # ``list_objects`` sorts by Key, which for a flat prefix is order-
    # equivalent to sorting by RelativePath (RelativePath = Key with
    # the same constant prefix stripped). Either way the manifest is
    # byte-deterministic across runs, which matters for operators
    # diffing two bag creations.
    objects = list_objects(src_client, src_bucket, src_prefix, sort=True)
    if not objects:
        raise BagError(f"Source prefix s3://{src_bucket}/{src_prefix} is empty; nothing to bag.")

    log.info(
        "Creating bag '%s' from s3://%s/%s (%d files) -> s3://%s/%s",
        bag_name,
        src_bucket,
        src_prefix,
        len(objects),
        dest_bucket,
        dest_key,
    )

    mtime = int(time.time())

    with _tar_streamed_to_s3(dst_client, dest_bucket, dest_key) as tar:
        manifest_entries, total_octets, file_count = _add_payload_members(
            tar,
            src_client,
            src_bucket,
            objects,
            bag_name=bag_name,
            algorithm=algorithm,
            mtime=mtime,
            verbose=verbose,
            on_progress=on_progress,
        )
        _write_tag_members(
            tar,
            manifest_entries,
            bag_name=bag_name,
            algorithm=algorithm,
            mtime=mtime,
            total_octets=total_octets,
            file_count=file_count,
            bag_info=bag_info,
        )

    log.info(
        "Bag created: %d payload files, %d bytes -> s3://%s/%s",
        file_count,
        total_octets,
        dest_bucket,
        dest_key,
    )
    return file_count, total_octets
