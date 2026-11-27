"""Store-to-Membus v2 mirror, parity verifier, and governed migration engine.

Pure-stdlib core module mirroring records + CAS exchange objects into Membus v2 via
MultiRouteBackend + SchemaMigrationController. Sealed entries always emit opaque
SearchVisibility='Sealed' custody envelopes (never indexed into Tantivy).
"""

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from managed_ai_information.core.multi_route import (
    MultiRouteBackend,
    RoutingMode,
    SchemaMigrationController,
)
from managed_ai_information.core.storage import (
    MAX_BYTES,
    OCC_SEALED,
    ObjectStore,
    encode,
    is_ordinary_file_stat,
    read_bytes,
    sha,
    strict_json_loads,
)
from managed_ai_information.core.storage_backend import (
    BackendUnavailableError,
    EmbeddedFileBackend,
    MembusV2Backend,
    SearchHit,
    SearchVisibility,
    StorageBackend,
    StorageBackendError,
)
from managed_ai_information.core.storage_backend_protocol import (
    InMemoryMirrorBackend,
    ensure_ordinary_dir,
    validate_endpoint_url,
    validate_identifier,
)

MIRROR_REPORT_FORMAT = "managed-ai-membus-mirror/1"
SEALED_CUSTODY_FORMAT = "managed-ai-sealed-custody/1"

__all__ = [
    "MIRROR_REPORT_FORMAT",
    "SEALED_CUSTODY_FORMAT",
    "BackendUnavailableError",
    "InMemoryMirrorBackend",
    "MembusStoreMirror",
    "MembusV2Backend",
    "MirrorConfig",
    "MirrorInventoryItem",
    "MirrorPlan",
    "MultiRouteBackend",
    "RoutingMode",
    "SearchHit",
    "StorageBackendError",
    "classify_record_visibility",
    "validate_endpoint_url",
    "validate_identifier",
]


@dataclass(frozen=True, slots=True)
class MirrorConfig:
    """Policy governing visibility classification and scope of a store-to-Membus mirror."""

    namespace: str
    sealed_ids: frozenset[str] = frozenset()
    sealed_kinds: frozenset[str] = frozenset()
    sealed_prefixes: tuple[str, ...] = ()
    metadata_only_ids: frozenset[str] = frozenset()
    metadata_only_kinds: frozenset[str] = frozenset()
    include_blobs: bool = True
    include_inactive: bool = True

    def __post_init__(self) -> None:
        validate_identifier(self.namespace, "namespace")


@dataclass(frozen=True, slots=True)
class MirrorInventoryItem:
    """One record scheduled for mirroring into Membus v2."""

    record_id: str
    key: str
    path: Path
    kind: str
    title: str
    visibility: SearchVisibility
    sealed_reason: str | None
    expected_sha256: str
    payload_bytes: int
    active: bool


@dataclass(frozen=True, slots=True)
class MirrorPlan:
    """Read-only inventory of records and CAS blobs to mirror."""

    store_root: str
    namespace: str
    instance_id: str
    store_schema: str
    schema_bytes: bytes
    records: tuple[MirrorInventoryItem, ...]
    blobs: tuple[tuple[str, int], ...]
    public_count: int
    metadata_only_count: int
    sealed_count: int
    skipped_inactive: int
    skipped_oversize_records: tuple[str, ...]
    skipped_oversize_blobs: tuple[str, ...]
    skipped_sealed_blobs: tuple[str, ...] = ()

    def to_report(self) -> dict[str, Any]:
        return {
            "format": MIRROR_REPORT_FORMAT,
            "mode": "dry-run",
            "store_root": self.store_root,
            "namespace": self.namespace,
            "instance_id": self.instance_id,
            "store_schema": self.store_schema,
            "record_count": len(self.records),
            "blob_count": len(self.blobs),
            "visibility_counts": {
                "Public": self.public_count,
                "MetadataOnly": self.metadata_only_count,
                "Sealed": self.sealed_count,
            },
            "skipped_inactive": self.skipped_inactive,
            "skipped_oversize_records": list(self.skipped_oversize_records),
            "skipped_oversize_blobs": list(self.skipped_oversize_blobs),
            "skipped_sealed_blobs": list(self.skipped_sealed_blobs),
            "sealed_records": [
                {"record_id": item.record_id, "reason": item.sealed_reason}
                for item in self.records
                if item.visibility == "Sealed"
            ],
        }


