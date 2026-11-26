"""Material continuation around the unchanged bounded callable controller.

One cooperative local owner. Historical callback returns are replayed; effects
are never blindly retried. A pending intent remains an unresolved outcome.
"""
from __future__ import annotations

import argparse
from collections.abc import Mapping
from contextlib import contextmanager
import json
from pathlib import Path
import sys

try:
    from .config import load_controller as _load_controller
    from .io_utils import (
        BasisMismatchError,
        OwnerLockError,
        atomic_json,
        canonical_bytes as _canonical,
        canonical_digest as _digest,
        detached_json as _snapshot,
        exclusive_lock,
        read_json_strict as _read,
        sha256_bytes,
    )
except ImportError:
    _src_root = Path(__file__).resolve().parents[4]
    if str(_src_root) not in sys.path:
        sys.path.insert(0, str(_src_root))
    from managed_ai.companions.workbench.core.config import load_controller as _load_controller
    from managed_ai.companions.workbench.core.io_utils import (
        BasisMismatchError,
        OwnerLockError,
        atomic_json,
        canonical_bytes as _canonical,
        canonical_digest as _digest,
        detached_json as _snapshot,
        exclusive_lock,
        read_json_strict as _read,
        sha256_bytes,
    )

__all__ = [
    "BasisMismatchError",
    "run_durable_agent",
    "inspect_durable_state",
    "resolve_durable_operation",
]

SCHEMA = "g60-durable-agent/1"


class _StateError(Exception):
    pass


class _ReplayMismatch(BaseException):
    pass


class _InfrastructureStop(BaseException):
    pass


def _file_digest(path):
    return sha256_bytes(Path(path).read_bytes())


def _write_new(path, value):
    """Publish one immutable complete record; never overwrite prior evidence."""
    atomic_json(path, value, exclusive=True)


@contextmanager
def _lock(root):
    try:
        with exclusive_lock(Path(root) / ".owner.lock"):
            yield
    except OwnerLockError as exc:
        raise _StateError(str(exc)) from exc


def _counts(operations):
    intents = {"model": 0, "tool": 0}
    completions = {"model": 0, "tool": 0}
    unresolved = {"model": 0, "tool": 0}
    for operation in operations:
        kind = operation["intent"]["callback"]
        intents[kind] += 1
        if operation["completion"] is None:
            unresolved[kind] += 1
        else:
            completions[kind] += 1
    return intents, completions, unresolved


def _load_state(root):
    manifest = _read(root / "manifest.json")
    if manifest.get("schema") != SCHEMA:
        raise _StateError("unsupported journal manifest")
    if _digest(manifest["contract"]) != manifest["contract_sha256"]:
        raise _StateError("manifest contract identity mismatch")
    if _digest(manifest["initial_checkpoint"]) != manifest["initial_checkpoint_sha256"]:
        raise _StateError("initial checkpoint identity mismatch")
    operations = []
    pending_seen = False
    files = sorted((root / "calls").glob("*.intent.json"))
    for index, path in enumerate(files, 1):
        identifier = f"{index:06d}"
        if path.name != identifier + ".intent.json":
            raise _StateError("noncontiguous operation identities")
        intent = _read(path)
        if (intent.get("schema") != SCHEMA or intent.get("id") != identifier
                or intent.get("callback") not in ("model", "tool")
                or _digest(intent["request"]) != intent["request_sha256"]):
            raise _StateError("invalid callback intent: " + identifier)
        if pending_seen:
            raise _StateError("operation exists after unresolved intent")
        return_path = root / "calls" / (identifier + ".return.json")
        completion = None
        if return_path.exists():
            completion = _read(return_path)
            if (completion.get("schema") != SCHEMA
                    or completion.get("operation_id") != identifier
                    or completion.get("intent_sha256") != _digest(intent)
                    or completion.get("outcome", {}).get("kind") not in ("return", "exception")
                    or _digest(completion["checkpoint"]) != completion["checkpoint_sha256"]):
                raise _StateError("invalid callback completion: " + identifier)
        else:
            pending_seen = True
        operations.append({"intent": intent, "completion": completion})
    expected_returns = {op["intent"]["id"] + ".return.json"
                        for op in operations if op["completion"] is not None}
    if {p.name for p in (root / "calls").glob("*.return.json")} != expected_returns:
        raise _StateError("orphan callback completion")
    return manifest, operations


