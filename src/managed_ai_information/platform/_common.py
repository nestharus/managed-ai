"""OS-independent filesystem, hashing, strict JSON, and weak-referenced lock helpers.

Stdlib-only leaf module safe for standalone vendoring (e.g. material_retention
recovery archives) with zero imports from runtime packages or heavy stdlib modules.
"""

import errno
import math
import os
import stat
import threading
import time
import weakref
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_REPARSE_ATTR = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_BYTES_LIKE = (bytes, bytearray, memoryview)
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


def is_reserved_windows_segment(name: str) -> bool:
    """Return True if a path segment collides with a Windows reserved device name."""
    if not isinstance(name, str) or not name:
        return False
    stem = name.rstrip(" .").split(".", 1)[0].upper()
    return stem in _WINDOWS_RESERVED_DEVICE_NAMES


def validate_safe_segment(name: str, label: str = "path segment") -> str:
    """Validate a portable path segment (rejects reserved Windows devices and trailing dot)."""
    if not isinstance(name, str) or not name or "\0" in name:
        raise ValueError(f"invalid {label}: {name!r}")

    if name in (".", "..") or "/" in name or "\\" in name or ":" in name:
        raise ValueError(f"unsafe {label}: {name!r}")

    if name.endswith((".", " ")):
        raise ValueError(f"trailing dot or space forbidden in {label}: {name!r}")

    if is_reserved_windows_segment(name):
        raise ValueError(f"reserved Windows device name forbidden in {label}: {name!r}")

    return name


def is_reparse_or_symlink_stat(info: os.stat_result) -> bool:
    """Return True for POSIX symlinks or Windows NTFS symlinks/junctions/reparse points."""
    return bool(
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_reparse_tag", 0)
        or (getattr(info, "st_file_attributes", 0) & _REPARSE_ATTR)
    )


def is_reparse_or_symlink(target: os.stat_result | Path | str) -> bool:
    """Return True if a stat_result or path is a symlink or NTFS reparse point."""
    if isinstance(target, os.stat_result):
        return is_reparse_or_symlink_stat(target)

    try:
        info = Path(target).lstat()
    except FileNotFoundError, NotADirectoryError:
        return False

    return is_reparse_or_symlink_stat(info)


def is_ordinary_file_stat(info: os.stat_result) -> bool:
    """Return True when stat_result refers to a regular file and not a symlink/reparse point."""
    return not is_reparse_or_symlink_stat(info) and stat.S_ISREG(info.st_mode)


def is_ordinary_dir_stat(info: os.stat_result) -> bool:
    """Return True when stat_result refers to a directory and not a symlink/reparse point."""
    return not is_reparse_or_symlink_stat(info) and stat.S_ISDIR(info.st_mode)


def has_ordinary_file(path: Path | str) -> bool:
    """Return True if path exists as an ordinary file."""
    try:
        return is_ordinary_file_stat(Path(path).lstat())
    except OSError:
        return False


def has_ordinary_dir(path: Path | str) -> bool:
    """Return True if path exists as an ordinary directory."""
    try:
        return is_ordinary_dir_stat(Path(path).lstat())
    except OSError:
        return False


def _validate_input_path_segments(path: Path | str, label: str) -> None:
    raw_text = os.fspath(path)
    if not isinstance(raw_text, str) or "\0" in raw_text:
        raise ValueError(f"invalid {label}: {path!r}")

    raw_path = Path(raw_text)
    segments = raw_path.parts[1:] if raw_path.anchor else raw_path.parts
    for segment in segments:
        if segment in ("", ".", ".."):
            continue
        validate_safe_segment(segment, label)


def safe_relpath(path: Path | str, start: Path | str) -> str:
    """Return a POSIX relative path on the same mount/drive, else canonical absolute path."""
    _validate_input_path_segments(path, "path segment")
    try:
        rel = os.path.relpath(path, start).replace("\\", "/")
    except ValueError:
        resolved = Path(path).resolve()
        for segment in resolved.parts[1:]:
            if segment not in ("", ".", ".."):
                validate_safe_segment(segment, "path segment")
        return str(resolved)

    for segment in rel.split("/"):
        if segment in ("", ".", ".."):
            continue
        validate_safe_segment(segment, "relative path segment")
    return rel


