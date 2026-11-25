"""Cross-platform OS primitives for cooperative locking, durability, ordinary files, and strict JSON.

Supports POSIX (Linux/macOS) and native Windows with zero external dependencies or compatibility bridges.
"""
from __future__ import annotations

import errno
import json
import math
import os
from pathlib import Path
import stat
import time
from typing import Any

if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    import msvcrt

    fcntl = None  # type: ignore[assignment]

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ('Internal', ctypes.c_void_p),
            ('InternalHigh', ctypes.c_void_p),
            ('Offset', wintypes.DWORD),
            ('OffsetHigh', wintypes.DWORD),
            ('hEvent', wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _kernel32.UnlockFileEx.restype = wintypes.BOOL

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_SHARING_VIOLATION = 32
    _LOCK_ALL_LOW = 0xFFFFFFFF
    _LOCK_ALL_HIGH = 0xFFFFFFFF
else:
    import fcntl


OPEN_RO_NOFOLLOW = (
    os.O_RDONLY
    | getattr(os, 'O_BINARY', 0)
    | getattr(os, 'O_NOFOLLOW', 0)
    | getattr(os, 'O_NONBLOCK', 0)
    | getattr(os, 'O_NOCTTY', 0)
)
OPEN_DIR_NOFOLLOW = (
    os.O_RDONLY
    | getattr(os, 'O_DIRECTORY', 0)
    | getattr(os, 'O_NOFOLLOW', 0)
)
_REPARSE_ATTR = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)


def is_reparse_or_symlink(info: os.stat_result) -> bool:
    """Return True for POSIX symlinks or Windows NTFS symlinks/junctions/reparse points."""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & _REPARSE_ATTR)


def is_ordinary_file_stat(info: os.stat_result) -> bool:
    return not is_reparse_or_symlink(info) and stat.S_ISREG(info.st_mode)


def is_ordinary_dir_stat(info: os.stat_result) -> bool:
    return not is_reparse_or_symlink(info) and stat.S_ISDIR(info.st_mode)


def has_ordinary_file(path: Path) -> bool:
    try:
        return is_ordinary_file_stat(path.lstat())
    except OSError:
        return False


def has_ordinary_dir(path: Path) -> bool:
    try:
        return is_ordinary_dir_stat(path.lstat())
    except OSError:
        return False


def check_ordinary_ancestors(path: Path, *, expect_dir: bool = False,
                             error_prefix: str = 'ordinary file required') -> Path:
    """Verify path and every parent component contains no symlink or NTFS reparse point."""
    raw_path = Path(path)
    if raw_path.anchor and not raw_path.is_absolute():
        raise ValueError(f'{error_prefix}: {raw_path}')
    if not raw_path.is_absolute():
        base = Path.cwd()
        if os.name != 'nt':
            pwd = os.environ.get('PWD')
            if pwd and os.path.isabs(pwd):
                try:
                    if os.path.samefile(pwd, '.'):
                        base = Path(pwd)
                except OSError:
                    pass
        parts = base.parts + raw_path.parts
    else:
        parts = raw_path.parts
    if not parts:
        raise ValueError(f'{error_prefix}: {raw_path}')
    current = Path(parts[0])
    try:
        if is_reparse_or_symlink(current.lstat()):
            raise ValueError(f'{error_prefix}: {current}')
    except OSError:
        raise ValueError(f'{error_prefix}: {current}') from None
    for part in parts[1:]:
        if part in ('', '.'):
            continue
        if part == '..':
            current = current.parent
            continue
        current = current / part
        try:
            if is_reparse_or_symlink(current.lstat()):
                raise ValueError(f'{error_prefix}: {current}')
        except FileNotFoundError:
            # Nonexistent intermediates cannot be symlinks/reparse points (lstat succeeds on
            # dangling symlinks). Final resolved ancestors and leaf are strictly verified below.
            continue
        except OSError:
            raise ValueError(f'{error_prefix}: {current}') from None
    resolved = Path(os.path.abspath(raw_path))
    for ancestor in resolved.parents:
        try:
            a_info = ancestor.lstat()
        except OSError:
            raise ValueError(f'{error_prefix}: {resolved}') from None
        if is_reparse_or_symlink(a_info):
            raise ValueError(f'{error_prefix}: {resolved}')
    try:
        info = resolved.lstat()
    except OSError:
        raise ValueError(f'{error_prefix}: {resolved}') from None
    ok = is_ordinary_dir_stat(info) if expect_dir else is_ordinary_file_stat(info)
    if not ok:
        raise ValueError(f'{error_prefix}: {resolved}')
    return resolved


