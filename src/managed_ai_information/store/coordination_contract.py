"""Versioned information-layer contract for managed-ai-coordination."""

import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from managed_ai_information.core.storage import (
    MAX_BYTES,
    encode,
    now,
    read_json,
    sha,
    strict_json_loads,
)
from managed_ai_information.core.storage_backend import (
    BackendConflictError,
    BackendContendedError,
    LeaseGrant,
    SignalEvent,
    StorageBackend,
)
from managed_ai_information.store.turns.view_chunks import (
    outside_view_record_count,
    validate_outside_view_chunks_metadata,
)

COORDINATION_CONTRACT_FORMAT = "managed-ai-coordination-contract/1"
BUNDLE_REF_FORMAT = "managed-ai-bounded-bundle-ref/1"
ARTIFACT_RECEIPT_FORMAT = "managed-ai-artifact-receipt/1"
COMMIT_RECEIPT_FORMAT = "managed-ai-consolidation-commit-receipt/1"


@dataclass(frozen=True, slots=True)
class BoundedBundleRef:
    """Strictly bounded (<= MAX_BYTES) immutable handle to a prepared turn context."""

    format: str
    turn: str
    consumer: str
    request_sha256: str
    context_sha256: str
    exposure_sha256: str | None
    full_view_object: str
    selected_records: tuple[str, ...]
    outside_view_record_count: int
    outside_view_record_objects: tuple[str, ...]
    bundle_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "turn": self.turn,
            "consumer": self.consumer,
            "request_sha256": self.request_sha256,
            "context_sha256": self.context_sha256,
            "exposure_sha256": self.exposure_sha256,
            "full_view_object": self.full_view_object,
            "selected_records": list(self.selected_records),
            "outside_view_record_count": self.outside_view_record_count,
            "outside_view_record_objects": list(self.outside_view_record_objects),
            "bundle_bytes": self.bundle_bytes,
        }


@dataclass(frozen=True, slots=True)
class ArtifactReceipt:
    """Verified CAS receipt for an artifact attached during a coordination turn."""

    format: str
    turn_id: str
    role: str
    sha256: str
    bytes: int
    provenance: dict[str, Any]
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "turn_id": self.turn_id,
            "role": self.role,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "provenance": dict(self.provenance),
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class CommitReceipt:
    """Verified receipt for a lease-fenced, optimistic-CAS consolidation delta commit."""

    format: str
    lease_namespace: str
    lease_resource: str
    lease_holder: str
    lease_generation: int
    committed_count: int
    records: tuple[dict[str, Any], ...]
    committed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "lease_namespace": self.lease_namespace,
            "lease_resource": self.lease_resource,
            "lease_holder": self.lease_holder,
            "lease_generation": self.lease_generation,
            "committed_count": self.committed_count,
            "records": [dict(row) for row in self.records],
            "committed_at": self.committed_at,
        }


