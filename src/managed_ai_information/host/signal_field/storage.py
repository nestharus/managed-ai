"""Pure-stdlib JSON storage, path boundary validation, and read-only planning snapshots."""

import copy
import hashlib
import os
import stat
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from managed_ai_information.platform import (
    KeyedLockPool,
    ThreadLock,
    atomic_bytes,
    check_unfollowed_ancestors,
    is_reparse_or_symlink,
    lock_file,
    sha256_file,
    sync_dir,
    unlock_file,
)

from .config import DEFAULT_CONFIG, SignalFieldConfig, digest, encoded, loads_strict, require

_SQLITE_HEADER = b"SQLite format 3\x00"
_LOCK_POOL: KeyedLockPool[ThreadLock] = KeyedLockPool(ThreadLock)


def product_root() -> Path:
    """Return the enclosing package root, guarding against internal DB placement."""
    here = Path(__file__).resolve().parent
    if here.name == "signal_field" and here.parent.name == "host":
        return here.parents[1]
    return here


def inside_product(path: Path, product: Path | None = None) -> bool:
    """Return True if path resolves inside the package or release root."""
    root = product_root() if product is None else Path(product).resolve()
    resolved = Path(path).expanduser().resolve()
    if resolved == root or resolved.is_relative_to(root):
        return True

    return (root.parent / "CURRENT-RELEASE.json").is_file() and (
        resolved == root.parent or resolved.is_relative_to(root.parent)
    )


def require_ordinary_non_symlink(
    path: Path | str,
    role: str,
    *,
    must_exist: bool = False,
) -> Path:
    """Verify path and all ancestors are ordinary, unfollowed, non-reparse entries."""
    expanded = Path(path).expanduser()
    abs_path = check_unfollowed_ancestors(expanded, role, include_target=False)
    require(
        not is_reparse_or_symlink(abs_path),
        f"{role} must not be a symlink or reparse point: {abs_path}",
    )

    try:
        info = abs_path.lstat()
    except FileNotFoundError:
        if must_exist:
            raise ValueError(f"ordinary {role} required: {abs_path}") from None
        return abs_path

    require(stat.S_ISREG(info.st_mode), f"ordinary {role} required: {abs_path}")
    return abs_path


def validate_db_path(path: Path | str, *, product: Path | None = None) -> Path:
    """Validate that a signal-field store path and its sidecars are ordinary and outside the
    package.
    """
    require(
        isinstance(path, (str, Path)) and bool(str(path).strip()) and "\0" not in str(path),
        "database path must be a valid nonempty path",
    )

    raw = Path(path).expanduser()
    resolved = raw.resolve()
    require(
        not inside_product(resolved, product)
        and not inside_product(Path(os.path.abspath(raw)), product),
        "database must be outside the product directory",
    )

    abs_db = require_ordinary_non_symlink(raw, "database file", must_exist=False)
    for suffix in ("-journal", "-wal", "-shm", ".lock"):
        require_ordinary_non_symlink(
            Path(str(abs_db) + suffix), "database journal file", must_exist=False
        )

    if abs_db.parent.exists():
        require(
            abs_db.parent.is_dir() and not is_reparse_or_symlink(abs_db.parent),
            "database parent must be an ordinary directory",
        )

    return resolved


def no_journal(path: Path | str) -> None:
    """Refuse planning snapshots when legacy journal or WAL sidecars are present."""
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        require(
            not sidecar.exists() and not is_reparse_or_symlink(sidecar),
            "host journal/WAL state requires its owner; no host operations prepared",
        )


def _empty_state(config: SignalFieldConfig) -> dict[str, Any]:
    return {
        "meta": {"format": config.format_version, "sequence": "0"},
        "sources": {},
        "uses": {},
        "dependencies": {},
        "jobs": {},
        "selected_history": {},
    }


def _parse_store_bytes(raw: bytes, config: SignalFieldConfig) -> dict[str, Any]:
    if raw.startswith(_SQLITE_HEADER):
        raise ValueError(
            "legacy SQLite signal-field database detected; migrate to pure-JSON format before "
            "opening"
        )
    try:
        doc = loads_strict(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"corrupt signal field store: {exc}") from exc

    require(isinstance(doc, dict), "unsupported signal field format")
    meta = doc.get("meta")
    require(
        isinstance(meta, dict) and meta.get("format") == config.format_version,
        "unsupported signal field format",
    )
    meta.setdefault("sequence", "0")
    for key in ("sources", "uses", "dependencies", "jobs", "selected_history"):
        bucket = doc.get(key)
        require(isinstance(bucket, dict), f"invalid signal field {key} section")
    for job in doc["jobs"].values():
        if isinstance(job, dict):
            job.setdefault("worker_outcome", None)
    return doc


def _open_lock_file(lock_path: Path) -> Any:
    require_ordinary_non_symlink(lock_path, "database journal file", must_exist=False)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        st = os.fstat(fd)
        require(stat.S_ISREG(st.st_mode), f"ordinary database journal file required: {lock_path}")
        return os.fdopen(fd, "r+b")
    except BaseException:
        os.close(fd)
        raise


