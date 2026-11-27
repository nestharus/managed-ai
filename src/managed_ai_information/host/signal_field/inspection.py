"""Signal-field bounded inspection, status summary, and bounded worker execution loop."""

import math
from collections.abc import Callable
from typing import Any

from .config import encoded, loads_strict, require
from .models import job_view, oversized_job_summary, worker_outcome


def pending_jobs(field: Any, after: str, limit: int, max_bytes: int) -> dict[str, Any]:
    require(
        isinstance(after, str)
        and type(limit) is int
        and 1 <= limit <= field.config.max_pending_limit,
        "pending requires a string cursor and limit from 1 through "
        f"{field.config.max_pending_limit}",
    )
    require(
        type(max_bytes) is int
        and field.config.min_max_bytes <= max_bytes <= field.config.max_inspect_bytes,
        f"pending byte limit must be from {field.config.min_max_bytes} through "
        f"{field.config.max_inspect_bytes}",
    )

    state = field._read_state()
    matching = [row for jid, row in sorted(state["jobs"].items()) if jid > after]
    rows = matching[: limit + 1]
    field.rows_returned += len(rows)

    items: list[dict[str, Any]] = []
    # Exact UTF-8 JSON array length: "[]" is 2 bytes, each subsequent element adds 1 byte for ",".
    running_bytes = 2

    for row in rows[:limit]:
        candidate = job_view(row)
        candidate_bytes = len(encoded(candidate).encode("utf-8"))
        added = candidate_bytes + (1 if items else 0)
        if running_bytes + added <= max_bytes:
            items.append(candidate)
            running_bytes += added
            continue
        if items:
            break

        summary = oversized_job_summary(row, candidate_bytes + 2)
        require(
            len(encoded([summary]).encode("utf-8")) <= max_bytes,
            "next pending job exceeds max_bytes and cannot be summarized within bound; request "
            "a larger max_bytes",
        )
        items.append(summary)
        break

    return {
        "items": items,
        "next_after": items[-1]["job"] if items else after,
        "more": len(rows) > len(items),
        "next_omitted": rows[len(items)]["id"] if len(rows) > len(items) else None,
        "scope": (
            "Trusted host recovery metadata, bounded output over enrolled jobs. The token "
            "supplies no external-effect authority."
        ),
    }


def inspect_entity(
    field: Any,
    use_id: str | None,
    job_id: str | None,
    history_name: str | None,
    max_bytes: int,
) -> dict[str, Any]:
    require(
        sum(x is not None for x in (use_id, job_id, history_name)) == 1,
        "inspect exactly one use, pending job or selected historical account",
    )
    for label, value in (
        ("use_id", use_id),
        ("job_id", job_id),
        ("history_name", history_name),
    ):
        if value is not None:
            require(isinstance(value, str) and value.strip(), f"{label} must be nonempty text")
    require(
        type(max_bytes) is int
        and field.config.min_max_bytes <= max_bytes <= field.config.max_inspect_bytes,
        f"inspection byte limit must be from {field.config.min_max_bytes} through "
        f"{field.config.max_inspect_bytes}",
    )

    state = field._read_state()
    if job_id is not None:
        row = state["jobs"].get(job_id)
        require(row is not None, "pending job is absent")
        field.rows_returned += 1
        result = job_view(row)
    elif history_name is not None:
        row = state["selected_history"].get(history_name)
        require(row is not None, "selected historical account is absent")
        field.rows_returned += 1
        result = {"name": history_name, "historical": loads_strict(row["snapshot"])}
    else:
        row = state["uses"].get(use_id)
        require(row is not None, "receiving use is absent")
        field.rows_returned += 1
        spec = loads_strict(row["spec"])
        basis = field._basis_from_map(spec, field._sources_map_from_state(state, spec["sources"]))
        latest = loads_strict(row["latest"]) if row["latest"] else None
        result = {
            "use": use_id,
            "spec": spec,
            "current_declared_basis": basis,
            "latest": latest,
            "definition_matches": latest["signature"] == row["signature"] if latest else None,
            "declared_basis_matches": latest["basis"] == basis if latest else None,
            "last_rejected": loads_strict(row["last_rejected"]) if row["last_rejected"] else None,
        }

    result["scope"] = (
        "Observed bindings and retained accounts only; semantic validity and effect permission "
        "require their actual receivers."
    )
    require(
        len(encoded(result).encode("utf-8")) <= max_bytes,
        "selected inspection exceeds output byte limit; request a larger explicit bound",
    )
    return result


