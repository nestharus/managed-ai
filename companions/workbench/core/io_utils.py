"""Shared canonical JSON, durable atomic I/O, and owner locking for the workbench."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import uuid

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


class OwnerLockError(OSError):
    """Raised when the cooperative owner lock cannot be acquired."""


class BasisMismatchError(ValueError):
    """Raised by restore hooks when a continuation would mix incompatible bases."""

    def __init__(self, message, *, code="binding_mismatch", details=None):
        super().__init__(message)
        self.code = code
        self.details = details


def detached_json(value):
    """Return a detached copy of finite JSON-compatible data."""
    active = set()

    def clone(item):
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("nonfinite JSON number")
            return item
        if not isinstance(item, (list, dict)):
            raise TypeError("durable value must be finite JSON")
        identity = id(item)
        if identity in active:
            raise ValueError("cyclic JSON value")
        active.add(identity)
        try:
            if isinstance(item, list):
                return [clone(v) for v in item]
            if any(not isinstance(k, str) for k in item):
                raise TypeError("JSON object keys must be strings")
            return {k: clone(v) for k, v in item.items()}
        finally:
            active.remove(identity)

    return clone(value)


def canonical_bytes(value):
    return json.dumps(
        detached_json(value),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def canonical_digest(value):
    return sha256_bytes(canonical_bytes(value))


def pin_file(path):
    data = Path(path).read_bytes()
    return {"bytes": len(data), "sha256": sha256_bytes(data)}


def loads_strict(raw):
    """Decode strict UTF-8 JSON, refusing duplicate object keys and nonfinite numbers."""
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8")
    elif isinstance(raw, str):
        text = raw
    else:
        raise TypeError("JSON input must be bytes or str")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("nonfinite JSON constant: " + value)

    return detached_json(json.loads(
        text,
        object_pairs_hook=unique_pairs,
        parse_constant=reject_constant,
    ))


def read_json_strict(path):
    return loads_strict(Path(path).read_bytes())


def sync_dir(path):
    if not hasattr(os, "O_DIRECTORY"):
        return
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.EBADF, errno.EROFS):
            raise
    finally:
        os.close(directory)


def atomic_bytes(path, data, *, exclusive=False):
    """Durably publish bytes via unique temp file, fsync, atomic install, and dir sync."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive and path.exists():
        raise FileExistsError(str(path))
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_json(path, value, *, exclusive=False):
    atomic_bytes(path, canonical_bytes(value) + b"\n", exclusive=exclusive)


def is_reparse_or_symlink(path: Path) -> bool:
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return False
    return bool(
        stat.S_ISLNK(info.st_mode)
        or (getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


@contextmanager
def exclusive_lock(lock_path):
    """Acquire a non-blocking cooperative exclusive lock on POSIX or Windows."""
    lock_path = Path(lock_path)
    if is_reparse_or_symlink(lock_path):
        raise OwnerLockError("owner lock must not be a symlink or reparse point")
    with lock_path.open("a+b") as handle:
        locked = False
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise OwnerLockError("another local owner holds this occurrence") from exc
                locked = True
            elif os.name == "nt":
                overlapped = _OVERLAPPED()
                ok = _LockFileEx(
                    msvcrt.get_osfhandle(handle.fileno()),
                    _LOCKFILE_EXCLUSIVE_LOCK | _LOCKFILE_FAIL_IMMEDIATELY,
                    0, 1, 0, ctypes.byref(overlapped),
                )
                if not ok:
                    err = ctypes.get_last_error()
                    if err in (_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING):
                        raise OwnerLockError("another local owner holds this occurrence")
                    raise ctypes.WinError(err)
                locked = True
            yield
        finally:
            if locked:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif os.name == "nt":
                    overlapped = _OVERLAPPED()
                    _UnlockFileEx(
                        msvcrt.get_osfhandle(handle.fileno()),
                        0, 1, 0, ctypes.byref(overlapped),
                    )
