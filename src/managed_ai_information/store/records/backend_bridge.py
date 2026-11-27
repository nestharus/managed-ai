"""Selectable Membus v2 backend bridge for System writes, Tantivy BM25 discovery, and cutover.

Embedded local files remain the zero-daemon default whenever system.json omits 'backend' or sets
'backend.type' to 'embedded'. When 'backend.type' is 'membus-v2', this bridge routes live record
and non-sealed object writes via MultiRouteBackend + KeepAliveHTTPPool, enforces strict
SearchVisibility='Sealed' custody envelopes (never leaking sealed text to Tantivy), powers
sub-millisecond Tantivy BM25 discovery with graceful fallback, and governs CLI
status/sync/cutover/rollback transitions.
"""

import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from managed_ai_information.core.membus_mirror import (
    SEALED_CUSTODY_FORMAT,
    MembusStoreMirror,
    MirrorConfig,
    build_mirror_config,
    classify_record_visibility,
)
from managed_ai_information.core.multi_route import MultiRouteBackend, RoutingMode
from managed_ai_information.core.storage import (
    MAX_BYTES,
    OCC_SEALED,
    encode,
    read_bytes,
    strict_json_loads,
    write_json,
)
from managed_ai_information.core.storage_backend import (
    BackendUnavailableError,
    MembusV2Backend,
    SearchHit,
    StorageBackendError,
)
from managed_ai_information.core.storage_backend_protocol import (
    InMemoryMirrorBackend,
    validate_endpoint_url,
    validate_identifier,
)

BACKEND_STATUS_FORMAT = "managed-ai-backend-status/1"
BACKEND_TYPES = frozenset({"embedded", "membus-v2"})
MEMBUS_MODES = frozenset({"shadow-async", "dual-sync", "primary"})
_STRING_LIST_KEYS = (
    "sealed_ids",
    "sealed_kinds",
    "sealed_prefixes",
    "metadata_only_ids",
    "metadata_only_kinds",
)
_BACKEND_KEYS = frozenset({"type", "url", "namespace", "mode", "token_env", *_STRING_LIST_KEYS})

type BackendType = Literal["embedded", "membus-v2"]
type MembusMode = Literal["shadow-async", "dual-sync", "primary"]


def _validate_str_tuple(raw: Any, field_name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(v, str) or not v.strip() for v in raw):
        raise ValueError(f"backend.{field_name} must be a list of nonempty strings")
    return tuple(dict.fromkeys(v.strip() for v in raw))


@dataclass(frozen=True, slots=True)
class BackendConfig:
    """Validated store backend configuration from system.json['backend']."""

    backend_type: BackendType = "embedded"
    url: str | None = None
    namespace: str | None = None
    mode: MembusMode = "dual-sync"
    token_env: str | None = None
    sealed_ids: tuple[str, ...] = ()
    sealed_kinds: tuple[str, ...] = ()
    sealed_prefixes: tuple[str, ...] = ()
    metadata_only_ids: tuple[str, ...] = ()
    metadata_only_kinds: tuple[str, ...] = ()

    @property
    def is_membus(self) -> bool:
        return self.backend_type == "membus-v2"

    def resolve_token(self) -> str | None:
        if not self.token_env:
            return None
        val = os.environ.get(self.token_env)
        return val if val else None

    def to_descriptor(self) -> dict[str, Any]:
        if not self.is_membus:
            return {"type": "embedded"}
        out: dict[str, Any] = {
            "type": "membus-v2",
            "url": self.url,
            "namespace": self.namespace,
            "mode": self.mode,
        }
        if self.token_env is not None:
            out["token_env"] = self.token_env
        for key in _STRING_LIST_KEYS:
            if vals := getattr(self, key):
                out[key] = list(vals)
        return out