def admission_deadline(forwarded: float | None = None) -> float | None:
    """Return stricter of forwarded monotonic cutoff and admission-seconds env var."""
    if forwarded is not None and not math.isfinite(forwarded):
        raise ValueError("invalid admission deadline")

    env_name = "MANAGED_AI_INFORMATION_DOMAIN_ADMISSION_SECONDS"
    value = os.environ.get(env_name)
    if value is None:
        env_name = "MANAGED_AI_DOMAIN_ADMISSION_SECONDS"
        value = os.environ.get(env_name)
    if value is None:
        return forwarded

    seconds = float(value)
    if not math.isfinite(seconds) or not 0 <= seconds <= 600:
        raise ValueError(f"{env_name} must be between 0 and 600")

    end = time.monotonic() + seconds
    return end if forwarded is None else min(forwarded, end)


def strict_json_loads(
    raw: bytes | str,
    label: Path | str = "JSON",
    *,
    require_object: bool = True,
) -> Any:
    """Parse UTF-8 JSON strictly, rejecting duplicate keys and nonfinite constants."""
    import json

    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray, memoryview)) else raw

    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, val in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}: {label}")
            result[key] = val
        return result

    def reject_constant(token: str) -> Any:
        raise ValueError(f"nonfinite JSON constant {token!r}: {label}")

    value = json.loads(text, object_pairs_hook=unique_keys, parse_constant=reject_constant)
    if require_object and not isinstance(value, dict):
        raise ValueError(f"JSON object required: {label}")

    return value


def sha256_stream(stream: Any, *, chunk_size: int = 65536) -> str:
    """Compute a streaming SHA-256 hex digest from a binary stream without full-file buffering."""
    import hashlib

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    if chunk_size == 65536 and hasattr(hashlib, "file_digest"):
        return hashlib.file_digest(stream, "sha256").hexdigest()

    hasher = hashlib.sha256()
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        hasher.update(chunk)

    return hasher.hexdigest()


def sha256_file_common(
    path: Path | str,
    *,
    chunk_size: int = 65536,
    follow_symlinks: bool = False,
    open_ro_nofollow_flags: int = 0,
) -> str:
    """Compute a streaming SHA-256 hex digest of an ordinary file."""
    target = Path(path)

    if not follow_symlinks and open_ro_nofollow_flags and hasattr(os, "O_NOFOLLOW"):
        try:
            fd = os.open(target, open_ro_nofollow_flags)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENXIO, errno.ENOTDIR, errno.ENOENT):
                raise ValueError(f"ordinary file required: {target}") from None
            raise

        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f"ordinary file required: {target}")
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            return sha256_stream(stream, chunk_size=chunk_size)

    if not follow_symlinks:
        try:
            info = target.lstat()
        except OSError:
            raise ValueError(f"ordinary file required: {target}") from None
        if not is_ordinary_file_stat(info):
            raise ValueError(f"ordinary file required: {target}")

    with target.open("rb") as stream:
        if not follow_symlinks and not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"ordinary file required: {target}")
        return sha256_stream(stream, chunk_size=chunk_size)


