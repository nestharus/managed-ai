"""Filesystem durability and content-addressed bytes shared by the local engines."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import random
import stat
import threading
import time
from typing import Any, Iterator
import uuid
import zlib

from managed_ai.platform import (
    OPEN_DIR_NOFOLLOW as _OPEN_DIR_NOFOLLOW,
    OPEN_RO_NOFOLLOW as _OPEN_RO_NOFOLLOW,
    admission_deadline,
    atomic_bytes as _platform_atomic_bytes,
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
from .pack_reader import (
    DEFAULT_SHARD_TARGET_BYTES,
    FRAME_HEADER_STRUCT,
    IDX_ENTRIES_OFFSET,
    IDX_ENTRY_STRUCT,
    IDX_FANOUT_OFFSET,
    IDX_HEADER_STRUCT,
    MAX_BYTES,
    OCC_HEADER_STRUCT,
    OCC_LIVE_DEFLATE,
    OCC_LIVE_RAW,
    OCC_SEALED,
    OCC_TOMBSTONE,
    PACK_CURRENT_FORMAT,
    PACK_FRAME_MAGIC,
    PACK_IDX_MAGIC,
    PACK_OCC_MAGIC,
    PACK_SLAB_MAGIC,
    PackSession,
    PackShardHandle,
    SLAB_HEADER_STRUCT,
    decompress_bounded,
    has_pack_pointer,
    parse_pack_current,
)

_ORIG_PATH_OPEN = Path.open


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


class ObjectStore:
    """Immutable content addressed by SHA-256 within one exchange (hybrid loose + reserved shard slabs)."""

    _decompress_bounded = staticmethod(decompress_bounded)

    def __init__(self, directory: Path):
        self.directory = directory
        self.packs_dir = directory / 'packs'
        self._active_pack_session: PackSession | None = None
        self._pack_session_depth = 0
        self._pack_lock = threading.RLock()

    def _open_pack_session(self) -> PackSession:
        return PackSession(self.packs_dir, verify_sha=lambda data: sha(data))

    @contextmanager
    def pack_session(self) -> Iterator[PackSession]:
        """Hold open read-only mmap PackSession across a batch of reads/audits, closing deterministically."""
        with self._pack_lock:
            if self._active_pack_session is None:
                self._active_pack_session = self._open_pack_session()
            self._pack_session_depth += 1
            session = self._active_pack_session

        try:
            yield session
        finally:
            to_close: PackSession | None = None
            with self._pack_lock:
                self._pack_session_depth -= 1
                if self._pack_session_depth == 0:
                    to_close = self._active_pack_session
                    self._active_pack_session = None
            if to_close is not None:
                to_close.close()

    def _has_pack_pointer(self) -> bool:
        return has_pack_pointer(self.packs_dir)

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('invalid object identity')

    def has(self, key: str) -> bool:
        if not isinstance(key, str) or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            return False
        active = self._active_pack_session
        if active is not None and active.has(key):
            return True
        if has_ordinary_file(self.directory / key):
            return True
        if has_ordinary_file(self.directory / (key + '.deflate')):
            return True
        if active is not None:
            return False
        if not self._has_pack_pointer():
            return False
        with self._open_pack_session() as session:
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
        active = self._active_pack_session
        if active is not None:
            return active.live_entries()
        if not self._has_pack_pointer():
            return []
        with self._open_pack_session() as session:
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

    def _read_from_packs(
        self,
        key: str,
        *,
        retained: bool,
        expected_bytes: int | None,
    ) -> bytes | None:
        active = self._active_pack_session
        if active is not None:
            return active.read(key, retained=retained, expected_bytes=expected_bytes)
        if not self._has_pack_pointer():
            return None
        with self._open_pack_session() as session:
            return session.read(key, retained=retained, expected_bytes=expected_bytes)

    def _read_deflate_or_pack(
        self,
        key: str,
        *,
        retained: bool,
        expected_bytes: int | None,
        dir_fd: int | None,
    ) -> tuple[bytes, bool]:
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
                        return packed, True
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
                return self._decompress_bounded(stream, limit, path), False

        try:
            info = path.lstat()
        except FileNotFoundError:
            packed = self._read_from_packs(key, retained=retained, expected_bytes=expected_bytes)
            if packed is not None:
                return packed, True
            if retained or expected_bytes is not None:
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(self.directory / key)) from None
            raise ValueError(f'ordinary file required: {self.directory / key}') from None
        if not is_ordinary_file_stat(info):
            raise ValueError(f'ordinary file required: {path}')
        with path.open('rb') as stream:
            return self._decompress_bounded(stream, limit, path), False

    def read(self, key: str, *, retained: bool = False,
             expected_bytes: int | None = None, dir_fd: int | None = None) -> bytes:
        self._validate_key(key)
        if expected_bytes is not None and (type(expected_bytes) is not int or expected_bytes < 0):
            raise ValueError('invalid expected object length')

        # Fast-path inside active pack_session(): PackShardHandle.read_frame() verifies CRC32 + SHA-256 + length.
        active = self._active_pack_session
        if active is not None and read_bytes is _READ_BYTES:
            packed = active.read(key, retained=retained, expected_bytes=expected_bytes)
            if packed is not None:
                return packed

        path = self.directory / key
        data: bytes | None = None
        verified_in_pack = False
        if hasattr(os, 'O_NOFOLLOW') and read_bytes is _READ_BYTES:
            target = key if dir_fd is not None else path
            try:
                fd = os.open(target, _OPEN_RO_NOFOLLOW, dir_fd=dir_fd)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    data, verified_in_pack = self._read_deflate_or_pack(
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
                data, verified_in_pack = self._read_deflate_or_pack(
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
        if verified_in_pack:
            return data
        if expected_bytes is not None and len(data) != expected_bytes:
            raise ValueError('content object changed (length changed)')
        if sha(data) != key:
            raise ValueError('content object changed')
        return data