def parse_backend_config(raw: Any) -> BackendConfig:
    """Parse and strictly validate optional system.json['backend'] configuration."""
    if raw is None:
        return BackendConfig(backend_type="embedded")
    if not isinstance(raw, Mapping):
        raise ValueError("system.json backend must be a JSON object")
    if unknown := set(raw) - _BACKEND_KEYS:
        raise ValueError("unknown backend configuration fields: " + ", ".join(sorted(unknown)))

    btype = raw.get("type", "embedded")
    if not isinstance(btype, str) or btype not in BACKEND_TYPES:
        raise ValueError(f"unsupported backend type: {btype!r}")
    if btype == "embedded":
        disallowed = [
            k for k in ("url", "namespace", "mode", "token_env") if raw.get(k) is not None
        ]
        if disallowed:
            raise ValueError("embedded backend rejects remote fields: " + ", ".join(disallowed))
        return BackendConfig(backend_type="embedded")

    url_raw = raw.get("url")
    if not isinstance(url_raw, str) or not url_raw.strip():
        raise ValueError("membus-v2 backend requires a nonempty url")
    validate_endpoint_url(url_raw.strip())

    ns_raw = raw.get("namespace")
    if not isinstance(ns_raw, str) or not ns_raw.strip():
        raise ValueError("membus-v2 backend requires a nonempty namespace")
    ns = validate_identifier(ns_raw.strip(), "namespace")

    mode_raw = raw.get("mode", "dual-sync")
    if not isinstance(mode_raw, str) or mode_raw not in MEMBUS_MODES:
        raise ValueError(f"unsupported membus-v2 backend mode: {mode_raw!r}")

    token_env = raw.get("token_env")
    if token_env is not None:
        if not isinstance(token_env, str) or not token_env.strip() or "\0" in token_env:
            raise ValueError("backend.token_env must be a nonempty environment variable name")
        token_env = token_env.strip()

    lists = {k: _validate_str_tuple(raw.get(k), k) for k in _STRING_LIST_KEYS}
    return BackendConfig(
        backend_type="membus-v2",
        url=url_raw.strip(),
        namespace=ns,
        mode=mode_raw,  # type: ignore[arg-type]
        token_env=token_env,
        **lists,
    )


def _sealed_custody_bytes(record_id: str, kind: str) -> bytes:
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