def check_ordinary_ancestors_common(
    path: Path | str,
    *,
    expect_dir: bool = False,
    error_prefix: str = "ordinary file required",
    logical_cwd_fn: Callable[[], Path],
) -> Path:
    """Verify path and every ancestor contain no symlink or NTFS reparse point."""
    text = os.fspath(path)
    if not isinstance(text, str) or "\0" in text:
        raise ValueError(f"{error_prefix}: {path}")

    raw_path = Path(text)
    if raw_path.anchor and not raw_path.is_absolute():
        raise ValueError(f"{error_prefix}: {raw_path}")

    if not raw_path.is_absolute():
        base = logical_cwd_fn()
        parts = base.parts + raw_path.parts
    else:
        parts = raw_path.parts

    if not parts:
        raise ValueError(f"{error_prefix}: {raw_path}")

    current = Path(parts[0])
    try:
        if is_reparse_or_symlink_stat(current.lstat()):
            raise ValueError(f"{error_prefix}: {current}")
    except OSError:
        raise ValueError(f"{error_prefix}: {current}") from None

    for part in parts[1:]:
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        current = current / part
        try:
            if is_reparse_or_symlink_stat(current.lstat()):
                raise ValueError(f"{error_prefix}: {current}")
        except FileNotFoundError:
            continue
        except OSError:
            raise ValueError(f"{error_prefix}: {current}") from None

    resolved = Path(os.path.abspath(raw_path))
    for ancestor in resolved.parents:
        try:
            ancestor_info = ancestor.lstat()
        except OSError:
            raise ValueError(f"{error_prefix}: {resolved}") from None
        if is_reparse_or_symlink_stat(ancestor_info):
            raise ValueError(f"{error_prefix}: {resolved}")

    try:
        info = resolved.lstat()
    except OSError:
        raise ValueError(f"{error_prefix}: {resolved}") from None

    ok = is_ordinary_dir_stat(info) if expect_dir else is_ordinary_file_stat(info)
    if not ok:
        raise ValueError(f"{error_prefix}: {resolved}")

    return resolved


def check_unfollowed_ancestors_common(
    raw_path: Path | str,
    role: str = "Path",
    *,
    include_target: bool = True,
    logical_cwd_fn: Callable[[], Path],
) -> Path:
    """Reject symlinks/reparse points along every unfollowed ancestor, allowing missing target."""
    text = os.fspath(raw_path)
    if not isinstance(text, str) or not text.strip() or "\0" in text:
        raise ValueError(f"{role} must be a valid nonempty path")

    raw = Path(text)
    if not raw.is_absolute():
        if raw.drive or raw.anchor:
            raise ValueError(f"{role} must not use a drive-relative path: {text}")
        logical = logical_cwd_fn()
        for ancestor in (*reversed(logical.parents), logical):
            if is_reparse_or_symlink(ancestor):
                raise ValueError(f"{role} must not traverse a symlink or reparse point: {ancestor}")
        current = Path(os.getcwd())
        raw_parts = raw.parts
    else:
        current = Path(raw.anchor)
        raw_parts = raw.parts[1:]

    for idx, part in enumerate(raw_parts):
        if part == ".":
            continue
        is_last = idx == len(raw_parts) - 1
        current = current.parent if part == ".." else current / part
        if (include_target or not is_last) and is_reparse_or_symlink(current):
            raise ValueError(
                f"{role} must not traverse or be a symlink or reparse point: {current}"
            )

    abs_path = Path(os.path.abspath(raw))
    check_parts = (
        (*reversed(abs_path.parents), abs_path)
        if include_target
        else tuple(reversed(abs_path.parents))
    )
    for ancestor_part in check_parts:
        if is_reparse_or_symlink(ancestor_part):
            raise ValueError(
                f"{role} must not traverse or be a symlink or reparse point: {ancestor_part}"
            )

    return abs_path