def safe_relpath(path: Path | str, start: Path | str) -> str:
    """Return a POSIX-separated relative path when on the same mount/drive, else canonical absolute path."""
    try:
        return os.path.relpath(path, start).replace('\\', '/')
    except ValueError:
        return str(Path(path).resolve())


def admission_deadline(forwarded: float | None = None) -> float | None:
    """Return the stricter of a forwarded monotonic cutoff and MANAGED_AI_DOMAIN_ADMISSION_SECONDS."""
    if forwarded is not None and not math.isfinite(forwarded):
        raise ValueError('invalid admission deadline')
    value = os.environ.get('MANAGED_AI_DOMAIN_ADMISSION_SECONDS')
    if value is None:
        return forwarded
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 <= seconds <= 600:
        raise ValueError('MANAGED_AI_DOMAIN_ADMISSION_SECONDS must be between 0 and 600')
    end = time.monotonic() + seconds
    return end if forwarded is None else min(forwarded, end)


def sync_dir(directory: Path) -> None:
    """Flush directory metadata on POSIX; clean no-op on platforms without O_DIRECTORY fsync."""
    if not hasattr(os, 'O_DIRECTORY'):
        return
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def lock_file_nonblocking(stream, *, exclusive: bool) -> None:
    """Acquire a nonblocking cooperative shared or exclusive file lock.

    Raises BlockingIOError when another holder currently conflicts.
    """
    if os.name == 'nt':
        handle = msvcrt.get_osfhandle(stream.fileno())
        flags = _LOCKFILE_FAIL_IMMEDIATELY | (_LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0)
        ov = _OVERLAPPED()
        ok = _kernel32.LockFileEx(handle, flags, 0, _LOCK_ALL_LOW, _LOCK_ALL_HIGH, ctypes.byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err in (_ERROR_LOCK_VIOLATION, _ERROR_SHARING_VIOLATION):
                raise BlockingIOError(errno.EAGAIN, 'file is locked by another owner')
            raise ctypes.WinError(err)
        return
    mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
    fcntl.flock(stream.fileno(), mode)


def unlock_file(stream) -> None:
    """Release a cooperative file lock acquired via lock_file_nonblocking."""
    if os.name == 'nt':
        handle = msvcrt.get_osfhandle(stream.fileno())
        ov = _OVERLAPPED()
        _kernel32.UnlockFileEx(handle, 0, _LOCK_ALL_LOW, _LOCK_ALL_HIGH, ctypes.byref(ov))
        return
    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def strict_json_loads(raw: bytes | str, label: Path | str = 'JSON', *,
                      require_object: bool = True) -> Any:
    """Parse UTF-8 JSON strictly, rejecting duplicate keys and nonfinite constants."""
    text = raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else raw

    def unique_keys(pairs):
        result = {}
        for key, val in pairs:
            if key in result:
                raise ValueError(f'duplicate JSON key {key!r}: {label}')
            result[key] = val
        return result

    def reject_constant(token):
        raise ValueError(f'nonfinite JSON constant {token!r}: {label}')

    value = json.loads(text, object_pairs_hook=unique_keys, parse_constant=reject_constant)
    if require_object and not isinstance(value, dict):
        raise ValueError(f'JSON object required: {label}')
    return value
