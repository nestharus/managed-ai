"""Filesystem durability and content-addressed bytes shared by the local engines."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import io
import json
import mmap
import os
from pathlib import Path
import random
import re
import stat
import struct
import time
from typing import Any, Iterator
import uuid
import zlib

from managed_ai.platform import atomic_bytes as _platform_atomic_bytes

from ._platform import (
    OPEN_DIR_NOFOLLOW as _OPEN_DIR_NOFOLLOW,
    OPEN_RO_NOFOLLOW as _OPEN_RO_NOFOLLOW,
    admission_deadline,
    check_ordinary_ancestors,
    has_ordinary_dir,
    has_ordinary_file,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    is_reparse_or_symlink,
    lock_file_nonblocking,
    safe_relpath,
    strict_json_loads,
    sync_dir,
    unlock_file,
)

MAX_BYTES = 2 * 1024 * 1024
_ORIG_PATH_OPEN = Path.open

# Reserved append-only shard slab layout constants (shared by ObjectStore PackSession & store.records.pack_slab).
PACK_CURRENT_FORMAT = 'managed-ai-pack-current/1'
DEFAULT_SHARD_TARGET_BYTES = 64 * 1024 * 1024
PACK_SLAB_MAGIC = b'MAISLAB1'
PACK_IDX_MAGIC = b'MAIIDX01'
PACK_OCC_MAGIC = b'MAIOCC01'
PACK_FRAME_MAGIC = 0x5346
OCC_TOMBSTONE = 0
OCC_LIVE_RAW = 1
OCC_LIVE_DEFLATE = 2
OCC_SEALED = 3

SLAB_HEADER_STRUCT = struct.Struct('<8sHHIIQ36x')
FRAME_HEADER_STRUCT = struct.Struct('<BBHII32sI')
IDX_HEADER_STRUCT = struct.Struct('<8sHHIIQ4x')
IDX_FANOUT_OFFSET = IDX_HEADER_STRUCT.size
IDX_ENTRIES_OFFSET = IDX_FANOUT_OFFSET + 256 * 4
IDX_ENTRY_STRUCT = struct.Struct('<32sQII')
OCC_HEADER_STRUCT = struct.Struct('<8sHHI')

_PACK_SLAB_NAME_RE = re.compile(r'^pack-[0-9]{6}-[0-9]{4}\.slab$')
_PACK_IDX_NAME_RE = re.compile(r'^pack-[0-9]{6}-[0-9]{4}\.idx$')
_PACK_OCC_NAME_RE = re.compile(r'^pack-[0-9]{6}-[0-9]{4}\.occ[1-9][0-9]*$')
_PACK_HEX64_RE = re.compile(r'^[0-9a-f]{64}$')


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_bytes(path: Path, data: Any) -> None:
    _platform_atomic_bytes(path, data)


def write_json(path: Path, value: Any) -> None:
    """Durably replace one JSON record."""
    atomic_bytes(path, encode(value))


def read_json(path: Path) -> dict[str, Any]:
    if hasattr(os, 'O_NOFOLLOW'):
        try:
            fd = os.open(path, _OPEN_RO_NOFOLLOW)
        except FileNotFoundError:
            raise
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                raise ValueError(f"ordinary JSON file required: {path}") from None
            raise

        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f"ordinary JSON file required: {path}")
            stream = os.fdopen(fd, 'rb')
        except BaseException:
            os.close(fd)
            raise

        with stream:
            raw = stream.read()
    else:
        info = os.lstat(path)

        if not is_ordinary_file_stat(info):
            raise ValueError(f"ordinary JSON file required: {path}")
        raw = path.read_bytes()

    return strict_json_loads(raw, path, require_object=True)


class OwnerBusy(RuntimeError):
    def __init__(self, path: Path):
        super().__init__(f"another owner holds {path}")
        self.path = path


class AdmissionContended(OwnerBusy):
    """The caller timed out acquiring its guards before entering the operation."""

    def result(self, operation):
        return dict(status='contended', operation=operation, lock=str(self.path), error=str(self))


@contextmanager
def owner_admission(acquire, *, timeout=None, deadline=None):
    """Try immediately, then back off on pre-entry contention until cancelled or timed out."""

    if timeout is not None:
        end = time.monotonic() + timeout
        deadline = end if deadline is None else min(end, deadline)
    interval = 0.05

    while True:
        with ExitStack() as guards:
            try:
                selection = acquire(guards)
            except OwnerBusy as exc:
                busy_path = exc.path
            else:
                yield selection
                return

        # Equal jitter separates simultaneous waiters while retaining a positive floor.
        delay = random.uniform(interval / 2, interval)

        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdmissionContended(busy_path)
            delay = min(delay, remaining)
        time.sleep(delay)

        if deadline is not None and time.monotonic() >= deadline:
            raise AdmissionContended(busy_path)
        interval = min(interval * 2, 2.0)


def _open_lock_stream(path: Path, *, allow_readonly_open: bool):
    """Open a lock file without following symlinks or Windows NTFS junctions/reparse points."""
    target = Path(path)
    parent = target.parent

    try:
        parent_info = parent.lstat()
    except FileNotFoundError:
        raise
    except OSError:
        raise ValueError(f'ordinary lock directory required: {parent}') from None

    if not is_ordinary_dir_stat(parent_info):
        raise ValueError(f'ordinary lock directory required: {parent}')
    check_ordinary_ancestors(parent, expect_dir=True, error_prefix='ordinary lock directory required')

    if hasattr(os, 'O_NOFOLLOW') and Path.open is _ORIG_PATH_OPEN:
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
        mode = 'a+b'

        try:
            fd = os.open(target, flags, 0o666)
        except OSError as open_error:
            if open_error.errno in (errno.ELOOP, errno.EISDIR, errno.ENOTDIR, errno.ENXIO):
                raise ValueError(f'ordinary lock file required: {target}') from None
            if not allow_readonly_open or open_error.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
                raise

            try:
                fd = os.open(target, _OPEN_RO_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0))
                mode = 'rb'
            except OSError as read_error:
                if read_error.errno in (errno.ELOOP, errno.EISDIR, errno.ENOTDIR, errno.ENXIO):
                    raise ValueError(f'ordinary lock file required: {target}') from None
                raise open_error from read_error

        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f'ordinary lock file required: {target}')
            return os.fdopen(fd, mode)
        except BaseException:
            os.close(fd)
            raise

    try:
        leaf_info = target.lstat()
    except FileNotFoundError:
        leaf_info = None
    except OSError:
        raise ValueError(f'ordinary lock file required: {target}') from None

    if leaf_info is not None and not is_ordinary_file_stat(leaf_info):
        raise ValueError(f'ordinary lock file required: {target}')

    try:
        return target.open('a+b')
    except OSError as open_error:
        if not allow_readonly_open or open_error.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise

        try:
            return target.open('rb')
        except OSError as read_error:
            raise open_error from read_error


@contextmanager
def exclusive(path: Path, *, allow_readonly_open: bool = False) -> Iterator[None]:
    with _open_lock_stream(path, allow_readonly_open=allow_readonly_open) as stream:
        try:
            lock_file_nonblocking(stream, exclusive=True)
        except BlockingIOError as exc:
            raise OwnerBusy(path) from exc

        try:
            yield
        finally:
            unlock_file(stream)


@contextmanager
def shared(path: Path, *, allow_readonly_open: bool = True) -> Iterator[None]:
    """Share a cooperative observation guard; retain existing lock-file creation."""

    with _open_lock_stream(path, allow_readonly_open=allow_readonly_open) as stream:
        try:
            lock_file_nonblocking(stream, exclusive=False)
        except BlockingIOError as exc:
            raise OwnerBusy(path) from exc

        try:
            yield
        finally:
            unlock_file(stream)


def open_directory(path: Path | str, *, dir_fd: int | None = None) -> int:
    return os.open(path, _OPEN_DIR_NOFOLLOW, dir_fd=dir_fd)


def read_bytes(path: Path | str, limit: int = MAX_BYTES, *,
               dir_fd: int | None = None, label: Path | str | None = None) -> bytes:
    display = path if label is None else label

    if hasattr(os, 'O_NOFOLLOW'):
        try:
            fd = os.open(path, _OPEN_RO_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                raise ValueError(f'ordinary file required: {display}') from None
            raise

        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f'ordinary file required: {display}')
            stream = os.fdopen(fd, 'rb')
        except BaseException:
            os.close(fd)
            raise

        with stream:
            data = stream.read(limit + 1)
    else:
        target = Path(path)

        try:
            info = target.lstat()
        except OSError:
            raise ValueError(f'ordinary file required: {display}') from None

        if not is_ordinary_file_stat(info):
            raise ValueError(f'ordinary file required: {display}')

        with target.open('rb') as stream:
            data = stream.read(limit + 1)

    if len(data) > limit:
        raise ValueError(f'file exceeds {limit} bytes: {display}')

    return data


_READ_BYTES = read_bytes


def _open_readonly_regular(path: Path):
    """Open an ordinary non-symlink regular file read-only, returning (stream, size) or raising ValueError."""

    if hasattr(os, 'O_NOFOLLOW'):
        try:
            fd = os.open(path, _OPEN_RO_NOFOLLOW)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                raise ValueError(f'ordinary pack file required: {path}') from None
            raise

        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f'ordinary pack file required: {path}')
            return os.fdopen(fd, 'rb'), info.st_size
        except BaseException:
            os.close(fd)
            raise

    try:
        info = path.lstat()
    except OSError:
        raise ValueError(f'ordinary pack file required: {path}') from None

    if not is_ordinary_file_stat(info):
        raise ValueError(f'ordinary pack file required: {path}')

    return path.open('rb'), info.st_size


def parse_pack_current(packs_dir: Path) -> dict[str, Any] | None:
    """Read and strictly validate packs/CURRENT.json, or return None if no pack directory/pointer exists."""
    pointer = packs_dir / 'CURRENT.json'

    try:
        info = pointer.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError(f'ordinary pack pointer required: {pointer}') from None

    if not is_ordinary_file_stat(info):
        raise ValueError(f'ordinary pack pointer required: {pointer}')

    doc = read_json(pointer)

    if doc.get('format') != PACK_CURRENT_FORMAT:
        raise ValueError(f'unsupported pack pointer format: {doc.get("format")!r}')
    gen = doc.get('generation')
    occ_gen = doc.get('occ_generation')

    if type(gen) is not int or gen < 1 or type(occ_gen) is not int or occ_gen < 1:
        raise ValueError('invalid pack pointer generation')

    shards = doc.get('shards')

    if not isinstance(shards, list):
        raise ValueError('pack pointer shards must be a list')

    for shard in shards:
        if not isinstance(shard, dict):
            raise ValueError('pack shard descriptor must be an object')

        if type(shard.get('shard')) is not int or shard['shard'] < 0:
            raise ValueError('invalid pack shard id')

        if not isinstance(shard.get('slab'), str) or not _PACK_SLAB_NAME_RE.fullmatch(shard['slab']):
            raise ValueError(f'invalid pack slab filename: {shard.get("slab")!r}')

        if not isinstance(shard.get('idx'), str) or not _PACK_IDX_NAME_RE.fullmatch(shard['idx']):
            raise ValueError(f'invalid pack idx filename: {shard.get("idx")!r}')

        if not isinstance(shard.get('occ'), str) or not _PACK_OCC_NAME_RE.fullmatch(shard['occ']):
            raise ValueError(f'invalid pack occ filename: {shard.get("occ")!r}')

        if type(shard.get('object_count')) is not int or shard['object_count'] < 0:
            raise ValueError('invalid pack shard object_count')

        for sha_field in ('slab_sha256', 'idx_sha256', 'occ_sha256'):
            val = shard.get(sha_field)
            if not isinstance(val, str) or not _PACK_HEX64_RE.fullmatch(val):
                raise ValueError(f'invalid {sha_field} in pack pointer')

    return doc


class PackShardHandle:
    """Read-only mmap handle over one immutable (.slab, .idx, .occ) shard triplet."""

    def __init__(self, packs_dir: Path, descriptor: dict[str, Any]) -> None:
        self.descriptor = descriptor
        self.shard_id: int = descriptor['shard']
        self.slab_path = packs_dir / descriptor['slab']
        self.idx_path = packs_dir / descriptor['idx']
        self.occ_path = packs_dir / descriptor['occ']
        self.object_count: int = descriptor['object_count']
        self._closed = False

        self._idx_stream = None
        self._idx_mm: mmap.mmap | None = None
        self._occ_stream = None
        self._occ_mm: mmap.mmap | None = None
        self._slab_stream = None
        self._slab_mm: mmap.mmap | None = None
        self._slab_size = 0
        self._fanout: tuple[int, ...] = ()

        try:
            self._idx_stream, idx_size = _open_readonly_regular(self.idx_path)
            expected_idx_size = IDX_ENTRIES_OFFSET + self.object_count * IDX_ENTRY_STRUCT.size
            if idx_size != expected_idx_size:
                raise ValueError(f'pack idx size mismatch: {self.idx_path}')
            self._idx_mm = mmap.mmap(self._idx_stream.fileno(), 0, access=mmap.ACCESS_READ)
            idx_magic, idx_ver, _idx_flags, idx_sid, idx_cnt, _idx_gen = IDX_HEADER_STRUCT.unpack_from(self._idx_mm, 0)
            if idx_magic != PACK_IDX_MAGIC or idx_ver != 1 or idx_sid != self.shard_id or idx_cnt != self.object_count:
                raise ValueError(f'corrupt pack idx header: {self.idx_path}')
            self._fanout = struct.unpack_from('<256I', self._idx_mm, IDX_FANOUT_OFFSET)

            self._occ_stream, occ_size = _open_readonly_regular(self.occ_path)
            expected_occ_size = OCC_HEADER_STRUCT.size + self.object_count
            if occ_size != expected_occ_size:
                raise ValueError(f'pack occ size mismatch: {self.occ_path}')
            self._occ_mm = mmap.mmap(self._occ_stream.fileno(), 0, access=mmap.ACCESS_READ)
            occ_magic, occ_ver, _occ_flags, occ_cnt = OCC_HEADER_STRUCT.unpack_from(self._occ_mm, 0)
            if occ_magic != PACK_OCC_MAGIC or occ_ver != 1 or occ_cnt != self.object_count:
                raise ValueError(f'corrupt pack occ header: {self.occ_path}')

            self._slab_stream, self._slab_size = _open_readonly_regular(self.slab_path)
            if self._slab_size < SLAB_HEADER_STRUCT.size:
                raise ValueError(f'corrupt pack slab size: {self.slab_path}')
            self._slab_mm = mmap.mmap(self._slab_stream.fileno(), 0, access=mmap.ACCESS_READ)
            slab_magic, slab_ver, _slab_flags, slab_sid, slab_cnt, _slab_gen = SLAB_HEADER_STRUCT.unpack_from(
                self._slab_mm, 0,
            )
            if slab_magic != PACK_SLAB_MAGIC or slab_ver != 1 or slab_sid != self.shard_id or slab_cnt != self.object_count:
                raise ValueError(f'corrupt pack slab header: {self.slab_path}')
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        for attr in ('_slab_mm', '_occ_mm', '_idx_mm'):
            mm = getattr(self, attr, None)
            setattr(self, attr, None)
            if mm is not None:
                try:
                    mm.close()
                except (BufferError, ValueError, OSError):
                    pass

        for attr in ('_slab_stream', '_occ_stream', '_idx_stream'):
            stream = getattr(self, attr, None)
            setattr(self, attr, None)
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def __del__(self) -> None:
        self.close()

    def lookup(self, key_bytes: bytes) -> tuple[int, int, int, int, int] | None:
        """O(log(N/256)) 256-entry fanout binary search in mmap. Returns (slot, occ, slab_off, stored_len, raw_len)."""

        if self._closed or self.object_count == 0:
            return None
        first_byte = key_bytes[0]
        lo = 0 if first_byte == 0 else self._fanout[first_byte - 1]
        hi = self._fanout[first_byte]
        idx_mm = self._idx_mm
        assert idx_mm is not None
        assert self._occ_mm is not None

        while lo < hi:
            mid = (lo + hi) >> 1
            pos = IDX_ENTRIES_OFFSET + mid * 48
            cur = idx_mm[pos:pos + 32]
            if cur < key_bytes:
                lo = mid + 1
            elif cur > key_bytes:
                hi = mid
            else:
                occ = self._occ_mm[OCC_HEADER_STRUCT.size + mid]
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
            raise ValueError(f'sealed packed object cannot be read directly: {key_hex}')

        if occ not in (OCC_LIVE_RAW, OCC_LIVE_DEFLATE):
            raise ValueError(f'invalid packed object state ({occ}): {key_hex}')

        if expected_bytes is not None and raw_len != expected_bytes:
            raise ValueError('content object changed (length changed)')
        limit = raw_len if retained else min(raw_len, MAX_BYTES)

        if not retained and raw_len > MAX_BYTES:
            raise ValueError(f'file exceeds {MAX_BYTES} bytes: {self.slab_path}')

        frame_end = slab_off + FRAME_HEADER_STRUCT.size + stored_len

        if slab_off < SLAB_HEADER_STRUCT.size or frame_end > self._slab_size:
            raise ValueError(f'corrupt pack frame bounds: {self.slab_path}')

        slab_mm = self._slab_mm
        assert slab_mm is not None
        codec, _flags, magic2, hdr_stored, hdr_raw, hdr_sha, hdr_crc = FRAME_HEADER_STRUCT.unpack_from(
            slab_mm, slab_off,
        )

        if (
            magic2 != PACK_FRAME_MAGIC
            or codec != occ
            or hdr_stored != stored_len
            or hdr_raw != raw_len
            or hdr_sha != key_bytes
        ):
            raise ValueError(f'corrupt pack frame header for {key_hex}')

        payload_start = slab_off + FRAME_HEADER_STRUCT.size
        payload = slab_mm[payload_start:payload_start + stored_len]

        if (zlib.crc32(payload) & 0xFFFFFFFF) != hdr_crc:
            raise ValueError(f'corrupt pack frame crc32 for {key_hex}')

        if codec == OCC_LIVE_RAW:
            data = payload
        else:
            data = ObjectStore._decompress_bounded(io.BytesIO(payload), limit, self.slab_path)

        if len(data) != raw_len:
            raise ValueError('content object changed (length changed)')

        if expected_bytes is not None and len(data) != expected_bytes:
            raise ValueError('content object changed (length changed)')

        if sha(data) != key_hex:
            raise ValueError('content object changed')

        return data

    def iter_live_slots(self) -> Iterator[tuple[str, int, int, int, int, int, int]]:
        """Yield (key_hex, shard_id, slot_idx, occ, slab_off, stored_len, raw_len) for non-tombstoned slots."""
        idx_mm = self._idx_mm
        occ_mm = self._occ_mm
        assert idx_mm is not None and occ_mm is not None
        occ_base = OCC_HEADER_STRUCT.size

        for slot in range(self.object_count):
            occ = occ_mm[occ_base + slot]
            if occ == OCC_TOMBSTONE:
                continue
            sha_bin, slab_off, stored_len, raw_len = IDX_ENTRY_STRUCT.unpack_from(
                idx_mm, IDX_ENTRIES_OFFSET + slot * 48,
            )
            yield sha_bin.hex(), self.shard_id, slot, occ, slab_off, stored_len, raw_len


class PackSession:
    """Context-managed read-only mmap session across all active shards in packs/CURRENT.json."""

    def __init__(self, packs_dir: Path, *, current_doc: dict[str, Any] | None = None) -> None:
        self.packs_dir = packs_dir
        self._closed = False
        self.current = parse_pack_current(packs_dir) if current_doc is None else current_doc
        self.shards: list[PackShardHandle] = []

        if self.current is not None:
            try:
                for shard_desc in self.current['shards']:
                    self.shards.append(PackShardHandle(packs_dir, shard_desc))
            except BaseException:
                self.close()
                raise

    @property
    def has_packs(self) -> bool:
        return bool(self.shards)

    def __enter__(self) -> PackSession:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        for shard in self.shards:
            shard.close()
        self.shards.clear()

    def __del__(self) -> None:
        self.close()

    def locate(self, key: str) -> tuple[PackShardHandle, int, int, int, int, int] | None:
        if not self.shards:
            return None
        key_bytes = bytes.fromhex(key)
        # Search newest shards first so a newer repacked/appended shard shadows any older tombstoned slot.
        for shard in reversed(self.shards):
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
        for shard in reversed(self.shards):
            for key_hex, shard_id, slot, occ, slab_off, stored_len, raw_len in shard.iter_live_slots():
                if key_hex in seen:
                    continue
                seen[key_hex] = {
                    'object': key_hex,
                    'shard': shard_id,
                    'slot': slot,
                    'occ': occ,
                    'compressed': occ == OCC_LIVE_DEFLATE,
                    'slab_offset': slab_off,
                    'stored_bytes': stored_len,
                    'raw_bytes': raw_len,
                    'disk_bytes': stored_len + FRAME_HEADER_STRUCT.size,
                }
        return [seen[k] for k in sorted(seen)]

    def iter_verified_sequential(
        self,
        keys_filter: set[str] | None = None,
        *,
        retained: bool = True,
    ) -> Iterator[tuple[str, bytes]]:
        """Scan live packed frames in strict ascending (shard, slab_offset) order for peak sequential throughput."""
        emitted: set[str] = set()
        for shard in self.shards:
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


class ObjectStore:
    """Immutable content addressed by SHA-256 within one exchange (hybrid loose + reserved shard slabs)."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.packs_dir = directory / 'packs'
        self._active_pack_session: PackSession | None = None
        self._pack_session_depth = 0

    @contextmanager
    def pack_session(self) -> Iterator[PackSession]:
        """Hold open read-only mmap PackSession across a batch of reads/audits, closing deterministically."""
        if self._active_pack_session is not None:
            self._pack_session_depth += 1
            try:
                yield self._active_pack_session
            finally:
                self._pack_session_depth -= 1
            return

        session = PackSession(self.packs_dir)
        self._active_pack_session = session
        self._pack_session_depth = 1
        try:
            yield session
        finally:
            self._pack_session_depth = 0
            self._active_pack_session = None
            session.close()

    def _has_pack_pointer(self) -> bool:
        return (self.packs_dir / 'CURRENT.json').exists()

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('invalid object identity')

    def has(self, key: str) -> bool:
        if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            return False
        if has_ordinary_file(self.directory / key):
            return True
        if has_ordinary_file(self.directory / (key + '.deflate')):
            return True
        if self._active_pack_session is not None:
            return self._active_pack_session.has(key)
        if not self._has_pack_pointer():
            return False
        with PackSession(self.packs_dir) as session:
            return session.has(key)

    def entries(self) -> list[tuple[str, Path, bool, int]]:
        """Return sorted ordinary loose object entries as (key, path, is_compressed, disk_bytes)."""
        if not self.directory.is_dir():
            return []
        found: list[tuple[str, Path, bool, int]] = []
        with os.scandir(self.directory) as items:
            for item in items:
                name = item.name
                if name.startswith('.'):
                    continue
                path = self.directory / name
                info = item.stat(follow_symlinks=False)
                if name == 'packs':
                    if not is_ordinary_dir_stat(info):
                        raise ValueError(f'ordinary packs directory required: {path}')
                    continue
                if not is_ordinary_file_stat(info):
                    raise ValueError(f'ordinary object file required: {path}')
                if name.endswith('.deflate'):
                    key = name[:-8]
                    self._validate_key(key)
                    found.append((key, path, True, info.st_size))
                else:
                    self._validate_key(name)
                    found.append((name, path, False, info.st_size))
        found.sort(key=lambda row: (row[0], row[2]))
        return found

    def packed_entries(self) -> list[dict[str, Any]]:
        """Return live (non-tombstoned) packed object metadata from packs/CURRENT.json."""
        if self._active_pack_session is not None:
            return self._active_pack_session.live_entries()
        if not self._has_pack_pointer():
            return []
        with PackSession(self.packs_dir) as session:
            return session.live_entries()

    def iter_objects(self) -> Iterator[dict[str, Any]]:
        """Yield all live objects across loose files and reserved shard slabs."""
        seen: set[str] = set()
        for key, path, is_comp, disk_bytes in self.entries():
            seen.add(key)
            yield {
                'object': key,
                'storage': 'loose',
                'path': path,
                'compressed': is_comp,
                'disk_bytes': disk_bytes,
            }
        for row in self.packed_entries():
            if row['object'] in seen:
                continue
            seen.add(row['object'])
            yield dict(row, storage='packed')

    def put(self, data: bytes, *, compress: bool = False) -> str:
        if compress:
            return self.put_retained(data, max_bytes=MAX_BYTES, compress=True)
        return self.put_retained(data, max_bytes=MAX_BYTES)

    def put_retained(self, data: bytes, *, max_bytes: int, compress: bool = False) -> str:
        """Allow a caller that already validated a historical head to retain its exact bytes."""
        if len(data) > max_bytes:
            raise ValueError('object exceeds the per-object limit')
        key = sha(data)
        raw_path = self.directory / key
        deflate_path = self.directory / (key + '.deflate')
        if raw_path.exists() or deflate_path.exists() or self.has(key):
            if self.read(key, retained=True, expected_bytes=len(data)) != data:
                raise ValueError('content object changed')
            return key
        if compress:
            compressed = zlib.compress(data, level=6)
            if len(compressed) < len(data):
                atomic_bytes(deflate_path, compressed)
                return key
        atomic_bytes(raw_path, data)
        return key

    @staticmethod
    def _decompress_bounded(stream, limit: int | None, display: Path) -> bytes:
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
                        raise ValueError(f'file exceeds {limit} bytes: {display}')
                if decompressor.unconsumed_tail and limit is not None:
                    raise ValueError(f'file exceeds {limit} bytes: {display}')
                if decompressor.eof:
                    if decompressor.unused_data or stream.read(1):
                        raise ValueError(f'trailing data after compressed object: {display}')
                    break
            tail = decompressor.flush() if limit is None else decompressor.flush((limit + 1) - total)
            if tail:
                chunks.append(tail)
                total += len(tail)
                if limit is not None and total > limit:
                    raise ValueError(f'file exceeds {limit} bytes: {display}')
            if not decompressor.eof:
                raise ValueError(f'truncated compressed object: {display}')
        except zlib.error as exc:
            raise ValueError(f'corrupt compressed object {display}: {exc}') from None
        return b''.join(chunks)

    def _read_from_packs(
        self,
        key: str,
        *,
        retained: bool,
        expected_bytes: int | None,
    ) -> bytes | None:
        if self._active_pack_session is not None:
            return self._active_pack_session.read(key, retained=retained, expected_bytes=expected_bytes)
        if not self._has_pack_pointer():
            return None
        with PackSession(self.packs_dir) as session:
            return session.read(key, retained=retained, expected_bytes=expected_bytes)

    def _read_deflate_or_pack(
        self,
        key: str,
        *,
        retained: bool,
        expected_bytes: int | None,
        dir_fd: int | None,
    ) -> bytes:
        filename = key + '.deflate'
        path = self.directory / filename
        if expected_bytes is not None:
            limit: int | None = expected_bytes if retained else min(expected_bytes, MAX_BYTES)
        else:
            limit = None if retained else MAX_BYTES

        if hasattr(os, 'O_NOFOLLOW'):
            target = filename if dir_fd is not None else path
            try:
                fd = os.open(target, _OPEN_RO_NOFOLLOW, dir_fd=dir_fd)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    packed = self._read_from_packs(key, retained=retained, expected_bytes=expected_bytes)
                    if packed is not None:
                        return packed
                    if not retained and expected_bytes is None:
                        raise ValueError(f'ordinary file required: {self.directory / key}') from None
                    raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(self.directory / key)) from None
                if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENOTDIR, errno.ENXIO):
                    raise ValueError(f'ordinary file required: {path}') from None
                raise
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f'ordinary file required: {path}')
                stream = os.fdopen(fd, 'rb')
            except BaseException:
                os.close(fd)
                raise
            with stream:
                return self._decompress_bounded(stream, limit, path)

        try:
            info = path.lstat()
        except FileNotFoundError:
            packed = self._read_from_packs(key, retained=retained, expected_bytes=expected_bytes)
            if packed is not None:
                return packed
            if retained or expected_bytes is not None:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(self.directory / key)) from None
            raise ValueError(f'ordinary file required: {self.directory / key}') from None
        if not is_ordinary_file_stat(info):
            raise ValueError(f'ordinary file required: {path}')
        with path.open('rb') as stream:
            return self._decompress_bounded(stream, limit, path)

    def read(self, key: str, *, retained: bool = False,
             expected_bytes: int | None = None, dir_fd: int | None = None) -> bytes:
        self._validate_key(key)
        if expected_bytes is not None and (type(expected_bytes) is not int or expected_bytes < 0):
            raise ValueError('invalid expected object length')
        path = self.directory / key
        data: bytes | None = None
        if hasattr(os, 'O_NOFOLLOW') and read_bytes is _READ_BYTES:
            target = key if dir_fd is not None else path
            try:
                fd = os.open(target, _OPEN_RO_NOFOLLOW, dir_fd=dir_fd)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    data = self._read_deflate_or_pack(
                        key, retained=retained, expected_bytes=expected_bytes, dir_fd=dir_fd,
                    )
                elif exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENOTDIR, errno.ENXIO):
                    raise ValueError(f'ordinary file required: {path}') from None
                else:
                    raise
            else:
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError(f'ordinary file required: {path}')
                    size = info.st_size
                    if expected_bytes is not None:
                        if size != expected_bytes:
                            raise ValueError('content object changed (length changed)')
                        limit = expected_bytes if retained else min(expected_bytes, MAX_BYTES)
                    else:
                        limit = size if retained else MAX_BYTES
                    stream = os.fdopen(fd, 'rb')
                except BaseException:
                    os.close(fd)
                    raise
                with stream:
                    data = stream.read(limit + 1)
                if len(data) > limit:
                    raise ValueError(f'file exceeds {limit} bytes: {path}')
        else:
            try:
                raw_info = path.lstat()
            except FileNotFoundError:
                data = self._read_deflate_or_pack(
                    key, retained=retained, expected_bytes=expected_bytes, dir_fd=None,
                )
            else:
                if not is_ordinary_file_stat(raw_info):
                    raise ValueError(f'ordinary file required: {path}')
                size = raw_info.st_size
                if expected_bytes is not None:
                    if size != expected_bytes:
                        raise ValueError('content object changed (length changed)')
                    limit = expected_bytes if retained else min(expected_bytes, MAX_BYTES)
                else:
                    limit = size if retained else MAX_BYTES
                if read_bytes is _READ_BYTES:
                    with path.open('rb') as stream:
                        data = stream.read(limit + 1)
                    if len(data) > limit:
                        raise ValueError(f'file exceeds {limit} bytes: {path}')
                else:
                    data = read_bytes(path, limit)
        if expected_bytes is not None and len(data) != expected_bytes:
            raise ValueError('content object changed (length changed)')
        if sha(data) != key:
            raise ValueError('content object changed')
        return data