def check_unfollowed_descendant_common(
    validated_root: Path | str,
    rel_path: Path | str,
    *,
    expect_dir: bool = False,
    allow_missing_leaf: bool = False,
    role: str = "Path",
) -> Path:
    """Validate only relative child segments below an already-validated root directory."""
    rel_text = os.fspath(rel_path)
    if not isinstance(rel_text, str) or not rel_text or "\0" in rel_text:
        raise ValueError(f"{role}: unsafe relative path")

    if "\\" in rel_text:
        rel_text = rel_text.replace("\\", "/")

    if rel_text.startswith("/") or (len(rel_text) >= 2 and rel_text[1] == ":"):
        raise ValueError(f"{role}: relative path required: {rel_path}")

    parts = [segment for segment in rel_text.split("/") if segment not in ("", ".")]
    if not parts:
        raise ValueError(f"{role}: unsafe relative path: {rel_path}")

    for segment in parts:
        validate_safe_segment(segment, role)

    current = Path(validated_root)
    for segment in parts[:-1]:
        current = current / segment
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing_leaf:
                continue
            raise ValueError(f"{role}: missing directory: {current}") from None
        except OSError:
            raise ValueError(f"{role}: ordinary directory required: {current}") from None
        if not is_ordinary_dir_stat(info):
            raise ValueError(f"{role}: ordinary directory required: {current}")

    leaf = current / parts[-1]
    try:
        info = leaf.lstat()
    except FileNotFoundError:
        if allow_missing_leaf:
            return leaf
        raise ValueError(f"{role}: missing path: {leaf}") from None
    except OSError:
        raise ValueError(f"{role}: ordinary path required: {leaf}") from None

    ok = is_ordinary_dir_stat(info) if expect_dir else is_ordinary_file_stat(info)
    if not ok:
        raise ValueError(
            f"{role}: ordinary {'directory' if expect_dir else 'file'} required: {leaf}"
        )

    return leaf


def validate_lock_timeout(timeout: float | None) -> float | None:
    """Validate that a lock timeout is either None or a nonnegative finite number."""
    if timeout is None:
        return None
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout < 0
    ):
        raise ValueError(f"lock timeout must be a nonnegative finite number: {timeout!r}")
    return float(timeout)


def atomic_bytes_common(
    path: Path | str,
    payload: bytes | bytearray | memoryview | Iterable[bytes | bytearray | memoryview],
    *,
    exclusive: bool = False,
    sync_dir_fn: Callable[[Path], None],
    publish_exclusive_fn: Callable[[Path, Path], None],
) -> None:
    """Atomically write bytes or an iterable of byte chunks with fsync and directory sync."""
    target = Path(path)
    temp = target.with_name("." + target.name + "." + os.urandom(16).hex())

    try:
        with temp.open("xb") as stream:
            if isinstance(payload, _BYTES_LIKE):
                stream.write(payload)
            else:
                for chunk in payload:
                    stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())

        if exclusive:
            publish_exclusive_fn(temp, target)
        else:
            os.replace(temp, target)

        sync_dir_fn(target.parent)
    finally:
        temp.unlink(missing_ok=True)


class ThreadLock:
    """Weak-referenceable wrapper around threading.Lock for leak-free keyed pools."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if not blocking:
            return self._lock.acquire(False)
        return self._lock.acquire(True, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> ThreadLock:
        self._lock.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._lock.release()


class ThreadRLockState:
    """Weak-referenceable reentrant lock paired with thread-local reentrancy state."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.local = threading.local()


class ThreadRWLock:
    """Weak-referenceable readers-writer lock matching shared/exclusive file-lock semantics."""

    def __init__(self) -> None:
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
                if self._readers <= 0:
                    raise RuntimeError("cannot release unacquired shared lock")
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()
            else:
                if not self._writer:
                    raise RuntimeError("cannot release unacquired exclusive lock")
                self._writer = False
                self._cond.notify_all()

    @contextmanager
    def hold(self, *, shared: bool = False, nonblocking: bool = False) -> Generator[ThreadRWLock]:
        if not self.acquire(shared=shared, nonblocking=nonblocking):
            raise BlockingIOError("in-process lock is busy")
        try:
            yield self
        finally:
            self.release(shared=shared)


class KeyedLockPool[T]:
    """Leak-free weakref.WeakValueDictionary pool for keyed in-process locks."""

    def __init__(self, factory: Callable[[], T]) -> None:
        self._factory = factory
        self._guard = threading.Lock()
        self._locks: weakref.WeakValueDictionary[Any, T] = weakref.WeakValueDictionary()

    def get(self, key: Any) -> T:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._factory()
                self._locks[key] = lock
            return lock

    def __len__(self) -> int:
        with self._guard:
            return len(self._locks)
