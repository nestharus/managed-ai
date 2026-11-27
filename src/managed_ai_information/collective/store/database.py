"""Open or upgrade an existing store; govern every command receipt and transaction."""

import copy
import importlib
import os
import stat
import threading
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .contract import CONTEXT_LIMIT, Refused, encoded, loads_strict, name, require

PREVIOUS_FORMAT = "g60-collective-work/1"
FORMAT = "g60-collective-work/2"

_SQLITE_HEADER = b"SQLite format 3\x00"
_LOCK_RETRY_SLEEP = 0.005
_LOCK_TIMEOUT_SECONDS = 30.0
_REPLACE_RETRIES = 50
_REPLACE_SLEEP = 0.01

_PLATFORM: Any = (
    importlib.import_module("managed_ai_information.platform")
    if (__package__ or "").startswith("managed_ai_information.")
    else None
)
_PLAT_POOL: Any = _PLATFORM.KeyedLockPool(_PLATFORM.ThreadLock) if _PLATFORM is not None else None
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}

_V2_DICT_KEYS = (
    "artifacts",
    "versions",
    "contexts",
    "proposals",
    "command_receipts",
    "claims",
    "watches",
    "impact_reviews",
    "use_sources",
)
_V1_DICT_KEYS = ("artifacts", "versions", "contexts", "proposals", "claims", "watches")


def _thread_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _is_reparse_or_symlink(path: Path) -> bool:
    if _PLATFORM is not None:
        return bool(_PLATFORM.is_reparse_or_symlink(path))
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


def _check_logical_pwd_ancestors(role: str) -> None:
    pwd = os.environ.get("PWD")
    if not pwd or not os.path.isabs(pwd):
        return
    try:
        same = os.path.samefile(pwd, os.getcwd())
    except OSError:
        return
    if not same:
        return
    logical = Path(pwd)
    for anc in (*reversed(logical.parents), logical):
        if _is_reparse_or_symlink(anc):
            raise ValueError(f"{role} must not traverse a symlink or reparse point: {anc}")


def _check_unfollowed_ancestors(path: Path, role: str) -> Path:
    if _PLATFORM is not None:
        return Path(_PLATFORM.check_unfollowed_ancestors(path, role, include_target=True))
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        _check_logical_pwd_ancestors(role)
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
    if product_root.name != "collective":
        return False
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
    for suffix in ("-journal", "-wal", "-shm", ".lock"):
        _require_ordinary_path(Path(str(raw) + suffix), "database sidecar file", must_exist=False)
    resolved = raw.resolve()
    if inside_product(resolved, product):
        raise ValueError("Database must be outside the product directory")
    if raw.parent.exists() and not raw.parent.is_dir():
        raise ValueError("Database parent must be a directory")
    return resolved


def _empty_state(fmt: str = FORMAT) -> dict[str, Any]:
    return {
        "format": fmt,
        "artifacts": {},
        "versions": {},
        "contexts": {},
        "proposals": {},
        "command_receipts": {},
        "claims": {},
        "watches": {},
        "events": [],
        "next_event_seq": 1,
        "notices": [],
        "next_notice_id": 1,
        "impact_reviews": {},
        "use_sources": {},
    }


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    if _PLATFORM is not None:
        _PLATFORM.atomic_bytes(path, data)
        _PLATFORM.sync_dir(path.parent)
        return
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with tmp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt + 1 >= _REPLACE_RETRIES:
                    raise
                time.sleep(_REPLACE_SLEEP)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _validate_store_schema(data: dict[str, Any], *, allow_v1: bool) -> None:
    fmt = data.get("format")
    req_dicts: tuple[str, ...]
    if fmt == FORMAT:
        req_dicts = _V2_DICT_KEYS
    elif allow_v1 and fmt == PREVIOUS_FORMAT:
        req_dicts = _V1_DICT_KEYS
        if "command_receipts" in data and not isinstance(data["command_receipts"], dict):
            raise ValueError("Corrupt collective store: command_receipts must be an object")
        if "receipts" in data and not isinstance(data["receipts"], dict):
            raise ValueError("Corrupt collective store: receipts must be an object")
    else:
        raise ValueError("Unsupported collective store format")
    for k in req_dicts:
        if not isinstance(data.get(k), dict):
            raise ValueError(f"Corrupt collective store: missing or invalid '{k}' section")
    for k in ("events", "notices"):
        if not isinstance(data.get(k), list):
            raise ValueError(f"Corrupt collective store: missing or invalid '{k}' section")
    for k in ("next_event_seq", "next_notice_id"):
        v = data.get(k)
        if type(v) is not int or v < 1:
            raise ValueError(f"Corrupt collective store: invalid '{k}' counter")
    for row in data["notices"]:
        if not isinstance(row, dict) or type(row.get("id")) is not int:
            raise ValueError("Corrupt collective store: invalid notice entry")


