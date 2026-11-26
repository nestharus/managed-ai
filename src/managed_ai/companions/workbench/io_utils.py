"""Shared canonical JSON, durable atomic I/O, and owner locking for the workbench."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from managed_ai.platform import (
    atomic_bytes as _platform_atomic_bytes,
    is_reparse_or_symlink,
    lock_file_nonblocking,
    strict_json_loads,
    sync_dir,
    unlock_file,
)


class OwnerLockError(OSError):
    """Raised when the cooperative owner lock cannot be acquired."""


class BasisMismatchError(ValueError):
    """Raised by restore hooks when a continuation would mix incompatible bases."""

    def __init__(self, message: str, *, code: str = "binding_mismatch", details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def detached_json(value: Any) -> Any:
    """Return a detached copy of finite JSON-compatible data."""
    active: set[int] = set()

    def clone(item: Any) -> Any:
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


def canonical_bytes(value: Any) -> bytes:
    """Encode finite JSON canonically without a redundant Python deep-copy pass."""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(data: bytes | bytearray | memoryview) -> str:
    """Return the lowercase SHA-256 hex digest of data."""
    return hashlib.sha256(data).hexdigest()


def canonical_digest(value: Any) -> str:
    """Return the SHA-256 hex digest of canonical UTF-8 JSON bytes."""
    return sha256_bytes(canonical_bytes(value))


def pin_file(path: Path | str) -> dict[str, Any]:
    """Read an existing file and return its byte count and SHA-256 hex digest."""
    data = Path(path).read_bytes()
    return {"bytes": len(data), "sha256": sha256_bytes(data)}


def loads_strict(raw: bytes | bytearray | str) -> Any:
    """Decode strict UTF-8 JSON, refusing duplicate object keys and nonfinite numbers."""
    if not isinstance(raw, (bytes, bytearray, str)):
        raise TypeError("JSON input must be bytes or str")
    return strict_json_loads(raw, label="Workbench JSON", require_object=False)


def read_json_strict(path: Path | str) -> Any:
    """Read and strictly decode a UTF-8 JSON file."""
    return loads_strict(Path(path).read_bytes())


def atomic_bytes(path: Path | str, data: bytes, *, exclusive: bool = False) -> None:
    """Durably publish bytes via unique temp file, fsync, atomic install, and dir sync."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _platform_atomic_bytes(target, data, exclusive=exclusive)


def atomic_json(path: Path | str, value: Any, *, exclusive: bool = False) -> None:
    """Durably publish canonical JSON followed by a trailing newline."""
    atomic_bytes(path, canonical_bytes(value) + b"\n", exclusive=exclusive)


@contextmanager
def exclusive_lock(lock_path: Path | str):
    """Acquire a non-blocking cooperative exclusive lock via managed_ai.platform."""
    target = Path(lock_path)
    if is_reparse_or_symlink(target):
        raise OwnerLockError("owner lock must not be a symlink or reparse point")

    with target.open("a+b") as handle:
        locked = False
        try:
            try:
                lock_file_nonblocking(handle, exclusive=True)
            except BlockingIOError as exc:
                raise OwnerLockError("another local owner holds this occurrence") from exc
            locked = True
            yield
        finally:
            if locked:
                unlock_file(handle)


__all__ = [
    "BasisMismatchError",
    "OwnerLockError",
    "atomic_bytes",
    "atomic_json",
    "canonical_bytes",
    "canonical_digest",
    "detached_json",
    "exclusive_lock",
    "is_reparse_or_symlink",
    "loads_strict",
    "pin_file",
    "read_json_strict",
    "sha256_bytes",
    "sync_dir",
]
