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
import stat
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

_REPARSE_ATTR = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[Path, '_ThreadRWLock'] = {}


def is_reparse_or_symlink(path) -> bool:
    """Return True if path is a symbolic link or Windows NTFS reparse point (junction/mount)."""
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return False
    return bool(stat.S_ISLNK(info.st_mode) or (getattr(info, 'st_file_attributes', 0) & _REPARSE_ATTR))


def check_unfollowed_ancestors(raw_path, role: str = 'Path', *, include_target: bool = True) -> Path:
    """Reject symlinks and NTFS reparse points along every unfollowed ancestor (including raw '..' prefixes)."""
    text = os.fspath(raw_path)
    if not isinstance(text, str) or not text.strip() or '\0' in text:
        raise ValueError(f'{role} must be a valid nonempty path')
    raw = Path(text)
    if not raw.is_absolute():
        if raw.drive or raw.anchor:
            raise ValueError(f'{role} must not use a drive-relative path: {text}')
        pwd = os.environ.get('PWD')
        if pwd and os.path.isabs(pwd):
            try:
                if os.path.samefile(pwd, os.getcwd()):
                    logical = Path(pwd)
                    for ancestor in (*reversed(logical.parents), logical):
                        if is_reparse_or_symlink(ancestor):
                            raise ValueError(f'{role} must not traverse a symlink or reparse point: {ancestor}')
            except OSError:
                pass
        cur = Path(os.getcwd())
        raw_parts = raw.parts
    else:
        cur = Path(raw.anchor)
        raw_parts = raw.parts[1:]
    for idx, part in enumerate(raw_parts):
        if part == '.':
            continue
        is_last = idx == len(raw_parts) - 1
        cur = cur.parent if part == '..' else cur / part
        if include_target or not is_last:
            if is_reparse_or_symlink(cur):
                raise ValueError(f'{role} must not traverse or be a symlink or reparse point: {cur}')
    abs_path = Path(os.path.abspath(raw))
    parts = (*reversed(abs_path.parents), abs_path) if include_target else tuple(reversed(abs_path.parents))
    for part in parts:
        if is_reparse_or_symlink(part):
            raise ValueError(f'{role} must not traverse or be a symlink or reparse point: {part}')
    return abs_path


class _ThreadRWLock:
    """In-process readers-writer lock matching POSIX/Win32 shared+exclusive semantics."""

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False

    def acquire(self, *, shared: bool, nonblocking: bool) -> bool:
        with self._cond:
            if shared:
                while self._writer:
                    if nonblocking:
                        return False
                    self._cond.wait()
                self._readers += 1
                return True
            while self._writer or self._readers > 0:
                if nonblocking:
                    return False
                self._cond.wait()
            self._writer = True
            return True

    def release(self, *, shared: bool) -> None:
        with self._cond:
            if shared:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()
            else:
                self._writer = False
                self._cond.notify_all()


def _thread_lock(path: Path) -> _ThreadRWLock:
    key = path.resolve()
    with _LOCK_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = _ThreadRWLock()
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
    lock_path = check_unfollowed_ancestors(path, 'Lock file', include_target=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    tlock = _thread_lock(lock_path)
    if not tlock.acquire(shared=shared, nonblocking=nonblocking):
        raise BlockingIOError('Host lock is busy: ' + str(lock_path))
    try:
        if is_reparse_or_symlink(lock_path):
            raise ValueError('Lock file must not be a symlink or reparse point: ' + str(lock_path))
        with lock_path.open('a+b') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Lock file must be an ordinary file: ' + str(lock_path))
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
        tlock.release(shared=shared)


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
    path = check_unfollowed_ancestors(path, 'Target file', include_target=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_reparse_or_symlink(path):
        raise ValueError('Target file must not be a symlink or reparse point: ' + str(path))
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
    checked = check_unfollowed_ancestors(path, 'JSON file', include_target=True)
    return loads_strict(checked.read_bytes())


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
