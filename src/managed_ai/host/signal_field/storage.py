"""SQLite schema, path boundary validation, and read-only planning snapshots for signal_field."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Callable

from managed_ai.platform import (
    check_unfollowed_ancestors,
    is_reparse_or_symlink,
    sha256_file,
)

from .config import DEFAULT_CONFIG, SignalFieldConfig, digest, loads_strict, require

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    epoch TEXT NOT NULL,
    position INTEGER NOT NULL,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uses (
    id TEXT PRIMARY KEY,
    spec TEXT NOT NULL,
    signature TEXT NOT NULL,
    generation INTEGER NOT NULL,
    latest TEXT,
    last_rejected TEXT
);
CREATE TABLE IF NOT EXISTS dependencies (
    source TEXT NOT NULL,
    use_id TEXT NOT NULL,
    PRIMARY KEY (source, use_id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    use_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    basis TEXT NOT NULL,
    spec TEXT NOT NULL,
    ticket INTEGER,
    token TEXT,
    gap TEXT,
    attempt TEXT,
    worker_outcome TEXT
);
CREATE INDEX IF NOT EXISTS eligible ON jobs(ticket) WHERE token IS NULL;
CREATE INDEX IF NOT EXISTS use_jobs ON jobs(use_id);
CREATE TABLE IF NOT EXISTS selected_history (
    name TEXT PRIMARY KEY,
    snapshot TEXT NOT NULL
);
"""


def product_root() -> Path:
    """Return the enclosing managed_ai package root guarding against internal DB placement."""
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

    if (root.parent / "CURRENT-RELEASE.json").is_file():
        if resolved == root.parent or resolved.is_relative_to(root.parent):
            return True

    return False


def require_ordinary_non_symlink(
    path: Path | str,
    role: str,
    *,
    must_exist: bool = False,
) -> Path:
    """Verify path and all ancestors are ordinary, unfollowed, non-reparse entries."""
    expanded = Path(path).expanduser()
    abs_path = check_unfollowed_ancestors(expanded, role, include_target=False)
    require(not is_reparse_or_symlink(abs_path), f"{role} must not be a symlink or reparse point: {abs_path}")

    try:
        info = abs_path.lstat()
    except FileNotFoundError:
        if must_exist:
            raise ValueError(f"ordinary {role} required: {abs_path}") from None
        return abs_path

    require(stat.S_ISREG(info.st_mode), f"ordinary {role} required: {abs_path}")
    return abs_path


def validate_db_path(path: Path | str, *, product: Path | None = None) -> Path:
    """Validate that a signal-field SQLite path and its sidecars are ordinary and outside the package."""
    require(
        isinstance(path, (str, Path)) and str(path).strip() and "\0" not in str(path),
        "database path must be a valid nonempty path",
    )

    raw = Path(path).expanduser()
    resolved = raw.resolve()
    require(
        not inside_product(resolved, product) and not inside_product(Path(os.path.abspath(raw)), product),
        "database must be outside the product directory",
    )

    abs_db = require_ordinary_non_symlink(raw, "database file", must_exist=False)
    for suffix in ("-journal", "-wal", "-shm"):
        require_ordinary_non_symlink(Path(str(abs_db) + suffix), "database journal file", must_exist=False)

    if abs_db.parent.exists():
        require(
            abs_db.parent.is_dir() and not is_reparse_or_symlink(abs_db.parent),
            "database parent must be an ordinary directory",
        )

    return resolved


def no_journal(path: Path | str) -> None:
    """Refuse planning snapshots when SQLite journal or WAL sidecars are present."""
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        require(
            not sidecar.exists() and not is_reparse_or_symlink(sidecar),
            "host journal/WAL state requires its owner; no host operations prepared",
        )


def open_signal_database(
    path: Path,
    config: SignalFieldConfig = DEFAULT_CONFIG,
) -> sqlite3.Connection:
    """Create or open a signal-field SQLite database and apply additive migrations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    try:
        conn.executescript(SCHEMA_DDL)
        conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
            if "worker_outcome" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN worker_outcome TEXT")
            conn.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", ("format", config.format_version))
            conn.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", ("sequence", "0"))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        fmt_row = conn.execute("SELECT value FROM meta WHERE key=?", ("format",)).fetchone()
        require(
            fmt_row is not None and fmt_row["value"] == config.format_version,
            "unsupported signal field format",
        )
        return conn
    except BaseException:
        conn.close()
        raise


def _verify_snapshot_unchanged(path: Path, expected_sha: str, expected_bytes: int) -> None:
    """Verify the underlying rollback-journal DB stayed byte-identical without buffering it in RAM."""
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
    """Read the exact rollback-journal cut guarded by the caller without repeated full-DB copies."""
    checked = require_ordinary_non_symlink(Path(path), "database file", must_exist=True)
    no_journal(checked)

    database_raw = read_guard()
    require(
        database_raw[:16] == b"SQLite format 3\x00"
        and len(database_raw) >= 100
        and database_raw[18:20] == b"\x01\x01",
        "only a rollback-journal host cut is supported; inspect WAL mode with its owner",
    )
    expected_sha = hashlib.sha256(database_raw).hexdigest()
    expected_bytes = len(database_raw)
    del database_raw

    try:
        db = sqlite3.connect(checked.as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ValueError(str(exc)) from exc

    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        fmt = db.execute("SELECT value FROM meta WHERE key=?", ("format",)).fetchone()
        require(fmt and fmt["value"] == config.format_version, "unsupported host format")

        sources = {
            row["id"]: {
                "source": row["id"],
                "epoch": row["epoch"],
                "position": row["position"],
                "value": loads_strict(row["value"]),
            }
            for row in db.execute("SELECT id, epoch, position, value FROM sources ORDER BY id")
        }

        uses: dict[str, dict[str, Any]] = {}
        for row in db.execute("SELECT id, spec, signature, generation FROM uses ORDER BY id"):
            spec = loads_strict(row["spec"])
            signature = digest(spec)
            require(
                spec.get("id") == row["id"]
                and signature == row["signature"]
                and type(row["generation"]) is int
                and row["generation"] > 0,
                "host use identity/signature differs",
            )
            uses[row["id"]] = spec

        dependencies = {
            (row["source"], row["use_id"])
            for row in db.execute("SELECT source, use_id FROM dependencies ORDER BY source, use_id")
        }
        expected_deps = {(source, ident) for ident, spec in uses.items() for source in spec["sources"]}
        require(dependencies == expected_deps, "host dependency rows differ from declared use definitions")

        jobs = [
            dict(row)
            for row in db.execute("SELECT id, use_id, generation, ticket, token, gap FROM jobs ORDER BY id")
        ]
        _verify_snapshot_unchanged(checked, expected_sha, expected_bytes)
    except sqlite3.Error as exc:
        raise ValueError(str(exc)) from exc
    finally:
        db.close()

    _verify_snapshot_unchanged(checked, expected_sha, expected_bytes)
    return {"sources": sources, "uses": uses, "jobs": jobs}