def _sealed_custody_payload(record_id: str, *, kind: str = "sealed") -> bytes:
    """Return an opaque metadata-only custody marker that never contains sealed text."""
    return encode(
        {"format": SEALED_CUSTODY_FORMAT, "id": record_id, "kind": kind, "visibility": "Sealed"}
    )


def _extract_title(record_id: str, record: Mapping[str, Any]) -> str:
    content = record.get("content")
    if isinstance(content, Mapping):
        for field in ("title", "name", "heading", "summary", "question"):
            val = content.get(field)
            if isinstance(val, str) and val.strip():
                return val.strip()[:160]
    return record_id


def _has_record_sealed_marker(record: Mapping[str, Any]) -> str | None:
    if record.get("availability") == "sealed":
        return "record-availability-sealed"
    if record.get("sealed") is True:
        return "record-sealed-flag"
    if record.get("visibility") == "Sealed":
        return "record-visibility-sealed"
    content = record.get("content")
    if isinstance(content, Mapping) and (
        content.get("availability") == "sealed" or content.get("sealed") is True
    ):
        return "content-sealed-marker"
    basis = record.get("basis")
    if isinstance(basis, list):
        for entry in basis:
            if isinstance(entry, Mapping) and entry.get("availability") == "sealed":
                return "basis-availability-sealed"
    return None


def classify_record_visibility(
    record_id: str,
    record: Mapping[str, Any] | None,
    config: MirrorConfig,
) -> tuple[SearchVisibility, str | None]:
    """Determine SearchVisibility and optional sealed reason for a record."""
    if record_id in config.sealed_ids:
        return "Sealed", "explicit-sealed-id"
    if any(record_id.startswith(prefix) for prefix in config.sealed_prefixes):
        return "Sealed", "explicit-sealed-prefix"
    if record is None:
        return "Public", None

    kind = str(record.get("kind", "record"))
    if kind in config.sealed_kinds:
        return "Sealed", "explicit-sealed-kind"
    marker = _has_record_sealed_marker(record)
    if marker is not None:
        return "Sealed", marker

    if (
        record_id in config.metadata_only_ids
        or kind in config.metadata_only_kinds
        or record.get("visibility") == "MetadataOnly"
    ):
        return "MetadataOnly", None
    return "Public", None


def _read_store_descriptor(store_root: Path) -> tuple[str, str, bytes]:
    sys_path = store_root / "system.json"
    info = sys_path.lstat()
    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary system.json required: {sys_path}")
    raw = read_bytes(sys_path, MAX_BYTES)
    doc = strict_json_loads(raw, "system.json")
    instance_id = str(doc.get("instance_id", store_root.name))
    store_schema = str(doc.get("store_schema", "managed-ai-store/1"))
    schema_doc = {
        "format": "managed-ai-membus-collection-schema/1",
        "instance_id": instance_id,
        "store_schema": store_schema,
        "schema_bindings": doc.get("schema_bindings", {}),
    }
    return instance_id, store_schema, encode(schema_doc)


def _make_sealed_item(
    record_id: str, key: str, rec_path: Path, kind: str, reason: str | None, *, active: bool
) -> MirrorInventoryItem:
    custody = _sealed_custody_payload(record_id, kind=kind)
    return MirrorInventoryItem(
        record_id,
        key,
        rec_path,
        kind,
        record_id,
        "Sealed",
        reason,
        sha(custody),
        len(custody),
        active,
    )


def _scan_record_file(
    rec_path: Path, config: MirrorConfig
) -> tuple[MirrorInventoryItem | None, str | None]:
    info = rec_path.lstat()
    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary record file required: {rec_path}")
    record_id = validate_identifier(rec_path.stem, "record_id")
    key = f"records/{record_id}"

    # Pre-read sealed check: NEVER open or parse payload text of pre-sealed IDs/prefixes!
    pre_vis, pre_reason = classify_record_visibility(record_id, None, config)
    if pre_vis == "Sealed":
        return _make_sealed_item(record_id, key, rec_path, "sealed", pre_reason, active=True), None
    if info.st_size > MAX_BYTES:
        return None, "oversize"

    raw = read_bytes(rec_path, MAX_BYTES)
    record = strict_json_loads(raw, rec_path.name)
    active = bool(record.get("active", True))
    if not config.include_inactive and not active:
        return None, "inactive"

    kind = str(record.get("kind", "record"))
    vis, reason = classify_record_visibility(record_id, record, config)
    if vis == "Sealed":
        return _make_sealed_item(record_id, key, rec_path, kind, reason, active=active), None
    return (
        MirrorInventoryItem(
            record_id,
            key,
            rec_path,
            kind,
            _extract_title(record_id, record),
            vis,
            None,
            sha(raw),
            len(raw),
            active,
        ),
        None,
    )


