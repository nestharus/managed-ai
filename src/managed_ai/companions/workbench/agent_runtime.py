"""A bounded, synchronous agent loop for caller-supplied Python callables.

The budget counts attempted model calls, including a call that raises. It does
not bound callback duration, resource consumption, or callback side effects.
Evidence snapshots are implemented by the companion ``evidence_support`` module.
"""

from collections.abc import Mapping
from typing import Any

from .evidence_support import (
    capture_return,
    detached_json,
    interaction_history,
    source_snapshots,
)

__all__ = ["run_agent"]


def _validated_inputs(
    task: Any,
    sources: Any,
    model: Any,
    tools: Any,
    max_steps: Any,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Validate every caller argument before invoking any callback."""
    if not isinstance(task, str):
        raise TypeError("task must be a string")
    if not isinstance(sources, Mapping):
        raise TypeError("sources must map string IDs to string bodies")

    source_bodies = dict(sources.items())
    if any(
        not isinstance(key, str) or not isinstance(body, str) for key, body in source_bodies.items()
    ):
        raise TypeError("sources must map string IDs to string bodies")
    if not callable(model):
        raise TypeError("model must be callable")
    if not isinstance(tools, Mapping):
        raise TypeError("tools must map string names to callables")

    registered_tools = dict(tools.items())
    if any(
        not isinstance(name, str) or not callable(callback)
        for name, callback in registered_tools.items()
    ):
        raise TypeError("tools must map string names to callables")
    if isinstance(max_steps, bool) or not isinstance(max_steps, int):
        raise TypeError("max_steps must be a nonnegative integer")
    if max_steps < 0:
        raise ValueError("max_steps must be a nonnegative integer")

    return source_bodies, registered_tools


def _action_problem(action: Any) -> str | None:
    """Describe an invalid action without invoking a tool or repairing it."""
    if not isinstance(action, dict):
        return "model return must be a JSON object"

    kind = action.get("kind")
    if kind == "final":
        if not isinstance(action.get("answer"), str):
            return "final action requires a string answer"
    elif kind == "tool":
        if not isinstance(action.get("name"), str):
            return "tool action requires a string name"
        if not isinstance(action.get("arguments"), dict):
            return "tool action requires an arguments object"
    else:
        return "action kind must be 'final' or 'tool'"

    return None


def _model_name(model: Any) -> str:
    name = getattr(model, "__name__", None)
    return name if isinstance(name, str) else type(model).__name__


def run_agent(
    *,
    task: str,
    sources: Mapping[str, str],
    model: Any,
    tools: Mapping[str, Any],
    max_steps: int,
) -> dict[str, Any]:
    """Run at most ``max_steps`` model calls and return a detached JSON trace."""
    source_bodies, registered_tools = _validated_inputs(task, sources, model, tools, max_steps)
    source_bodies, source_trace = source_snapshots(source_bodies)
    trace: dict[str, Any] = {
        "task": task,
        "sources": source_trace,
        "max_steps": max_steps,
        "events": [],
    }
    model_calls = 0
    tool_calls = 0

    def record(event: dict[str, Any]) -> None:
        trace["events"].append(event)

    def result(
        status: str, *, answer: str | None = None, error: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {
            "status": status,
            "answer": answer,
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "error": error,
            "trace": trace,
        }

    def callback_failure(step: int, callback: str, name: str, exc: Exception) -> dict[str, Any]:
        details = {
            "step": step,
            "callback": callback,
            "name": name,
            "exception_type": type(exc).__name__,
            "message": str(exc),
        }
        record({"kind": "callback_error", **details})
        status = callback + "_error"
        return result(status, error={"code": status, **details})

    for step in range(1, max_steps + 1):
        request = {
            "task": task,
            "sources": detached_json(source_bodies),
            "step": step,
            "remaining_steps": max_steps - step,
            "history": interaction_history(trace["events"]),
        }
        record({"kind": "model_request", "step": step, "request": detached_json(request)})
        model_calls += 1

        try:
            raw_action = model(request)
        except Exception as exc:
            return callback_failure(step, "model", _model_name(model), exc)

        action, action_is_json = capture_return(raw_action)
        record({"kind": "model_return", "step": step, "value": action})
        problem = (
            _action_problem(action)
            if action_is_json
            else "model return must be a finite JSON-compatible action"
        )
        if problem is not None:
            return result(
                "malformed_model_return",
                error={"code": "malformed_model_return", "step": step, "message": problem},
            )

        if action["kind"] == "final":
            return result("complete", answer=action["answer"])

        tool_name = action["name"]
        if tool_name not in registered_tools:
            return result(
                "unknown_tool",
                error={
                    "code": "unknown_tool",
                    "step": step,
                    "name": tool_name,
                    "message": "requested tool is not registered",
                },
            )

        arguments = detached_json(action["arguments"])
        record(
            {
                "kind": "tool_call",
                "step": step,
                "name": tool_name,
                "arguments": detached_json(arguments),
            }
        )
        tool_calls += 1

        try:
            raw_observation = registered_tools[tool_name](arguments)
        except Exception as exc:
            return callback_failure(step, "tool", tool_name, exc)

        observation, _ = capture_return(raw_observation)
        record({"kind": "tool_return", "step": step, "name": tool_name, "value": observation})

    return result("step_limit")
