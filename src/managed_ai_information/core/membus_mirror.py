"""Store-to-Membus v2 mirror, parity verifier, and governed chunked migration engine."""

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from managed_ai_information.core import membus_mirror_ops as _ops
from managed_ai_information.core import multi_route as _mr
from managed_ai_information.core import storage as _st
from managed_ai_information.core import storage_backend as _sb
from managed_ai_information.core import storage_backend_protocol as _sbp

COLD_RECORD_KINDS = _ops.COLD_RECORD_KINDS
DEFAULT_MIRROR_CHUNK_SIZE = _ops.DEFAULT_MIRROR_CHUNK_SIZE
DEFAULT_MIRROR_MAX_BATCH_BYTES = _ops.DEFAULT_MIRROR_MAX_BATCH_BYTES
MIRROR_CHECKPOINT_FORMAT = _ops.MIRROR_CHECKPOINT_FORMAT
MIRROR_REPORT_FORMAT = "managed-ai-membus-mirror/1"
SEALED_CUSTODY_FORMAT = "managed-ai-sealed-custody/1"
BACKEND_ERRORS = (_sb.BackendUnavailableError, _sb.StorageBackendError, OSError, ValueError)
InMemoryMirrorBackend = _sbp.InMemoryMirrorBackend
MembusV2Backend = _sb.MembusV2Backend
MultiRouteBackend = _mr.MultiRouteBackend
validate_endpoint_url = _sbp.validate_endpoint_url
validate_identifier = _sbp.validate_identifier


@dataclass(frozen=True, slots=True)
class MirrorConfig:
    namespace: str
    sealed_ids: frozenset[str] = frozenset()
    sealed_kinds: frozenset[str] = frozenset()
    sealed_prefixes: tuple[str, ...] = ()
    metadata_only_ids: frozenset[str] = frozenset()
    metadata_only_kinds: frozenset[str] = frozenset()
    include_blobs: bool = True
    include_inactive: bool = True
    include_cold_records: bool = False
    inactive_as_metadata_only: bool = False

    def __post_init__(self) -> None:
        _sbp.validate_identifier(self.namespace, "namespace")


@dataclass(frozen=True, slots=True)
class MirrorInventoryItem:
    record_id: str
    key: str
    path: Path
    kind: str
    title: str
    visibility: _sbp.SearchVisibility
    sealed_reason: str | None
    expected_sha256: str
    payload_bytes: int
    active: bool
    source_tier: str = "hot"


@dataclass(frozen=True, slots=True)
class MirrorPlan:
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
                {"record_id": i.record_id, "reason": i.sealed_reason}
                for i in self.records
                if i.visibility == "Sealed"
            ],
        }


def sealed_custody_payload(record_id: str, *, kind: str = "sealed") -> bytes:
    return _st.encode(
        {"format": SEALED_CUSTODY_FORMAT, "id": record_id, "kind": kind, "visibility": "Sealed"}
    )


def extract_record_title(record_id: str, record: Mapping[str, Any]) -> str:
    if isinstance(c := record.get("content"), Mapping):
        for f in ("title", "name", "heading", "summary", "question"):
            if isinstance(v := c.get(f), str) and v.strip():
                return v.strip()[:160]
    return record_id


def classify_record_visibility(
    record_id: str, record: Mapping[str, Any] | None, config: MirrorConfig
) -> tuple[_sbp.SearchVisibility, str | None]:
    if record_id in config.sealed_ids:
        return "Sealed", "explicit-sealed-id"
    if any(record_id.startswith(p) for p in config.sealed_prefixes):
        return "Sealed", "explicit-sealed-prefix"
    if record is None:
        return "Public", None
    k = str(record.get("kind", "record"))
    if k in config.sealed_kinds:
        return "Sealed", "explicit-sealed-kind"
    c, b = record.get("content"), record.get("basis")
    for cond, rsn in (
        (record.get("availability") == "sealed", "record-availability-sealed"),
        (record.get("sealed") is True, "record-sealed-flag"),
        (record.get("visibility") == "Sealed", "record-visibility-sealed"),
        (
            isinstance(c, Mapping)
            and (c.get("availability") == "sealed" or c.get("sealed") is True),
            "content-sealed-marker",
        ),
        (
            isinstance(b, list)
            and any(isinstance(e, Mapping) and e.get("availability") == "sealed" for e in b),
            "basis-availability-sealed",
        ),
    ):
        if cond:
            return "Sealed", rsn
    if (
        record_id in config.metadata_only_ids
        or k in config.metadata_only_kinds
        or k in COLD_RECORD_KINDS
        or record.get("visibility") == "MetadataOnly"
        or record.get("cold") is True
        or record.get("archived") is True
        or (config.inactive_as_metadata_only and record.get("active") is False)
    ):
        return "MetadataOnly", None
    return "Public", None


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
    include_cold_records: bool = False,
    inactive_as_metadata_only: bool = False,
) -> MirrorConfig:
    c = lambda xs: [s.strip() for s in xs if s.strip()]  # noqa: E731
    return MirrorConfig(
        namespace,
        frozenset(c(sealed_ids)),
        frozenset(c(sealed_kinds)),
        tuple(c(sealed_prefixes)),
        frozenset(c(metadata_only_ids)),
        frozenset(c(metadata_only_kinds)),
        include_blobs,
        include_inactive,
        include_cold_records,
        inactive_as_metadata_only,
    )