def _payload_for_item(item: MirrorInventoryItem) -> bytes:
    if item.visibility == "Sealed":
        return _sealed_custody_payload(item.record_id, kind=item.kind)
    raw = read_bytes(item.path, MAX_BYTES)
    if sha(raw) != item.expected_sha256:
        raise ValueError(f"record changed concurrently during mirror: {item.record_id}")
    return raw


def _record_metadata(item: MirrorInventoryItem) -> dict[str, Any]:
    if item.visibility == "Sealed":
        return {"kind": item.kind, "title": item.record_id, "tags": ["sealed"]}
    status_tag = "active" if item.active else "inactive"
    return {"kind": item.kind, "title": item.title, "tags": [item.kind, status_tag]}


class MembusStoreMirror:
    """Mirrors a managed-ai-information store into Membus v2 with governed parity."""

    def plan(self, store_root: Path | str, config: MirrorConfig) -> MirrorPlan:
        root = ensure_ordinary_dir(Path(store_root))
        instance_id, store_schema, schema_bytes = _read_store_descriptor(root)
        records_dir = ensure_ordinary_dir(root / "work/records")

        items: list[MirrorInventoryItem] = []
        pub_count = meta_count = sealed_count = skipped_inactive = 0
        oversize_records: list[str] = []

        for entry in sorted(records_dir.iterdir(), key=lambda p: p.name):
            if entry.name.startswith(".") or not entry.name.endswith(".json"):
                continue
            item, skip_reason = _scan_record_file(entry, config)
            if skip_reason == "oversize":
                oversize_records.append(entry.stem)
                continue
            if skip_reason == "inactive" or item is None:
                skipped_inactive += 1
                continue
            items.append(item)
            if item.visibility == "Public":
                pub_count += 1
            elif item.visibility == "MetadataOnly":
                meta_count += 1
            else:
                sealed_count += 1

        blobs: list[tuple[str, int]] = []
        oversize_blobs: list[str] = []
        sealed_blobs: list[str] = []
        objects_dir = root / "exchange/objects"
        if config.include_blobs and objects_dir.exists():
            ensure_ordinary_dir(objects_dir)
            for row in ObjectStore(objects_dir).iter_objects():
                digest = str(row["object"])
                if row.get("occ") == OCC_SEALED:
                    sealed_blobs.append(digest)
                    continue
                raw_size = int(row.get("raw_bytes", row.get("disk_bytes", 0)))
                if raw_size > MAX_BYTES:
                    oversize_blobs.append(digest)
                    continue
                blobs.append((digest, raw_size))
            blobs.sort(key=lambda pair: pair[0])
            sealed_blobs.sort()

        return MirrorPlan(
            str(root),
            config.namespace,
            instance_id,
            store_schema,
            schema_bytes,
            tuple(items),
            tuple(blobs),
            pub_count,
            meta_count,
            sealed_count,
            skipped_inactive,
            tuple(oversize_records),
            tuple(oversize_blobs),
            tuple(sealed_blobs),
        )

    def verify_parity(
        self,
        store_root: Path | str,
        backend: StorageBackend,
        config: MirrorConfig,
        *,
        plan: MirrorPlan | None = None,
        verify_blob_bytes: bool = False,
    ) -> dict[str, Any]:
        """Read-only SHA-256 parity check across all planned records and CAS blobs."""
        active_plan = plan if plan is not None else self.plan(store_root, config)
        ns = active_plan.namespace
        missing_records: list[str] = []
        mismatched_records: list[str] = []
        sealed_verified = 0

        for item in active_plan.records:
            remote = backend.get_record(ns, item.key)
            if remote is None:
                missing_records.append(item.record_id)
                continue
            if sha(remote.value) != item.expected_sha256:
                mismatched_records.append(item.record_id)
                continue
            if item.visibility == "Sealed":
                sealed_verified += 1

        missing_blobs: list[str] = []
        mismatched_blobs: list[str] = []
        for digest, _ in active_plan.blobs:
            if verify_blob_bytes:
                raw = backend.get_blob(ns, digest)
                if raw is None:
                    missing_blobs.append(digest)
                elif sha(raw) != digest:
                    mismatched_blobs.append(digest)
            elif not backend.has_blob(ns, digest):
                missing_blobs.append(digest)

        ok = not (missing_records or mismatched_records or missing_blobs or mismatched_blobs)
        return {
            "format": MIRROR_REPORT_FORMAT,
            "mode": "verify-parity",
            "status": "passed" if ok else "failed",
            "namespace": ns,
            "records_checked": len(active_plan.records),
            "blobs_checked": len(active_plan.blobs),
            "sealed_verified": sealed_verified,
            "missing_records": missing_records,
            "mismatched_records": mismatched_records,
            "missing_blobs": missing_blobs,
            "mismatched_blobs": mismatched_blobs,
        }

    def _prepare_collection_version(
        self, backend: Any, namespace: str, schema_bytes: bytes
    ) -> tuple[int, bool]:
        if not hasattr(backend, "get_collection") or not hasattr(backend, "register_collection"):
            return 1, False
        existing = backend.get_collection(namespace)
        if existing is None:
            reg = backend.register_collection(
                namespace, schema_version=1, schema_bytes=schema_bytes
            )
            return int(reg.get("schema_version", 1)), True
        return int(existing.get("schema_version", 1)), False

    def _mirror_records_and_blobs(
        self,
        root: Path,
        plan: MirrorPlan,
        router: MultiRouteBackend,
        backend: StorageBackend,
    ) -> tuple[int, int, int]:
        ns = plan.namespace
        records_written = 0
        for item in plan.records:
            payload = _payload_for_item(item)
            meta = _record_metadata(item)
            router.put_record(
                ns,
                item.key,
                payload,
                expected_version=None,
                visibility=item.visibility,
                metadata=meta,
            )
            records_written += 1

        blobs_written = 0
        blobs_skipped_existing = 0
        objects_dir = root / "exchange/objects"
        if plan.blobs and objects_dir.exists():
            obj_store = ObjectStore(objects_dir)
            with obj_store.pack_session():
                for digest, _ in plan.blobs:
                    if backend.has_blob(ns, digest):
                        blobs_skipped_existing += 1
                        continue
                    raw = obj_store.read(digest, retained=False)
                    if len(raw) > MAX_BYTES:
                        continue
                    router.put_blob(ns, raw)
                    blobs_written += 1
        return records_written, blobs_written, blobs_skipped_existing

    def apply(
        self,
        store_root: Path | str,
        backend: StorageBackend,
        config: MirrorConfig,
        *,
        local_route_dir: Path | None = None,
        contract_cutover: bool = True,
        verify_blob_bytes: bool = False,
    ) -> dict[str, Any]:
        """Run Expand -> Dual-Write -> Verify -> Contract mirror into Membus v2."""
        started = time.monotonic()
        root = ensure_ordinary_dir(Path(store_root))
        plan = self.plan(root, config)
        ns = plan.namespace

        from_version, registered = self._prepare_collection_version(backend, ns, plan.schema_bytes)
        target_version = from_version + 1
        target_schema = encode(
            {
                "format": "managed-ai-membus-collection-schema/1",
                "instance_id": plan.instance_id,
                "store_schema": plan.store_schema,
                "mirrored_version": target_version,
                "record_count": len(plan.records),
                "blob_count": len(plan.blobs),
            }
        )

        route_a: StorageBackend = (
            InMemoryMirrorBackend()
            if local_route_dir is None
            else EmbeddedFileBackend(ensure_ordinary_dir(local_route_dir))
        )
        with MultiRouteBackend(route_a, backend, mode="single", route_b_timeout_ms=10_000) as (
            router
        ):
            controller = SchemaMigrationController(router=router, catalog_backend=backend)
            controller.begin_expand(
                ns,
                from_version=from_version,
                target_version=target_version,
                target_sha256=sha(target_schema),
                target_schema_bytes=target_schema,
            )
            try:
                controller.enable_dual_write(routing_mode="dual-sync")
                rec_written, blob_written, blob_skipped = self._mirror_records_and_blobs(
                    root, plan, router, backend
                )
                route_parity = router.parity_summary(recent_limit=5)
                verify_report = self.verify_parity(
                    root, backend, config, plan=plan, verify_blob_bytes=verify_blob_bytes
                )
                mismatches = int(route_parity["counts"]["mismatched_ops"]) + int(
                    route_parity["counts"]["route_b_errors"]
                )
                anomalies = sum(
                    len(verify_report[k])
                    for k in (
                        "missing_records",
                        "mismatched_records",
                        "missing_blobs",
                        "mismatched_blobs",
                    )
                )
                if mismatches != 0 or anomalies != 0:
                    raise ValueError(
                        f"mirror parity failed: mismatches={mismatches}, anomalies={anomalies}"
                    )
                live_seq = int(backend.health().get("latest_seq", 0))
                final_state = controller.record_verification(
                    verified_through=live_seq, mismatch_count=0, anomalies=0
                )
                if contract_cutover:
                    final_state = controller.commit_cutover(cutover_seq=live_seq)
                final_router_mode = router.mode
            except Exception as exc:
                st = controller.state
                if st is not None and st.phase in ("expanding", "dual-write", "verified"):
                    controller.abort_rollback(str(exc) or type(exc).__name__)
                raise

        elapsed_ms = round((time.monotonic() - started) * 1000.0, 2)
        return {
            "format": MIRROR_REPORT_FORMAT,
            "mode": "apply",
            "status": "passed",
            "store_root": plan.store_root,
            "namespace": ns,
            "collection_registered": registered,
            "migration": {
                "from_version": from_version,
                "target_version": target_version,
                "final_phase": final_state.phase if final_state is not None else "unknown",
                "final_router_mode": final_router_mode,
                "verified_through": live_seq,
            },
            "records_mirrored": rec_written,
            "blobs_mirrored": blob_written,
            "blobs_reused": blob_skipped,
            "visibility_counts": {
                "Public": plan.public_count,
                "MetadataOnly": plan.metadata_only_count,
                "Sealed": plan.sealed_count,
            },
            "route_parity": route_parity["counts"],
            "parity_verification": verify_report,
            "elapsed_ms": elapsed_ms,
        }

    def search(
        self,
        backend: StorageBackend,
        query: str,
        *,
        namespace: str,
        kind: str | None = None,
        mode: str = "Bm25",
        limit: int = 20,
    ) -> dict[str, Any]:
        """Run a live Tantivy search query against a mirrored Membus v2 namespace."""
        ns = validate_identifier(namespace, "namespace")
        hits: list[SearchHit] = backend.search(
            query, namespace=ns, kind=kind, mode=mode, limit=limit
        )
        return {
            "format": MIRROR_REPORT_FORMAT,
            "mode": "search",
            "namespace": ns,
            "query": query,
            "search_mode": mode,
            "kind": kind,
            "hit_count": len(hits),
            "hits": [
                {
                    "namespace": h.namespace,
                    "doc_id": h.doc_id,
                    "kind": h.kind,
                    "title": h.title,
                    "score": h.score,
                    "visibility": h.visibility,
                }
                for h in hits
            ],
        }


def build_mirror_config(
    namespace: str,
    *,
    sealed_ids: Iterable[str] = (),
    sealed_kinds: Iterable[str] = (),
    sealed_prefixes: Iterable[str] = (),
    metadata_only_ids: Iterable[str] = (),
    metadata_only_kinds: Iterable[str] = (),
    include_blobs: bool = True,
    include_inactive: bool = True,
) -> MirrorConfig:
    """Convenience constructor normalizing iterable filters into a frozen MirrorConfig."""
    return MirrorConfig(
        namespace=namespace,
        sealed_ids=frozenset(s.strip() for s in sealed_ids if s.strip()),
        sealed_kinds=frozenset(s.strip() for s in sealed_kinds if s.strip()),
        sealed_prefixes=tuple(s.strip() for s in sealed_prefixes if s.strip()),
        metadata_only_ids=frozenset(s.strip() for s in metadata_only_ids if s.strip()),
        metadata_only_kinds=frozenset(s.strip() for s in metadata_only_kinds if s.strip()),
        include_blobs=include_blobs,
        include_inactive=include_inactive,
    )