class StoreBackendBridge:
    """Manages optional Membus v2 live write mirroring and Tantivy BM25 discovery for a System."""

    def __init__(self, system: Any) -> None:
        self._system = system
        self.config: BackendConfig = parse_backend_config(system.descriptor.get("backend"))
        self._client: MembusV2Backend | None = None
        self._router: MultiRouteBackend | None = None

    @property
    def is_membus(self) -> bool:
        return self.config.is_membus

    def reload(self) -> None:
        self.close()
        self.config = parse_backend_config(self._system.descriptor.get("backend"))

    def mirror_config(self, *, include_blobs: bool = True) -> MirrorConfig:
        return _mirror_config_for(self._system, self.config, include_blobs=include_blobs)

    def _ensure_client(self) -> tuple[MembusV2Backend, MultiRouteBackend]:
        if not self.config.is_membus or self.config.url is None:
            raise ValueError("membus-v2 backend is not configured")
        if self._client is None:
            self._client = MembusV2Backend(
                self.config.url, token=self.config.resolve_token(), timeout=10.0
            )
        if self._router is None:
            route_mode: RoutingMode = (
                "shadow-async" if self.config.mode == "shadow-async" else "dual-sync"
            )
            self._router = MultiRouteBackend(
                InMemoryMirrorBackend(),
                self._client,
                mode=route_mode,
                route_b_timeout_ms=5_000,
                fail_on_dual_mismatch=(self.config.mode != "shadow-async"),
            )
        return self._client, self._router

    def close(self) -> None:
        router, self._router = self._router, None
        client, self._client = self._client, None
        if router is not None:
            router.close()
        if client is not None:
            client.close()

    def _mirror_referenced_blobs(
        self, record: Mapping[str, Any], ns: str, client: MembusV2Backend, router: MultiRouteBackend
    ) -> int:
        basis = record.get("basis")
        if not isinstance(basis, list):
            return 0
        obj_store = self._system.ex.objects
        mirrored = 0
        for entry in basis:
            if not isinstance(entry, Mapping):
                continue
            path_val = entry.get("path")
            if not isinstance(path_val, str) or not path_val.startswith("object:"):
                continue
            digest = path_val.removeprefix("object:")
            if entry.get("availability") == "sealed" or obj_store.occ_of(digest) == OCC_SEALED:
                continue
            if self.config.mode != "shadow-async" and client.has_blob(ns, digest):
                continue
            raw = obj_store.read(digest, retained=False)
            if len(raw) <= MAX_BYTES:
                router.put_blob(ns, raw)
                mirrored += 1
        return mirrored

    def mirror_record(self, record: Mapping[str, Any]) -> None:
        """Mirror one committed native record (and non-sealed object basis) into Membus v2."""
        if not self.config.is_membus or self.config.namespace is None:
            return
        ns, (client, router) = self.config.namespace, self._ensure_client()
        cfg = self.mirror_config(include_blobs=True)
        record_id, kind = str(record["id"]), str(record.get("kind", "record"))
        vis, _ = classify_record_visibility(record_id, None, cfg)
        full_record: Mapping[str, Any] = record
        raw = b""
        if vis != "Sealed":
            rec_path = self._system.record_store.path(record_id)
            raw = read_bytes(rec_path, MAX_BYTES) if rec_path.is_file() else encode(record)
            if len(raw) > MAX_BYTES:
                return
            full_record = strict_json_loads(raw, rec_path.name) if rec_path.is_file() else record
            kind = str(full_record.get("kind", kind))
            vis, _ = classify_record_visibility(record_id, full_record, cfg)

        if vis == "Sealed":
            payload = _sealed_custody_bytes(record_id, kind)
            meta: dict[str, Any] = {"kind": kind, "title": record_id, "tags": ["sealed"]}
        else:
            active_tag = "active" if bool(full_record.get("active", True)) else "inactive"
            payload = raw
            meta = {
                "kind": kind,
                "title": _extract_title(record_id, full_record),
                "tags": [kind, active_tag],
            }

        router.put_record(
            ns,
            f"records/{record_id}",
            payload,
            expected_version=None,
            visibility=vis,
            metadata=meta,
        )
        if vis != "Sealed":
            self._mirror_referenced_blobs(full_record, ns, client, router)

    def mirror_batch_receipt(self, receipt: Mapping[str, Any]) -> None:
        """Mirror every record committed by a System.put_batch receipt into Membus v2."""
        if not self.config.is_membus or not isinstance(rows := receipt.get("records"), list):
            return
        for row in rows:
            if isinstance(row, Mapping) and isinstance(row.get("id"), str):
                self.mirror_record(self._system.show(row["id"]))

    def flush(self, timeout: float = 5.0) -> bool:
        return True if self._router is None else self._router.flush_shadow(timeout=timeout)

    def search_records(
        self,
        query: str,
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 25,
        search_mode: str = "auto",
    ) -> dict[str, Any] | None:
        """Query Membus Tantivy BM25 for record hits with strict mode semantics."""
        if search_mode == "lexical" or not query.strip():
            return None
        if not self.config.is_membus or self.config.namespace is None:
            if search_mode == "bm25":
                raise ValueError("search_mode='bm25' requires a configured membus-v2 backend")
            return None

        ns = self.config.namespace
        kind_list = list(kinds) if kinds else []
        single_kind = kind_list[0] if len(kind_list) == 1 else None
        fetch_limit = min(200, max(limit * 4, 50)) if len(kind_list) > 1 else min(200, limit * 2)
        started = time.perf_counter()

        try:
            client, _ = self._ensure_client()
            raw_hits: list[SearchHit] = client.search(
                query, namespace=ns, kind=single_kind, mode="Bm25", limit=fetch_limit
            )
        except (BackendUnavailableError, StorageBackendError, OSError, ValueError) as exc:
            if search_mode == "bm25":
                raise
            return {
                "requested": search_mode,
                "engine": "lexical-fallback",
                "namespace": ns,
                "fallback_reason": str(exc),
                "hits": None,
            }

        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        parsed_hits: list[tuple[str, float]] = []
        seen: set[str] = set()
        for hit in raw_hits:
            if hit.visibility == "Sealed" or not hit.doc_id.startswith("records/"):
                continue
            rid = hit.doc_id.removeprefix("records/")
            if (kind_list and hit.kind not in kind_list) or rid in seen:
                continue
            seen.add(rid)
            parsed_hits.append((rid, hit.score))

        return {
            "requested": search_mode,
            "engine": "membus-bm25",
            "namespace": ns,
            "elapsed_ms": elapsed_ms,
            "hit_count": len(parsed_hits),
            "hits": parsed_hits,
        }

    def status(
        self, *, check_parity: bool = True, verify_blob_bytes: bool = False
    ) -> dict[str, Any]:
        return backend_status(
            self._system, check_parity=check_parity, verify_blob_bytes=verify_blob_bytes
        )

    def sync(self, **kwargs: Any) -> dict[str, Any]:
        return backend_sync(self._system, **kwargs)

    def cutover(self, **kwargs: Any) -> dict[str, Any]:
        return backend_cutover(self._system, **kwargs)

    def rollback(self, *, reason: str | None = None) -> dict[str, Any]:
        return backend_rollback(self._system, reason=reason)