def _mk_sealed(
    rid: str, key: str, p: Path, k: str, rsn: str | None, *, active: bool
) -> MirrorInventoryItem:
    c = sealed_custody_payload(rid, kind=k)
    return MirrorInventoryItem(rid, key, p, k, rid, "Sealed", rsn, _st.sha(c), len(c), active)


def _scan_rec(rp: Path, cfg: MirrorConfig) -> tuple[MirrorInventoryItem | None, str | None]:
    info = rp.lstat()
    if not _st.is_ordinary_file_stat(info):
        raise ValueError(f"ordinary record file required: {rp}")
    rid = _sbp.validate_identifier(rp.stem, "record_id")
    key = f"records/{rid}"
    pv, pr = classify_record_visibility(rid, None, cfg)
    if pv == "Sealed":
        return _mk_sealed(rid, key, rp, "sealed", pr, active=True), None
    if info.st_size > _st.MAX_BYTES:
        return None, "oversize"
    raw = _st.read_bytes(rp, _st.MAX_BYTES)
    rec = _st.strict_json_loads(raw, rp.name)
    act = bool(rec.get("active", True))
    if not cfg.include_inactive and not act:
        return None, "inactive"
    kind = str(rec.get("kind", "record"))
    vis, rsn = classify_record_visibility(rid, rec, cfg)
    if vis == "Sealed":
        return _mk_sealed(rid, key, rp, kind, rsn, active=act), None
    t = extract_record_title(rid, rec)
    return MirrorInventoryItem(rid, key, rp, kind, t, vis, None, _st.sha(raw), len(raw), act), None


def _item_payload(root: Path, it: MirrorInventoryItem) -> bytes:
    if it.visibility == "Sealed":
        return sealed_custody_payload(it.record_id, kind=it.kind)
    if it.source_tier == "cold":
        return _ops.read_cold_archive_payload(
            root, it.record_id, it.payload_bytes, it.expected_sha256
        )
    raw = _st.read_bytes(it.path, _st.MAX_BYTES)
    if _st.sha(raw) != it.expected_sha256:
        raise ValueError(f"record changed concurrently during mirror: {it.record_id}")
    return raw


def _item_meta(it: MirrorInventoryItem) -> dict[str, Any]:
    if it.visibility == "Sealed":
        return {"kind": it.kind, "title": it.record_id, "tags": ["sealed"]}
    tags = [it.kind, "active" if it.active else "inactive"]
    if it.source_tier == "cold":
        tags.append("cold-archive")
    return {"kind": it.kind, "title": it.title, "tags": tags}