def _stats(manifest, operations, *, actual=None, replayed=None):
    intents, completions, unresolved = _counts(operations)
    limit = manifest["contract"]["max_steps"]
    return {
        "contract_sha256": manifest["contract_sha256"],
        "replayed_callbacks_this_invocation": replayed or {"model": 0, "tool": 0},
        "actual_dispatches_this_invocation": actual or {"model": 0, "tool": 0},
        "dispatch_intents_total": intents,
        "observed_completions_total": completions,
        "unresolved_intents_total": unresolved,
        "model_budget_limit": limit,
        "model_budget_charged": intents["model"],
        "model_budget_remaining": max(0, limit - intents["model"]),
        "dispatch_evidence": "completed outcomes establish observations; pending intents do not prove invocation",
    }


def _operation_summary(intent):
    return {key: intent[key] for key in ("id", "callback", "name", "logical_step")}


def _failure(status, message, *, manifest=None, operations=None, operation=None,
             actual=None, replayed=None, details=None, error_code=None):
    error = {"code": error_code or status, "message": message}
    if operation is not None:
        error["operation"] = _operation_summary(operation)
    if details is not None:
        error["details"] = details
    result = {"status": status, "answer": None, "error": error,
              "model_calls": 0, "tool_calls": 0, "trace": None}
    if manifest is not None:
        result["durable"] = _stats(manifest, operations or [], actual=actual,
                                   replayed=replayed)
    return result


def inspect_durable_state(state_dir):
    """Read the last published material state without callback invocation."""
    root = Path(state_dir).resolve()
    try:
        manifest, operations = _load_state(root)
        pending = [_operation_summary(op["intent"]) for op in operations
                   if op["completion"] is None]
        latest = manifest["initial_checkpoint"]
        for op in operations:
            if op["completion"] is not None:
                latest = op["completion"]["checkpoint"]
        result_path = root / "controller-result.json"
        result = _read(result_path) if result_path.exists() else None
        return {
            "schema": SCHEMA,
            "status": "unresolved" if pending else "ready",
            "manifest": manifest,
            "operations": [
                {
                    **_operation_summary(op["intent"]),
                    "status": "completed" if op["completion"] is not None else "unresolved",
                    "completion_origin": op["completion"].get("origin") if op["completion"] else None,
                }
                for op in operations
            ],
            "pending": pending,
            "checkpoint": latest,
            "controller_result": result,
            "durable": _stats(manifest, operations),
        }
    except (OSError, ValueError, TypeError, KeyError, _StateError) as exc:
        return _failure("state_error", str(exc))


class _CallbackJournal:
    def __init__(self, root, operations, actual, replayed, max_steps, checkpoint, after_record):
        self.calls = root / "calls"
        self.operations = operations
        self.actual = actual
        self.replayed = replayed
        self.max_steps = max_steps
        self.checkpoint = checkpoint
        self.after_record = after_record
        self.cursor = 0

    def invoke(self, callback_kind, name, current_step, request, callback):
        request = _snapshot(request)
        identifier = f"{self.cursor + 1:06d}"
        desired = {
            "schema": SCHEMA, "id": identifier,
            "callback": callback_kind, "name": name,
            "logical_step": current_step,
            "request": request, "request_sha256": _digest(request),
        }
        if self.cursor < len(self.operations):
            return self._replay_completed(desired)
        return self._record_new_call(desired, callback)

    def _replay_completed(self, desired):
        stored = self.operations[self.cursor]
        if _canonical(stored["intent"]) != _canonical(desired):
            raise _ReplayMismatch("reconstructed request differs at operation " + desired["id"])
        self.cursor += 1
        self.replayed[desired["callback"]] += 1
        outcome = stored["completion"]["outcome"]
        if outcome["kind"] == "exception":
            replay_exception = type(outcome["exception_type"], (Exception,), {})
            raise replay_exception(outcome["message"])
        return _snapshot(outcome["value"])

    def _record_new_call(self, desired, callback):
        identifier = desired["id"]
        callback_kind = desired["callback"]
        if callback_kind == "model" and _counts(self.operations)[0]["model"] >= self.max_steps:
            raise _ReplayMismatch("original model budget exhausted before dispatch")
        try:
            _write_new(self.calls / (identifier + ".intent.json"), desired)
        except Exception as exc:
            raise _InfrastructureStop("intent persistence failed: " + str(exc)) from exc
        operation = {"intent": desired, "completion": None}
        self.operations.append(operation)
        self.cursor += 1
        self.actual[callback_kind] += 1
        caught = None
        try:
            value = callback(_snapshot(desired["request"]))
        except Exception as exc:
            caught = exc
            outcome = {"kind": "exception", "exception_type": type(exc).__name__,
                       "message": str(exc)}
        else:
            try:
                value = _snapshot(value)
            except Exception as exc:
                caught = TypeError("durable callback return must be finite JSON: " + str(exc))
                outcome = {"kind": "exception", "exception_type": type(caught).__name__,
                           "message": str(caught)}
            else:
                outcome = {"kind": "return", "value": value}
        try:
            post_state = _snapshot(self.checkpoint())
            completion = {
                "schema": SCHEMA, "operation_id": identifier,
                "intent_sha256": _digest(desired), "origin": "callback",
                "outcome": outcome, "checkpoint": post_state,
                "checkpoint_sha256": _digest(post_state),
            }
            completion_path = self.calls / (identifier + ".return.json")
            _write_new(completion_path, completion)
            operation["completion"] = completion
        except Exception as exc:
            raise _InfrastructureStop("completion persistence failed: " + str(exc)) from exc
        if self.after_record is not None:
            event = {
                "event": "callback_completed", "operation_id": identifier,
                "callback": callback_kind, "name": desired["name"],
                "logical_step": desired["logical_step"],
                "completion_path": str(completion_path), "resolved": False,
            }
            try:
                self.after_record(_snapshot(event))
            except Exception as exc:
                raise _InfrastructureStop("completion observer stopped: " + str(exc)) from exc
        if caught is not None:
            raise caught
        return _snapshot(value)


