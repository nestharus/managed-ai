"""Configuration, service contract, and canonical strict JSON helpers for signal_field."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from managed_ai.platform import strict_json_loads


@dataclass(frozen=True)
class SignalFieldConfig:
    """Explicit bounded configuration for a local signal-field queue instance."""

    format_version: str = "g60-signal-field/1"
    modes: frozenset[str] = frozenset({"live", "historical", "each-change"})
    max_worker_stream_bytes: int = 16 * 1024
    max_worker_stdout_bytes: int = 4 * 1024 * 1024
    max_worker_drain_bytes: int = 64 * 1024 * 1024
    default_pending_limit: int = 20
    max_pending_limit: int = 100
    min_max_bytes: int = 1024
    default_pending_max_bytes: int = 65536
    default_inspect_max_bytes: int = 1048576
    max_inspect_bytes: int = 4 * 1024 * 1024


DEFAULT_CONFIG = SignalFieldConfig()

FORMAT = DEFAULT_CONFIG.format_version
MODES = set(DEFAULT_CONFIG.modes)
MAX_WORKER_STREAM_BYTES = DEFAULT_CONFIG.max_worker_stream_bytes
MAX_WORKER_STDOUT_BYTES = DEFAULT_CONFIG.max_worker_stdout_bytes
MAX_WORKER_DRAIN_BYTES = DEFAULT_CONFIG.max_worker_drain_bytes


@runtime_checkable
class SignalFieldService(Protocol):
    """Local SQLite-backed observing-use eligibility and scheduling contract."""

    def enroll(self, spec: dict[str, Any]) -> dict[str, Any]: ...

    def change(
        self,
        source: str,
        epoch: str,
        position: int,
        value: Any,
        *,
        notify: bool = True,
        reset: bool = False,
    ) -> dict[str, Any]: ...

    def reconcile(self) -> dict[str, Any]: ...

    def pull(self) -> dict[str, Any] | None: ...

    def receive(self, packet: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]: ...

    def retry(self, job_id: str, token: str) -> dict[str, Any]: ...

    def retain(self, use_id: str, name: str) -> dict[str, Any]: ...

    def pending(
        self, after: str = "", limit: int = 20, max_bytes: int = 65536
    ) -> dict[str, Any]: ...

    def inspect(
        self,
        use_id: str | None = None,
        job_id: str | None = None,
        history_name: str | None = None,
        max_bytes: int = 1048576,
    ) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...

    def run(self, argv: list[str], budget: int, timeout: int | float) -> dict[str, Any]: ...

    def close(self) -> None: ...


def encoded(value: Any) -> str:
    """Serialize finite JSON deterministically without whitespace."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: Any) -> str:
    """Return the SHA-256 hex digest of a canonical JSON encoding."""
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def require(ok: Any, message: str) -> None:
    """Raise ValueError with message when condition fails."""
    if not ok:
        raise ValueError(message)


def loads_strict(raw: bytes | bytearray | str) -> Any:
    """Parse strict UTF-8 JSON rejecting duplicate keys and nonfinite numbers."""
    payload: bytes | str = bytes(raw) if isinstance(raw, bytearray) else raw
    return strict_json_loads(payload, label="signal_field", require_object=False)
