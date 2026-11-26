"""Read-only mmap reader for reserved append-only shard slabs (.slab + 256-fanout .idx + .occ)."""

import errno
import hashlib
import io
import mmap
import os
import re
import stat
import struct
import threading
import zlib
from collections.abc import Callable, Iterator
from compression import zstd
from contextlib import suppress
from pathlib import Path
from typing import IO, Any

from managed_ai.platform import (
    OPEN_RO_NOFOLLOW as _OPEN_RO_NOFOLLOW,
)
from managed_ai.platform import (
    check_ordinary_ancestors,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    strict_json_loads,
)

MAX_BYTES = 2 * 1024 * 1024

# Reserved append-only shard slab layout constants (ObjectStore & store.records.pack_slab).
PACK_CURRENT_FORMAT = "managed-ai-pack-current/1"
DEFAULT_SHARD_TARGET_BYTES = 64 * 1024 * 1024
PACK_SLAB_MAGIC = b"MAISLAB1"
PACK_IDX_MAGIC = b"MAIIDX01"
PACK_OCC_MAGIC = b"MAIOCC01"
PACK_FRAME_MAGIC = 0x5346
OCC_TOMBSTONE = 0
OCC_LIVE_RAW = 1
OCC_LIVE_DEFLATE = 2
OCC_SEALED = 3
OCC_LIVE_ZSTD = 4

SLAB_HEADER_STRUCT = struct.Struct("<8sHHIIQ36x")
FRAME_HEADER_STRUCT = struct.Struct("<BBHII32sI")
IDX_HEADER_STRUCT = struct.Struct("<8sHHIIQ4x")
IDX_FANOUT_OFFSET = IDX_HEADER_STRUCT.size
IDX_ENTRIES_OFFSET = IDX_FANOUT_OFFSET + 256 * 4
IDX_ENTRY_STRUCT = struct.Struct("<32sQII")
OCC_HEADER_STRUCT = struct.Struct("<8sHHI")

_PACK_SLAB_NAME_RE = re.compile(r"^pack-[0-9]{6}-[0-9]{4}\.slab$")
_PACK_IDX_NAME_RE = re.compile(r"^pack-[0-9]{6}-[0-9]{4}\.idx$")
_PACK_OCC_NAME_RE = re.compile(r"^pack-[0-9]{6}-[0-9]{4}\.occ[1-9][0-9]*$")
_PACK_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_VALID_OCC_BYTES = b"\x00\x01\x02\x03\x04"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decompress_bounded(stream: IO[bytes], limit: int | None, display: Path) -> bytes:
    """Stream-decompress a zlib payload while strictly enforcing a raw-byte ceiling."""
    decompressor = zlib.decompressobj()
    chunks: list[bytes] = []
    total = 0

    try:
        while True:
            compressed_chunk = stream.read(65536)
            if not compressed_chunk:
                break

            if limit is None:
                out = decompressor.decompress(compressed_chunk)
            else:
                remaining = (limit + 1) - total
                out = decompressor.decompress(compressed_chunk, remaining)

            if out:
                chunks.append(out)
                total += len(out)
                if limit is not None and total > limit:
                    raise ValueError(f"file exceeds {limit} bytes: {display}")

            if decompressor.unconsumed_tail and limit is not None:
                raise ValueError(f"file exceeds {limit} bytes: {display}")

            if decompressor.eof:
                if decompressor.unused_data or stream.read(1):
                    raise ValueError(f"trailing data after compressed object: {display}")
                break

        tail = decompressor.flush() if limit is None else decompressor.flush((limit + 1) - total)
        if tail:
            chunks.append(tail)
            total += len(tail)
            if limit is not None and total > limit:
                raise ValueError(f"file exceeds {limit} bytes: {display}")

        if not decompressor.eof:
            raise ValueError(f"truncated compressed object: {display}")
    except zlib.error as exc:
        raise ValueError(f"corrupt compressed object {display}: {exc}") from None

    return b"".join(chunks)


def decompress_zstd_bounded(payload: bytes, limit: int | None, display: Path) -> bytes:
    """Decompress a single Zstandard frame while strictly enforcing a raw-byte ceiling."""
    decompressor = zstd.ZstdDecompressor()
    try:
        if limit is None:
            out = decompressor.decompress(payload)
        else:
            out = decompressor.decompress(payload, max_length=limit + 1)
            if len(out) > limit or (not decompressor.eof and not decompressor.needs_input):
                raise ValueError(f"file exceeds {limit} bytes: {display}")

        if not decompressor.eof:
            raise ValueError(f"truncated compressed object: {display}")

        if decompressor.unused_data:
            raise ValueError(f"trailing data after compressed object: {display}")
    except zstd.ZstdError as exc:
        raise ValueError(f"corrupt compressed object {display}: {exc}") from None

    return out


