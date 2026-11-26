"""Allowlisted JSON operation dispatcher and CLI entrypoint for signal_field."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .config import loads_strict, require
from .operations import Field
from .storage import require_ordinary_non_symlink


def _check_keys(
    op: str,
    params: dict[str, Any],
    *,
    required: set[str] | frozenset[str] = frozenset(),
    optional: set[str] | frozenset[str] = frozenset(),
) -> None:
    missing = set(required) - set(params)
    require(not missing, f"{op} missing required parameters: " + ", ".join(sorted(missing)))
    extra = set(params) - set(required) - set(optional)
    require(not extra, f"{op} received unsupported parameters: " + ", ".join(sorted(extra)))


def _op_enroll(field: Field, params: dict[str, Any]) -> dict[str, Any]:
    _check_keys("enroll", params, required={"spec"})
    return field.enroll(params["spec"])


def _op_change(field: Field, params: dict[str, Any]) -> dict[str, Any]:
    _check_keys(
        "change",
        params,
        required={"source", "epoch", "position", "value"},
        optional={"notify", "reset"},
    )
    return field.change(
        params["source"],
        params["epoch"],
        params["position"],
        params["value"],
        notify=params.get("notify", True),
        reset=params.get("reset", False),
    )


def _op_reconcile(field: Field, params: dict[str, Any]) -> dict[str, Any]:
    _check_keys("reconcile", params)
    return field.reconcile()


def _op_pull(field: Field, params: dict[str, Any]) -> dict[str, Any] | None:
    _check_keys("pull", params)
    return field.pull()


def _op_receive(field: Field, params: dict[str, Any]) -> dict[str, Any]:
    _check_keys("receive", params, required={"packet", "result"})
    return field.receive(params["packet"], params["result"])


def _op_retry(field: Field, params: dict[str, Any]) -> dict[str, Any]:
    _check_keys("retry", params, required={"job_id", "token"})
    return field.retry(params["job_id"], params["token"])


def _op_retain(field: Field, params: dict[str, Any]) -> dict[str, Any]:
    _check_keys("retain", params, required={"use_id", "name"})
    return field.retain(params["use_id"], params["name"])


def _op_pending(field: Field, params: dict[str, Any]) -> dict[str, Any]:
    _check_keys("pending", params, optional={"after", "limit", "max_bytes"})
    return field.pending(
        after=params.get("after", ""),
        limit=params.get("limit", 20),
        max_bytes=params.get("max_bytes", 65536),
    )


def _op_inspect(field: Field, params: dict[str, Any]) -> dict[str, Any]:
    _check_keys("inspect", params, optional={"use_id", "job_id", "history_name", "max_bytes"})
    return field.inspect(
        use_id=params.get("use_id"),
        job_id=params.get("job_id"),
        history_name=params.get("history_name"),
        max_bytes=params.get("max_bytes", 1048576),
    )


def _op_status(field: Field, params: dict[str, Any]) -> dict[str, Any]:
    _check_keys("status", params)
    return field.status()


def _op_run(field: Field, params: dict[str, Any]) -> dict[str, Any]:
    _check_keys("run", params, required={"argv", "budget", "timeout"})
    return field.run(params["argv"], params["budget"], params["timeout"])


OPERATIONS = {
    "enroll": _op_enroll,
    "change": _op_change,
    "reconcile": _op_reconcile,
    "pull": _op_pull,
    "receive": _op_receive,
    "retry": _op_retry,
    "retain": _op_retain,
    "pending": _op_pending,
    "inspect": _op_inspect,
    "status": _op_status,
    "run": _op_run,
}


def dispatch_operation(field: Field, request: Any) -> Any:
    """Validate and dispatch one allowlisted signal-field JSON operation."""
    require(isinstance(request, dict), "request must be a JSON object")
    op = request.get("op")
    require(isinstance(op, str) and op in OPERATIONS, "unknown operation")
    params = {k: v for k, v in request.items() if k != "op"}
    return OPERATIONS[op](field, params)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for signal-field queue operations."""
    parser = argparse.ArgumentParser(
        description="Optional host queue for supplied observing uses.",
        allow_abbrev=False,
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument(
        "--request", type=Path, help="JSON operation; omit to read one operation from stdin"
    )
    args = parser.parse_args(argv)

    try:
        if args.request is not None:
            req_path = require_ordinary_non_symlink(args.request, "request file", must_exist=True)
            raw_request = req_path.read_bytes()
        else:
            raw_request = sys.stdin.buffer.read()

        request = loads_strict(raw_request)
        with Field(args.db) as field:
            result = dispatch_operation(field, request)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(
            json.dumps({"status": "unresolved", "reason": str(exc)}, allow_nan=False),
            file=sys.stderr,
        )
        return 2
