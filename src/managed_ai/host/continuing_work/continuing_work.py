#!/usr/bin/env python3
"""Optional G60 host bridge: continuing commission, useful reminders, and causal repair custody.

No model client, semantic scheduler, universal ontology, or inferred completeness.
Uses the existing native runtime for context, Attention, receive/adopt, and goal use.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any

HERE = Path(__file__).resolve().parent
SRC_ROOT = HERE.parents[2]
if __package__ in (None, "") and str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from managed_ai.host.continuing_work.capture_native import native_sources
from managed_ai.host.continuing_work.contract import (
    ContinuingWorkService,
    DEFAULT_HOST_CONFIG,
    HostConfig,
)
from managed_ai.host.continuing_work.native_bridge import (
    MAX_EVENT_LINE_BYTES,
    MAX_EVENT_STREAM_BYTES,
    NativeBridge,
    _safe_jsonl,
    build_sibling_cli_command,
    capture_error,
    resolve_sibling_cli,
)
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
    write,
)
from managed_ai.host.continuing_work.repair_custody import RepairCustody
from managed_ai.host.continuing_work.scope_lifecycle import (
    ScopeLifecycle,
    operation_guide_path,
    overlap_rows,
    prepare_attention_selection,
    prepare_questions,
)
from managed_ai.platform import safe_relpath as _safe_relpath

_FIELD_TEXT = (
    "The full commission remains active. A local contribution may finish while other duties remain owed. "
    "Reopen broader alternatives when evidence changes the useful contribution. "
    "Discover unrepresented relationships; the host only routes nominated ones.\n"
)
_START_RECEIPT_TEXT = (
    "Host initialized exact commission, explicit duties and the continuing commission Attention item. "
    "This is enrollment, not a model reading or semantic completion claim.\n"
)
_PRODUCER_DEPENDENCY_FILES = (
    "contract.py",
    "occurrence_files.py",
    "repair_custody.py",
    "native_bridge.py",
    "scope_prepare.py",
    "scope_receive.py",
    "scope_lifecycle.py",
)


def _read_ordinary_file_bytes(raw_path: Path | str, role: str) -> tuple[Path, bytes]:
    checked = check_unfollowed_ancestors(raw_path, role, include_target=True)
    try:
        info = checked.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{role} is absent: {checked}") from exc

    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{role} must be an ordinary file: {checked}")

    resolved = checked.resolve()
    return resolved, resolved.read_bytes()


def _require_spec_object(raw_spec: Any, action: str) -> dict[str, Any]:
    if not isinstance(raw_spec, dict):
        raise ValueError(f"{action} spec must be a JSON object")
    return raw_spec


class Host:
    """Continuing-work host bridge coordinating local custody with the native runtime."""

    def __init__(self, root: Path | str, *, host_config: HostConfig = DEFAULT_HOST_CONFIG) -> None:
        checked_root = check_unfollowed_ancestors(root, "Host root", include_target=True)
        self.root = checked_root.resolve()
        self.host_config = host_config
        self.config_path = self.root / "host.json"
        self.config = read(self.config_path) if self.config_path.exists() else None
        self.native = (self.root / (self.config or {}).get("native_root", "native")).resolve()
        self.work = self.native / "work"
        self.bridge = NativeBridge(self)
        self.repairs = RepairCustody(self)
        self.lifecycle = ScopeLifecycle(self)

    def _write(self, path: Path | str, value: Any) -> None:
        write(path, value)

    def _put_object_bytes(self, raw: bytes) -> tuple[str, str]:
        return self.bridge._put_object_bytes(raw)

    def event(self, action: str, value: dict[str, Any]) -> dict[str, Any]:
        return self.bridge.event(action, value)

    def call(self, *args: Any, scoped_errors: bool = False) -> Any:
        return self.bridge.call(*args, scoped_errors=scoped_errors)

    def inspect_native(self, sid: str, scoped_errors: bool = False) -> dict[str, Any]:
        return self.bridge.inspect_native(sid, scoped_errors=scoped_errors)

    def capture_native(self, sid: str, receipt: Path | str, producer: str) -> dict[str, Any]:
        return self.bridge.capture_native(sid, receipt, producer)

    def reconcile_capture_native(self, sid: str, receipt: Path | str, producer: str) -> dict[str, Any]:
        return self.bridge.reconcile_capture_native(sid, receipt, producer)

    def temp(self, name: str, value: Any) -> Path:
        return self.bridge.temp(name, value)

    def pin(self, path: Path | str) -> dict[str, Any]:
        return self.bridge.pin(path)

    def capture(self, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
        return self.bridge.capture(sid, spec)

    def repair_open(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self.repairs.repair_open(spec)

    def repair_accept(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self.repairs.repair_accept(spec)

    def prepare(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.prepare(spec)

    def receive(self, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.receive(sid, spec)

    def resume(self, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.resume(sid, spec)

    def supersede(self, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.supersede(sid, spec)

    def status(self) -> dict[str, Any]:
        return self.lifecycle.status()

    @property
    def engine(self) -> str:
        return str((self.root / self.config["engine"]).resolve())

    def producer_dependencies(self) -> list[dict[str, Any]]:
        return [self.pin(HERE / name) for name in _PRODUCER_DEPENDENCY_FILES]

    def goal(self) -> dict[str, Any]:
        return self.call("show", "goal")

    def current(self, pin: dict[str, Any], label: str) -> None:
        path = Path(pin["path"])
        if is_reparse_or_symlink(path):
            raise ValueError(label + " must not be a symlink or reparse point")
        actual = digest(path.read_bytes()) if path.is_file() else None
        if actual != pin["sha256"]:
            self.event(
                "changed-current-source",
                {"label": label, "captured": pin, "current_sha256": actual},
            )
            raise ValueError(
                label
                + " changed outside this captured occurrence; preserve the proposal and prepare current work",
            )

    def received_outputs(self, scope: dict[str, Any]) -> list[dict[str, Any]]:
        if "result" not in scope:
            raise ValueError("Scope has no actual received output")
        for pin in scope["result"]["outputs"]:
            self.current(pin, "Received output " + scope["id"])
            obj = self.root / pin["object"]
            if is_reparse_or_symlink(obj) or not obj.is_file() or digest(obj.read_bytes()) != pin["sha256"]:
                raise ValueError("Retained received-output object differs")
        return scope["result"]["outputs"]

    def _validate_init_spec(
        self,
        spec: dict[str, Any],
    ) -> tuple[Path, Path, bytes, bytes, list[dict[str, Any]]]:
        if self.config is not None or (self.root.exists() and any(self.root.iterdir())):
            raise ValueError("Retain the existing host; initialize only a new empty root")

        engine_checked = check_unfollowed_ancestors(
            spec["engine"],
            "Current native engine",
            include_target=True,
        )
        if not engine_checked.exists() or not stat.S_ISREG(engine_checked.lstat().st_mode):
            raise ValueError("Current native engine is absent")

        engine = engine_checked.resolve()
        commission, commission_bytes = _read_ordinary_file_bytes(
            spec["commission"],
            "Commission source",
        )
        operation_bytes = operation_guide_path().read_bytes()

        duties = spec["duties"]
        if not isinstance(duties, list) or not duties:
            raise ValueError("This continuing-work route requires explicit commissioned duties")
        for row in duties:
            ident(row["id"])
            nonempty(row["text"], "duty text")
        if len({row["id"] for row in duties}) != len(duties):
            raise ValueError("Duplicate duty")

        return engine, commission, commission_bytes, operation_bytes, duties

    def _init_attached_host(
        self,
        spec: dict[str, Any],
        engine: Path,
        commission: Path,
        duties: list[dict[str, Any]],
    ) -> dict[str, Any]:
        native_checked = check_unfollowed_ancestors(
            spec["native_root"],
            "Native root",
            include_target=True,
        )
        self.native = native_checked.resolve()
        self.work = self.native / "work"
        descriptor = read(self.native / "system.json")
        self.root.mkdir(parents=True, exist_ok=True)

        self.config = {
            "format": self.host_config.format_version,
            "engine": _safe_relpath(engine, self.root),
            "created_at_utc": now(),
            "commission": self.pin(commission),
            "native_root": _safe_relpath(self.native, self.root),
            "attached_instance_id": descriptor["instance_id"],
            "scope": "Caller-enrolled continuing work; semantic completeness is not inferred.",
        }
        try:
            status = self.call("status")
            supplied = set(status["owed_record_obligations"]) | {
                row["id"] for row in status["owed_goal_obligations"]
            }
            if any(row["id"] not in supplied for row in duties):
                raise ValueError("attached host duties must name existing owed native obligations")
        except BaseException:
            self.config = None
            raise

        self._write(self.config_path, self.config)
        return {
            "root": str(self.root),
            "native": str(self.native),
            "attached": True,
            "instance_id": descriptor["instance_id"],
            "goal": status["current_goal"],
            "boundary": (
                "The existing native store retains goals and duty ownership. Attachment creates "
                "no new runtime or artificial participant return."
            ),
        }

    def _init_new_native_host(
        self,
        engine: Path,
        commission: Path,
        commission_bytes: bytes,
        operation_bytes: bytes,
        duties: list[dict[str, Any]],
    ) -> dict[str, Any]:
        commission_text = commission_bytes.decode("utf-8")
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = {
            "format": self.host_config.format_version,
            "engine": _safe_relpath(engine, self.root),
            "created_at_utc": now(),
            "commission": self.pin(commission),
            "scope": "Caller-enrolled continuing work; semantic completeness is not inferred.",
        }

        try:
            initial = {
                "id": "goal",
                "kind": "goal",
                "content": {
                    "contribution": commission_text,
                    "obligations": [dict(row, disposition="open") for row in duties],
                },
            }
            self.call("init", "--goal", self.temp("init-goal.json", initial))
            atomic_bytes(self.work / "sources/host/COMMISSION.md", commission_bytes)
            atomic_bytes(self.work / "sources/host/OPERATION.md", operation_bytes)

            current = self.goal()
            bound = {
                "id": "goal",
                "kind": "goal",
                "content": current["content"],
                "basis": ["sources/host/COMMISSION.md"],
            }
            self.call(
                "put",
                "--record",
                self.temp("bind-commission.json", bound),
                "--expected",
                current["revision"],
            )
            atomic_bytes(self.work / "sources/host/FIELD.md", _FIELD_TEXT.encode("utf-8"))

            question = {
                "field": "sources/host/FIELD.md",
                "items": [
                    {
                        "id": "continuing-commission",
                        "question": "Which commissioned effects remain owed beyond this local contribution?",
                        "use": "Keep current work aligned with the containing commission.",
                        "basis": ["sources/host/COMMISSION.md", "records/goal.json"],
                        "groups": ["continuation"],
                    },
                ],
            }
            self.call("track", "--spec", self.temp("initial-attention.json", question))
            reply = self.root / "requests/start-receipt.md"
            atomic_bytes(reply, _START_RECEIPT_TEXT.encode("utf-8"))
            self.call("receive", "start", "--reply", reply, "--producer", "continuing-work enrollment adapter")
            final_goal = self.goal()
            self._write(self.config_path, self.config)
        except BaseException:
            self.config = None
            raise

        return {
            "root": str(self.root),
            "native": str(self.native),
            "goal": final_goal,
            "boundary": "Subsequent actual capable participant must read the prepared context.",
        }

    def init(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Initialize a new continuing-work host or attach an existing native store."""
        engine, commission, commission_bytes, operation_bytes, duties = self._validate_init_spec(spec)
        if spec.get("native_root"):
            return self._init_attached_host(spec, engine, commission, duties)
        return self._init_new_native_host(engine, commission, commission_bytes, operation_bytes, duties)

    def bind_continuation(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Add current-goal dependency to an existing host-owned inquiry through native APIs."""
        if set(spec) != {"id", "account"}:
            raise ValueError("Continuation binding requires a fresh id and account only")

        operation = ident(spec["id"])
        account = nonempty(spec["account"], "continuation binding account")
        destination = self.root / "continuation-bindings" / f"{operation}.json"
        if destination.exists():
            raise ValueError("Retain the existing continuation binding; use a fresh operation identity")

        self.current(self.config["commission"], "Current commission")
        goal = self.goal()
        if not (self.work / "records/goal.json").is_file():
            raise ValueError("Current native goal material is absent")

        command, env = build_sibling_cli_command(
            Path(self.engine),
            "managed_ai.core.attention",
            "core/attention.py",
            "track_attention.py",
            "--root",
            self.native / "exchange",
            "feedback",
            operation,
            "--item",
            "continuing-commission",
            "--limit",
            "1",
        )
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", env=env)
        self.event(
            "continuation-binding-observation",
            {
                "argv": command,
                "exit": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )
        if result.returncode:
            raise ValueError(result.stderr or result.stdout)

        observed = loads_strict(result.stdout)
        if (
            observed["omitted"]
            or len(observed["delivered"]) != 1
            or observed["delivered"][0]["id"] != "continuing-commission"
        ):
            raise ValueError(
                "The continuing inquiry was not completely delivered; preserve the observation and inspect it",
            )

        before = observed["delivered"][0]["spec"]
        after = dict(before, basis=sorted(set(before["basis"]) | {"records/goal.json"}))
        changed = after != before
        tracked = None
        if changed:
            tracked = self.call(
                "track",
                "--spec",
                self.temp(operation + "-continuation-binding.json", {"items": [after]}),
            )

        record = {
            "id": operation,
            "at_utc": now(),
            "account": account,
            "goal": {k: goal[k] for k in ("revision", "sha256")},
            "observation": {
                "id": observed["id"],
                "object": observed["feedback_object"],
                "context_sha256": observed["context_sha256"],
            },
            "before": before,
            "current": after,
            "changed": changed,
            "track": tracked,
            "boundary": (
                "Basis enrollment only. Historical accounts remain unchanged; the next actual "
                "feedback and semantic receiving determine current applicability. External effects "
                "absent from the basis are not inferred."
            ),
        }
        self._write(destination, record)
        self.event(
            "continuation-basis-bound",
            {"id": operation, "record": self.pin(destination), "changed": changed},
        )
        return record

    def commission(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Explicitly revise current instructions while preserving earlier enrolled duties."""
        old = self.config["commission"]
        account = nonempty(spec["account"], "commission continuation account")
        source_path, new_bytes = _read_ordinary_file_bytes(spec["path"], "Commission source")
        new_text = new_bytes.decode("utf-8")
        goal = self.goal()

        duties = {row["id"]: dict(row) for row in goal["content"]["obligations"]}
        for row in spec.get("duties", []):
            rid = ident(row["id"])
            nonempty(row["text"], "duty text")
            if rid in duties and duties[rid]["text"] != row["text"]:
                raise ValueError(
                    "Do not silently replace an earlier duty; add a new identity or reopen/account for it",
                )
            if rid not in duties:
                duties[rid] = dict(row, disposition="open")

        new = self.pin(source_path)
        target = self.work / "sources/host/COMMISSION.md"
        prior_exists = target.exists() and not is_reparse_or_symlink(target)
        prior_bytes = target.read_bytes() if prior_exists else None
        atomic_bytes(target, new_bytes)

        try:
            content = {
                "contribution": new_text,
                "obligations": list(duties.values()),
                "continuation_account": account,
            }
            record = {"id": "goal", "kind": "goal", "content": content, "basis": ["sources/host/COMMISSION.md"]}
            result = self.call(
                "put",
                "--record",
                self.temp("commission-" + str(goal["revision"]) + ".json", record),
                "--expected",
                goal["revision"],
            )
        except BaseException:
            if prior_exists and prior_bytes is not None:
                atomic_bytes(target, prior_bytes)
            else:
                target.unlink(missing_ok=True)
            raise

        self.config["commission"] = new
        self._write(self.config_path, self.config)
        self.event(
            "commission-continued",
            {"before": old, "current": new, "account": account, "preserved_duties": list(duties)},
        )
        return result

    def upgrade(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Select a regenerated host/guidance release with byte-identical native programs."""
        new_checked = check_unfollowed_ancestors(
            spec["engine"],
            "New installed engine",
            include_target=True,
        )
        if not new_checked.exists() or not stat.S_ISREG(new_checked.lstat().st_mode):
            raise ValueError("New installed engine is absent")

        new = new_checked.resolve()
        old = Path(self.engine)
        if native_sources(old)[1] != native_sources(new)[1]:
            raise ValueError(
                "This upgrade route requires identical native programs; a native-code change "
                "needs its own compatibility receiving",
            )

        account = nonempty(spec["account"], "upgrade account")
        before = self.call("status")
        previous = dict(self.config)
        self.config = dict(self.config, engine=_safe_relpath(new, self.root))

        try:
            after = self.call("status")
        except Exception:
            self.config = previous
            raise

        if before["current_goal"]["sha256"] != after["current_goal"]["sha256"]:
            self.config = previous
            raise ValueError("Current goal changed while checking the selected release")

        self._write(self.config_path, self.config)
        host_producer = self.pin(Path(__file__).resolve())
        host_dependencies = self.producer_dependencies()
        host_method = self.pin(operation_guide_path())
        self.event(
            "producing-release-selected",
            {
                "before_engine": self.pin(old),
                "current_engine": self.pin(new),
                "host_producer": host_producer,
                "host_dependencies": host_dependencies,
                "host_method": host_method,
                "account": account,
                "native_programs_unchanged": True,
                "prior_captures_unchanged": True,
            },
        )
        return {
            "engine": str(new),
            "host_producer": host_producer,
            "host_method": host_method,
            "host_dependencies": host_dependencies,
            "native_programs_unchanged": True,
            "parent_duties_unchanged": True,
        }

    def refresh_resource(self, rid: str, source: Path | str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        rid = ident(rid)
        old_path = self.root / "resources" / f"{rid}.json"
        previous = read(old_path) if old_path.exists() else None
        pin = self.pin(source)
        suffix = Path(source).suffix or ".txt"
        path = "sources/resources/" + rid + suffix

        if previous and previous["work_path"] != path:
            raise ValueError(
                "A resource retains its file type/path; use a new identity for another interpretation",
            )

        target = self.work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or digest(target.read_bytes()) != pin["sha256"]:
            atomic_bytes(target, (self.root / pin["object"]).read_bytes())

        row = {"id": rid, **pin, "work_path": path}
        self._write(old_path, row)
        return row, previous if previous and previous["sha256"] != row["sha256"] else None

    resource = refresh_resource

    def scopes(self) -> list[dict[str, Any]]:
        return [read(path) for path in sorted((self.root / "scopes").glob("*.json"))]

    def duty(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Account for fulfilling or reopening an enrolled parent duty."""
        self.current(self.config["commission"], "Current commission")
        goal = self.goal()
        rows = goal["content"]["obligations"]
        selected = next((row for row in rows if row["id"] == spec["id"]), None)
        if selected is None:
            raise ValueError("Unknown parent duty")
        if spec["disposition"] not in ("fulfilled", "open"):
            raise ValueError("This route cannot release a user duty")

        evidence = spec.get("evidence", [])
        if spec["disposition"] == "fulfilled":
            if not evidence:
                raise ValueError("Fulfillment requires actual received evidence")
            evidence_set = set(evidence)
            all_scopes = self.scopes()
            published = {
                pub["path"]
                for scope in all_scopes
                if "result" in scope
                for pub in scope["result"]["publications"]
            }
            if evidence_set - published:
                raise ValueError("Use actual current returned outputs as evidence")
            for scope in all_scopes:
                if "result" in scope and {
                    pub["path"] for pub in scope["result"]["publications"]
                } & evidence_set:
                    self.received_outputs(scope)
            selected.update(
                disposition="fulfilled",
                account=nonempty(spec["account"], "duty account"),
                evidence=evidence,
            )
        else:
            selected.update(
                disposition="open",
                account=nonempty(spec["account"], "reopened duty account"),
            )

        record = {"id": "goal", "kind": "goal", "content": goal["content"], "basis": ["sources/host/COMMISSION.md"]}
        result = self.call(
            "put",
            "--record",
            self.temp("duty-" + ident(spec["id"]) + "-" + str(goal["revision"]) + ".json", record),
            "--expected",
            goal["revision"],
        )
        self.event(
            "duty-accounted",
            {"id": spec["id"], "disposition": spec["disposition"], "evidence": evidence},
        )
        return result

    def finish(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Record whole-goal completion once all parent duties are fulfilled or released."""
        self.current(self.config["commission"], "Current commission")
        goal = self.goal()
        owed = [
            row["id"]
            for row in goal["content"]["obligations"]
            if row["disposition"] not in ("fulfilled", "released")
        ]
        if owed:
            raise ValueError("Containing commission remains owed: " + ", ".join(owed))

        account = self.root / "requests/whole-goal-account.md"
        atomic_bytes(account, nonempty(spec["account"], "whole goal account").encode("utf-8"))
        args: list[Any] = [
            "interpret",
            "--goal-revision",
            goal["revision"],
            "--disposition",
            "met",
            "--account",
            account,
        ]
        for path in spec.get("evidence", []):
            args += ["--evidence", path]
        return self.call(*args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--root", required=True, type=Path)
    sub = parser.add_subparsers(dest="action", required=True)

    spec_only_actions = (
        "init",
        "commission",
        "upgrade",
        "prepare",
        "repair-open",
        "repair-accept",
        "duty",
        "finish",
        "bind-continuation",
    )
    for name in spec_only_actions:
        sub.add_parser(name, allow_abbrev=False).add_argument("--spec", required=True, type=Path)

    for name in ("receive", "capture", "resume", "supersede"):
        cmd = sub.add_parser(name, allow_abbrev=False)
        cmd.add_argument("id")
        cmd.add_argument("--spec", required=True, type=Path)

    sub.add_parser("status", allow_abbrev=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for continuing-work host operations."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        checked_root = check_unfollowed_ancestors(args.root, "Host root", include_target=True)
        root = checked_root.resolve()
        root.parent.mkdir(parents=True, exist_ok=True)

        with file_lock(root.parent / (root.name + ".host-lock")):
            host = Host(root)
            if args.action != "init" and host.config is None:
                raise ValueError("Initialize the explicit continuing commission first")

            if args.action == "status":
                result = host.status()
            elif args.action in ("capture", "receive", "resume", "supersede"):
                spec = _require_spec_object(read(args.spec), args.action)
                result = getattr(host, args.action)(args.id, spec)
            else:
                spec = _require_spec_object(read(args.spec), args.action)
                result = getattr(host, args.action.replace("-", "_"))(spec)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