def _persist_backend_block(system: Any, config: BackendConfig) -> None:
    updated = dict(system.persisted_descriptor)
    if config.is_membus:
        updated["backend"] = config.to_descriptor()
    else:
        updated.pop("backend", None)
    write_json(system.root / "system.json", updated)
    system.persisted_descriptor = updated
    system.descriptor = updated
    system.backend_bridge.reload()


def _resolve_target_config(
    system: Any,
    *,
    url: str | None,
    namespace: str | None,
    mode: str | None,
    token_env: str | None,
    default_mode: MembusMode,
) -> BackendConfig:
    cur = system.backend_bridge.config
    raw: dict[str, Any] = {
        "type": "membus-v2",
        "url": url if url is not None else cur.url,
        "namespace": namespace if namespace is not None else cur.namespace,
        "mode": mode if mode is not None else (cur.mode if cur.is_membus else default_mode),
    }
    tok = token_env if token_env is not None else cur.token_env
    if tok is not None:
        raw["token_env"] = tok
    for key in _STRING_LIST_KEYS:
        if vals := getattr(cur, key):
            raw[key] = list(vals)
    return parse_backend_config(raw)


def _mirror_config_for(system: Any, cfg: BackendConfig, *, include_blobs: bool) -> MirrorConfig:
    if not cfg.is_membus or cfg.namespace is None:
        raise ValueError("membus-v2 backend requires a nonempty namespace")
    protected = tuple(getattr(system.domain_selection, "protected_record_ids", ()))
    return build_mirror_config(
        cfg.namespace,
        sealed_ids=(*cfg.sealed_ids, *protected),
        sealed_kinds=cfg.sealed_kinds,
        sealed_prefixes=cfg.sealed_prefixes,
        metadata_only_ids=cfg.metadata_only_ids,
        metadata_only_kinds=cfg.metadata_only_kinds,
        include_blobs=include_blobs,
        include_inactive=True,
    )