class MembusStoreMirror:
    """Mirrors a managed-ai-information store into Membus v2 with governed chunked parity."""

    def plan(self, store_root: Path | str, config: MirrorConfig) -> MirrorPlan:
        root = _sbp.ensure_ordinary_dir(Path(store_root))
        sp = root / "system.json"
        if not _st.is_ordinary_file_stat(sp.lstat()):
            raise ValueError(f"ordinary system.json required: {sp}")
        desc = _st.strict_json_loads(_st.read_bytes(sp, _st.MAX_BYTES), "system.json")
        iid = str(desc.get("instance_id", root.name))
        sc = str(desc.get("store_schema", "managed-ai-store/1"))
        sb = _st.encode(
            {
                "format": "managed-ai-membus-collection-schema/1",
                "instance_id": iid,
                "store_schema": sc,
                "schema_bindings": desc.get("schema_bindings", {}),
            }
        )
        items: list[MirrorInventoryItem] = []
        seen: set[str] = set()
        sk_inact, over_r = 0, []
        for e in sorted(
            _sbp.ensure_ordinary_dir(root / "work/records").iterdir(), key=lambda p: p.name
        ):
            if e.name.startswith(".") or not e.name.endswith(".json"):
                continue
            it, rsn = _scan_rec(e, config)
            if rsn == "oversize":
                over_r.append(e.stem)
            elif rsn == "inactive" or it is None:
                sk_inact += 1
                seen.add(e.stem)
            else:
                items.append(it)
                seen.add(it.record_id)
        if config.include_cold_records:
            ci, co = _ops.scan_cold_archive_records(
                root, config, classify_record_visibility, MirrorInventoryItem, _mk_sealed
            )
            over_r.extend(co)
            for it in ci:
                if it.record_id not in seen:
                    items.append(it)
                    seen.add(it.record_id)
        items.sort(key=lambda i: i.record_id)
        pub, meta = (
            sum(i.visibility == "Public" for i in items),
            sum(i.visibility == "MetadataOnly" for i in items),
        )
        blobs: list[tuple[str, int]] = []
        over_b, sealed_b, odir = [], [], root / "exchange/objects"
        if config.include_blobs and odir.exists():
            _sbp.ensure_ordinary_dir(odir)
            for row in _st.ObjectStore(odir).iter_objects():
                d = str(row["object"])
                if row.get("occ") == _st.OCC_SEALED:
                    sealed_b.append(d)
                elif (sz := int(row.get("raw_bytes", row.get("disk_bytes", 0)))) > _st.MAX_BYTES:
                    over_b.append(d)
                else:
                    blobs.append((d, sz))
            blobs.sort()
            sealed_b.sort()
        return MirrorPlan(
            str(root),
            config.namespace,
            iid,
            sc,
            sb,
            tuple(items),
            tuple(blobs),
            pub,
            meta,
            len(items) - pub - meta,
            sk_inact,
            tuple(over_r),
            tuple(over_b),
            tuple(sealed_b),
        )

    def verify_parity(
        self,
        store_root: Path | str,
        backend: _sb.StorageBackend,
        config: MirrorConfig,
        *,
        plan: MirrorPlan | None = None,
        verify_blob_bytes: bool = False,
    ) -> dict[str, Any]:
        act = plan or self.plan(store_root, config)
        ns = act.namespace
        mr: list[str] = []
        br: list[str] = []
        mb: list[str] = []
        bb: list[str] = []
        sok = 0
        for it in act.records:
            if (rem := backend.get_record(ns, it.key)) is None:
                mr.append(it.record_id)
            elif _st.sha(rem.value) != it.expected_sha256 or (
                rem.visibility_reported and rem.visibility != it.visibility
            ):
                br.append(it.record_id)
            elif it.visibility == "Sealed":
                sok += 1
        for d, _ in act.blobs:
            if verify_blob_bytes:
                if (raw := backend.get_blob(ns, d)) is None:
                    mb.append(d)
                elif _st.sha(raw) != d:
                    bb.append(d)
            elif not backend.has_blob(ns, d):
                mb.append(d)
        ok = not (mr or br or mb or bb)
        return {
            "format": MIRROR_REPORT_FORMAT,
            "mode": "verify-parity",
            "status": "passed" if ok else "failed",
            "namespace": ns,
            "records_checked": len(act.records),
            "blobs_checked": len(act.blobs),
            "sealed_verified": sok,
            "missing_records": mr,
            "mismatched_records": br,
            "missing_blobs": mb,
            "mismatched_blobs": bb,
        }

    def apply(
        self,
        store_root: Path | str,
        backend: _sb.StorageBackend,
        config: MirrorConfig,
        *,
        local_route_dir: Path | None = None,
        contract_cutover: bool = True,
        verify_blob_bytes: bool = False,
        chunk_size: int = DEFAULT_MIRROR_CHUNK_SIZE,
        max_batch_bytes: int = DEFAULT_MIRROR_MAX_BATCH_BYTES,
        checkpoint_path: Path | str | None = None,
        on_chunk: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        t0, root = time.monotonic(), _sbp.ensure_ordinary_dir(Path(store_root))
        plan = self.plan(root, config)
        ns, ckpt = plan.namespace, Path(checkpoint_path) if checkpoint_path is not None else None
        fv, reg = 1, False
        if hasattr(backend, "get_collection") and hasattr(backend, "register_collection"):
            b: Any = backend
            ex = b.get_collection(ns)
            if ex is None:
                r = b.register_collection(ns, schema_version=1, schema_bytes=plan.schema_bytes)
                fv, reg = int(r.get("schema_version", 1)), True
            else:
                fv = int(ex.get("schema_version", 1))
        tv = fv + 1
        ts = _st.encode(
            {
                "format": "managed-ai-membus-collection-schema/1",
                "instance_id": plan.instance_id,
                "store_schema": plan.store_schema,
                "mirrored_version": tv,
                "record_count": len(plan.records),
                "blob_count": len(plan.blobs),
            }
        )
        ra: _sb.StorageBackend = (
            _sbp.InMemoryMirrorBackend()
            if local_route_dir is None
            else _sb.EmbeddedFileBackend(_sbp.ensure_ordinary_dir(local_route_dir))
        )
        with _mr.MultiRouteBackend(ra, backend, mode="single", route_b_timeout_ms=10_000) as rtr:
            ctrl = _mr.SchemaMigrationController(router=rtr, catalog_backend=backend)
            ctrl.begin_expand(
                ns,
                from_version=fv,
                target_version=tv,
                target_sha256=_st.sha(ts),
                target_schema_bytes=ts,
            )
            try:
                ctrl.enable_dual_write(routing_mode="dual-sync")
                rw, bw, bs, rc, bc, rr = _ops.execute_chunked_mirror(
                    root,
                    plan,
                    rtr,
                    backend,
                    _item_payload,
                    _item_meta,
                    chunk_size=chunk_size,
                    max_batch_bytes=max_batch_bytes,
                    checkpoint_path=ckpt,
                    on_chunk=on_chunk,
                )
                rp = rtr.parity_summary(recent_limit=5)["counts"]
                ver = self.verify_parity(
                    root, backend, config, plan=plan, verify_blob_bytes=verify_blob_bytes
                )
                mm = int(rp["mismatched_ops"]) + int(rp["route_b_errors"])
                an = sum(
                    len(ver[f"{p}_{s}"])
                    for p in ("missing", "mismatched")
                    for s in ("records", "blobs")
                )
                if mm or an:
                    raise ValueError(f"mirror parity failed: mismatches={mm}, anomalies={an}")
                seq = int(backend.health().get("latest_seq", 0))
                fst = ctrl.record_verification(verified_through=seq, mismatch_count=0, anomalies=0)
                if contract_cutover:
                    fst = ctrl.commit_cutover(cutover_seq=seq)
                fmode = rtr.mode
            except Exception as exc:
                if (st := ctrl.state) is not None and st.phase in (
                    "expanding",
                    "dual-write",
                    "verified",
                ):
                    ctrl.abort_rollback(str(exc) or type(exc).__name__)
                raise
        return {
            "format": MIRROR_REPORT_FORMAT,
            "mode": "apply",
            "status": "passed",
            "store_root": plan.store_root,
            "namespace": ns,
            "collection_registered": reg,
            "migration": {
                "from_version": fv,
                "target_version": tv,
                "final_phase": fst.phase if fst is not None else "unknown",
                "final_router_mode": fmode,
                "verified_through": seq,
            },
            "records_mirrored": rw,
            "records_resumed_from_checkpoint": rr,
            "blobs_mirrored": bw,
            "blobs_reused": bs,
            "chunk_count": rc,
            "blob_chunk_count": bc,
            "chunk_size": chunk_size,
            "max_batch_bytes": max_batch_bytes,
            "visibility_counts": {
                "Public": plan.public_count,
                "MetadataOnly": plan.metadata_only_count,
                "Sealed": plan.sealed_count,
            },
            "route_parity": rp,
            "parity_verification": ver,
            "elapsed_ms": round((time.monotonic() - t0) * 1000.0, 2),
        }

    def sync(
        self,
        store_root: Path | str,
        backend: _sb.StorageBackend,
        config: MirrorConfig,
        *,
        contract_cutover: bool = False,
        **kw: Any,
    ) -> dict[str, Any]:
        return self.apply(store_root, backend, config, contract_cutover=contract_cutover, **kw)

    def search(
        self,
        backend: _sb.StorageBackend,
        query: str,
        *,
        namespace: str,
        kind: str | None = None,
        mode: str = "Bm25",
        limit: int = 20,
    ) -> dict[str, Any]:
        ns = _sbp.validate_identifier(namespace, "namespace")
        hits = backend.search(query, namespace=ns, kind=kind, mode=mode, limit=limit)
        keys = ("namespace", "doc_id", "kind", "title", "score", "visibility")
        return {
            "format": MIRROR_REPORT_FORMAT,
            "mode": "search",
            "namespace": ns,
            "query": query,
            "search_mode": mode,
            "kind": kind,
            "hit_count": len(hits),
            "hits": [{k: getattr(h, k) for k in keys} for h in hits],
        }