def field_status(field: Any) -> dict[str, Any]:
    state = field._read_state()
    jobs = list(state["jobs"].values())
    total = len(jobs)
    blocked_occurrences = sum(1 for row in jobs if row["gap"] is not None)
    in_flight = sum(1 for row in jobs if row["gap"] is None and row["token"] is not None)
    eligible = sum(1 for row in jobs if row["gap"] is None and row["token"] is None)

    blocked_live = 0
    for use in state["uses"].values():
        if not use["latest"]:
            continue
        if loads_strict(use["spec"]).get("mode") == "each-change":
            continue
        latest_doc = loads_strict(use["latest"])
        if isinstance(latest_doc, dict) and latest_doc.get("result", {}).get("state") == "blocked":
            blocked_live += 1

    return {
        "format": field.config.format_version,
        "eligible": eligible,
        "in_flight": in_flight,
        "blocked_occurrences": blocked_occurrences,
        "blocked_live_uses": blocked_live,
        "uses": len(state["uses"]),
        "selected_historical_observations": len(state["selected_history"]),
        "remaining_jobs": total,
        "sql_calls_this_connection": field.sql_calls,
        "rows_returned_this_connection": field.rows_returned,
        "database_bytes": field.path.stat().st_size,
        "scope": "Known enrolled uses only. Quiet queue is not whole-field semantic success.",
    }


def invoke_one_packet(
    field: Any,
    argv: list[str],
    packet: dict[str, Any],
    payload: bytes,
    timeout: int | float,
    *,
    runner: Callable[..., Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    try:
        worker = runner(
            argv,
            payload,
            timeout,
            max_stdout_bytes=field.config.max_worker_stdout_bytes,
            max_stderr_bytes=field.config.max_worker_stream_bytes,
            max_drain_bytes=field.config.max_worker_drain_bytes,
        )
    except OSError as exc:
        outcome = worker_outcome(
            "launch-error", returncode=None, error=str(exc), errno_value=exc.errno
        )
        return None, outcome, str(exc)

    if worker.timed_out:
        reason = worker.error or f"worker timed out after {timeout} seconds"
        outcome = worker_outcome(
            "timeout",
            returncode=None,
            stdout=worker.stdout,
            stderr=worker.stderr,
            error=reason,
            timeout=timeout,
        )
        return None, outcome, reason

    if worker.returncode != 0:
        reason = "worker exited without a valid return"
        outcome = worker_outcome(
            "non-zero-exit",
            returncode=worker.returncode,
            stdout=worker.stdout,
            stderr=worker.stderr,
            error=reason,
        )
        return None, outcome, reason

    if worker.stdout.limit_exceeded:
        reason = f"worker stdout exceeded {field.config.max_worker_stdout_bytes}-byte return limit"
        outcome = worker_outcome(
            "invalid-return",
            returncode=worker.returncode,
            stdout=worker.stdout,
            stderr=worker.stderr,
            error=reason,
        )
        return None, outcome, reason

    try:
        result = loads_strict(bytes(worker.stdout.kept))
        return field.receive(packet, result), None, None
    except (ValueError, TypeError) as exc:
        outcome = worker_outcome(
            "invalid-return",
            returncode=worker.returncode,
            stdout=worker.stdout,
            stderr=worker.stderr,
            error=str(exc),
        )
        return None, outcome, str(exc)


def run_worker_loop(
    field: Any, argv: list[str], budget: int, timeout: int | float
) -> dict[str, Any]:
    require(
        isinstance(argv, list)
        and argv
        and all(isinstance(x, str) and x and "\0" not in x for x in argv),
        "worker requires explicit subprocess argv",
    )
    require(type(budget) is int and budget > 0, "positive invocation budget required")
    require(
        type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
        "finite positive worker timeout required before any work is claimed",
    )

    field.reconcile()
    invoked = 0
    packet_bytes = 0
    received: list[dict[str, Any]] = []

    for _ in range(budget):
        packet = field.pull()
        if packet is None:
            break
        payload = encoded(packet).encode("utf-8")
        packet_bytes += len(payload)
        invoked += 1

        receipt, failure_outcome, failure_reason = field._invoke_one_packet(
            argv, packet, payload, timeout
        )
        if failure_outcome is not None:
            return field._unresolved_run(
                packet,
                failure_outcome,
                invoked=invoked,
                packet_bytes=packet_bytes,
                received=received,
                reason=failure_reason or "worker failed",
            )
        if receipt is not None:
            received.append(receipt)

    state = field.status()
    if state["eligible"]:
        overall = "budget-exhausted"
    elif state["in_flight"]:
        overall = "awaiting-in-flight"
    elif state["blocked_occurrences"] or state["blocked_live_uses"]:
        overall = "blocked"
    else:
        overall = "current-enrolled-service-caught-up"

    return {
        "status": overall,
        "invocations": invoked,
        "packet_bytes": packet_bytes,
        "returns": received,
        "remaining": state,
        "scope": (
            "Actual configured worker calls. No provider/model or all-dimension semantic "
            "measurement is inferred."
        ),
    }
