"""A/B multi-route storage writer and Expand->Dual-Write->Verify->Contract schema governance."""

import queue
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from managed_ai_information.core.storage import now, sha
from managed_ai_information.core.storage_backend import (
    BackendRecord,
    LeaseGrant,
    SearchHit,
    SearchVisibility,
    SignalEvent,
    StorageBackend,
)

RoutingMode = Literal["single", "shadow-async", "dual-sync", "canary-cutover"]
MigrationPhase = Literal["idle", "expanding", "dual-write", "verified", "committed", "aborted"]

_VALID_MODES: frozenset[str] = frozenset({"single", "shadow-async", "dual-sync", "canary-cutover"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class ParityEvent:
    """Structured telemetry record comparing Route A vs Route B for one operation."""

    op: str
    key: str
    a_sha: str | None
    b_sha: str | None
    a_ms: float
    b_ms: float | None
    b_error: str | None
    match: bool
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "key": self.key,
            "a_sha": self.a_sha,
            "b_sha": self.b_sha,
            "a_ms": round(self.a_ms, 3),
            "b_ms": None if self.b_ms is None else round(self.b_ms, 3),
            "b_error": self.b_error,
            "match": self.match,
            "recorded_at": self.recorded_at,
        }


def _digest_result(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, BackendRecord):
        return sha(value.value)
    if isinstance(value, bytes):
        return sha(value)
    if isinstance(value, str):
        return value if _SHA256_RE.match(value) else sha(value.encode("utf-8"))
    if isinstance(value, bool):
        return sha(b"true" if value else b"false")
    return sha(repr(value).encode("utf-8"))


class MultiRouteBackend:
    """A/B routing facade with fault-isolated shadow-async, dual-sync, and canary-cutover modes."""

    def __init__(
        self,
        route_a: StorageBackend,
        route_b: StorageBackend | None = None,
        *,
        mode: RoutingMode = "single",
        route_b_timeout_ms: int = 500,
        max_queue_size: int = 256,
        max_shadow_workers: int = 4,
        max_parity_events: int = 4096,
        fail_on_dual_mismatch: bool = True,
    ) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(f"unsupported routing mode: {mode!r}")
        if mode != "single" and route_b is None:
            raise ValueError(f"routing mode {mode!r} requires route_b")
        if (
            route_b_timeout_ms <= 0
            or max_queue_size <= 0
            or max_shadow_workers <= 0
            or max_parity_events <= 0
        ):
            raise ValueError("timeout, queue, worker, and parity capacities must be positive")
        self.route_a = route_a
        self.route_b = route_b
        self._mode: RoutingMode = mode
        self.route_b_timeout_ms = route_b_timeout_ms
        self._max_shadow_workers = max_shadow_workers
        self.fail_on_dual_mismatch = fail_on_dual_mismatch
        self._lock = threading.RLock()
        self._events: deque[ParityEvent] = deque(maxlen=max_parity_events)
        self._counts: dict[str, int] = {
            "total_ops": 0,
            "matched_ops": 0,
            "mismatched_ops": 0,
            "route_b_errors": 0,
            "route_b_timeouts": 0,
            "dropped_shadow_ops": 0,
            "canary_ops": 0,
        }
        self._queue: queue.Queue[tuple[str, str, str | None, float, Callable[[], Any]]] = (
            queue.Queue(maxsize=max_queue_size)
        )
        self._stop_event = threading.Event()
        self._closed = False
        self._worker: threading.Thread | None = None
        self._shadow_pool: Any = None
        self._shadow_slots = threading.BoundedSemaphore(max_shadow_workers)

    def __enter__(self) -> MultiRouteBackend:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def mode(self) -> RoutingMode:
        with self._lock:
            return self._mode

    def set_mode(self, mode: RoutingMode) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(f"unsupported routing mode: {mode!r}")
        with self._lock:
            if self._closed:
                raise RuntimeError("MultiRouteBackend is closed")
            if mode != "single" and self.route_b is None:
                raise ValueError(f"routing mode {mode!r} requires route_b")
            self._mode = mode

    def _ensure_shadow_started(self) -> None:
        with self._lock:
            if self._worker is not None:
                return
            from concurrent.futures import ThreadPoolExecutor

            self._shadow_pool = ThreadPoolExecutor(
                max_workers=self._max_shadow_workers, thread_name_prefix="multi-route-b"
            )
            self._worker = threading.Thread(
                target=self._shadow_loop, name="multi-route-shadow", daemon=True
            )
            self._worker.start()

    def _record_event(self, event: ParityEvent) -> None:
        with self._lock:
            self._events.append(event)
            self._counts["total_ops"] += 1
            if event.match:
                self._counts["matched_ops"] += 1
            else:
                self._counts["mismatched_ops"] += 1
            if event.b_error == "timeout":
                self._counts["route_b_timeouts"] += 1
            elif event.b_error in ("shadow-queue-full", "shadow-saturated"):
                self._counts["dropped_shadow_ops"] += 1
            elif event.b_error is not None:
                self._counts["route_b_errors"] += 1

    def _run_shadow_bounded(self, call_b: Callable[[], Any]) -> tuple[Any, float, str | None]:
        if not self._shadow_slots.acquire(blocking=False):
            return None, 0.0, "shadow-saturated"
        pool = self._shadow_pool
        if pool is None:
            self._shadow_slots.release()
            return None, 0.0, "shadow-closed"
        t0 = time.perf_counter()

        def wrapped() -> Any:
            try:
                return call_b()
            finally:
                self._shadow_slots.release()

        fut = pool.submit(wrapped)
        try:
            val = fut.result(timeout=self.route_b_timeout_ms / 1000.0)
            return val, (time.perf_counter() - t0) * 1000.0, None
        except TimeoutError:
            return None, (time.perf_counter() - t0) * 1000.0, "timeout"
        except Exception as exc:
            return None, (time.perf_counter() - t0) * 1000.0, f"{type(exc).__name__}: {exc}"

    def _shadow_loop(self) -> None:
        while True:
            try:
                op, key, a_sha, a_ms, call_b = self._queue.get(timeout=0.05)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            try:
                b_val, b_ms, b_err = self._run_shadow_bounded(call_b)
                b_sha = None if b_err is not None else _digest_result(b_val)
                self._record_event(
                    ParityEvent(
                        op=op,
                        key=key,
                        a_sha=a_sha,
                        b_sha=b_sha,
                        a_ms=a_ms,
                        b_ms=b_ms,
                        b_error=b_err,
                        match=(b_err is None and a_sha == b_sha),
                        recorded_at=now(),
                    )
                )
            finally:
                self._queue.task_done()

    def flush_shadow(self, timeout: float = 5.0) -> bool:
        """Wait up to timeout seconds for queued shadow-async tasks to complete."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks > 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            worker = self._worker
            pool = self._shadow_pool
        self.flush_shadow(timeout=1.0)
        self._stop_event.set()
        if worker is not None:
            worker.join(timeout=2.0)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _dispatch[T](
        self,
        op: str,
        key: str,
        call_a: Callable[[StorageBackend], T],
        call_b: Callable[[StorageBackend], T] | None = None,
    ) -> T:
        with self._lock:
            if self._closed:
                raise RuntimeError("MultiRouteBackend is closed")
            mode = self._mode
            route_b = self.route_b
        fn_b = call_a if call_b is None else call_b
        if mode == "single":
            return call_a(self.route_a)
        if route_b is None:
            raise ValueError(f"routing mode {mode!r} requires route_b")
        if mode == "canary-cutover":
            res_b = fn_b(route_b)
            with self._lock:
                self._counts["canary_ops"] += 1
            return res_b

        t0 = time.perf_counter()
        res_a = call_a(self.route_a)
        a_ms = (time.perf_counter() - t0) * 1000.0
        a_sha = _digest_result(res_a)

        if mode == "shadow-async":
            self._ensure_shadow_started()
            try:
                self._queue.put_nowait((op, key, a_sha, a_ms, lambda: fn_b(route_b)))
            except queue.Full:
                self._record_event(
                    ParityEvent(op, key, a_sha, None, a_ms, None, "shadow-queue-full", False, now())
                )
            return res_a

        # dual-sync: execute Route B synchronously and verify parity.
        tb0 = time.perf_counter()
        try:
            res_b = fn_b(route_b)
        except Exception as exc:
            b_ms = (time.perf_counter() - tb0) * 1000.0
            self._record_event(
                ParityEvent(
                    op, key, a_sha, None, a_ms, b_ms, f"{type(exc).__name__}: {exc}", False, now()
                )
            )
            raise RuntimeError(
                f"dual-sync route_b failed after route_a already committed ({op} {key}): "
                f"{exc}; reconcile route_b or abort migration"
            ) from exc

        b_ms = (time.perf_counter() - tb0) * 1000.0
        b_sha = _digest_result(res_b)
        matched = a_sha == b_sha
        self._record_event(ParityEvent(op, key, a_sha, b_sha, a_ms, b_ms, None, matched, now()))
        if self.fail_on_dual_mismatch and not matched:
            raise ValueError(
                f"dual-sync parity mismatch after route_a committed ({op} {key}): "
                f"{a_sha} != {b_sha}; inspect route_b and reconcile or abort migration"
            )
        return res_a

    def _active_backend(self) -> StorageBackend:
        with self._lock:
            if self._closed:
                raise RuntimeError("MultiRouteBackend is closed")
            if self._mode == "canary-cutover" and self.route_b is not None:
                return self.route_b
        return self.route_a

    def put_record(
        self,
        namespace: str,
        key: str,
        value: bytes,
        *,
        expected_version: int | None = None,
        visibility: SearchVisibility = "Public",
        metadata: Mapping[str, Any] | None = None,
    ) -> BackendRecord:
        return self._dispatch(
            "put_record",
            f"{namespace}/{key}",
            lambda b: b.put_record(
                namespace,
                key,
                value,
                expected_version=expected_version,
                visibility=visibility,
                metadata=metadata,
            ),
        )

    def get_record(self, namespace: str, key: str) -> BackendRecord | None:
        return self._dispatch(
            "get_record", f"{namespace}/{key}", lambda b: b.get_record(namespace, key)
        )

    def delete_record(
        self, namespace: str, key: str, *, expected_version: int | None = None
    ) -> bool:
        return self._dispatch(
            "delete_record",
            f"{namespace}/{key}",
            lambda b: b.delete_record(namespace, key, expected_version=expected_version),
        )

    def put_blob(self, namespace: str, data: bytes) -> str:
        return self._dispatch(
            "put_blob", f"{namespace}/{sha(data)[:16]}", lambda b: b.put_blob(namespace, data)
        )

    def get_blob(self, namespace: str, sha256_hex: str) -> bytes | None:
        return self._dispatch(
            "get_blob", f"{namespace}/{sha256_hex}", lambda b: b.get_blob(namespace, sha256_hex)
        )

    def has_blob(self, namespace: str, sha256_hex: str) -> bool:
        return self._dispatch(
            "has_blob", f"{namespace}/{sha256_hex}", lambda b: b.has_blob(namespace, sha256_hex)
        )

    def search(
        self,
        query: str,
        *,
        namespace: str | None = None,
        kind: str | None = None,
        mode: str = "Bm25",
        limit: int = 20,
    ) -> list[SearchHit]:
        return self._active_backend().search(
            query, namespace=namespace, kind=kind, mode=mode, limit=limit
        )

    def acquire_lease(self, namespace: str, resource: str, holder: str, ttl_ms: int) -> LeaseGrant:
        return self._active_backend().acquire_lease(namespace, resource, holder, ttl_ms)

    def renew_lease(self, grant: LeaseGrant, ttl_ms: int) -> LeaseGrant:
        return self._active_backend().renew_lease(grant, ttl_ms)

    def release_lease(self, grant: LeaseGrant) -> bool:
        return self._active_backend().release_lease(grant)

    def validate_lease(self, grant: LeaseGrant) -> bool:
        return self._active_backend().validate_lease(grant)

    def stream_signals(
        self,
        namespace: str,
        *,
        prefix: str | None = None,
        since_seq: int | None = None,
        epoch: int | None = None,
        mode: str = "each",
        timeout: float | None = None,
    ) -> Iterator[SignalEvent]:
        return self._active_backend().stream_signals(
            namespace,
            prefix=prefix,
            since_seq=since_seq,
            epoch=epoch,
            mode=mode,
            timeout=timeout,
        )

    def health(self) -> dict[str, Any]:
        return self.route_status()

    def route_status(self) -> dict[str, Any]:
        """Return loopback /control/route-status snapshot."""
        mode = self.mode
        return {
            "format": "managed-ai-route-status/1",
            "mode": mode,
            "authoritative": "route_b" if mode == "canary-cutover" else "route_a",
            "route_b_configured": self.route_b is not None,
            "route_b_timeout_ms": self.route_b_timeout_ms,
            "pending_shadow_ops": self._queue.unfinished_tasks,
            "route_a_health": self.route_a.health(),
        }

    def parity_summary(self, *, recent_limit: int = 20) -> dict[str, Any]:
        """Return loopback /control/route-parity aggregate counters and recent events."""
        with self._lock:
            counts = dict(self._counts)
            recent = (
                []
                if recent_limit <= 0
                else [ev.to_dict() for ev in list(self._events)[-recent_limit:]]
            )
        compared = counts["matched_ops"] + counts["mismatched_ops"]
        match_rate = 1.0 if compared == 0 else round(counts["matched_ops"] / compared, 6)
        return {
            "format": "managed-ai-route-parity/1",
            "mode": self.mode,
            "counts": counts,
            "match_rate": match_rate,
            "recent_events": recent,
        }


@dataclass(frozen=True, slots=True)
class MigrationRecord:
    """Immutable snapshot of a governed schema migration state."""

    namespace: str
    phase: MigrationPhase
    from_version: int
    target_version: int
    target_sha256: str
    verified_through: int
    mismatch_count: int
    reason: str | None = None
    updated_at: str = ""


class SchemaMigrationController:
    """Enforces Expand -> Dual-Write -> Verify -> Contract / Abort schema governance."""

    def __init__(
        self,
        *,
        router: MultiRouteBackend | None = None,
        catalog_backend: Any | None = None,
    ) -> None:
        self._router = router
        self._catalog = catalog_backend
        self._state: MigrationRecord | None = None

    @property
    def state(self) -> MigrationRecord | None:
        return self._state

    def begin_expand(
        self,
        namespace: str,
        *,
        from_version: int,
        target_version: int,
        target_sha256: str,
        target_schema_bytes: bytes = b"",
    ) -> MigrationRecord:
        if not namespace:
            raise ValueError("namespace required")
        if (
            isinstance(from_version, bool)
            or isinstance(target_version, bool)
            or target_version <= from_version
        ):
            raise ValueError(f"lossy schema downgrade refused: {from_version} -> {target_version}")
        if not _SHA256_RE.match(target_sha256):
            raise ValueError(f"invalid target_sha256: {target_sha256!r}")
        if self._state is not None and self._state.phase not in ("idle", "committed", "aborted"):
            raise ValueError(f"migration already active in phase {self._state.phase!r}")
        if self._catalog is not None and hasattr(self._catalog, "begin_migration"):
            self._catalog.begin_migration(
                namespace,
                expected_version=from_version,
                target_version=target_version,
                target_sha256=target_sha256,
                target_schema_bytes=target_schema_bytes,
            )
        self._state = MigrationRecord(
            namespace, "expanding", from_version, target_version, target_sha256, 0, 0, None, now()
        )
        return self._state

    def enable_dual_write(self, *, routing_mode: RoutingMode = "dual-sync") -> MigrationRecord:
        cur = self._require_phase({"expanding"}, "enable_dual_write")
        if routing_mode not in ("shadow-async", "dual-sync"):
            raise ValueError("dual-write requires 'shadow-async' or 'dual-sync' mode")
        if self._router is not None:
            self._router.set_mode(routing_mode)
        self._state = MigrationRecord(
            cur.namespace,
            "dual-write",
            cur.from_version,
            cur.target_version,
            cur.target_sha256,
            cur.verified_through,
            cur.mismatch_count,
            None,
            now(),
        )
        return self._state

    def record_verification(
        self, verified_through: int, *, mismatch_count: int = 0, anomalies: int = 0
    ) -> MigrationRecord:
        cur = self._require_phase({"dual-write", "verified"}, "record_verification")
        if isinstance(verified_through, bool) or verified_through < 0:
            raise ValueError("verified_through must be a non-negative integer")
        if verified_through < cur.verified_through:
            raise ValueError(
                f"non-monotonic verified_through: {verified_through} < {cur.verified_through}"
            )
        if mismatch_count != 0 or anomalies != 0:
            raise ValueError(
                f"verification gate failed: mismatches={mismatch_count}, anomalies={anomalies}"
            )
        if self._catalog is not None and hasattr(self._catalog, "advance_migration"):
            self._catalog.advance_migration(
                cur.namespace,
                target_version=cur.target_version,
                verified_through=verified_through,
            )
        self._state = MigrationRecord(
            cur.namespace,
            "verified",
            cur.from_version,
            cur.target_version,
            cur.target_sha256,
            verified_through,
            0,
            None,
            now(),
        )
        return self._state

    def commit_cutover(self, cutover_seq: int) -> MigrationRecord:
        cur = self._require_phase({"verified"}, "commit_cutover")
        if isinstance(cutover_seq, bool) or cutover_seq < 0 or cur.verified_through < cutover_seq:
            raise ValueError(
                f"cannot commit cutover at seq {cutover_seq}: "
                f"verified_through is only {cur.verified_through}"
            )
        if self._router is not None:
            dropped = int(
                self._router.parity_summary(recent_limit=0)["counts"]["dropped_shadow_ops"]
            )
            if dropped != 0:
                raise ValueError(
                    f"cannot commit cutover with dropped_shadow_ops={dropped}: "
                    "re-verify complete shadow parity before cutover"
                )
        if self._catalog is not None and hasattr(self._catalog, "commit_migration"):
            self._catalog.commit_migration(
                cur.namespace,
                target_version=cur.target_version,
                verified_through=cur.verified_through,
            )
        if self._router is not None:
            self._router.set_mode("canary-cutover")
        self._state = MigrationRecord(
            cur.namespace,
            "committed",
            cur.from_version,
            cur.target_version,
            cur.target_sha256,
            cur.verified_through,
            0,
            None,
            now(),
        )
        return self._state

    def abort_rollback(self, reason: str) -> MigrationRecord:
        cur = self._require_phase({"expanding", "dual-write", "verified"}, "abort_rollback")
        if not reason.strip():
            raise ValueError("abort reason required")
        if self._catalog is not None and hasattr(self._catalog, "abort_migration"):
            self._catalog.abort_migration(
                cur.namespace, target_version=cur.target_version, reason=reason
            )
        if self._router is not None:
            self._router.set_mode("single")
        self._state = MigrationRecord(
            cur.namespace,
            "aborted",
            cur.from_version,
            cur.target_version,
            cur.target_sha256,
            cur.verified_through,
            cur.mismatch_count,
            reason,
            now(),
        )
        return self._state

    def _require_phase(self, allowed: set[MigrationPhase], action: str) -> MigrationRecord:
        if self._state is None or self._state.phase not in allowed:
            cur_phase = "idle" if self._state is None else self._state.phase
            raise ValueError(f"invalid migration transition {action!r} from phase {cur_phase!r}")
        return self._state