def resolve_context_bundle(system: Any, turn: str) -> BoundedBundleRef:
    """Resolve one prepared turn into a strictly bounded (<= MAX_BYTES) immutable handle."""
    if not isinstance(turn, str) or not turn.strip():
        raise ValueError("turn identifier required")
    prefix = f".system/turns/{turn}/"
    with system.ex.objects.pack_session():
        directory, request = system.ex.load(turn)
        binding = read_json(directory / "binding.json")
        materials = request.get("materials", ())
        exposure_row = next(
            (row for row in materials if row.get("path") == prefix + "exposure.json"), None
        )
        exposure_sha: str | None = None
        if exposure_row is not None:
            exposure_sha = str(exposure_row["sha256"])
            exp_doc = strict_json_loads(system.ex.objects.read(exposure_sha), "exposure.json")
            full_view_object = str(exp_doc["full_view_object"])
        else:
            view_row = next(
                (row for row in materials if row.get("path") == prefix + "view.json"), None
            )
            if view_row is None:
                raise ValueError(f"no captured view for turn {turn!r}")
            full_view_object = str(view_row["sha256"])

        view_doc = strict_json_loads(system.ex.objects.read(full_view_object), "view.json")
        chunk_meta = validate_outside_view_chunks_metadata(view_doc)
        outside_count = outside_view_record_count(view_doc)
        chunk_hashes = tuple(chunk_meta[0]) if chunk_meta is not None else ()
        selected = tuple(str(r) for r in view_doc.get("selected_records", ()))

    provisional = BoundedBundleRef(
        format=BUNDLE_REF_FORMAT,
        turn=turn,
        consumer=str(request["consumer"]),
        request_sha256=str(binding["request_sha256"]),
        context_sha256=str(binding["context_sha256"]),
        exposure_sha256=exposure_sha,
        full_view_object=full_view_object,
        selected_records=selected,
        outside_view_record_count=outside_count,
        outside_view_record_objects=chunk_hashes,
        bundle_bytes=0,
    )
    encoded_len = len(encode(provisional.to_dict()))
    if encoded_len > MAX_BYTES:
        raise ValueError(f"resolved bundle ref exceeds MAX_BYTES ({encoded_len} > {MAX_BYTES})")
    return BoundedBundleRef(
        format=provisional.format,
        turn=provisional.turn,
        consumer=provisional.consumer,
        request_sha256=provisional.request_sha256,
        context_sha256=provisional.context_sha256,
        exposure_sha256=provisional.exposure_sha256,
        full_view_object=provisional.full_view_object,
        selected_records=provisional.selected_records,
        outside_view_record_count=provisional.outside_view_record_count,
        outside_view_record_objects=provisional.outside_view_record_objects,
        bundle_bytes=encoded_len,
    )


def attach_artifact(
    objects: Any,
    turn_id: str,
    payload: bytes,
    role: str,
    provenance: Mapping[str, Any],
) -> ArtifactReceipt:
    """Store one immutable artifact blob in CAS and return its verified ArtifactReceipt."""
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise ValueError("turn_id required")
    if not isinstance(role, str) or not role.strip():
        raise ValueError("artifact role required")
    if not isinstance(payload, bytes):
        raise ValueError("artifact payload must be bytes")
    if len(payload) > MAX_BYTES:
        raise ValueError(f"artifact payload exceeds MAX_BYTES ({len(payload)} > {MAX_BYTES})")
    prov = dict(provenance)
    # Validate provenance is strictly JSON-serializable (no NaN/Infinity/unsupported types).
    encode(prov)
    expected_digest = sha(payload)
    digest = str(objects.put(payload))
    if digest != expected_digest:
        raise ValueError("object store returned mismatched SHA-256 digest")
    return ArtifactReceipt(
        format=ARTIFACT_RECEIPT_FORMAT,
        turn_id=turn_id,
        role=role,
        sha256=digest,
        bytes=len(payload),
        provenance=prov,
        recorded_at=now(),
    )


def _validate_lease_for_commit(
    lease: LeaseGrant,
    *,
    backend: StorageBackend | None,
    now_ms: int | None,
    min_generation: int = 1,
) -> None:
    if not lease.holder.strip() or not lease.token.strip():
        raise BackendContendedError("lease holder and token are required", code="invalid_lease")
    if lease.generation < min_generation:
        raise BackendContendedError(
            f"stale lease generation {lease.generation} < fenced {min_generation}",
            code="stale_generation",
        )
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if lease.expires_at_ms <= current_ms:
        raise BackendContendedError("lease has expired", code="lease_expired")
    if backend is not None and not backend.validate_lease(lease):
        raise BackendContendedError("lease rejected by backend validation", code="lease_invalid")