def _decode_store_bytes(raw: bytes, *, allow_v1: bool = False) -> dict[str, Any]:
    if raw.startswith(_SQLITE_HEADER):
        raise ValueError(
            "Legacy binary SQLite collective store detected; export and recreate as a pure-JSON "
            "collective store before opening"
        )
    try:
        data = loads_strict(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"Corrupt or unsupported collective store: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("format"), str):
        raise ValueError("Unsupported collective store format")
    _validate_store_schema(data, allow_v1=allow_v1)
    return data


def _receipt_key(actor: str, command: str) -> str:
    return f"{actor}\0{command}"


class CollectiveState:
    """In-memory transactional view over a pure-stdlib JSON collective store."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self._notices_by_id: dict[int, dict[str, Any]] = {
            int(row["id"]): row for row in data["notices"]
        }

    def checkpoint(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.data = snapshot
        self._notices_by_id = {int(row["id"]): row for row in self.data["notices"]}

    def artifact_raw(self, ident: str) -> str | None:
        val = self.data["artifacts"].get(ident)
        return val if isinstance(val, str) else None

    def version_raw(self, ident: str, revision: int) -> str | None:
        versions = self.data["versions"].get(ident)
        if not isinstance(versions, dict):
            return None
        val = versions.get(str(revision))
        return val if isinstance(val, str) else None

    def max_version_revision(self, ident: str) -> int:
        versions = self.data["versions"].get(ident)
        if not isinstance(versions, dict) or not versions:
            return 0
        return max(int(k) for k in versions)

    def save_artifact(self, ident: str, revision: int, raw: str) -> None:
        self.data["artifacts"][ident] = raw
        versions = self.data["versions"].setdefault(ident, {})
        versions[str(revision)] = raw

    def get_context_raw(self, ident: str) -> str | None:
        row = self.data["contexts"].get(ident)
        return row["record"] if isinstance(row, dict) else None

    def put_context(self, ident: str, actor: str, raw: str) -> None:
        self.data["contexts"][ident] = {"actor": actor, "record": raw}

    def get_proposal_raw(self, ident: str) -> str | None:
        row = self.data["proposals"].get(ident)
        return row["record"] if isinstance(row, dict) else None

    def put_proposal(self, ident: str, actor: str, raw: str) -> None:
        self.data["proposals"][ident] = {"actor": actor, "record": raw}

    def get_receipt(self, actor: str, command: str) -> dict[str, Any] | None:
        row = self.data["command_receipts"].get(_receipt_key(actor, command))
        return row if isinstance(row, dict) else None

    def put_receipt(self, actor: str, command: str, raw_request: str, raw_result: str) -> None:
        self.data["command_receipts"][_receipt_key(actor, command)] = {
            "actor": actor,
            "command": command,
            "request": raw_request,
            "result": raw_result,
        }

    def get_claim(self, target: str) -> dict[str, Any] | None:
        row = self.data["claims"].get(target)
        return row if isinstance(row, dict) else None

    def put_claim(
        self,
        target: str,
        actor: str,
        generation: int,
        token: str,
        expires: float,
        active: int = 1,
    ) -> None:
        self.data["claims"][target] = {
            "target": target,
            "actor": actor,
            "generation": generation,
            "token": token,
            "expires": expires,
            "active": active,
        }

    def set_claim_inactive(self, target: str) -> None:
        row = self.data["claims"].get(target)
        if isinstance(row, dict):
            row["active"] = 0

    def put_watch(self, actor: str, subject: str, reason: str) -> None:
        bucket = self.data["watches"].setdefault(subject, {})
        bucket[actor] = reason

    def watches_for_subject(self, subject: str) -> list[dict[str, str]]:
        bucket = self.data["watches"].get(subject)
        if not isinstance(bucket, dict):
            return []
        return [{"actor": actor, "reason": reason} for actor, reason in bucket.items()]

    def emit_event(self, kind: str, subject: str, actor: str, raw_body: str) -> int:
        seq = int(self.data["next_event_seq"])
        self.data["next_event_seq"] = seq + 1
        self.data["events"].append(
            {"seq": seq, "kind": kind, "subject": subject, "actor": actor, "body": raw_body}
        )
        return seq

    def max_event_seq(self) -> int:
        events = self.data["events"]
        return int(events[-1]["seq"]) if events else 0

    def add_notice(
        self,
        receiver: str,
        target: str,
        kind: str,
        required: int,
        raw_body: str,
        state: str = "pending",
    ) -> int:
        ident = int(self.data["next_notice_id"])
        self.data["next_notice_id"] = ident + 1
        row: dict[str, Any] = {
            "id": ident,
            "receiver": receiver,
            "target": target,
            "kind": kind,
            "required": int(required),
            "body": raw_body,
            "state": state,
            "acknowledgment": None,
            "receipt": None,
        }
        self.data["notices"].append(row)
        self._notices_by_id[ident] = row
        return ident

    def get_notice(self, ident: int) -> dict[str, Any] | None:
        return self._notices_by_id.get(ident)

    def put_review(self, proposal: str, notice_id: int, raw_judgment: str) -> None:
        bucket = self.data["impact_reviews"].setdefault(proposal, {})
        bucket[str(notice_id)] = raw_judgment

    def get_review(self, proposal: str, notice_id: int) -> str | None:
        bucket = self.data["impact_reviews"].get(proposal)
        if not isinstance(bucket, dict):
            return None
        val = bucket.get(str(notice_id))
        return val if isinstance(val, str) else None

    def reviews_for_proposal(self, proposal: str) -> dict[int, str]:
        bucket = self.data["impact_reviews"].get(proposal)
        if not isinstance(bucket, dict):
            return {}
        return {int(k): v for k, v in bucket.items()}

    def get_use_source(self, target: str, source: str) -> str | None:
        bucket = self.data["use_sources"].get(target)
        if not isinstance(bucket, dict):
            return None
        val = bucket.get(source)
        return val if isinstance(val, str) else None

    def use_sources_for_target(self, target: str) -> dict[str, str]:
        bucket = self.data["use_sources"].get(target)
        return dict(bucket) if isinstance(bucket, dict) else {}

    def put_use_source(
        self, target: str, source: str, raw_pin: str, *, overwrite: bool = True
    ) -> None:
        bucket = self.data["use_sources"].setdefault(target, {})
        if overwrite or source not in bucket:
            bucket[source] = raw_pin

    def delete_use_source(self, target: str, source: str | None = None) -> None:
        if source is None:
            self.data["use_sources"].pop(target, None)
            return
        bucket = self.data["use_sources"].get(target)
        if isinstance(bucket, dict):
            bucket.pop(source, None)
            if not bucket:
                self.data["use_sources"].pop(target, None)


def _open_lock_handle(lock_path: Path) -> Any:
    _require_ordinary_path(lock_path, "database sidecar file", must_exist=False)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"Ordinary database sidecar file required: {lock_path}")
        return os.fdopen(fd, "r+b")
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _exclusive_store_lock(path: Path) -> Generator[None]:
    """Kernel-fenced exclusive lock via platform (or token-owned fallback when standalone)."""
    lock_path = Path(str(path) + ".lock")
    if _PLATFORM is not None and _PLAT_POOL is not None:
        with _PLAT_POOL.get(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            with _open_lock_handle(lock_path) as handle:
                _PLATFORM.lock_file(handle)
                try:
                    yield
                finally:
                    _PLATFORM.unlock_file(handle)
        return

    with _thread_lock_for(path):
        _require_ordinary_path(lock_path, "database sidecar file", must_exist=False)
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        flags = (
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        token = f"{os.getpid()}:{uuid.uuid4().hex}".encode()
        fd: int | None = None
        while True:
            try:
                fd = os.open(lock_path, flags, 0o600)
                os.write(fd, token)
                os.close(fd)
                fd = None
                break
            except FileExistsError, PermissionError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting for collective store lock: {lock_path}"
                    ) from None
                time.sleep(_LOCK_RETRY_SLEEP)
            except BaseException:
                if fd is not None:
                    os.close(fd)
                raise
        try:
            yield
        finally:
            try:
                if lock_path.read_bytes() == token:
                    lock_path.unlink(missing_ok=True)
            except OSError:
                pass


class CollectiveDatabase:
    def __init__(
        self,
        database: Path | str,
        product: Path | str,
        *,
        backend: Any | None = None,
        namespace: str = "collective",
    ) -> None:
        self.path = _validate_db_path(database, product, must_exist=True)
        self._backend = backend
        self._namespace = namespace
        self.last_mirror_error: str | None = None
        with self.read() as state:
            if state.data.get("format") != FORMAT:
                raise ValueError("Unsupported collective store format")

    @staticmethod
    def initialize(database: Path | str, product: Path | str) -> dict[str, str]:
        path = _validate_db_path(database, product, must_exist=False)
        if path.exists():
            raise ValueError("Initialization requires a new database outside the product directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_store_lock(path):
            if path.exists():
                raise ValueError(
                    "Initialization requires a new database outside the product directory"
                )
            _atomic_write_bytes(path, (encoded(_empty_state(FORMAT)) + "\n").encode("utf-8"))
        return {"format": FORMAT, "database": str(path)}

    @staticmethod
    def upgrade(database: Path | str, product: Path | str) -> dict[str, Any]:
        """Offline v1 to v2 JSON transition; every existing duty and receipt stays in place."""
        path = _validate_db_path(database, product, must_exist=True)
        with _exclusive_store_lock(path):
            data = _decode_store_bytes(path.read_bytes(), allow_v1=True)
            version = data.get("format")
            if version == FORMAT:
                return {"format": FORMAT, "changed": False}
            if version != PREVIOUS_FORMAT:
                raise ValueError("Unsupported collective store format")
            if "command_receipts" not in data and isinstance(data.get("receipts"), dict):
                data["command_receipts"] = data.pop("receipts")
            data.setdefault("command_receipts", {})
            data.setdefault("impact_reviews", {})
            use_sources: dict[str, dict[str, str]] = data.setdefault("use_sources", {})
            for art_id, raw_art in data.get("artifacts", {}).items():
                rec = loads_strict(raw_art)
                art_body = rec.get("body")
                if (
                    isinstance(art_body, dict)
                    and art_body.get("format") == "g60-receiver-use/1"
                    and art_body.get("disposition") == "ready"
                ):
                    bucket = use_sources.setdefault(art_id, {})
                    for item in rec.get("basis", ()):
                        if isinstance(item, dict) and item.get("id") != art_id:
                            bucket.setdefault(str(item["id"]), encoded(item))
            data["format"] = FORMAT
            _validate_store_schema(data, allow_v1=False)
            _atomic_write_bytes(path, (encoded(data) + "\n").encode("utf-8"))
            return {"format": FORMAT, "changed": True}

    def _load_state_unlocked(self) -> CollectiveState:
        _require_ordinary_path(self.path, "database file", must_exist=True)
        data = _decode_store_bytes(self.path.read_bytes(), allow_v1=False)
        return CollectiveState(data)

    def _mirror_backend(self, raw_bytes: bytes) -> None:
        if self._backend is None:
            return
        try:
            self._backend.put_record(
                self._namespace,
                f"collective/{self.path.name}",
                raw_bytes,
                expected_version=None,
                visibility="MetadataOnly",
                metadata={"kind": "collective-store", "title": self.path.name},
            )
            self.last_mirror_error = None
        except (OSError, ValueError, RuntimeError) as exc:
            self.last_mirror_error = str(exc)

    @contextmanager
    def read(self) -> Generator[CollectiveState]:
        with _exclusive_store_lock(self.path):
            yield self._load_state_unlocked()

    def receipt(self, actor: str, command: str) -> Any:
        with self.read() as state:
            row = state.get_receipt(name(actor), name(command))
            return loads_strict(row["result"]) if row else None

    def execute(
        self,
        request: dict[str, Any],
        operations: Callable[
            [CollectiveState], dict[str, Callable[[dict[str, Any]], dict[str, Any]]]
        ],
    ) -> dict[str, Any]:
        """Execute one exact command, recording accepted or refused results atomically."""
        require(isinstance(request, dict), "invalid-input", "Request must be an object")
        actor, command, op = (name(request.get(k), k) for k in ("actor", "command", "op"))
        raw = encoded(request)
        require(len(raw.encode()) <= CONTEXT_LIMIT, "request-bound", "Request exceeds 2 MiB")
        serialized: bytes | None = None
        with _exclusive_store_lock(self.path):
            state = self._load_state_unlocked()
            handlers = operations(state)
            require(op in handlers, "unknown-operation", "Unknown operation")
            old = state.get_receipt(actor, command)
            if old is not None:
                require(
                    old["request"] == raw,
                    "command-conflict",
                    "This command identity already names a different request",
                )
                return loads_strict(old["result"])
            snapshot = state.checkpoint()
            try:
                result = {"status": "accepted", **handlers[op](request)}
            except Refused as exc:
                state.restore(snapshot)
                result = {"status": "refused", "code": exc.code, "detail": exc.detail}
            result.update(actor=actor, command=command, operation=op)
            state.put_receipt(actor, command, raw, encoded(result))
            serialized = (encoded(state.data) + "\n").encode("utf-8")
            _atomic_write_bytes(self.path, serialized)
        if serialized is not None:
            self._mirror_backend(serialized)
        return result
