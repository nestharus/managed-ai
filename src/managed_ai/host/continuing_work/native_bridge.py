"""Native subprocess bridge, durable event/object custody, and candidate capture reconciliation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile
from typing import Any

from managed_ai.host.continuing_work.contract import DEFAULT_HOST_CONFIG
from managed_ai.host.continuing_work.occurrence_files import (
    atomic_bytes,
    check_unfollowed_ancestors,
    digest,
    file_lock,
    ident,
    is_reparse_or_symlink,
    loads_strict,
    nonempty,
    now,
    read,
    sync_dir,
)

HERE = Path(__file__).resolve().parent

MAX_EVENT_STREAM_BYTES = DEFAULT_HOST_CONFIG.max_event_stream_bytes
MAX_EVENT_LINE_BYTES = DEFAULT_HOST_CONFIG.max_event_line_bytes

SAFE_CAPTURE_ERRORS = frozenset(
    {
        "disposition must be considered or deferred",
        "each assessment must name a distinct actually delivered item",
        "receipt identity already used",
        "receipt identity not recorded in native Attention",
        "receipt must contain id and assessments",
        "invalid assessment fields",
        "nonempty bounded assessments required",
        "Native return already exists; use its retained body or a new current occurrence",
        "Selected native engine does not match the reviewed capture adapter; requalify this adapter before capture",
        "External native engine requires explicit --expected-profile or verified --deployment pin before capture",
        "Selected native engine does not satisfy the required capture adapter contract",
    },
)

_PRECOMMIT_CLEANUP_ERRORS = frozenset(
    {
        "disposition must be considered or deferred",
        "each assessment must name a distinct actually delivered item",
        "receipt must contain id and assessments",
        "invalid assessment fields",
        "nonempty bounded assessments required",
        "Native return already exists; use its retained body or a new current occurrence",
        "Selected native engine does not match the reviewed capture adapter; requalify this adapter before capture",
        "External native engine requires explicit --expected-profile or verified --deployment pin before capture",
        "Selected native engine does not satisfy the required capture adapter contract",
    },
)


def _safe_jsonl(row: Any) -> bytes:
    """Serialize one JSONL record while escaping Unicode line separators."""
    encoded = json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    encoded = (
        encoded.replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
        .replace("\u0085", "\\u0085")
    )
    if "\n" in encoded or "\r" in encoded:
        raise ValueError("event line framing contains unescaped newline")
    return (encoded + "\n").encode("utf-8")


def capture_error(raw: str) -> str:
    """Preserve allowlisted selected-capture errors without leaking arbitrary owner history."""
    try:
        parsed = loads_strict(raw)
        detail = parsed.get("error") if isinstance(parsed, dict) else None
    except (ValueError, AttributeError):
        detail = None

    if isinstance(detail, str) and detail in SAFE_CAPTURE_ERRORS:
        return detail

    return (
        "Candidate capture could not be confirmed; raw failure details remain in owner evidence. "
        "Do not blindly repeat the receipt."
    )


def _temp_receipt_file(directory: Path, body: bytes) -> Path:
    """Write a closed temporary JSON file readable by child processes on all platforms."""
    checked_dir = check_unfollowed_ancestors(directory, "Temporary request directory", include_target=True)
    checked_dir.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=checked_dir, suffix=".json", delete=False) as selected:
        selected.write(body)
        selected.flush()
        return Path(selected.name)


def resolve_sibling_cli(engine_entry: Path, canonical_rel: str, legacy_name: str) -> Path:
    """Resolve an engine CLI relative to store/manage_store.py or historical runtime/manage_store.py."""
    entry = check_unfollowed_ancestors(engine_entry, "Engine entry", include_target=True).resolve()
    if entry.parent.name == "store":
        candidate = entry.parents[1] / canonical_rel
        if candidate.is_file() and not is_reparse_or_symlink(candidate):
            return candidate
    return entry.with_name(legacy_name)


def build_sibling_cli_command(
    engine_entry: Path,
    module_name: str,
    canonical_rel: str,
    legacy_name: str,
    *args: Any,
) -> tuple[list[str], dict[str, str] | None]:
    """Build subprocess argv + env invoking a canonical core module directly or legacy script."""
    entry = check_unfollowed_ancestors(engine_entry, "Engine entry", include_target=True).resolve()
    str_args = [str(arg) for arg in args]
    if entry.parent.name == "store":
        engine_root = entry.parents[1]
        candidate = engine_root / canonical_rel
        if candidate.is_file() and not is_reparse_or_symlink(candidate):
            if engine_root.name == "managed_ai" and (engine_root / "__init__.py").is_file():
                env = dict(os.environ)
                env["PYTHONPATH"] = str(engine_root.parent)
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                return [sys.executable, "-B", "-m", module_name, *str_args], env
            return [sys.executable, "-B", str(candidate), *str_args], None
    return [sys.executable, "-B", str(entry.with_name(legacy_name)), *str_args], None


class NativeBridge:
    """Collaborator owning durable object/event custody, native subprocesses, and capture reconciliation."""

    def __init__(self, host: Any) -> None:
        self._host = host

    def _put_object_bytes(self, raw: bytes) -> tuple[str, str]:
        sha = digest(raw)
        obj = self._host.root / "objects" / sha
        if is_reparse_or_symlink(obj) or obj.exists():
            if is_reparse_or_symlink(obj) or not obj.is_file():
                raise ValueError(f"Retained object must be an ordinary file: {obj}")
            check_unfollowed_ancestors(obj, "Retained object", include_target=True)
            existing = obj.read_bytes()
            if existing != raw or digest(existing) != sha:
                raise ValueError(f"Retained object integrity mismatch for {sha}")
        else:
            atomic_bytes(obj, raw)
        return obj.relative_to(self._host.root).as_posix(), sha

    def _bound_event_value(self, value: dict[str, Any]) -> dict[str, Any]:
        bounded = dict(value)
        for field in ("stdout", "stderr"):
            text = bounded.get(field)
            if isinstance(text, str):
                raw = text.encode("utf-8", errors="replace")
                if len(raw) > MAX_EVENT_STREAM_BYTES:
                    obj_rel, sha = self._put_object_bytes(raw)
                    bounded[field] = raw[:MAX_EVENT_STREAM_BYTES].decode("utf-8", errors="replace")
                    bounded[field + "_truncated"] = True
                    bounded[field + "_bytes"] = len(raw)
                    bounded[field + "_sha256"] = sha
                    bounded[field + "_object"] = obj_rel
        return bounded

    def event(self, action: str, value: dict[str, Any]) -> dict[str, Any]:
        """Durably append one bounded strict-JSONL event row under cooperative file lock."""
        check_unfollowed_ancestors(self._host.root, "Host root", include_target=True)
        self._host.root.mkdir(parents=True, exist_ok=True)
        bounded = self._bound_event_value(value)
        row = {"at_utc": now(), "action": action, **bounded}
        line = _safe_jsonl(row)
        written_row = row

        if len(line) > MAX_EVENT_LINE_BYTES:
            obj_rel, sha = self._put_object_bytes(line)
            summary: dict[str, Any] = {
                "at_utc": row["at_utc"],
                "action": action,
                "truncated": True,
                "event_bytes": len(line),
                "event_sha256": sha,
                "event_object": obj_rel,
            }
            for key in ("id", "receipt", "exit"):
                if key in row and len(_safe_jsonl(row[key])) <= 1024:
                    summary[key] = row[key]
            if isinstance(row.get("argv"), list) and len(_safe_jsonl(row["argv"])) <= 4096:
                summary["argv"] = row["argv"]
            line = _safe_jsonl(summary)
            written_row = summary

        events_path = self._host.root / "EVENTS.jsonl"
        events_lock = self._host.root / ".events.lock"
        with file_lock(events_lock):
            if is_reparse_or_symlink(events_path):
                raise ValueError("EVENTS.jsonl must be an ordinary file")
            check_unfollowed_ancestors(events_path, "EVENTS.jsonl", include_target=True)
            created = not events_path.exists()
            with events_path.open("ab") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("EVENTS.jsonl must be an ordinary file")
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            if created:
                sync_dir(self._host.root)

        return written_row

    def call(self, *args: Any, scoped_errors: bool = False) -> Any:
        if (
            self._host.config.get("attached_instance_id")
            and read(self._host.native / "system.json").get("instance_id")
            != self._host.config["attached_instance_id"]
        ):
            raise ValueError("attached native store identity changed")

        argv = [sys.executable, "-B", self._host.engine, "--root", str(self._host.native), *map(str, args)]
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
        self._host.event(
            "native-call",
            {"argv": argv, "exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr},
        )
        if proc.returncode:
            raise ValueError(
                capture_error(proc.stderr or proc.stdout) if scoped_errors else (proc.stderr or proc.stdout),
            )
        return loads_strict(proc.stdout)

    def inspect_native(self, sid: str, scoped_errors: bool = False) -> dict[str, Any]:
        argv, env = build_sibling_cli_command(
            Path(self._host.engine),
            "managed_ai.core.exchange.cli",
            "core/exchange/cli.py",
            "exchange_packets.py",
            "--root",
            self._host.native / "exchange",
            "inspect",
            ident(sid),
        )
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", env=env)
        self._host.event(
            "native-inspection",
            {"argv": argv, "exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr},
        )
        if proc.returncode:
            raise ValueError(
                capture_error(proc.stderr or proc.stdout) if scoped_errors else (proc.stderr or proc.stdout),
            )
        return loads_strict(proc.stdout)

    def _capture_qualification_flags(self) -> list[str]:
        flags: list[str] = []
        config = self._host.config or {}
        if config.get("expected_profile"):
            flags.extend(["--expected-profile", str(config["expected_profile"])])
        if config.get("deployment"):
            flags.extend(["--deployment", str((self._host.root / config["deployment"]).resolve())])
        return flags

    def capture_native(self, sid: str, receipt: Path | str, producer: str) -> dict[str, Any]:
        """Run the qualified host capture adapter for a candidate assessment."""
        adapter = HERE / "capture_native.py"
        argv = [
            sys.executable,
            "-B",
            str(adapter),
            "--engine",
            self._host.engine,
            "--root",
            str(self._host.native),
            *self._capture_qualification_flags(),
            "assess",
            sid,
            "--receipt",
            str(receipt),
            "--producer",
            producer,
        ]
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
        self._host.event(
            "native-call",
            {
                "argv": argv,
                "exit": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "capture_adapter": self._host.pin(adapter),
            },
        )
        if proc.returncode:
            raise ValueError(capture_error(proc.stderr or proc.stdout))
        return loads_strict(proc.stdout)

    def reconcile_capture_native(self, sid: str, receipt: Path | str, producer: str) -> dict[str, Any]:
        """Verify an already-committed native Attention receipt for crash recovery."""
        adapter = HERE / "capture_native.py"
        argv = [
            sys.executable,
            "-B",
            str(adapter),
            "--engine",
            self._host.engine,
            "--root",
            str(self._host.native),
            *self._capture_qualification_flags(),
            "reconcile-assess",
            sid,
            "--receipt",
            str(receipt),
            "--producer",
            producer,
        ]
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
        self._host.event(
            "native-capture-reconciliation",
            {
                "argv": argv,
                "exit": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "capture_adapter": self._host.pin(adapter),
            },
        )
        if proc.returncode:
            raise ValueError(capture_error(proc.stderr or proc.stdout))
        return loads_strict(proc.stdout)

    def temp(self, name: str, value: Any) -> Path:
        path = self._host.root / "requests" / name
        self._host._write(path, value)
        return path

    def pin(self, path: Path | str) -> dict[str, Any]:
        checked = check_unfollowed_ancestors(path, "Pinned file", include_target=True)
        try:
            info = checked.lstat()
        except FileNotFoundError as exc:
            raise ValueError(f"Pinned file is absent: {checked}") from exc

        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Pinned file must be an ordinary file: {checked}")

        resolved = checked.resolve()
        raw = resolved.read_bytes()
        obj_rel, sha = self._put_object_bytes(raw)
        return {"path": str(resolved), "sha256": sha, "bytes": len(raw), "object": obj_rel}

    @staticmethod
    def _capture_record(sid: str, result: dict[str, Any]) -> dict[str, Any]:
        attention = result["attention"]
        status = result["status"]
        matching = [row for row in status.get("turns", []) if row.get("packet") == sid]
        turn = (
            {
                "id": sid,
                "transport": matching[0]["transport"],
                "publication_count": len(matching[0].get("publications", [])),
            }
            if len(matching) == 1
            else {"id": sid, "transport": "unavailable", "publication_count": None}
        )
        return {
            "id": sid,
            "attention": {k: attention[k] for k in ("receipt", "object", "recorded", "boundary")},
            "turn": turn,
            "parent_duties_closed": False,
            "scope": (
                "Candidate custody and requested-turn status only; no unrelated history or "
                "parent-duty inventory. This is not a completed return, semantic verdict or "
                "access-control boundary."
            ),
        }

    def _pending_capture_paths(self, sid: str) -> list[Path]:
        folder = self._host.root / "captures" / sid
        if is_reparse_or_symlink(folder) or not folder.is_dir():
            return []
        return sorted(folder.glob(".pending-*.json"))

    def _reconcile_pending_captures(self, sid: str) -> list[dict[str, Any]]:
        reconciled: list[dict[str, Any]] = []
        requests_dir = self._host.root / "requests"
        requests_dir.mkdir(parents=True, exist_ok=True)

        for pending_path in self._pending_capture_paths(sid):
            pending = read(pending_path)
            rid = ident(pending["receipt"])
            producer = nonempty(pending["producer"], "producer")
            target = self._host.root / "captures" / sid / f"{rid}.json"
            if target.exists():
                pending_path.unlink(missing_ok=True)
                continue

            obj_path = self._host.root / pending["receipt_object"]
            if is_reparse_or_symlink(obj_path) or not obj_path.is_file():
                raise ValueError(f"Retained pending receipt object is unavailable: {rid}")
            check_unfollowed_ancestors(obj_path, "Retained pending receipt object", include_target=True)
            body = obj_path.read_bytes()
            if digest(body) != pending["receipt_sha256"]:
                raise ValueError(f"Retained pending receipt object integrity mismatch: {rid}")

            selected_path = _temp_receipt_file(requests_dir, body)
            try:
                try:
                    result = self._host.reconcile_capture_native(sid, selected_path, producer)
                except ValueError as exc:
                    if str(exc) == "receipt identity not recorded in native Attention":
                        pending_path.unlink(missing_ok=True)
                        self._host.event("pending-capture-uncommitted", {"id": sid, "receipt": rid})
                        continue
                    raise
            finally:
                selected_path.unlink(missing_ok=True)

            capture = self._capture_record(sid, result)
            self._host._write(target, capture)
            pending_path.unlink(missing_ok=True)
            self._host.event(
                "candidate-captured",
                {
                    "id": sid,
                    "receipt": rid,
                    "capture": self._host.pin(target),
                    "reconciled_from_native": True,
                },
            )
            reconciled.append(capture)

        return reconciled

    def _validate_capture_request(
        self,
        sid: str,
        spec: dict[str, Any],
    ) -> tuple[str, str, str, bytes, Path]:
        sid = ident(sid)
        scope = read(self._host.root / "scopes" / f"{sid}.json")
        if set(spec) != {"receipt", "producer"}:
            raise ValueError("Candidate capture requires receipt and producer only")
        if "result" in scope or "superseded" in scope:
            raise ValueError(
                "Candidate capture uses an open producing scope, not a completed or superseded one",
            )

        producer = nonempty(spec["producer"], "producer")
        checked_receipt = check_unfollowed_ancestors(
            spec["receipt"],
            "Assessment receipt",
            include_target=True,
        )
        receipt = checked_receipt.resolve()
        with receipt.open("rb") as submitted:
            if not stat.S_ISREG(os.fstat(submitted.fileno()).st_mode):
                raise ValueError("An ordinary assessment receipt file is required")
            body = submitted.read()

        rid = ident(loads_strict(body)["id"])
        target = self._host.root / "captures" / sid / f"{rid}.json"
        if target.exists() or is_reparse_or_symlink(target):
            raise ValueError(
                "Retain the existing candidate capture; use a fresh receipt for a new occurrence",
            )
        return sid, rid, producer, body, target

    def _prepare_pending_capture(
        self,
        sid: str,
        rid: str,
        producer: str,
        receipt_obj: str,
        receipt_sha: str,
        requests_dir: Path,
    ) -> tuple[Path, bool]:
        pending_path = self._host.root / "captures" / sid / f".pending-{rid}.json"
        created_pending = not pending_path.exists()

        if not created_pending:
            existing_pending = read(pending_path)
            if (
                existing_pending.get("receipt_sha256") != receipt_sha
                or existing_pending.get("producer") != producer
            ):
                old_obj = self._host.root / existing_pending["receipt_object"]
                old_body = (
                    old_obj.read_bytes()
                    if old_obj.is_file() and not is_reparse_or_symlink(old_obj)
                    else None
                )
                if old_body is not None and digest(old_body) == existing_pending.get("receipt_sha256"):
                    prior_path = _temp_receipt_file(requests_dir, old_body)
                    try:
                        try:
                            self._host.reconcile_capture_native(
                                sid,
                                prior_path,
                                existing_pending["producer"],
                            )
                        except ValueError as prior_exc:
                            if str(prior_exc) == "receipt identity not recorded in native Attention":
                                created_pending = True
                        else:
                            raise ValueError("receipt identity already used")
                    finally:
                        prior_path.unlink(missing_ok=True)

        if created_pending:
            self._host._write(
                pending_path,
                {
                    "id": sid,
                    "receipt": rid,
                    "producer": producer,
                    "receipt_object": receipt_obj,
                    "receipt_sha256": receipt_sha,
                    "started_at_utc": now(),
                },
            )

        return pending_path, created_pending

    def _execute_or_reconcile_capture(
        self,
        sid: str,
        producer: str,
        selected_path: Path,
        pending_path: Path,
        created_pending: bool,
    ) -> tuple[dict[str, Any], bool]:
        try:
            return self._host.capture_native(sid, selected_path, producer), False
        except ValueError as exc:
            if str(exc) != "receipt identity already used":
                if created_pending:
                    if str(exc) in _PRECOMMIT_CLEANUP_ERRORS:
                        pending_path.unlink(missing_ok=True)
                    else:
                        try:
                            self._host.reconcile_capture_native(sid, selected_path, producer)
                        except ValueError as probe_exc:
                            if str(probe_exc) == "receipt identity not recorded in native Attention":
                                pending_path.unlink(missing_ok=True)
                raise

            try:
                result = self._host.reconcile_capture_native(sid, selected_path, producer)
            except ValueError as reconcile_exc:
                if created_pending and str(reconcile_exc) in {
                    "receipt identity already used",
                    "receipt must contain id and assessments",
                }:
                    pending_path.unlink(missing_ok=True)
                raise
            return result, True

    def capture(self, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Retain candidate assessments through native Attention without exposing global status."""
        sid, rid, producer, body, target = self._validate_capture_request(sid, spec)
        receipt_obj, receipt_sha = self._put_object_bytes(body)
        requests_dir = self._host.root / "requests"
        requests_dir.mkdir(parents=True, exist_ok=True)

        pending_path, created_pending = self._prepare_pending_capture(
            sid,
            rid,
            producer,
            receipt_obj,
            receipt_sha,
            requests_dir,
        )
        selected_path = _temp_receipt_file(requests_dir, body)
        try:
            result, reconciled = self._execute_or_reconcile_capture(
                sid,
                producer,
                selected_path,
                pending_path,
                created_pending,
            )
        finally:
            selected_path.unlink(missing_ok=True)

        capture = self._capture_record(sid, result)
        self._host._write(target, capture)
        pending_path.unlink(missing_ok=True)
        event_data: dict[str, Any] = {
            "id": sid,
            "receipt": rid,
            "capture": self._host.pin(target),
        }
        if reconciled:
            event_data["reconciled_from_native"] = True
        self._host.event("candidate-captured", event_data)
        return capture
