"""Cooperative locking, strict JSON codecs, atomic durability, and event logging for managed_material."""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import threading
import time
import uuid
import zlib

try:
    import fcntl
except ImportError:
    fcntl = None

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _LockFileEx = _kernel32.LockFileEx
    _LockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_OVERLAPPED),
    ]
    _LockFileEx.restype = wintypes.BOOL
    _UnlockFileEx = _kernel32.UnlockFileEx
    _UnlockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(_OVERLAPPED),
    ]
    _UnlockFileEx.restype = wintypes.BOOL
    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_IO_PENDING = 997

FORMAT = "managed-material/1"
RAW_BODY_KINDS = {"blob", "slice-view", "record-view", "recipe-view"}
IMMUTABLE_RECORD_DIRS = ("manifests", "selections", "lifecycle", "occurrences", "transitions")

_LOCK_REGISTRY_GUARD = threading.Lock()
_LOCK_REGISTRY: dict[Path, tuple[threading.RLock, threading.local]] = {}
_EVENT_LOCKS_GUARD = threading.Lock()
_EVENT_LOCKS: dict[Path, threading.Lock] = {}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Nonfinite JSON constant: " + value)


def loads_json(raw):
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    return json.loads(text, object_pairs_hook=_unique_json, parse_constant=_reject_constant)


def js(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sync_dir(path):
    if not hasattr(os, "O_DIRECTORY"):
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.EBADF, errno.EROFS):
            raise
    finally:
        os.close(fd)


def _lock_entry(root):
    key = Path(root).resolve()
    with _LOCK_REGISTRY_GUARD:
        entry = _LOCK_REGISTRY.get(key)
        if entry is None:
            entry = (threading.RLock(), threading.local())
            _LOCK_REGISTRY[key] = entry
        return entry


def _event_lock(path):
    key = Path(path).resolve()
    with _EVENT_LOCKS_GUARD:
        lock = _EVENT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _EVENT_LOCKS[key] = lock
        return lock


def _win32_lock(stream, lock_path, *, shared=False, nonblocking=False, timeout=60.0):
    handle = msvcrt.get_osfhandle(stream.fileno())
    flags = _LOCKFILE_FAIL_IMMEDIATELY
    if not shared:
        flags |= _LOCKFILE_EXCLUSIVE_LOCK
    deadline = time.monotonic() + timeout
    while True:
        overlapped = _OVERLAPPED()
        if _LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
            return
        err = ctypes.get_last_error()
        if err not in (_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING):
            raise ctypes.WinError(err)
        if nonblocking or time.monotonic() >= deadline:
            raise BlockingIOError("Managed material store is busy: " + str(lock_path))
        time.sleep(0.02)


def _win32_unlock(stream):
    handle = msvcrt.get_osfhandle(stream.fileno())
    overlapped = _OVERLAPPED()
    if not _UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
        err = ctypes.get_last_error()
        raise ctypes.WinError(err)


@contextmanager
def store_lock(root, *, shared=False, nonblocking=False):
    """Cooperative store lock serializing index/CURRENT mutations across processes and threads."""
    root = Path(root)
    rlock, state = _lock_entry(root)
    if not rlock.acquire(blocking=not nonblocking):
        raise BlockingIOError("Managed material store is busy: " + str(root / ".store.lock"))
    try:
        depth = getattr(state, "depth", 0)
        if depth > 0:
            if not shared and not getattr(state, "exclusive", False):
                raise RuntimeError("Cannot upgrade a shared managed-material store lock to exclusive")
            state.depth = depth + 1
            try:
                yield
            finally:
                state.depth -= 1
            return
        lock_path = root / ".store.lock"
        if shared:
            require(not lock_path.is_symlink(), "Store lock path must not be a symlink: " + str(lock_path))
            try:
                stream = lock_path.open("a+b")
            except OSError as open_exc:
                if open_exc.errno not in (errno.ENOENT, errno.EACCES, errno.EPERM, errno.EROFS):
                    raise
                try:
                    stream = lock_path.open("rb")
                except OSError as read_exc:
                    if read_exc.errno in (errno.ENOENT, errno.EACCES, errno.EPERM, errno.EROFS):
                        state.depth = 1
                        state.exclusive = False
                        try:
                            yield
                        finally:
                            state.depth = 0
                        return
                    raise open_exc from read_exc
        else:
            root.mkdir(parents=True, exist_ok=True)
            require(not lock_path.is_symlink(), "Store lock path must not be a symlink: " + str(lock_path))
            stream = lock_path.open("a+b")
        with stream:
            locked = False
            try:
                if fcntl is not None:
                    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                    if nonblocking:
                        mode |= fcntl.LOCK_NB
                    fcntl.flock(stream.fileno(), mode)
                    locked = True
                elif os.name == "nt":
                    _win32_lock(stream, lock_path, shared=shared, nonblocking=nonblocking)
                    locked = True
                state.depth = 1
                state.exclusive = not shared
                try:
                    yield
                finally:
                    state.depth = 0
                    state.exclusive = False
            finally:
                if locked:
                    if fcntl is not None:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                    elif os.name == "nt":
                        _win32_unlock(stream)
    finally:
        rlock.release()


def append_event_jsonl(root, kind, data):
    root = Path(root)
    path = root / "events.jsonl"
    lock_path = root / ".events.lock"
    payload = js({"at": utc(), "kind": kind, **data})
    root.mkdir(parents=True, exist_ok=True)
    with _event_lock(path):
        require(not path.is_symlink(), "Event log must not be a symlink: " + str(path))
        require(not lock_path.is_symlink(), "Event lock must not be a symlink: " + str(lock_path))
        created = not path.exists()
        with lock_path.open("a+b") as lock_stream:
            locked = False
            try:
                if fcntl is not None:
                    fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
                    locked = True
                elif os.name == "nt":
                    _win32_lock(lock_stream, lock_path, shared=False, nonblocking=False)
                    locked = True
                with path.open("ab") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
            finally:
                if locked:
                    if fcntl is not None:
                        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
                    elif os.name == "nt":
                        _win32_unlock(lock_stream)
        if created:
            sync_dir(root)


def immutable(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "Immutable object must not be a symlink: " + str(path))
    if path.exists():
        require(path.read_bytes() == raw, "Immutable object conflict: " + str(path))
        return
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def replace_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "Replaced JSON target must not be a symlink: " + str(path))
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as f:
            f.write(js(value))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def deflate(raw, level=6, mem_level=8, strategy=zlib.Z_DEFAULT_STRATEGY):
    c = zlib.compressobj(level, zlib.DEFLATED, -15, mem_level, strategy)
    return c.compress(raw) + c.flush()


def safe_relative(value):
    p = PurePosixPath(value)
    require(not p.is_absolute() and p.parts and ".." not in p.parts and "\\" not in value,
            "Unsafe materialization path: " + value)
    return p


def path_state(path):
    s = path.lstat()
    return {"bytes": s.st_size, "mtime_ns": s.st_mtime_ns, "mode": stat.S_IMODE(s.st_mode),
            "device": s.st_dev, "inode": s.st_ino, "type": stat.S_IFMT(s.st_mode)}
