"""Cooperative locking, strict JSON codecs, reparse/path guards, and durability for material_retention."""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
from typing import Any
import zlib

from ._platform_bridge import platform
from .config import FORMAT, IMMUTABLE_RECORD_DIRS, RAW_BODY_KINDS

_LOCK_POOL = platform.KeyedLockPool(platform.ThreadRLockState)
_EVENT_LOCK_POOL = platform.KeyedLockPool(platform.ThreadLock)

is_reparse_or_symlink = platform.is_reparse_or_symlink
check_unfollowed_ancestors = platform.check_unfollowed_ancestors
check_unfollowed_descendant = platform.check_unfollowed_descendant
sync_dir = platform.sync_dir
sha256_stream = platform.sha256_stream


def sha(raw: bytes | bytearray | memoryview) -> str:
    """Return the lowercase SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(raw).hexdigest()


def sha256_file_checked(path: Path | str, *, label: str = "File") -> str:
    """Compute a streaming SHA-256 of an ordinary non-reparse file with uniform errors across OSes."""
    target = Path(path)
    require(
        not is_reparse_or_symlink(target),
        f"{label} must not be a symlink or reparse point: {target}",
    )
    try:
        return platform.sha256_file(target, follow_symlinks=False)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} must be an ordinary readable file: {target}") from exc


def loads_json(raw: bytes | bytearray | str) -> Any:
    """Parse UTF-8 JSON strictly, rejecting duplicate keys and nonfinite constants."""
    return platform.strict_json_loads(raw, label="Managed material JSON", require_object=False)


def js(value: Any) -> bytes:
    """Serialize a canonical UTF-8 JSON line with allow_nan=False."""
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (encoded + "\n").encode("utf-8")


def utc() -> str:
    """Return the current UTC timestamp as an ISO-8601 string."""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def require(ok: Any, message: str) -> None:
    """Raise ValueError with message when condition ok is falsy."""
    if not ok:
        raise ValueError(message)


def _open_store_lock_stream(root: Path, lock_path: Path, *, shared: bool) -> Any | None:
    """Open the cooperative .store.lock file, supporting read-only shared inspection."""
    if not shared:
        root.mkdir(parents=True, exist_ok=True)
        require(
            not is_reparse_or_symlink(lock_path),
            "Store lock path must not be a symlink or reparse point: " + str(lock_path),
        )
        return lock_path.open("a+b")

    require(
        not is_reparse_or_symlink(lock_path),
        "Store lock path must not be a symlink or reparse point: " + str(lock_path),
    )
    try:
        return lock_path.open("a+b")
    except OSError as open_exc:
        if open_exc.errno not in (errno.ENOENT, errno.EACCES, errno.EPERM, errno.EROFS):
            raise
        try:
            return lock_path.open("rb")
        except OSError as read_exc:
            if read_exc.errno in (errno.ENOENT, errno.EACCES, errno.EPERM, errno.EROFS):
                return None
            raise open_exc from read_exc


@contextmanager
def store_lock(root: Path | str, *, shared: bool = False, nonblocking: bool = False):
    """Cooperative store lock serializing index/CURRENT mutations across processes and threads."""
    checked_root = check_unfollowed_ancestors(root, "Store root", include_target=True)
    entry = _LOCK_POOL.get(checked_root.resolve())
    rlock = entry.lock
    state = entry.local

    if not rlock.acquire(blocking=not nonblocking):
        raise BlockingIOError("Managed material store is busy: " + str(checked_root / ".store.lock"))

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

        lock_path = checked_root / ".store.lock"
        stream = _open_store_lock_stream(checked_root, lock_path, shared=shared)
        if stream is None:
            state.depth = 1
            state.exclusive = False
            try:
                yield
            finally:
                state.depth = 0
            return

        with stream:
            locked = False
            try:
                platform.lock_file(stream, shared=shared, nonblocking=nonblocking, timeout=60.0)
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
                    platform.unlock_file(stream)
    finally:
        rlock.release()


def append_event_jsonl(root: Path | str, kind: str, data: dict[str, Any]) -> None:
    """Durably append one strict JSONL event row under cooperative cross-process lock."""
    checked_root = check_unfollowed_ancestors(root, "Store root", include_target=True)
    path = checked_root / "events.jsonl"
    lock_path = checked_root / ".events.lock"
    payload = js({"at": utc(), "kind": kind, **data})
    checked_root.mkdir(parents=True, exist_ok=True)

    event_lock = _EVENT_LOCK_POOL.get(path.resolve())
    with event_lock:
        require(
            not is_reparse_or_symlink(path),
            "Event log must not be a symlink or reparse point: " + str(path),
        )
        require(
            not is_reparse_or_symlink(lock_path),
            "Event lock must not be a symlink or reparse point: " + str(lock_path),
        )
        created = not path.exists()
        with lock_path.open("a+b") as lock_stream:
            locked = False
            try:
                platform.lock_file(lock_stream, shared=False, nonblocking=False, timeout=60.0)
                locked = True
                with path.open("ab") as stream:
                    require(
                        stat.S_ISREG(os.fstat(stream.fileno()).st_mode),
                        "Event log must be an ordinary file: " + str(path),
                    )
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if locked:
                    platform.unlock_file(lock_stream)
        if created:
            sync_dir(checked_root)


def _write_immutable_checked(target: Path, raw: bytes) -> None:
    """Verify existing immutable file bytes or atomically publish new immutable bytes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    require(
        not is_reparse_or_symlink(target),
        "Immutable object must not be a symlink or reparse point: " + str(target),
    )
    if target.exists():
        require(
            stat.S_ISREG(target.lstat().st_mode),
            "Immutable object must be an ordinary file: " + str(target),
        )
        require(target.read_bytes() == raw, "Immutable object conflict: " + str(target))
        return

    platform.atomic_bytes(target, raw)