def _open_readonly_regular(path: Path) -> tuple[IO[bytes], int]:
    """Open an ordinary non-symlink regular file read-only; returns (stream, size)."""
    if hasattr(os, "O_NOFOLLOW"):
        try:
            fd = os.open(path, _OPEN_RO_NOFOLLOW)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                raise ValueError(f"ordinary pack file required: {path}") from None
            raise

        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"ordinary pack file required: {path}")
            return os.fdopen(fd, "rb"), info.st_size
        except BaseException:
            os.close(fd)
            raise

    try:
        info = path.lstat()
    except OSError:
        raise ValueError(f"ordinary pack file required: {path}") from None

    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary pack file required: {path}")

    return path.open("rb"), info.st_size


def _read_pointer_json(pointer: Path) -> dict[str, Any]:
    stream, size = _open_readonly_regular(pointer)
    with stream:
        if size > MAX_BYTES:
            raise ValueError(f"file exceeds {MAX_BYTES} bytes: {pointer}")
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError(f"file exceeds {MAX_BYTES} bytes: {pointer}")
    return strict_json_loads(raw, pointer, require_object=True)


def _validate_packs_dir(packs_dir: Path, *, allow_missing: bool) -> bool:
    """Validate packs_dir and every ancestor contain no symlink or NTFS reparse point."""
    try:
        dir_info = packs_dir.lstat()
    except FileNotFoundError:
        if allow_missing:
            return False
        raise ValueError(f"ordinary packs directory required: {packs_dir}") from None
    except OSError:
        raise ValueError(f"ordinary packs directory required: {packs_dir}") from None

    if not is_ordinary_dir_stat(dir_info):
        raise ValueError(f"ordinary packs directory required: {packs_dir}")

    check_ordinary_ancestors(
        packs_dir, expect_dir=True, error_prefix="ordinary packs directory required"
    )
    return True


def has_pack_pointer(packs_dir: Path) -> bool:
    """Return True if packs_dir is an ordinary dir with an ordinary CURRENT.json pointer."""
    if not _validate_packs_dir(packs_dir, allow_missing=True):
        return False

    pointer = packs_dir / "CURRENT.json"
    try:
        info = pointer.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise ValueError(f"ordinary pack pointer required: {pointer}") from None

    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary pack pointer required: {pointer}")
    return True


def parse_pack_current(packs_dir: Path) -> dict[str, Any] | None:
    """Read and validate packs/CURRENT.json, or return None if no pack pointer exists."""
    if not _validate_packs_dir(packs_dir, allow_missing=True):
        return None

    pointer = packs_dir / "CURRENT.json"

    try:
        info = pointer.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError(f"ordinary pack pointer required: {pointer}") from None

    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary pack pointer required: {pointer}")

    doc = _read_pointer_json(pointer)

    if doc.get("format") != PACK_CURRENT_FORMAT:
        raise ValueError(f"unsupported pack pointer format: {doc.get('format')!r}")

    gen = doc.get("generation")
    occ_gen = doc.get("occ_generation")

    if type(gen) is not int or gen < 1 or type(occ_gen) is not int or occ_gen < 1:
        raise ValueError("invalid pack pointer generation")

    shards = doc.get("shards")

    if not isinstance(shards, list):
        raise ValueError("pack pointer shards must be a list")

    for shard in shards:
        if not isinstance(shard, dict):
            raise ValueError("pack shard descriptor must be an object")

        if type(shard.get("shard")) is not int or shard["shard"] < 0:
            raise ValueError("invalid pack shard id")

        if not isinstance(shard.get("slab"), str) or not _PACK_SLAB_NAME_RE.fullmatch(
            shard["slab"]
        ):
            raise ValueError(f"invalid pack slab filename: {shard.get('slab')!r}")

        if not isinstance(shard.get("idx"), str) or not _PACK_IDX_NAME_RE.fullmatch(shard["idx"]):
            raise ValueError(f"invalid pack idx filename: {shard.get('idx')!r}")

        if not isinstance(shard.get("occ"), str) or not _PACK_OCC_NAME_RE.fullmatch(shard["occ"]):
            raise ValueError(f"invalid pack occ filename: {shard.get('occ')!r}")

        if type(shard.get("object_count")) is not int or shard["object_count"] < 0:
            raise ValueError("invalid pack shard object_count")

        slab_bytes = shard.get("slab_bytes")
        if type(slab_bytes) is not int or slab_bytes < SLAB_HEADER_STRUCT.size:
            raise ValueError("invalid pack shard slab_bytes")

        for sha_field in ("slab_sha256", "idx_sha256", "occ_sha256"):
            val = shard.get(sha_field)
            if not isinstance(val, str) or not _PACK_HEX64_RE.fullmatch(val):
                raise ValueError(f"invalid {sha_field} in pack pointer")

    return doc