def run_durable_agent(*, state_dir, binding, task, sources, model, tools,
                      max_steps, resume=False, checkpoint=None, restore=None,
                      after_record=None, controller_dir=None):
    """Start or explicitly resume one bound occurrence using material replay."""
    root = Path(state_dir).resolve()
    actual = {"model": 0, "tool": 0}
    replayed = {"model": 0, "tool": 0}
    manifest = None
    operations = []
    try:
        if not isinstance(task, str):
            raise TypeError("task must be a string")
        if (not isinstance(sources, Mapping)
                or any(not isinstance(k, str) or not isinstance(v, str)
                       for k, v in sources.items())):
            raise TypeError("sources must map string IDs to string bodies")
        if not callable(model):
            raise TypeError("model must be callable")
        if (not isinstance(tools, Mapping)
                or any(not isinstance(k, str) or not callable(v) for k, v in tools.items())):
            raise TypeError("tools must map string names to callables")
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 0:
            raise ValueError("max_steps must be a nonnegative integer")
        if not isinstance(resume, bool):
            raise TypeError("resume must be boolean")
        for name, value in (("checkpoint", checkpoint), ("restore", restore),
                            ("after_record", after_record)):
            if value is not None and not callable(value):
                raise TypeError(name + " must be callable")
        checkpoint = checkpoint or (lambda: None)
        restore = restore or (lambda value: None)
        run_agent, code = _load_controller(controller_dir)
        code["durable_agent.py"] = _file_digest(__file__)
        contract = {
            "task": task, "sources": dict(sources),
            "tool_names": sorted(tools), "max_steps": max_steps,
            "binding": _snapshot(binding), "code": code,
        }
        contract_sha256 = _digest(contract)
        if resume and not (root / "manifest.json").is_file():
            return _failure("state_error", "resume requires an existing manifest")
        root.mkdir(parents=True, exist_ok=True)
        with _lock(root):
            if resume:
                manifest, operations = _load_state(root)
                if manifest["contract_sha256"] != contract_sha256:
                    changed = [
                        key for key in contract
                        if _canonical(contract[key]) != _canonical(manifest["contract"].get(key))
                    ]
                    return _failure(
                        "binding_mismatch", "current inputs do not match this occurrence",
                        manifest=manifest, operations=operations,
                        details={
                            "changed_fields": changed,
                            "expected_contract_sha256": manifest["contract_sha256"],
                            "observed_contract_sha256": contract_sha256,
                        },
                    )
            else:
                if (root / "manifest.json").exists():
                    return _failure("state_error", "occurrence exists; use explicit resume")
                if any((root / "calls").glob("*.json")):
                    return _failure("state_error", "orphan call records prevent a new occurrence")
                initial = _snapshot(checkpoint())
                manifest = {
                    "schema": SCHEMA, "contract": contract,
                    "contract_sha256": contract_sha256,
                    "initial_checkpoint": initial,
                    "initial_checkpoint_sha256": _digest(initial),
                }
                _write_new(root / "manifest.json", manifest)
            pending = [op for op in operations if op["completion"] is None]
            if pending:
                return _failure(
                    "unresolved", "operation outcome is not durably known; no redispatch",
                    manifest=manifest, operations=operations,
                    operation=pending[0]["intent"],
                )
            latest = operations[-1]["completion"]["checkpoint"] if operations else manifest["initial_checkpoint"]
            try:
                restore(_snapshot(latest))
            except BasisMismatchError as exc:
                result = _failure(
                    "binding_mismatch", str(exc),
                    manifest=manifest, operations=operations,
                    actual=actual, replayed=replayed,
                    details=exc.details, error_code=exc.code,
                )
                if isinstance(exc.details, dict) and "changed_paths" in exc.details:
                    result["error"]["changed_paths"] = exc.details["changed_paths"]
                return result
            journal = _CallbackJournal(root, operations, actual, replayed, max_steps, checkpoint, after_record)
            current_step = 0

            def durable_model(request):
                nonlocal current_step
                current_step = request["step"]
                return journal.invoke("model", "model", current_step, request, model)

            # Preserve the original display name in controller error traces.
            durable_model.__name__ = getattr(model, "__name__", type(model).__name__)
            wrapped_tools = {
                name: (lambda arguments, n=name, callback=callback:
                       journal.invoke("tool", n, current_step, arguments, callback))
                for name, callback in tools.items()
            }
            try:
                result = run_agent(
                    task=task, sources=dict(sources), model=durable_model,
                    tools=wrapped_tools, max_steps=max_steps,
                )
                if journal.cursor != len(operations):
                    raise _ReplayMismatch("controller ended before recorded operations were consumed")
            except _ReplayMismatch as exc:
                return _failure("binding_mismatch", str(exc), manifest=manifest,
                                operations=operations, actual=actual, replayed=replayed)
            except _InfrastructureStop as exc:
                unresolved = next((op for op in operations if op["completion"] is None), None)
                return _failure(
                    "unresolved" if unresolved else "state_error", str(exc),
                    manifest=manifest, operations=operations,
                    operation=unresolved["intent"] if unresolved else None,
                    actual=actual, replayed=replayed,
                )
            result_path = root / "controller-result.json"
            if result_path.exists():
                if _canonical(_read(result_path)) != _canonical(result):
                    return _failure("binding_mismatch", "reconstructed terminal result changed",
                                    manifest=manifest, operations=operations,
                                    actual=actual, replayed=replayed)
            else:
                _write_new(result_path, result)
            result["durable"] = _stats(manifest, operations, actual=actual, replayed=replayed)
            return result
    except BasisMismatchError as exc:
        result = _failure(
            "binding_mismatch", str(exc),
            manifest=manifest, operations=operations,
            actual=actual, replayed=replayed,
            details=exc.details, error_code=exc.code,
        )
        if isinstance(exc.details, dict) and "changed_paths" in exc.details:
            result["error"]["changed_paths"] = exc.details["changed_paths"]
        return result
    except (OSError, ValueError, TypeError, KeyError, _StateError) as exc:
        return _failure("state_error", str(exc), manifest=manifest, operations=operations,
                        actual=actual, replayed=replayed)