def backend_status(
    system: Any, *, check_parity: bool = True, verify_blob_bytes: bool = False
) -> dict[str, Any]:
    """Report active store backend configuration, live Membus health, and route parity."""
    cfg = system.backend_bridge.config
    if not cfg.is_membus or cfg.url is None or cfg.namespace is None:
        return {
            "format": BACKEND_STATUS_FORMAT,
            "action": "status",
            "store_root": str(system.root),
            "backend": cfg.to_descriptor(),
            "healthy": True,
        }

    health: dict[str, Any] | None = None
    parity: dict[str, Any] | None = None
    error: str | None = None
    healthy = False
    with MembusV2Backend(cfg.url, token=cfg.resolve_token(), timeout=10.0) as client:
        try:
            health = client.health()
            healthy = True
            if check_parity:
                mcfg = _mirror_config_for(system, cfg, include_blobs=True)
                parity = MembusStoreMirror().verify_parity(
                    system.root, client, mcfg, verify_blob_bytes=verify_blob_bytes
                )
        except (BackendUnavailableError, StorageBackendError, OSError, ValueError) as exc:
            error = str(exc)

    result: dict[str, Any] = {
        "format": BACKEND_STATUS_FORMAT,
        "action": "status",
        "store_root": str(system.root),
        "backend": cfg.to_descriptor(),
        "healthy": healthy,
        "health": health,
    }
    if parity is not None:
        result["parity"] = parity
    if error is not None:
        result["error"] = error
    if system.backend_bridge._router is not None:
        result["live_route_parity"] = system.backend_bridge._router.parity_summary(recent_limit=5)
    return result


def _apply_transition(
    system: Any,
    action: Literal["sync", "cutover"],
    *,
    url: str | None = None,
    namespace: str | None = None,
    mode: str | None = None,
    token_env: str | None = None,
    include_blobs: bool = True,
    verify_blob_bytes: bool = False,
) -> dict[str, Any]:
    is_cutover = action == "cutover"
    target = _resolve_target_config(
        system,
        url=url,
        namespace=namespace,
        mode=mode,
        token_env=token_env,
        default_mode="primary" if is_cutover else "dual-sync",
    )
    if target.url is None:
        raise ValueError("membus-v2 backend requires a nonempty url")
    mcfg = _mirror_config_for(system, target, include_blobs=include_blobs)
    with MembusV2Backend(target.url, token=target.resolve_token(), timeout=10.0) as client:
        existing = client.get_collection(target.namespace or "")
        if isinstance(existing, Mapping):
            active_mig = existing.get("migration") or existing.get("active_migration")
            if isinstance(active_mig, Mapping) and active_mig.get("target_version") is not None:
                client.abort_migration(
                    target.namespace or "",
                    target_version=int(active_mig["target_version"]),
                    reason=f"superseded by backend {action}",
                )
        report = MembusStoreMirror().apply(
            system.root,
            client,
            mcfg,
            contract_cutover=True,
            verify_blob_bytes=verify_blob_bytes,
        )
    if is_cutover or mode is not None or (url is not None and not system.backend_bridge.is_membus):
        _persist_backend_block(system, target)
    return {
        "format": BACKEND_STATUS_FORMAT,
        "action": action,
        "store_root": str(system.root),
        "backend": system.backend_bridge.config.to_descriptor(),
        "mirror": report,
    }


def backend_sync(system: Any, **kwargs: Any) -> dict[str, Any]:
    """Backfill and parity-verify local store records & blobs into Membus v2."""
    return _apply_transition(system, "sync", **kwargs)


def backend_cutover(system: Any, *, mode: str = "primary", **kwargs: Any) -> dict[str, Any]:
    """Run governed Expand -> Dual-Write -> Verify -> Contract cutover into Membus v2."""
    if mode not in {"dual-sync", "primary"}:
        raise ValueError("cutover mode must be 'dual-sync' or 'primary'")
    return _apply_transition(system, "cutover", mode=mode, **kwargs)


def backend_rollback(system: Any, *, reason: str | None = None) -> dict[str, Any]:
    """Cleanly roll store backend configuration back to zero-daemon embedded local files."""
    previous = system.backend_bridge.config.to_descriptor()
    _persist_backend_block(system, BackendConfig(backend_type="embedded"))
    return {
        "format": BACKEND_STATUS_FORMAT,
        "action": "rollback",
        "store_root": str(system.root),
        "previous_backend": previous,
        "backend": system.backend_bridge.config.to_descriptor(),
        "reason": reason or "Rolled back to zero-daemon embedded local files.",
    }
