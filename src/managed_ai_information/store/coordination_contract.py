"""Versioned information-layer contract for managed-ai-coordination."""

import contextlib
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from managed_ai_information.core.formats import BATCH_FORMAT
from managed_ai_information.core.storage import (
    MAX_BYTES,
    atomic_bytes,
    encode,
    exclusive,
    is_ordinary_file_stat,
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
    """Verified receipt for a lease-fenced, atomic optimistic-CAS consolidation delta commit."""

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

    ref = BoundedBundleRef(
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
    # Fixed-point byte accounting so bundle_bytes matches exact final serialized size.
    for _ in range(3):
        size = len(encode(ref.to_dict()))
        if size == ref.bundle_bytes:
            break
        ref = replace(ref, bundle_bytes=size)
    if ref.bundle_bytes > MAX_BYTES:
        raise ValueError(
            f"resolved bundle ref exceeds MAX_BYTES ({ref.bundle_bytes} > {MAX_BYTES})"
        )
    return ref


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


def _fence_file_path(system: Any) -> Path:
    return Path(system.ex.root) / "coordination-fences.json"


def _load_fences(path: Path) -> dict[tuple[str, str], int]:
    try:
        doc = read_json(path)
    except FileNotFoundError:
        return {}
    return {
        (str(row["namespace"]), str(row["resource"])): int(row["generation"])
        for row in doc.get("fences", ())
    }


def _save_fences(path: Path, fences: Mapping[tuple[str, str], int]) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary fence file required: {path}")
    payload = {
        "format": COORDINATION_CONTRACT_FORMAT,
        "fences": [
            {"namespace": ns, "resource": res, "generation": gen}
            for (ns, res), gen in sorted(fences.items())
        ],
    }
    atomic_bytes(path, encode(payload))


def _build_batch_operations(
    system: Any,
    expected_revisions: Mapping[str, int],
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    operations: list[dict[str, Any]] = []
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
        exists = system.record_store.has_record(ident)
        current = system.record_store.show(ident) if exists else None
        cur_rev = int(current["revision"]) if current is not None else 0
        if cur_rev != exp_rev:
            raise BackendConflictError(
                f"revision mismatch for {ident!r}: expected {exp_rev}, observed {cur_rev}",
                status=409,
                code="revision_mismatch",
            )
        op: dict[str, Any] = {
            "id": ident,
            "mode": "replace",
            "expected_revision": exp_rev,
            "record": rec,
        }
        if exp_rev > 0 and current is not None:
            op["expected_sha256"] = str(current["sha256"])
        operations.append(op)
    return operations


def commit_consolidation_delta(
    system: Any,
    lease: LeaseGrant,
    expected_revisions: Mapping[str, int],
    records: Sequence[Mapping[str, Any]],
    *,
    backend: StorageBackend | None = None,
    now_ms: int | None = None,
    min_generation: int = 1,
    acquire_owner_lock: bool = True,
) -> CommitReceipt:
    """Validate lease fencing + optimistic CAS, then commit atomically via system.put_batch()."""
    if not records:
        raise ValueError("consolidation delta requires at least one record")
    lock_cm = (
        exclusive(Path(system.ex.root) / "owner.lock")
        if acquire_owner_lock
        else contextlib.nullcontext()
    )
    with lock_cm:
        fence_path = _fence_file_path(system)
        fences = _load_fences(fence_path)
        res_key = (lease.namespace, lease.resource)
        effective_min = max(min_generation, fences.get(res_key, 1))
        _validate_lease_for_commit(
            lease, backend=backend, now_ms=now_ms, min_generation=effective_min
        )
        operations = _build_batch_operations(system, expected_revisions, records)
        spec_id = f"coord-delta-g{lease.generation}-{sha(encode(operations))[:16]}"
        batch_spec: dict[str, Any] = {
            "format": BATCH_FORMAT,
            "id": spec_id,
            "instance_id": system.instance_id,
            "operations": operations,
        }
        batch_receipt = system.put_batch(batch_spec)
        fences[res_key] = max(effective_min, lease.generation)
        _save_fences(fence_path, fences)

    committed = tuple(
        {
            "id": str(row["id"]),
            "revision": int(row["after_revision"]),
            "sha256": str(row["after_sha256"]),
        }
        for row in batch_receipt["records"]
    )
    return CommitReceipt(
        format=COMMIT_RECEIPT_FORMAT,
        lease_namespace=lease.namespace,
        lease_resource=lease.resource,
        lease_holder=lease.holder,
        lease_generation=lease.generation,
        committed_count=len(committed),
        records=committed,
        committed_at=now(),
    )


class CoordinationFacade:
    """Boundary facade exposed to managed-ai-coordination (never reads work/records directly)."""

    contract_format = COORDINATION_CONTRACT_FORMAT

    def __init__(self, system: Any, *, backend: StorageBackend | None = None) -> None:
        self.system = system
        self.backend = backend
        self._lock = threading.Lock()
        self._high_water_gen: dict[tuple[str, str], int] = _load_fences(_fence_file_path(system))

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
        acquire_owner_lock: bool = True,
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
                acquire_owner_lock=acquire_owner_lock,
            )
            self._high_water_gen[res_key] = max(min_gen, lease.generation)
            return receipt

    def subscribe_signals(
        self,
        namespace: str,
        *,
        prefix: str | None = None,
        since_seq: int | None = None,
        epoch: int | None = None,
        mode: str = "each",
        timeout: float | None = None,
    ) -> Iterator[SignalEvent]:
        if self.backend is None:
            raise ValueError("signal subscription requires a configured StorageBackend")
        return self.backend.stream_signals(
            namespace,
            prefix=prefix,
            since_seq=since_seq,
            epoch=epoch,
            mode=mode,
            timeout=timeout,
        )