class SignalFieldStore:
    """Lock-fenced atomic pure-stdlib JSON store for signal_field."""

    def __init__(self, path: Path, config: SignalFieldConfig = DEFAULT_CONFIG) -> None:
        self.path = path
        self.config = config
        self.lock_path = Path(str(path) + ".lock")
        self.closed = False
        self._stat_key: tuple[int, int, int, int] | None = None
        self._last_raw: bytes = b""
        self.last_committed_bytes: bytes | None = None
        self.data: dict[str, Any] = _empty_state(config)
        self._init_or_load()

    @contextmanager
    def _file_lock(self) -> Generator[None]:
        require(not self.closed, "signal field store is closed")
        with _LOCK_POOL.get(self.path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _open_lock_file(self.lock_path) as handle:
                lock_file(handle)
                try:
                    yield
                finally:
                    unlock_file(handle)

    def _init_or_load(self) -> None:
        with self._file_lock():
            if not self.path.exists():
                raw = (encoded(self.data) + "\n").encode("utf-8")
                atomic_bytes(self.path, raw)
                sync_dir(self.path.parent)
                st = self.path.lstat()
                self._stat_key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
                self._last_raw = raw
                return
            self._reload_unlocked(force=True)

    def _reload_unlocked(self, *, force: bool = False) -> None:
        require_ordinary_non_symlink(self.path, "database file", must_exist=True)
        st = self.path.lstat()
        key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
        if not force and self._stat_key == key:
            return
        raw = self.path.read_bytes()
        self.data = _parse_store_bytes(raw, self.config)
        self._stat_key = key
        self._last_raw = (encoded(self.data) + "\n").encode("utf-8")

    def refresh(self) -> dict[str, Any]:
        with self._file_lock():
            self._reload_unlocked(force=False)
            return copy.deepcopy(self.data)

    @contextmanager
    def transaction(self) -> Generator[dict[str, Any]]:
        self.last_committed_bytes = None
        with self._file_lock():
            self._reload_unlocked(force=True)
            before_raw = self._last_raw
            snapshot = copy.deepcopy(self.data)
            try:
                yield self.data
                new_raw = (encoded(self.data) + "\n").encode("utf-8")
                if new_raw != before_raw:
                    atomic_bytes(self.path, new_raw)
                    sync_dir(self.path.parent)
                    st = self.path.lstat()
                    self._stat_key = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
                    self._last_raw = new_raw
                    self.last_committed_bytes = new_raw
            except BaseException:
                self.data = snapshot
                self.last_committed_bytes = None
                raise

    def serialized_bytes(self) -> bytes:
        with self._file_lock():
            self._reload_unlocked(force=False)
            return self._last_raw

    def close(self) -> None:
        self.closed = True


def open_signal_database(
    path: Path,
    config: SignalFieldConfig = DEFAULT_CONFIG,
) -> SignalFieldStore:
    """Create or open a pure-stdlib signal-field JSON store and apply additive migrations."""
    return SignalFieldStore(path, config)


def _verify_snapshot_unchanged(path: Path, expected_sha: str, expected_bytes: int) -> None:
    """Verify the underlying host store stayed byte-identical without buffering it in RAM."""
    info = path.lstat()
    require(
        not is_reparse_or_symlink(info) and stat.S_ISREG(info.st_mode),
        "host database changed during planning snapshot",
    )
    require(
        info.st_size == expected_bytes and sha256_file(path) == expected_sha,
        "host database changed during planning snapshot",
    )
    no_journal(path)


def planning_snapshot(
    path: Path | str,
    read_guard: Callable[[], bytes],
    *,
    config: SignalFieldConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    """Read the exact host store cut guarded by the caller without repeated full copies."""
    checked = require_ordinary_non_symlink(Path(path), "database file", must_exist=True)
    no_journal(checked)

    database_raw = read_guard()
    expected_sha = hashlib.sha256(database_raw).hexdigest()
    expected_bytes = len(database_raw)
    doc = _parse_store_bytes(database_raw, config)
    del database_raw

    sources = {
        ident: {
            "source": ident,
            "epoch": row["epoch"],
            "position": row["position"],
            "value": loads_strict(row["value"]),
        }
        for ident, row in sorted(doc["sources"].items())
    }

    uses: dict[str, dict[str, Any]] = {}
    for ident, row in sorted(doc["uses"].items()):
        spec = loads_strict(row["spec"])
        signature = digest(spec)
        require(
            spec.get("id") == ident
            and signature == row["signature"]
            and type(row["generation"]) is int
            and row["generation"] > 0,
            "host use identity/signature differs",
        )
        uses[ident] = spec

    dependencies = {
        (source, use_id) for source, use_ids in doc["dependencies"].items() for use_id in use_ids
    }
    expected_deps = {(source, ident) for ident, spec in uses.items() for source in spec["sources"]}
    require(
        dependencies == expected_deps,
        "host dependency rows differ from declared use definitions",
    )

    jobs = [
        {
            "id": row["id"],
            "use_id": row["use_id"],
            "generation": row["generation"],
            "ticket": row["ticket"],
            "token": row["token"],
            "gap": row["gap"],
        }
        for _, row in sorted(doc["jobs"].items())
    ]

    _verify_snapshot_unchanged(checked, expected_sha, expected_bytes)
    return {"sources": sources, "uses": uses, "jobs": jobs}
