"""Material continuation around the unchanged bounded callable controller.

One cooperative local owner. Historical callback returns are replayed; effects
are never blindly retried. A pending intent remains an unresolved outcome.
"""

import argparse
import json
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

if __name__ == "__main__" and not __package__:
    _src_root = str(Path(__file__).resolve().parents[3])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)
    __package__ = "managed_ai_information.companions.workbench"
    __import__(__package__)

from .config import load_controller as _load_controller
from .io_utils import (
    BasisMismatchError,
    OwnerLockError,
    atomic_json,
    exclusive_lock,
    sha256_bytes,
)
from .io_utils import (
    canonical_bytes as _canonical,
)
from .io_utils import (
    canonical_digest as _digest,
)
from .io_utils import (
    detached_json as _snapshot,
)
from .io_utils import (
    read_json_strict as _read,
)

__all__ = [
    "BasisMismatchError",
    "inspect_durable_state",
    "resolve_durable_operation",
    "run_durable_agent",
]

SCHEMA = "g60-durable-agent/1"


class _StateError(Exception):
    pass


class _ReplayMismatch(BaseException):
    pass


class _InfrastructureStop(BaseException):
    pass


def _file_digest(path: Path | str) -> str:
    return sha256_bytes(Path(path).read_bytes())


def _write_new(path: Path, value: Any) -> None:
    """Publish one immutable complete record; never overwrite prior evidence."""
    atomic_json(path, value, exclusive=True)


@contextmanager
def _lock(root: Path | str) -> Generator[None]:
    try:
        with exclusive_lock(Path(root) / ".owner.lock"):
            yield
    except OwnerLockError as exc:
        raise _StateError(str(exc)) from exc