class PackShardHandle:
    """Read-only mmap handle over one immutable (.slab, .idx, .occ) shard triplet."""

    def __init__(
        self,
        packs_dir: Path,
        descriptor: dict[str, Any],
        *,
        verify_sha: Callable[[bytes], str] | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.shard_id: int = descriptor["shard"]
        self.slab_path = packs_dir / descriptor["slab"]
        self.idx_path = packs_dir / descriptor["idx"]
        self.occ_path = packs_dir / descriptor["occ"]
        self.object_count: int = descriptor["object_count"]
        self._verify_sha = verify_sha or _sha256_hex
        self._closed = False

        self._idx_stream: IO[bytes] | None = None
        self._idx_mm: mmap.mmap | None = None
        self._occ_stream: IO[bytes] | None = None
        self._occ_mm: mmap.mmap | None = None
        self._slab_stream: IO[bytes] | None = None
        self._slab_mm: mmap.mmap | None = None
        self._slab_size = 0
        self._fanout: tuple[int, ...] = ()

        try:
            self._open_idx()
            self._open_occ()
            self._open_slab()
        except BaseException:
            self.close()
            raise

    def _open_idx(self) -> None:
        idx_stream, idx_size = _open_readonly_regular(self.idx_path)
        self._idx_stream = idx_stream
        expected_idx_size = IDX_ENTRIES_OFFSET + self.object_count * IDX_ENTRY_STRUCT.size
        if idx_size != expected_idx_size:
            raise ValueError(f"pack idx size mismatch: {self.idx_path}")

        self._idx_mm = mmap.mmap(idx_stream.fileno(), 0, access=mmap.ACCESS_READ)
        idx_magic, idx_ver, _idx_flags, idx_sid, idx_cnt, _idx_gen = IDX_HEADER_STRUCT.unpack_from(
            self._idx_mm, 0
        )
        if (
            idx_magic != PACK_IDX_MAGIC
            or idx_ver != 1
            or idx_sid != self.shard_id
            or idx_cnt != self.object_count
        ):
            raise ValueError(f"corrupt pack idx header: {self.idx_path}")

        self._fanout = struct.unpack_from("<256I", self._idx_mm, IDX_FANOUT_OFFSET)
        prev_fanout = 0
        for fanout_count in self._fanout:
            if fanout_count < prev_fanout or fanout_count > self.object_count:
                raise ValueError(f"corrupt pack idx fanout: {self.idx_path}")
            prev_fanout = fanout_count

        if prev_fanout != self.object_count:
            raise ValueError(f"corrupt pack idx fanout: {self.idx_path}")

    def _open_occ(self) -> None:
        occ_stream, occ_size = _open_readonly_regular(self.occ_path)
        self._occ_stream = occ_stream
        expected_occ_size = OCC_HEADER_STRUCT.size + self.object_count
        if occ_size != expected_occ_size:
            raise ValueError(f"pack occ size mismatch: {self.occ_path}")

        self._occ_mm = mmap.mmap(occ_stream.fileno(), 0, access=mmap.ACCESS_READ)
        occ_magic, occ_ver, _occ_flags, occ_cnt = OCC_HEADER_STRUCT.unpack_from(self._occ_mm, 0)
        if occ_magic != PACK_OCC_MAGIC or occ_ver != 1 or occ_cnt != self.object_count:
            raise ValueError(f"corrupt pack occ header: {self.occ_path}")

        expected_occ_sha = self.descriptor.get("occ_sha256")
        if not isinstance(expected_occ_sha, str) or not _PACK_HEX64_RE.fullmatch(expected_occ_sha):
            raise ValueError(f"invalid pack occ_sha256: {self.occ_path}")

        occ_raw = self._occ_mm[:]
        if _sha256_hex(occ_raw) != expected_occ_sha:
            raise ValueError(f"pack occ sha256 mismatch: {self.occ_path}")

        if occ_raw[OCC_HEADER_STRUCT.size :].translate(None, _VALID_OCC_BYTES):
            raise ValueError(f"corrupt pack occ state byte: {self.occ_path}")

    def _open_slab(self) -> None:
        slab_stream, self._slab_size = _open_readonly_regular(self.slab_path)
        self._slab_stream = slab_stream
        expected_slab_bytes = self.descriptor.get("slab_bytes")
        if type(expected_slab_bytes) is not int or expected_slab_bytes < SLAB_HEADER_STRUCT.size:
            raise ValueError(f"invalid pack slab_bytes in descriptor: {self.slab_path}")
        if self._slab_size < SLAB_HEADER_STRUCT.size or self._slab_size != expected_slab_bytes:
            raise ValueError(f"corrupt pack slab size: {self.slab_path}")

        self._slab_mm = mmap.mmap(slab_stream.fileno(), 0, access=mmap.ACCESS_READ)
        slab_magic, slab_ver, _slab_flags, slab_sid, slab_cnt, _slab_gen = (
            SLAB_HEADER_STRUCT.unpack_from(
                self._slab_mm,
                0,
            )
        )
        if (
            slab_magic != PACK_SLAB_MAGIC
            or slab_ver != 1
            or slab_sid != self.shard_id
            or slab_cnt != self.object_count
        ):
            raise ValueError(f"corrupt pack slab header: {self.slab_path}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        for attr in ("_slab_mm", "_occ_mm", "_idx_mm"):
            mm = getattr(self, attr, None)
            setattr(self, attr, None)
            if mm is not None:
                with suppress(BufferError, ValueError, OSError):
                    mm.close()

        for attr in ("_slab_stream", "_occ_stream", "_idx_stream"):
            stream = getattr(self, attr, None)
            setattr(self, attr, None)
            if stream is not None:
                with suppress(OSError):
                    stream.close()

    def __del__(self) -> None:
        self.close()

    def lookup(self, key_bytes: bytes) -> tuple[int, int, int, int, int] | None:
        """256-fanout binary search in mmap: returns (slot, occ, slab_off, stored_len, raw_len)."""
        if self._closed or self.object_count == 0:
            return None

        first_byte = key_bytes[0]
        lo = 0 if first_byte == 0 else self._fanout[first_byte - 1]
        hi = self._fanout[first_byte]
        idx_mm = self._idx_mm
        occ_mm = self._occ_mm
        if idx_mm is None or occ_mm is None:
            raise RuntimeError("PackShardHandle is not open")

        while lo < hi:
            mid = (lo + hi) >> 1
            pos = IDX_ENTRIES_OFFSET + mid * 48
            cur = idx_mm[pos : pos + 32]
            if cur < key_bytes:
                lo = mid + 1
            elif cur > key_bytes:
                hi = mid
            else:
                occ = occ_mm[OCC_HEADER_STRUCT.size + mid]
                if occ == OCC_TOMBSTONE:
                    return None
                _, slab_off, stored_len, raw_len = IDX_ENTRY_STRUCT.unpack_from(idx_mm, pos)
                return mid, occ, slab_off, stored_len, raw_len
        return None

    def read_frame(
        self,
        key_hex: str,
        key_bytes: bytes,
        occ: int,
        slab_off: int,
        stored_len: int,
        raw_len: int,
        *,
        retained: bool,
        expected_bytes: int | None,
    ) -> bytes:
        """Read and cryptographically verify one frame from the mmapped slab."""
        if occ == OCC_SEALED:
            raise ValueError(f"sealed packed object cannot be read directly: {key_hex}")

        if occ not in (OCC_LIVE_RAW, OCC_LIVE_DEFLATE, OCC_LIVE_ZSTD):
            raise ValueError(f"invalid packed object state ({occ}): {key_hex}")

        if expected_bytes is not None and raw_len != expected_bytes:
            raise ValueError("content object changed (length changed)")

        limit = raw_len if retained else min(raw_len, MAX_BYTES)
        if not retained and raw_len > MAX_BYTES:
            raise ValueError(f"file exceeds {MAX_BYTES} bytes: {self.slab_path}")

        frame_end = slab_off + FRAME_HEADER_STRUCT.size + stored_len
        if slab_off < SLAB_HEADER_STRUCT.size or frame_end > self._slab_size:
            raise ValueError(f"corrupt pack frame bounds: {self.slab_path}")

        slab_mm = self._slab_mm
        if slab_mm is None:
            raise RuntimeError("PackShardHandle slab is not open")
        codec, _flags, magic2, hdr_stored, hdr_raw, hdr_sha, hdr_crc = (
            FRAME_HEADER_STRUCT.unpack_from(
                slab_mm,
                slab_off,
            )
        )

        if (
            magic2 != PACK_FRAME_MAGIC
            or codec != occ
            or hdr_stored != stored_len
            or hdr_raw != raw_len
            or hdr_sha != key_bytes
        ):
            raise ValueError(f"corrupt pack frame header for {key_hex}")

        payload_start = slab_off + FRAME_HEADER_STRUCT.size
        payload = slab_mm[payload_start : payload_start + stored_len]

        if (zlib.crc32(payload) & 0xFFFFFFFF) != hdr_crc:
            raise ValueError(f"corrupt pack frame crc32 for {key_hex}")

        if codec == OCC_LIVE_RAW:
            data = payload
        elif codec == OCC_LIVE_DEFLATE:
            data = decompress_bounded(io.BytesIO(payload), limit, self.slab_path)
        else:
            data = decompress_zstd_bounded(payload, limit, self.slab_path)

        if len(data) != raw_len:
            raise ValueError("content object changed (length changed)")

        if expected_bytes is not None and len(data) != expected_bytes:
            raise ValueError("content object changed (length changed)")

        if self._verify_sha(data) != key_hex:
            raise ValueError("content object changed")

        return data

    def iter_live_slots(self) -> Iterator[tuple[str, int, int, int, int, int, int]]:
        """Yield (key_hex, shard_id, slot, occ, slab_off, stored_len, raw_len) for live slots."""
        idx_mm = self._idx_mm
        occ_mm = self._occ_mm
        if idx_mm is None or occ_mm is None:
            raise RuntimeError("PackShardHandle is not open")
        occ_base = OCC_HEADER_STRUCT.size

        for slot in range(self.object_count):
            occ = occ_mm[occ_base + slot]
            if occ == OCC_TOMBSTONE:
                continue
            sha_bin, slab_off, stored_len, raw_len = IDX_ENTRY_STRUCT.unpack_from(
                idx_mm,
                IDX_ENTRIES_OFFSET + slot * 48,
            )
            yield sha_bin.hex(), self.shard_id, slot, occ, slab_off, stored_len, raw_len


class PackSession:
    """Thread-safe context-managed read-only mmap session over packs/CURRENT.json shards."""

    def __init__(
        self,
        packs_dir: Path,
        *,
        current_doc: dict[str, Any] | None = None,
        verify_sha: Callable[[bytes], str] | None = None,
    ) -> None:
        self.packs_dir = packs_dir
        self._verify_sha = verify_sha
        self._lock = threading.Lock()
        self._closed = False
        self._descriptors: tuple[dict[str, Any], ...] = ()
        self._handles: list[PackShardHandle | None] = []

        if current_doc is None:
            self.current = parse_pack_current(packs_dir)
        else:
            _validate_packs_dir(packs_dir, allow_missing=not bool(current_doc.get("shards")))
            self.current = current_doc

        if self.current is not None:
            self._descriptors = tuple(self.current["shards"])
            self._handles = [None] * len(self._descriptors)
            try:
                # Pin active shard mmaps on entry so concurrent GC cannot unlink this generation.
                for idx in range(len(self._descriptors)):
                    self._get_shard(idx)
            except BaseException:
                self.close()
                raise

    def _get_shard(self, idx: int) -> PackShardHandle:
        # Fast lock-free path once the immutable shard handle is cached.
        handles = self._handles
        if idx < len(handles):
            handle = handles[idx]
            if handle is not None:
                return handle

        with self._lock:
            if self._closed:
                raise ValueError("PackSession is closed")
            handle = self._handles[idx]
            if handle is None:
                handle = PackShardHandle(
                    self.packs_dir,
                    self._descriptors[idx],
                    verify_sha=self._verify_sha,
                )
                self._handles[idx] = handle
            return handle

    @property
    def shards(self) -> list[PackShardHandle]:
        if self._closed or not self._descriptors:
            return []
        return [self._get_shard(idx) for idx in range(len(self._descriptors))]

    @property
    def has_packs(self) -> bool:
        return bool(self._descriptors)

    def __enter__(self) -> PackSession:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        lock = getattr(self, "_lock", None)
        if lock is None:
            return
        with lock:
            if getattr(self, "_closed", True):
                return
            self._closed = True
            handles = list(getattr(self, "_handles", ()))
            self._handles = []

        for shard in handles:
            if shard is not None:
                shard.close()

    def __del__(self) -> None:
        self.close()

    def locate(self, key: str) -> tuple[PackShardHandle, int, int, int, int, int] | None:
        count = len(self._descriptors)
        if self._closed or count == 0:
            return None

        key_bytes = bytes.fromhex(key)
        # Search newest shards first so a repacked/appended shard shadows tombstoned slots.
        for idx in range(count - 1, -1, -1):
            shard = self._get_shard(idx)
            hit = shard.lookup(key_bytes)
            if hit is not None:
                return shard, *hit
        return None

    def has(self, key: str) -> bool:
        return self.locate(key) is not None

    def read(
        self,
        key: str,
        *,
        retained: bool = False,
        expected_bytes: int | None = None,
    ) -> bytes | None:
        located = self.locate(key)
        if located is None:
            return None

        shard, _slot, occ, slab_off, stored_len, raw_len = located
        return shard.read_frame(
            key,
            bytes.fromhex(key),
            occ,
            slab_off,
            stored_len,
            raw_len,
            retained=retained,
            expected_bytes=expected_bytes,
        )

    def live_entries(self) -> list[dict[str, Any]]:
        """Return live (non-tombstoned) packed object metadata sorted by key."""
        seen: dict[str, dict[str, Any]] = {}
        for idx in range(len(self._descriptors) - 1, -1, -1):
            shard = self._get_shard(idx)
            for (
                key_hex,
                shard_id,
                slot,
                occ,
                slab_off,
                stored_len,
                raw_len,
            ) in shard.iter_live_slots():
                if key_hex in seen:
                    continue
                seen[key_hex] = {
                    "object": key_hex,
                    "shard": shard_id,
                    "slot": slot,
                    "occ": occ,
                    "compressed": occ in (OCC_LIVE_DEFLATE, OCC_LIVE_ZSTD),
                    "slab_offset": slab_off,
                    "stored_bytes": stored_len,
                    "raw_bytes": raw_len,
                    "disk_bytes": stored_len + FRAME_HEADER_STRUCT.size,
                }
        return [seen[k] for k in sorted(seen)]

    def iter_verified_sequential(
        self,
        keys_filter: set[str] | None = None,
        *,
        retained: bool = True,
    ) -> Iterator[tuple[str, bytes]]:
        """Scan live packed frames in ascending (shard, slab_offset) order for sequential I/O."""
        emitted: set[str] = set()
        for idx in range(len(self._descriptors)):
            shard = self._get_shard(idx)
            ordered = sorted(shard.iter_live_slots(), key=lambda row: row[4])
            for key_hex, _sid, _slot, occ, slab_off, stored_len, raw_len in ordered:
                if key_hex in emitted:
                    continue
                if keys_filter is not None and key_hex not in keys_filter:
                    continue
                if occ == OCC_SEALED:
                    continue
                raw = shard.read_frame(
                    key_hex,
                    bytes.fromhex(key_hex),
                    occ,
                    slab_off,
                    stored_len,
                    raw_len,
                    retained=retained,
                    expected_bytes=raw_len,
                )
                emitted.add(key_hex)
                yield key_hex, raw


__all__ = [
    "DEFAULT_SHARD_TARGET_BYTES",
    "FRAME_HEADER_STRUCT",
    "IDX_ENTRIES_OFFSET",
    "IDX_ENTRY_STRUCT",
    "IDX_FANOUT_OFFSET",
    "IDX_HEADER_STRUCT",
    "MAX_BYTES",
    "OCC_HEADER_STRUCT",
    "OCC_LIVE_DEFLATE",
    "OCC_LIVE_RAW",
    "OCC_LIVE_ZSTD",
    "OCC_SEALED",
    "OCC_TOMBSTONE",
    "PACK_CURRENT_FORMAT",
    "PACK_FRAME_MAGIC",
    "PACK_IDX_MAGIC",
    "PACK_OCC_MAGIC",
    "PACK_SLAB_MAGIC",
    "SLAB_HEADER_STRUCT",
    "PackSession",
    "PackShardHandle",
    "decompress_bounded",
    "decompress_zstd_bounded",
    "has_pack_pointer",
    "parse_pack_current",
]
