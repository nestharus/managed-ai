"""Read, identify, cooperatively lock, and atomically replace continuing-work occurrences."""

import datetime as dt
import hashlib
import json
import os
import re
import stat
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from managed_ai.platform import (
    KeyedLockPool,
    ThreadRWLock,
    check_unfollowed_ancestors,
    is_reparse_or_symlink,
    is_reserved_windows_segment,
    lock_file,
    strict_json_loads,
    sync_dir,
    unlock_file,
)
from managed_ai.platform import (
    atomic_bytes as _platform_atomic_bytes,
)

_IDENT_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}")
_LOCK_POOL = KeyedLockPool(ThreadRWLock)


@contextmanager
def file_lock(
    path: Path | str,
    *,
    shared: bool = False,
    nonblocking: bool = False,
    timeout: float = 60.0,
) -> Generator[Any]:
    """Cross-platform cooperative file lock with leak-free in-process RW serialization."""
    lock_path = check_unfollowed_ancestors(path, "Lock file", include_target=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    tlock = _LOCK_POOL.get(lock_path.resolve())

    if not tlock.acquire(shared=shared, nonblocking=nonblocking):
        raise BlockingIOError("Host lock is busy: " + str(lock_path))

    try:
        if is_reparse_or_symlink(lock_path):
            raise ValueError("Lock file must not be a symlink or reparse point: " + str(lock_path))

        with lock_path.open("a+b") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Lock file must be an ordinary file: " + str(lock_path))

            locked = False
            try:
                lock_file(stream, shared=shared, nonblocking=nonblocking, timeout=timeout)
                locked = True
                yield stream
            finally:
                if locked:
                    unlock_file(stream)
    finally:
        tlock.release(shared=shared)


def now() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return dt.datetime.now(dt.UTC).isoformat()


def digest(raw: bytes | bytearray | memoryview) -> str:
    """Return the lowercase SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(raw).hexdigest()


def loads_strict(raw: bytes | bytearray | str) -> Any:
    """Parse UTF-8 JSON strictly, rejecting duplicate keys and nonfinite constants."""
    payload: bytes | str = bytes(raw) if isinstance(raw, bytearray) else raw
    return strict_json_loads(payload, label="Continuing work JSON", require_object=False)


def atomic_bytes(path: Path | str, data: bytes) -> None:
    """Durably replace path with data after verifying unfollowed non-reparse ancestors."""
    checked = check_unfollowed_ancestors(path, "Target file", include_target=True)
    checked.parent.mkdir(parents=True, exist_ok=True)
    if is_reparse_or_symlink(checked):
        raise ValueError("Target file must not be a symlink or reparse point: " + str(checked))
    _platform_atomic_bytes(checked, data)


def read(path: Path | str) -> Any:
    """Read and strictly parse an unfollowed UTF-8 JSON file."""
    checked = check_unfollowed_ancestors(path, "JSON file", include_target=True)
    return loads_strict(checked.read_bytes())


def write(path: Path | str, value: Any) -> None:
    """Serialize value with allow_nan=False and replace path atomically."""
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    atomic_bytes(path, encoded.encode("utf-8"))


def ident(value: Any) -> str:
    """Validate a portable local identity, refusing Windows reserved names and trailing
    dot/space.
    """
    if (
        not isinstance(value, str)
        or not _IDENT_RE.fullmatch(value)
        or value.endswith((".", " "))
        or is_reserved_windows_segment(value)
    ):
        raise ValueError("Use a simple local identity: " + str(value))
    return value


def nonempty(value: Any, label: str) -> str:
    """Require value to be a nonempty non-whitespace string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " must be nonempty text")
    return value


__all__ = [
    "_LOCK_POOL",
    "atomic_bytes",
    "check_unfollowed_ancestors",
    "digest",
    "file_lock",
    "ident",
    "is_reparse_or_symlink",
    "loads_strict",
    "nonempty",
    "now",
    "read",
    "sync_dir",
    "write",
]