def immutable(path: Path | str, raw: bytes) -> None:
    """Write an immutable content-addressed file with full ancestor reparse validation."""
    checked = check_unfollowed_ancestors(path, "Immutable object", include_target=True)
    _write_immutable_checked(checked, raw)


def immutable_below(validated_root: Path, rel_path: PurePosixPath | Path | str, raw: bytes) -> None:
    """Write an immutable file below an already-validated root without quadratic ancestor lstats."""
    checked = check_unfollowed_descendant(
        validated_root,
        rel_path,
        allow_missing_leaf=True,
        role="Immutable object",
    )
    _write_immutable_checked(checked, raw)


def replace_json(path: Path | str, value: Any) -> None:
    """Atomically replace a JSON pointer/index file with fsync and parent directory sync."""
    checked = check_unfollowed_ancestors(path, "Replaced JSON target", include_target=True)
    checked.parent.mkdir(parents=True, exist_ok=True)
    require(
        not is_reparse_or_symlink(checked),
        "Replaced JSON target must not be a symlink or reparse point: " + str(checked),
    )
    platform.atomic_bytes(checked, js(value))


def deflate(
    raw: bytes,
    level: int = 6,
    mem_level: int = 8,
    strategy: int = zlib.Z_DEFAULT_STRATEGY,
) -> bytes:
    """Encode raw bytes using raw DEFLATE framing (-15 window bits)."""
    compressor = zlib.compressobj(level, zlib.DEFLATED, -15, mem_level, strategy)
    return compressor.compress(raw) + compressor.flush()


def safe_relative(value: Any) -> PurePosixPath:
    """Validate a portable POSIX relative path, refusing traversal, drives, and Windows devices."""
    require(
        isinstance(value, str) and bool(value) and "\0" not in value and "\\" not in value,
        "Unsafe materialization path: " + str(value),
    )
    require(not PureWindowsPath(value).drive, "Unsafe materialization path: " + value)
    parsed = PurePosixPath(value)
    raw_segments = [seg for seg in value.split("/") if seg]
    require(
        not parsed.is_absolute()
        and bool(parsed.parts)
        and ".." not in parsed.parts
        and "." not in raw_segments
        and ".." not in raw_segments
        and not any(len(part) >= 2 and part[1] == ":" for part in parsed.parts)
        and not any(len(part) >= 2 and part[1] == ":" for part in raw_segments),
        "Unsafe materialization path: " + value,
    )

    for segment in raw_segments:
        try:
            platform.validate_safe_segment(segment, "materialization path")
        except ValueError as exc:
            raise ValueError("Unsafe materialization path: " + value) from exc

    return parsed


def path_state(path: Path) -> dict[str, int]:
    """Capture lstat metadata used to detect source drift during acquisition."""
    info = path.lstat()
    return {
        "bytes": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "mode": stat.S_IMODE(info.st_mode),
        "device": info.st_dev,
        "inode": info.st_ino,
        "type": stat.S_IFMT(info.st_mode),
    }


__all__ = [
    "FORMAT",
    "IMMUTABLE_RECORD_DIRS",
    "RAW_BODY_KINDS",
    "_EVENT_LOCK_POOL",
    "_LOCK_POOL",
    "append_event_jsonl",
    "check_unfollowed_ancestors",
    "check_unfollowed_descendant",
    "deflate",
    "immutable",
    "immutable_below",
    "is_reparse_or_symlink",
    "js",
    "loads_json",
    "path_state",
    "replace_json",
    "require",
    "safe_relative",
    "sha",
    "sha256_file_checked",
    "sha256_stream",
    "store_lock",
    "sync_dir",
    "utc",
]
