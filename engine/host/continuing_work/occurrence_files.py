"""Read, identify, cooperatively lock, and atomically replace local continuing-work occurrences."""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None

if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ('Internal', ctypes.c_void_p),
            ('InternalHigh', ctypes.c_void_p),
            ('Offset', wintypes.DWORD),
            ('OffsetHigh', wintypes.DWORD),
            ('hEvent', wintypes.HANDLE),
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

_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[Path, threading.Lock] = {}


def _thread_lock(path: Path) -> threading.Lock:
    key = path.resolve()
    with _LOCK_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _win32_lock(fd: int, lock_path: Path, *, shared: bool, nonblocking: bool, timeout: float) -> None:
    handle = msvcrt.get_osfhandle(fd)
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
            raise BlockingIOError('Host lock is busy: ' + str(lock_path))
        time.sleep(0.02)


def _win32_unlock(fd: int) -> None:
    handle = msvcrt.get_osfhandle(fd)
    overlapped = _OVERLAPPED()
    if not _UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
        err = ctypes.get_last_error()
        raise ctypes.WinError(err)


@contextmanager
def file_lock(path, *, shared: bool = False, nonblocking: bool = False, timeout: float = 60.0):
    """Cross-platform cooperative file lock (POSIX flock / Win32 LockFileEx) with thread serialization."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    tlock = _thread_lock(lock_path)
    if not tlock.acquire(blocking=not nonblocking):
        raise BlockingIOError('Host lock is busy: ' + str(lock_path))
    try:
        if lock_path.is_symlink():
            raise ValueError('Lock file must not be a symlink: ' + str(lock_path))
        with lock_path.open('a+b') as stream:
            locked = False
            try:
                if fcntl is not None:
                    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                    if nonblocking:
                        mode |= fcntl.LOCK_NB
                    fcntl.flock(stream.fileno(), mode)
                    locked = True
                elif os.name == 'nt':
                    _win32_lock(stream.fileno(), lock_path, shared=shared,
                                nonblocking=nonblocking, timeout=timeout)
                    locked = True
                yield stream
            finally:
                if locked:
                    if fcntl is not None:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                    elif os.name == 'nt':
                        _win32_unlock(stream.fileno())
    finally:
        tlock.release()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('nonfinite JSON constant: ' + value)


def loads_strict(raw):
    text = raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else raw
    return json.loads(text, object_pairs_hook=_unique_json, parse_constant=_reject_constant)


def sync_dir(path):
    if not hasattr(os, 'O_DIRECTORY'):
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.EBADF, errno.EROFS):
            raise
    finally:
        os.close(fd)


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def read(path):
    return loads_strict(Path(path).read_bytes())


def write(path, value):
    payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    atomic_bytes(path, payload)


def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}', value):
        raise ValueError('Use a simple local identity: '+str(value))
    return value


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label+' must be nonempty text')
    return value