def _counts(
    operations: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
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


def _load_state(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _read(root / "manifest.json")
    if manifest.get("schema") != SCHEMA:
        raise _StateError("unsupported journal manifest")
    if _digest(manifest["contract"]) != manifest["contract_sha256"]:
        raise _StateError("manifest contract identity mismatch")
    if _digest(manifest["initial_checkpoint"]) != manifest["initial_checkpoint_sha256"]:
        raise _StateError("initial checkpoint identity mismatch")

    operations: list[dict[str, Any]] = []
    pending_seen = False
    files = sorted((root / "calls").glob("*.intent.json"))

    for index, path in enumerate(files, 1):
        identifier = f"{index:06d}"
        if path.name != identifier + ".intent.json":
            raise _StateError("noncontiguous operation identities")

        intent = _read(path)
        if (
            intent.get("schema") != SCHEMA
            or intent.get("id") != identifier
            or intent.get("callback") not in ("model", "tool")
            or _digest(intent["request"]) != intent["request_sha256"]
        ):
            raise _StateError("invalid callback intent: " + identifier)
        if pending_seen:
            raise _StateError("operation exists after unresolved intent")

        return_path = root / "calls" / (identifier + ".return.json")
        completion = None
        if return_path.exists():
            completion = _read(return_path)
            if (
                completion.get("schema") != SCHEMA
                or completion.get("operation_id") != identifier
                or completion.get("intent_sha256") != _digest(intent)
                or completion.get("outcome", {}).get("kind") not in ("return", "exception")
                or _digest(completion["checkpoint"]) != completion["checkpoint_sha256"]
            ):
                raise _StateError("invalid callback completion: " + identifier)
        else:
            pending_seen = True

        operations.append({"intent": intent, "completion": completion})

    expected_returns = {
        op["intent"]["id"] + ".return.json" for op in operations if op["completion"] is not None
    }
    if {p.name for p in (root / "calls").glob("*.return.json")} != expected_returns:
        raise _StateError("orphan callback completion")

    return manifest, operations


def _stats(
    manifest: dict[str, Any],
    operations: list[dict[str, Any]],
    *,
    actual: dict[str, int] | None = None,
    replayed: dict[str, int] | None = None,
) -> dict[str, Any]:
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
        "dispatch_evidence": (
            "completed outcomes establish observations; pending intents do not prove invocation"
        ),
    }


def _operation_summary(intent: dict[str, Any]) -> dict[str, Any]:
    return {key: intent[key] for key in ("id", "callback", "name", "logical_step")}


def _failure(
    status: str,
    message: str,
    *,
    manifest: dict[str, Any] | None = None,
    operations: list[dict[str, Any]] | None = None,
    operation: dict[str, Any] | None = None,
    actual: dict[str, int] | None = None,
    replayed: dict[str, int] | None = None,
    details: Any = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": error_code or status, "message": message}
    if operation is not None:
        error["operation"] = _operation_summary(operation)
    if details is not None:
        error["details"] = details

    result: dict[str, Any] = {
        "status": status,
        "answer": None,
        "error": error,
        "model_calls": 0,
        "tool_calls": 0,
        "trace": None,
    }
    if manifest is not None:
        result["durable"] = _stats(manifest, operations or [], actual=actual, replayed=replayed)
    return result


def _basis_mismatch_failure(
    exc: BasisMismatchError,
    *,
    manifest: dict[str, Any] | None,
    operations: list[dict[str, Any]],
    actual: dict[str, int],
    replayed: dict[str, int],
) -> dict[str, Any]:
    result = _failure(
        "binding_mismatch",
        str(exc),
        manifest=manifest,
        operations=operations,
        actual=actual,
        replayed=replayed,
        details=exc.details,
        error_code=exc.code,
    )
    if isinstance(exc.details, dict) and "changed_paths" in exc.details:
        result["error"]["changed_paths"] = exc.details["changed_paths"]
    return result


def inspect_durable_state(state_dir: Path | str) -> dict[str, Any]:
    """Read the last published material state without callback invocation."""
    root = Path(state_dir).resolve()
    try:
        manifest, operations = _load_state(root)
        pending = [
            _operation_summary(op["intent"]) for op in operations if op["completion"] is None
        ]
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
                    "completion_origin": op["completion"].get("origin")
                    if op["completion"]
                    else None,
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
    def __init__(
        self,
        root: Path,
        operations: list[dict[str, Any]],
        actual: dict[str, int],
        replayed: dict[str, int],
        max_steps: int,
        checkpoint: Any,
        after_record: Any,
    ) -> None:
        self.calls = root / "calls"
        self.operations = operations
        self.actual = actual
        self.replayed = replayed
        self.max_steps = max_steps
        self.checkpoint = checkpoint
        self.after_record = after_record
        self.cursor = 0

    def invoke(
        self,
        callback_kind: str,
        name: str,
        current_step: int,
        request: Any,
        callback: Any,
    ) -> Any:
        snapshot = _snapshot(request)
        identifier = f"{self.cursor + 1:06d}"
        desired = {
            "schema": SCHEMA,
            "id": identifier,
            "callback": callback_kind,
            "name": name,
            "logical_step": current_step,
            "request": snapshot,
            "request_sha256": _digest(snapshot),
        }
        if self.cursor < len(self.operations):
            return self._replay_completed(desired)
        return self._record_new_call(desired, callback)

    def _replay_completed(self, desired: dict[str, Any]) -> Any:
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

    def _record_new_call(self, desired: dict[str, Any], callback: Any) -> Any:
        identifier = desired["id"]
        callback_kind = desired["callback"]
        if callback_kind == "model" and _counts(self.operations)[0]["model"] >= self.max_steps:
            raise _ReplayMismatch("original model budget exhausted before dispatch")

        try:
            _write_new(self.calls / (identifier + ".intent.json"), desired)
        except Exception as exc:
            raise _InfrastructureStop("intent persistence failed: " + str(exc)) from exc

        operation: dict[str, Any] = {"intent": desired, "completion": None}
        self.operations.append(operation)
        self.cursor += 1
        self.actual[callback_kind] += 1

        caught: Exception | None = None
        value: Any = None
        try:
            value = callback(_snapshot(desired["request"]))
        except Exception as exc:
            caught = exc
            outcome = {
                "kind": "exception",
                "exception_type": type(exc).__name__,
                "message": str(exc),
            }
        else:
            try:
                value = _snapshot(value)
            except Exception as exc:
                caught = TypeError("durable callback return must be finite JSON: " + str(exc))
                outcome = {
                    "kind": "exception",
                    "exception_type": type(caught).__name__,
                    "message": str(caught),
                }
            else:
                outcome = {"kind": "return", "value": value}

        try:
            post_state = _snapshot(self.checkpoint())
            completion = {
                "schema": SCHEMA,
                "operation_id": identifier,
                "intent_sha256": _digest(desired),
                "origin": "callback",
                "outcome": outcome,
                "checkpoint": post_state,
                "checkpoint_sha256": _digest(post_state),
            }
            completion_path = self.calls / (identifier + ".return.json")
            _write_new(completion_path, completion)
            operation["completion"] = completion
        except Exception as exc:
            raise _InfrastructureStop("completion persistence failed: " + str(exc)) from exc

        if self.after_record is not None:
            event = {
                "event": "callback_completed",
                "operation_id": identifier,
                "callback": callback_kind,
                "name": desired["name"],
                "logical_step": desired["logical_step"],
                "completion_path": str(completion_path),
                "resolved": False,
            }
            try:
                self.after_record(_snapshot(event))
            except Exception as exc:
                raise _InfrastructureStop("completion observer stopped: " + str(exc)) from exc

        if caught is not None:
            raise caught
        return _snapshot(value)


def _validate_run_inputs(
    task: Any,
    sources: Any,
    model: Any,
    tools: Any,
    max_steps: Any,
    resume: Any,
    checkpoint: Any,
    restore: Any,
    after_record: Any,
) -> tuple[Any, Any]:
    if not isinstance(task, str):
        raise TypeError("task must be a string")
    if not isinstance(sources, Mapping) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in sources.items()
    ):
        raise TypeError("sources must map string IDs to string bodies")
    if not callable(model):
        raise TypeError("model must be callable")
    if not isinstance(tools, Mapping) or any(
        not isinstance(k, str) or not callable(v) for k, v in tools.items()
    ):
        raise TypeError("tools must map string names to callables")
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 0:
        raise ValueError("max_steps must be a nonnegative integer")
    if not isinstance(resume, bool):
        raise TypeError("resume must be boolean")

    for name, value in (
        ("checkpoint", checkpoint),
        ("restore", restore),
        ("after_record", after_record),
    ):
        if value is not None and not callable(value):
            raise TypeError(name + " must be callable")

    return checkpoint or (lambda: None), restore or (lambda value: None)


def _open_or_create_manifest(
    root: Path,
    contract: dict[str, Any],
    contract_sha256: str,
    *,
    resume: bool,
    checkpoint: Any,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None]:
    if resume:
        manifest, operations = _load_state(root)
        if manifest["contract_sha256"] != contract_sha256:
            changed = [
                key
                for key in contract
                if _canonical(contract[key]) != _canonical(manifest["contract"].get(key))
            ]
            failure = _failure(
                "binding_mismatch",
                "current inputs do not match this occurrence",
                manifest=manifest,
                operations=operations,
                details={
                    "changed_fields": changed,
                    "expected_contract_sha256": manifest["contract_sha256"],
                    "observed_contract_sha256": contract_sha256,
                },
            )
            return manifest, operations, failure
        return manifest, operations, None

    if (root / "manifest.json").exists():
        return None, [], _failure("state_error", "occurrence exists; use explicit resume")
    if any((root / "calls").glob("*.json")):
        return None, [], _failure("state_error", "orphan call records prevent a new occurrence")

    initial = _snapshot(checkpoint())
    manifest = {
        "schema": SCHEMA,
        "contract": contract,
        "contract_sha256": contract_sha256,
        "initial_checkpoint": initial,
        "initial_checkpoint_sha256": _digest(initial),
    }
    _write_new(root / "manifest.json", manifest)
    return manifest, [], None


def _execute_replayed_controller(
    root: Path,
    run_agent: Any,
    manifest: dict[str, Any],
    operations: list[dict[str, Any]],
    *,
    task: str,
    sources: Mapping[str, str],
    model: Any,
    tools: Mapping[str, Any],
    max_steps: int,
    checkpoint: Any,
    after_record: Any,
    actual: dict[str, int],
    replayed: dict[str, int],
) -> dict[str, Any]:
    journal = _CallbackJournal(
        root, operations, actual, replayed, max_steps, checkpoint, after_record
    )
    current_step = 0

    def durable_model(request: dict[str, Any]) -> Any:
        nonlocal current_step
        current_step = request["step"]
        return journal.invoke("model", "model", current_step, request, model)

    durable_model.__name__ = getattr(model, "__name__", type(model).__name__)
    wrapped_tools = {
        name: (
            lambda arguments, n=name, callback=callback: journal.invoke(
                "tool",
                n,
                current_step,
                arguments,
                callback,
            )
        )
        for name, callback in tools.items()
    }

    try:
        result = run_agent(
            task=task,
            sources=dict(sources),
            model=durable_model,
            tools=wrapped_tools,
            max_steps=max_steps,
        )
        if journal.cursor != len(operations):
            raise _ReplayMismatch("controller ended before recorded operations were consumed")
    except _ReplayMismatch as exc:
        return _failure(
            "binding_mismatch",
            str(exc),
            manifest=manifest,
            operations=operations,
            actual=actual,
            replayed=replayed,
        )
    except _InfrastructureStop as exc:
        unresolved = next((op for op in operations if op["completion"] is None), None)
        return _failure(
            "unresolved" if unresolved else "state_error",
            str(exc),
            manifest=manifest,
            operations=operations,
            operation=unresolved["intent"] if unresolved else None,
            actual=actual,
            replayed=replayed,
        )

    result_path = root / "controller-result.json"
    if result_path.exists():
        if _canonical(_read(result_path)) != _canonical(result):
            return _failure(
                "binding_mismatch",
                "reconstructed terminal result changed",
                manifest=manifest,
                operations=operations,
                actual=actual,
                replayed=replayed,
            )
    else:
        _write_new(result_path, result)

    result["durable"] = _stats(manifest, operations, actual=actual, replayed=replayed)
    return result


def run_durable_agent(
    *,
    state_dir: Path | str,
    binding: Any,
    task: str,
    sources: Mapping[str, str],
    model: Any,
    tools: Mapping[str, Any],
    max_steps: int,
    resume: bool = False,
    checkpoint: Any = None,
    restore: Any = None,
    after_record: Any = None,
    controller_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Start or explicitly resume one bound occurrence using material replay."""
    root = Path(state_dir).resolve()
    actual = {"model": 0, "tool": 0}
    replayed = {"model": 0, "tool": 0}
    manifest: dict[str, Any] | None = None
    operations: list[dict[str, Any]] = []

    try:
        checkpoint_fn, restore_fn = _validate_run_inputs(
            task,
            sources,
            model,
            tools,
            max_steps,
            resume,
            checkpoint,
            restore,
            after_record,
        )
        run_agent, code = _load_controller(controller_dir)
        code["durable_agent.py"] = _file_digest(__file__)
        contract = {
            "task": task,
            "sources": dict(sources),
            "tool_names": sorted(tools),
            "max_steps": max_steps,
            "binding": _snapshot(binding),
            "code": code,
        }
        contract_sha256 = _digest(contract)

        if resume and not (root / "manifest.json").is_file():
            return _failure("state_error", "resume requires an existing manifest")

        root.mkdir(parents=True, exist_ok=True)
        with _lock(root):
            manifest, operations, error_result = _open_or_create_manifest(
                root,
                contract,
                contract_sha256,
                resume=resume,
                checkpoint=checkpoint_fn,
            )
            if error_result is not None:
                return error_result
            if manifest is None:
                return _failure("state_error", "manifest unavailable")

            pending = [op for op in operations if op["completion"] is None]
            if pending:
                return _failure(
                    "unresolved",
                    "operation outcome is not durably known; no redispatch",
                    manifest=manifest,
                    operations=operations,
                    operation=pending[0]["intent"],
                )

            latest = (
                operations[-1]["completion"]["checkpoint"]
                if operations
                else manifest["initial_checkpoint"]
            )
            restore_fn(_snapshot(latest))
            return _execute_replayed_controller(
                root,
                run_agent,
                manifest,
                operations,
                task=task,
                sources=sources,
                model=model,
                tools=tools,
                max_steps=max_steps,
                checkpoint=checkpoint_fn,
                after_record=after_record,
                actual=actual,
                replayed=replayed,
            )
    except BasisMismatchError as exc:
        return _basis_mismatch_failure(
            exc,
            manifest=manifest,
            operations=operations,
            actual=actual,
            replayed=replayed,
        )
    except (OSError, ValueError, TypeError, KeyError, _StateError) as exc:
        return _failure(
            "state_error",
            str(exc),
            manifest=manifest,
            operations=operations,
            actual=actual,
            replayed=replayed,
        )


def resolve_durable_operation(
    *,
    state_dir: Path | str,
    operation_id: str,
    value: Any,
    checkpoint: Any,
    evidence: str,
    expected_contract_sha256: str,
) -> dict[str, Any]:
    """Record an owner's observed outcome without dispatching or retrying the effect."""
    root = Path(state_dir).resolve()
    try:
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("nonempty owner evidence account is required")

        detached_value = _snapshot(value)
        detached_checkpoint = _snapshot(checkpoint)
        with _lock(root):
            manifest, operations = _load_state(root)
            if expected_contract_sha256 != manifest["contract_sha256"]:
                return _failure(
                    "binding_mismatch",
                    "resolution contract identity differs",
                    manifest=manifest,
                    operations=operations,
                )

            pending = [op for op in operations if op["completion"] is None]
            if len(pending) != 1 or pending[0]["intent"]["id"] != operation_id:
                return _failure(
                    "state_error",
                    "resolution must identify the single pending operation",
                    manifest=manifest,
                    operations=operations,
                )

            intent = pending[0]["intent"]
            completion = {
                "schema": SCHEMA,
                "operation_id": operation_id,
                "intent_sha256": _digest(intent),
                "origin": "owner_resolution",
                "outcome": {"kind": "return", "value": detached_value},
                "checkpoint": detached_checkpoint,
                "checkpoint_sha256": _digest(detached_checkpoint),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", allow_abbrev=False, help="inspect retained material state")
    inspect.add_argument("--state-dir", required=True)

    args = parser.parse_args(argv)
    result = inspect_durable_state(args.state_dir)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    return 2 if result["status"] == "state_error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
