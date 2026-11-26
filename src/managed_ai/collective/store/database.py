"""Open or upgrade an existing store; govern every command receipt and transaction."""

import os
import sqlite3
import stat
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from .contract import CONTEXT_LIMIT, Refused, encoded, loads_strict, name, require

PREVIOUS_FORMAT = "g60-collective-work/1"
FORMAT = "g60-collective-work/2"

LEGACY_RECEIPTS = (
    "CREATE VIEW receipts AS SELECT actor,command,request,result FROM command_receipts"
)

QUALIFIED_REVISION_INDEX = """CREATE INDEX qualified_revision
    ON events(subject,json_extract(body,'$.target.revision'))
    WHERE kind='qualified-use-source'"""

USE_RELATIONS = """CREATE TABLE use_sources (
    target TEXT NOT NULL, source TEXT NOT NULL, pin TEXT NOT NULL,
    PRIMARY KEY(target,source))"""

REVIEW_TABLE = """CREATE TABLE impact_reviews (
    proposal TEXT NOT NULL REFERENCES proposals(id),
    notice INTEGER NOT NULL REFERENCES notices(id),
    judgment TEXT NOT NULL, PRIMARY KEY(proposal,notice))"""

SCHEMA = """
CREATE TABLE metadata (format TEXT NOT NULL);
CREATE TABLE artifacts (id TEXT PRIMARY KEY, record TEXT NOT NULL);
CREATE TABLE versions (id TEXT NOT NULL, revision INTEGER NOT NULL, record TEXT NOT NULL,
                       PRIMARY KEY(id,revision));
CREATE TABLE contexts (id TEXT PRIMARY KEY, actor TEXT NOT NULL, record TEXT NOT NULL);
CREATE TABLE proposals (id TEXT PRIMARY KEY, actor TEXT NOT NULL, record TEXT NOT NULL);
CREATE TABLE command_receipts (actor TEXT NOT NULL, command TEXT NOT NULL, request TEXT NOT NULL,
                       result TEXT NOT NULL, PRIMARY KEY(actor,command));
CREATE TABLE claims (target TEXT PRIMARY KEY, actor TEXT NOT NULL, generation INTEGER NOT NULL,
                     token TEXT NOT NULL, expires REAL NOT NULL, active INTEGER NOT NULL);
CREATE TABLE watches (actor TEXT NOT NULL, subject TEXT NOT NULL, reason TEXT NOT NULL,
                      PRIMARY KEY(actor,subject));
CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                     subject TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE notices (id INTEGER PRIMARY KEY AUTOINCREMENT, receiver TEXT NOT NULL,
                      target TEXT NOT NULL, kind TEXT NOT NULL, required INTEGER NOT NULL,
                      body TEXT NOT NULL, state TEXT NOT NULL, acknowledgment TEXT, receipt TEXT);
CREATE INDEX notices_receiver ON notices(receiver,state,id);
CREATE INDEX notices_target ON notices(target,required,state);
"""


def check_engine(db: sqlite3.Connection) -> None:
    try:
        db.execute("SELECT json_array(1) -> '$[0]'").fetchone()
    except sqlite3.OperationalError as exc:
        raise ValueError("Collective v2 requires SQLite 3.38 or newer with JSON support") from exc


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return bool(
        stat.S_ISLNK(info.st_mode)
        or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
    )


def _check_unfollowed_ancestors(path: Path, role: str) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        pwd = os.environ.get("PWD")
        if pwd and os.path.isabs(pwd):
            try:
                if os.path.samefile(pwd, os.getcwd()):
                    logical = Path(pwd)
                    for anc in (*reversed(logical.parents), logical):
                        if _is_reparse_or_symlink(anc):
                            raise ValueError(
                                f"{role} must not traverse a symlink or reparse point: {anc}"
                            )
            except OSError:
                pass
    abs_path = Path(os.path.abspath(raw))
    for anc in (*reversed(abs_path.parents), abs_path):
        if _is_reparse_or_symlink(anc):
            raise ValueError(f"{role} must not be or traverse a symlink or reparse point: {anc}")
    return abs_path


def _require_ordinary_path(path: Path, role: str, *, must_exist: bool = False) -> None:
    abs_path = _check_unfollowed_ancestors(path, role)
    try:
        info = abs_path.lstat()
    except FileNotFoundError:
        if must_exist:
            raise ValueError(f"Ordinary {role} required: {abs_path}") from None
        return
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Ordinary {role} required: {abs_path}")


def inside_product(path: Path | str, product: Path | str) -> bool:
    product_root = Path(product).resolve()
    resolved = Path(path).expanduser().resolve()
    if resolved == product_root or resolved.is_relative_to(product_root):
        return True
    if (product_root.parent / "CURRENT-RELEASE.json").is_file() and (
        resolved == product_root.parent or resolved.is_relative_to(product_root.parent)
    ):
        return True
    # Guard enclosing package root when hosted under src/managed_ai/collective.
    if product_root.name == "collective":
        candidates: tuple[Path, ...] = (
            (product_root.parent, product_root.parents[1])
            if product_root.parent.name == "optional"
            else (product_root.parent,)
        )
        for pkg_root in candidates:
            if (pkg_root / "CURRENT-RELEASE.json").is_file() and (
                resolved == pkg_root or resolved.is_relative_to(pkg_root)
            ):
                return True
            engine_dir = pkg_root / "engine"
            if (engine_dir / "CURRENT-RELEASE.json").is_file() and (
                resolved == engine_dir or resolved.is_relative_to(engine_dir)
            ):
                return True
    return False