def resolve_durable_operation(*, state_dir, operation_id, value, checkpoint,
                              evidence, expected_contract_sha256):
    """Record an owner's observed outcome; never dispatch or retry the effect."""
    root = Path(state_dir).resolve()
    try:
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("nonempty owner evidence account is required")
        value = _snapshot(value)
        checkpoint = _snapshot(checkpoint)
        with _lock(root):
            manifest, operations = _load_state(root)
            if expected_contract_sha256 != manifest["contract_sha256"]:
                return _failure("binding_mismatch", "resolution contract identity differs",
                                manifest=manifest, operations=operations)
            pending = [op for op in operations if op["completion"] is None]
            if len(pending) != 1 or pending[0]["intent"]["id"] != operation_id:
                return _failure("state_error", "resolution must identify the single pending operation",
                                manifest=manifest, operations=operations)
            intent = pending[0]["intent"]
            completion = {
                "schema": SCHEMA, "operation_id": operation_id,
                "intent_sha256": _digest(intent), "origin": "owner_resolution",
                "outcome": {"kind": "return", "value": value},
                "checkpoint": checkpoint, "checkpoint_sha256": _digest(checkpoint),
                "resolution_evidence": evidence,
            }
            _write_new(root / "calls" / (operation_id + ".return.json"), completion)
            pending[0]["completion"] = completion
            return {
                "status": "resolved",
                "operation": _operation_summary(intent),
                "durable": _stats(manifest, operations),
                "qualification": "owner-supplied observation; no callback was dispatched",
            }
    except (OSError, ValueError, TypeError, KeyError, _StateError) as exc:
        return _failure("state_error", str(exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", allow_abbrev=False, help="inspect retained material state")
    inspect.add_argument("--state-dir", required=True)
    args = parser.parse_args()
    result = inspect_durable_state(args.state_dir)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    return 2 if result["status"] == "state_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