def commit_consolidation_delta(
    system: Any,
    lease: LeaseGrant,
    expected_revisions: Mapping[str, int],
    records: Sequence[Mapping[str, Any]],
    *,
    backend: StorageBackend | None = None,
    now_ms: int | None = None,
    min_generation: int = 1,
) -> CommitReceipt:
    """Validate lease fencing + optimistic-CAS expected revisions and commit records."""
    _validate_lease_for_commit(lease, backend=backend, now_ms=now_ms, min_generation=min_generation)
    if not records:
        raise ValueError("consolidation delta requires at least one record")

    seen_ids: set[str] = set()
    staged: list[tuple[str, dict[str, Any], int]] = []
    for raw_record in records:
        rec = dict(raw_record)
        ident = rec.get("id")
        if not isinstance(ident, str) or not ident.strip():
            raise ValueError("every delta record must define a non-empty string id")
        if ident in seen_ids:
            raise ValueError(f"duplicate record id in consolidation delta: {ident!r}")
        seen_ids.add(ident)
        if ident not in expected_revisions:
            raise BackendConflictError(
                f"missing expected_revision guard for record {ident!r}", code="missing_guard"
            )
        exp_rev = expected_revisions[ident]
        if isinstance(exp_rev, bool) or not isinstance(exp_rev, int) or exp_rev < 0:
            raise ValueError(f"invalid expected_revision for {ident!r}: {exp_rev!r}")
        cur_rev = (
            int(system.record_store.show(ident)["revision"])
            if system.record_store.has_record(ident)
            else 0
        )
        if cur_rev != exp_rev:
            raise BackendConflictError(
                f"revision mismatch for {ident!r}: expected {exp_rev}, observed {cur_rev}",
                status=409,
                code="revision_mismatch",
            )
        staged.append((ident, rec, exp_rev))

    committed: list[dict[str, Any]] = []
    for ident, rec, exp_rev in staged:
        saved = system.put(rec, exp_rev)
        committed.append(
            {
                "id": ident,
                "revision": int(saved["revision"]),
                "sha256": str(saved["sha256"]),
            }
        )
    return CommitReceipt(
        format=COMMIT_RECEIPT_FORMAT,
        lease_namespace=lease.namespace,
        lease_resource=lease.resource,
        lease_holder=lease.holder,
        lease_generation=lease.generation,
        committed_count=len(committed),
        records=tuple(committed),
        committed_at=now(),
    )


class CoordinationFacade:
    """Boundary facade exposed to managed-ai-coordination (never reads work/records directly)."""

    contract_format = COORDINATION_CONTRACT_FORMAT

    def __init__(self, system: Any, *, backend: StorageBackend | None = None) -> None:
        self.system = system
        self.backend = backend
        self._lock = threading.Lock()
        self._high_water_gen: dict[tuple[str, str], int] = {}

    def resolve_context_bundle(self, turn: str) -> BoundedBundleRef:
        return resolve_context_bundle(self.system, turn)

    def attach_artifact(
        self, turn_id: str, payload: bytes, role: str, provenance: Mapping[str, Any]
    ) -> ArtifactReceipt:
        return attach_artifact(self.system.ex.objects, turn_id, payload, role, provenance)

    def commit_consolidation_delta(
        self,
        lease: LeaseGrant,
        expected_revisions: Mapping[str, int],
        records: Sequence[Mapping[str, Any]],
        *,
        now_ms: int | None = None,
    ) -> CommitReceipt:
        res_key = (lease.namespace, lease.resource)
        with self._lock:
            min_gen = self._high_water_gen.get(res_key, 1)
            receipt = commit_consolidation_delta(
                self.system,
                lease,
                expected_revisions,
                records,
                backend=self.backend,
                now_ms=now_ms,
                min_generation=min_gen,
            )
            self._high_water_gen[res_key] = max(min_gen, lease.generation)
            return receipt

    def subscribe_signals(
        self,
        namespace: str,
        *,
        prefix: str | None = None,
        since_seq: int | None = None,
        mode: str = "each",
        timeout: float | None = None,
    ) -> Iterator[SignalEvent]:
        if self.backend is None:
            raise ValueError("signal subscription requires a configured StorageBackend")
        return self.backend.stream_signals(
            namespace, prefix=prefix, since_seq=since_seq, mode=mode, timeout=timeout
        )
