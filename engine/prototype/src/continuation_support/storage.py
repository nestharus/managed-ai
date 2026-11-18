"""Filesystem durability and content-addressed bytes shared by the local engines."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterator
import uuid

MAX_BYTES = 2 * 1024 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path: Path, data: bytes) -> None:
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with temp.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    """Durably replace one JSON record."""
    atomic_bytes(path, encode(value))


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


@contextmanager
def exclusive(path: Path) -> Iterator[None]:
    with path.open("a+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another owner holds {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def shared(path: Path) -> Iterator[None]:
    """Share a cooperative observation guard; retain existing lock-file creation."""
    with path.open("a+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another owner holds {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read_bytes(path: Path, limit: int = MAX_BYTES) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'ordinary file required: {path}')
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'file exceeds {limit} bytes: {path}')
    return data


class ObjectStore:
    """Immutable content addressed by SHA-256 within one exchange."""

    def __init__(self, directory: Path):
        self.directory = directory

    def put(self, data: bytes) -> str:
        return self.put_retained(data, max_bytes=MAX_BYTES)

    def put_retained(self, data: bytes, *, max_bytes: int) -> str:
        """Allow a caller that already validated a historical head to retain its exact bytes."""
        if len(data) > max_bytes:
            raise ValueError('object exceeds the per-object limit')
        key = sha(data)
        path = self.directory / key
        if path.exists():
            if read_bytes(path, max_bytes) != data:
                raise ValueError('content object changed')
        else:
            atomic_bytes(path, data)
        return key

    def read(self, key: str, *, retained: bool = False,
             expected_bytes: int | None = None) -> bytes:
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('invalid object identity')
        path = self.directory / key
        limit = path.stat().st_size if retained else MAX_BYTES
        if expected_bytes is not None:
            if type(expected_bytes) is not int or expected_bytes < 0:
                raise ValueError('invalid expected object length')
            if path.stat().st_size != expected_bytes:
                raise ValueError('content object changed (length changed)')
            limit = expected_bytes if retained else min(expected_bytes, MAX_BYTES)
        data = read_bytes(path, limit)
        if expected_bytes is not None and len(data) != expected_bytes:
            raise ValueError('content object changed (length changed)')
        if sha(data) != key:
            raise ValueError('content object changed')
        return data