def _validate_db_path(database: Any, product: Path | str, *, must_exist: bool) -> Path:
    if not isinstance(database, (str, Path)) or not str(database).strip() or "\0" in str(database):
        raise ValueError("Database path must be a valid nonempty path")
    raw = Path(database).expanduser()
    _require_ordinary_path(raw, "database file", must_exist=must_exist)
    for suffix in ("-journal", "-wal", "-shm"):
        _require_ordinary_path(Path(str(raw) + suffix), "database journal file", must_exist=False)
    resolved = raw.resolve()
    if inside_product(resolved, product):
        raise ValueError("Database must be outside the product directory")
    if raw.parent.exists() and not raw.parent.is_dir():
        raise ValueError("Database parent must be a directory")
    return resolved


class CollectiveDatabase:
    def __init__(self, database: Path | str, product: Path | str) -> None:
        self.path = _validate_db_path(database, product, must_exist=True)
        with closing(self.connect()) as db:
            check_engine(db)
            if db.execute("SELECT format FROM metadata").fetchone()[0] != FORMAT:
                raise ValueError("Unsupported collective store format")

    @staticmethod
    def initialize(database: Path | str, product: Path | str) -> dict[str, str]:
        path = _validate_db_path(database, product, must_exist=False)
        if path.exists():
            raise ValueError("Initialization requires a new database outside the product directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(":memory:")) as check:
            check_engine(check)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(SCHEMA)
            db.execute(REVIEW_TABLE)
            db.execute(USE_RELATIONS)
            db.execute(QUALIFIED_REVISION_INDEX)
            db.execute(LEGACY_RECEIPTS)
            db.execute("INSERT INTO metadata VALUES (?)", (FORMAT,))
        return {"format": FORMAT, "database": str(path)}

    @staticmethod
    def upgrade(database: Path | str, product: Path | str) -> dict[str, Any]:
        """Offline v1 to v2 transition; every existing duty and receipt stays in place."""
        path = _validate_db_path(database, product, must_exist=True)
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db, db:
            check_engine(db)
            db.execute("BEGIN EXCLUSIVE")
            (version,) = db.execute("SELECT format FROM metadata").fetchone()
            if version == FORMAT:
                return {"format": FORMAT, "changed": False}
            if version != PREVIOUS_FORMAT:
                raise ValueError("Unsupported collective store format")
            # v1 programs always insert a receipt inside their effect transaction.
            # The read-only old name fences a live v1 writer even after its earlier format check.
            db.execute("ALTER TABLE receipts RENAME TO command_receipts")
            db.execute(LEGACY_RECEIPTS)
            db.execute(REVIEW_TABLE)
            db.execute(USE_RELATIONS)
            db.execute(QUALIFIED_REVISION_INDEX)
            # Import the direct sources actually enrolled in current v1 ready decisions.
            # A past hold has no durable open commission declaration in the old format.
            db.execute("""INSERT INTO use_sources(target,source,pin)
                        SELECT a.id,json_extract(p.value,'$.id'),p.value
                        FROM artifacts a, json_each(a.record,'$.basis') p
                        WHERE json_extract(a.record,'$.body.format')='g60-receiver-use/1'
                        AND json_extract(a.record,'$.body.disposition')='ready'
                        AND a.id != json_extract(p.value,'$.id')""")
            db.execute("UPDATE metadata SET format=?", (FORMAT,))
            return {"format": FORMAT, "changed": True}

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def read(self) -> Generator[sqlite3.Connection]:
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            yield db

    def receipt(self, actor: str, command: str) -> Any:
        with self.read() as db:
            row = db.execute(
                "SELECT result FROM command_receipts WHERE actor=? AND command=?",
                (name(actor), name(command)),
            ).fetchone()
            return loads_strict(row[0]) if row else None

    def execute(
        self,
        request: dict[str, Any],
        operations: Callable[
            [sqlite3.Connection], dict[str, Callable[[dict[str, Any]], dict[str, Any]]]
        ],
    ) -> dict[str, Any]:
        """Execute one exact command, recording accepted or refused results atomically."""
        require(isinstance(request, dict), "invalid-input", "Request must be an object")
        actor, command, op = (name(request.get(k), k) for k in ("actor", "command", "op"))
        raw = encoded(request)
        require(len(raw.encode()) <= CONTEXT_LIMIT, "request-bound", "Request exceeds 2 MiB")
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            handlers = operations(db)
            require(op in handlers, "unknown-operation", "Unknown operation")
            old = db.execute(
                "SELECT request,result FROM command_receipts WHERE actor=? AND command=?",
                (actor, command),
            ).fetchone()
            if old:
                require(
                    old["request"] == raw,
                    "command-conflict",
                    "This command identity already names a different request",
                )
                result = loads_strict(old["result"])
                db.rollback()
                return result
            # A refused command cannot leave partial operation writes behind.
            db.execute("SAVEPOINT operation")
            try:
                result = {"status": "accepted", **handlers[op](request)}
                db.execute("RELEASE operation")
            except Refused as exc:
                db.execute("ROLLBACK TO operation")
                db.execute("RELEASE operation")
                result = {"status": "refused", "code": exc.code, "detail": exc.detail}
            result.update(actor=actor, command=command, operation=op)
            db.execute(
                "INSERT INTO command_receipts VALUES (?,?,?,?)",
                (actor, command, raw, encoded(result)),
            )
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
