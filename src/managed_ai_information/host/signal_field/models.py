"""Structured worker capture models and job projection views for signal_field."""

import hashlib
from typing import Any

from .config import MAX_WORKER_STREAM_BYTES, loads_strict


class StreamCapture:
    """Memory-bounded pipe capture preserving exact streamed byte count and SHA-256."""

    __slots__ = ("keep_limit", "kept", "limit_exceeded", "overflowed", "sha256", "total_bytes")

    def __init__(self, keep_limit: int) -> None:
        self.kept = bytearray()
        self.total_bytes = 0
        self.sha256 = hashlib.sha256().hexdigest()
        self.keep_limit = keep_limit
        self.limit_exceeded = False
        self.overflowed = False


class WorkerRunResult:
    """Outcome of one bounded worker subprocess invocation."""

    __slots__ = ("error", "returncode", "stderr", "stdout", "timed_out")

    def __init__(
        self,
        *,
        returncode: int | None,
        stdout: StreamCapture,
        stderr: StreamCapture,
        timed_out: bool = False,
        error: str | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.error = error


def stream_details(data: Any, limit: int = MAX_WORKER_STREAM_BYTES) -> tuple[str, int, str, bool]:
    """Return bounded UTF-8 text, total byte count, SHA-256, and truncation flag."""
    if isinstance(data, StreamCapture):
        truncated = data.total_bytes > limit
        text = bytes(data.kept[:limit]).decode("utf-8", errors="replace")
        return text, data.total_bytes, data.sha256, truncated

    if data is None:
        raw = b""
    elif isinstance(data, str):
        raw = data.encode("utf-8", errors="replace")
    else:
        raw = bytes(data)

    truncated = len(raw) > limit
    text = raw[:limit].decode("utf-8", errors="replace")
    return text, len(raw), hashlib.sha256(raw).hexdigest(), truncated


def worker_outcome(
    kind: str,
    *,
    returncode: int | None,
    stdout: Any = b"",
    stderr: Any = b"",
    error: str | None = None,
    timeout: int | float | None = None,
    errno_value: int | None = None,
) -> dict[str, Any]:
    """Construct a structured, bounded diagnostic record for worker failure modes."""
    out_text, out_bytes, out_sha, out_trunc = stream_details(stdout)
    err_text, err_bytes, err_sha, err_trunc = stream_details(stderr)

    outcome: dict[str, Any] = {
        "kind": kind,
        "returncode": returncode,
        "stdout": out_text,
        "stdout_bytes": out_bytes,
        "stdout_sha256": out_sha,
        "stdout_truncated": out_trunc,
        "stderr": err_text,
        "stderr_bytes": err_bytes,
        "stderr_sha256": err_sha,
        "stderr_truncated": err_trunc,
    }

    if error is not None:
        outcome["error"] = error
    if timeout is not None:
        outcome["timeout"] = timeout
    if errno_value is not None:
        outcome["errno"] = errno_value

    return outcome


def job_view(row: Any) -> dict[str, Any]:
    """Project a jobs record into its structured inspection representation."""
    columns = tuple(row.keys())
    outcome = (
        row["worker_outcome"] if "worker_outcome" in columns and row["worker_outcome"] else None
    )
    return {
        "job": row["id"],
        "use": row["use_id"],
        "desired_generation": row["generation"],
        "desired_basis": loads_strict(row["basis"]),
        "desired_spec": loads_strict(row["spec"]),
        "eligible_ticket": row["ticket"],
        "token": row["token"],
        "captured_attempt": loads_strict(row["attempt"]) if row["attempt"] else None,
        "blocked_recovery": loads_strict(row["gap"]) if row["gap"] else None,
        "worker_outcome": loads_strict(outcome) if outcome else None,
    }


def oversized_job_summary(row: Any, full_bytes: int) -> dict[str, Any]:
    """Return a compact placeholder preserving monotone pagination for oversized jobs."""
    columns = tuple(row.keys())
    has_outcome = bool(row["worker_outcome"]) if "worker_outcome" in columns else False
    return {
        "job": row["id"],
        "use": row["use_id"],
        "desired_generation": row["generation"],
        "eligible_ticket": row["ticket"],
        "token": row["token"],
        "oversized": True,
        "full_bytes": full_bytes,
        "has_captured_attempt": row["attempt"] is not None,
        "has_blocked_recovery": row["gap"] is not None,
        "has_worker_outcome": has_outcome,
        "detail": (
            "Job exceeds pending max_bytes; call inspect(job_id=...) with a larger max_bytes for "
            "full details."
        ),
    }
