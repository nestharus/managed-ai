"""Live Membus v2 backend bridge, Tantivy BM25 search, and governed transitions for System."""

import os
import time
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from managed_ai_information.core import membus_mirror as _mm
from managed_ai_information.core import storage as _st

BACKEND_STATUS_FORMAT = "managed-ai-backend-status/1"
BACKEND_TYPES = frozenset({"embedded", "membus-v2"})
MEMBUS_MODES = frozenset({"shadow-async", "dual-sync", "primary"})
DEFAULT_RECEIPT_MIRROR_CHUNK_SIZE = 200
_LKEYS = (
    "sealed_ids",
    "sealed_kinds",
    "sealed_prefixes",
    "metadata_only_ids",
    "metadata_only_kinds",
)
_BKEYS = frozenset({"type", "url", "namespace", "mode", "token_env", "include_blobs", *_LKEYS})

type BackendType = Literal["embedded", "membus-v2"]
type MembusMode = Literal["shadow-async", "dual-sync", "primary"]


def _str_tuple(raw: Any, f: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(v, str) or not v.strip() for v in raw):
        raise ValueError(f"backend.{f} must be a list of nonempty strings")
    return tuple(dict.fromkeys(v.strip() for v in raw))


@dataclass(frozen=True, slots=True)
class BackendConfig:
    backend_type: BackendType = "embedded"
    url: str | None = None
    namespace: str | None = None
    mode: MembusMode = "dual-sync"
    token_env: str | None = None
    include_blobs: bool = True
    sealed_ids: tuple[str, ...] = ()
    sealed_kinds: tuple[str, ...] = ()
    sealed_prefixes: tuple[str, ...] = ()
    metadata_only_ids: tuple[str, ...] = ()
    metadata_only_kinds: tuple[str, ...] = ()

    @property
    def is_membus(self) -> bool:
        return self.backend_type == "membus-v2"

    def resolve_token(self) -> str | None:
        return os.environ.get(self.token_env) or None if self.token_env else None

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
        if not self.include_blobs:
            out["include_blobs"] = False
        for k in _LKEYS:
            if vs := getattr(self, k):
                out[k] = list(vs)
        return out


def parse_backend_config(raw: Any) -> BackendConfig:
    if raw is None:
        return BackendConfig("embedded")
    if not isinstance(raw, Mapping):
        raise ValueError("system.json backend must be a JSON object")
    if unk := set(raw) - _BKEYS:
        raise ValueError("unknown backend configuration fields: " + ", ".join(sorted(unk)))
    bt = raw.get("type", "embedded")
    if not isinstance(bt, str) or bt not in BACKEND_TYPES:
        raise ValueError(f"unsupported backend type: {bt!r}")
    if bt == "embedded":
        rem = ("url", "namespace", "mode", "token_env", "include_blobs")
        if bad := [k for k in rem if raw.get(k) is not None]:
            raise ValueError("embedded backend rejects remote fields: " + ", ".join(bad))
        return BackendConfig("embedded")
    u, n, mr, tok, bl = (
        raw.get("url"),
        raw.get("namespace"),
        raw.get("mode", "dual-sync"),
        raw.get("token_env"),
        raw.get("include_blobs", True),
    )
    if not isinstance(u, str) or not u.strip():
        raise ValueError("membus-v2 backend requires a nonempty url")
    _mm.validate_endpoint_url(u.strip())
    if not isinstance(n, str) or not n.strip():
        raise ValueError("membus-v2 backend requires a nonempty namespace")
    ns = _mm.validate_identifier(n.strip(), "namespace")
    if not isinstance(mr, str) or mr not in MEMBUS_MODES:
        raise ValueError(f"unsupported membus-v2 backend mode: {mr!r}")
    if tok is not None:
        if not isinstance(tok, str) or not tok.strip() or "\0" in tok:
            raise ValueError("backend.token_env must be a nonempty environment variable name")
        tok = tok.strip()
    if type(bl) is not bool:
        raise ValueError("backend.include_blobs must be a boolean")
    return BackendConfig(
        "membus-v2",
        u.strip(),
        ns,
        mr,  # type: ignore[arg-type]
        tok,
        bl,
        **{k: _str_tuple(raw.get(k), k) for k in _LKEYS},
    )


def _abort_open(cli: Any, ns: str, action: str) -> None:
    if not isinstance(ex := cli.get_collection(ns), Mapping):
        return
    ms = ex.get("migration_state")
    act = (
        (ms.get("Migrating") if isinstance(ms, Mapping) else None)
        or ex.get("migration")
        or ex.get("active_migration")
    )
    if isinstance(act, Mapping) and act.get("target_version") is not None:
        cli.abort_migration(
            ns, target_version=int(act["target_version"]), reason=f"superseded by {action}"
        )


class StoreBackendBridge:
    """Manages optional Membus v2 live write mirroring, Tantivy search, and transitions."""

    def __init__(self, system: Any) -> None:
        self._sys = system
        self.config: BackendConfig = parse_backend_config(system.descriptor.get("backend"))
        self._client: Any = None
        self._router: Any = None
        self._defer = 0
        self._wid: int | None = None
        st = getattr(system, "record_store", None)
        orig = getattr(st, "_replace", None)
        if st is not None and orig is not None:

            def _wrap(*a: Any, **kw: Any) -> dict[str, Any]:
                rec: dict[str, Any] = orig(*a, **kw)
                if self._defer == 0:
                    self.mirror_record(rec)
                return rec

            self._wid = id(st)
            st._replace = _wrap

    @contextmanager
    def defer_replace_mirror(self) -> Generator[None]:
        self._defer += 1
        try:
            yield
        finally:
            self._defer -= 1

    @property
    def is_membus(self) -> bool:
        return self.config.is_membus

    def reload(self) -> None:
        self.close()
        self.config = parse_backend_config(self._sys.descriptor.get("backend"))

    def _mcfg(
        self, c: BackendConfig, *, blobs: bool, cold: bool = False, im: bool = False
    ) -> _mm.MirrorConfig:
        if not c.is_membus or c.namespace is None:
            raise ValueError("membus-v2 backend requires a nonempty namespace")
        prot = tuple(getattr(self._sys.domain_selection, "protected_record_ids", ()))
        return _mm.build_mirror_config(
            c.namespace,
            sealed_ids=(*c.sealed_ids, *prot),
            sealed_kinds=c.sealed_kinds,
            sealed_prefixes=c.sealed_prefixes,
            metadata_only_ids=c.metadata_only_ids,
            metadata_only_kinds=c.metadata_only_kinds,
            include_blobs=blobs,
            include_cold_records=cold,
            inactive_as_metadata_only=im,
        )

    def mirror_config(self, *, include_blobs: bool = True) -> _mm.MirrorConfig:
        return self._mcfg(self.config, blobs=include_blobs)

    def _ensure_client(self) -> tuple[Any, Any]:
        c = self.config
        if not c.is_membus or c.url is None:
            raise ValueError("membus-v2 backend is not configured")
        if self._client is None:
            self._client = _mm.MembusV2Backend(c.url, token=c.resolve_token(), timeout=10.0)
        if self._router is None:
            rm: Literal["shadow-async", "dual-sync"] = (
                "shadow-async" if c.mode == "shadow-async" else "dual-sync"
            )
            self._router = _mm.MultiRouteBackend(
                _mm.InMemoryMirrorBackend(),
                self._client,
                mode=rm,
                route_b_timeout_ms=5_000,
                fail_on_dual_mismatch=(c.mode != "shadow-async"),
            )
        return self._client, self._router

    def close(self) -> None:
        r, self._router = self._router, None
        c, self._client = self._client, None
        if r is not None:
            r.close()
        if c is not None:
            c.close()

    def _mirror_referenced_blobs(self, rec: Mapping[str, Any], ns: str, cli: Any, rtr: Any) -> int:
        if not isinstance(basis := rec.get("basis"), list):
            return 0
        objs, n = self._sys.ex.objects, 0
        for e in basis:
            if (
                not isinstance(e, Mapping)
                or not isinstance(p := e.get("path"), str)
                or not p.startswith("object:")
            ):
                continue
            d = p.removeprefix("object:")
            if (
                len(d) != 64
                or any(ch not in "0123456789abcdef" for ch in d)
                or e.get("availability") == "sealed"
                or objs.occ_of(d) == _st.OCC_SEALED
                or not objs.has(d)
            ):
                continue
            if self.config.mode != "shadow-async" and cli.has_blob(ns, d):
                continue
            if len(raw := objs.read(d, retained=False)) <= _st.MAX_BYTES:
                rtr.put_blob(ns, raw)
                n += 1
        return n

    def mirror_record(self, record: Mapping[str, Any]) -> None:
        if not self.config.is_membus or self.config.namespace is None:
            return
        ns, (cli, rtr) = self.config.namespace, self._ensure_client()
        cfg = self.mirror_config(include_blobs=self.config.include_blobs)
        rid, kind = str(record["id"]), str(record.get("kind", "record"))
        vis, _ = _mm.classify_record_visibility(rid, None, cfg)
        full, raw = record, b""
        if vis != "Sealed":
            rp = self._sys.record_store.path(rid)
            try:
                info = rp.lstat()
            except FileNotFoundError:
                info = None
            if info is not None:
                if not _st.is_ordinary_file_stat(info):
                    raise ValueError(f"ordinary record file required: {rp}")
                if info.st_size > _st.MAX_BYTES:
                    return
                raw = _st.read_bytes(rp, _st.MAX_BYTES)
                full = _st.strict_json_loads(raw, rp.name)
            elif len(raw := _st.encode(record)) > _st.MAX_BYTES:
                return
            kind = str(full.get("kind", kind))
            vis, _ = _mm.classify_record_visibility(rid, full, cfg)
        pl = _mm.sealed_custody_payload(rid, kind=kind) if vis == "Sealed" else raw
        meta: dict[str, Any] = (
            {"kind": kind, "title": rid, "tags": ["sealed"]}
            if vis == "Sealed"
            else {
                "kind": kind,
                "title": _mm.extract_record_title(rid, full),
                "tags": [kind, "active" if bool(full.get("active", True)) else "inactive"],
            }
        )
        rtr.put_record(
            ns, f"records/{rid}", pl, expected_version=None, visibility=vis, metadata=meta
        )
        if (
            vis != "Sealed"
            and self.config.include_blobs
            and kind not in _mm.COLD_RECORD_KINDS
            and full.get("cold") is not True
            and full.get("archived") is not True
        ):
            self._mirror_referenced_blobs(full, ns, cli, rtr)

    def mirror_batch_receipt(
        self, receipt: Mapping[str, Any], *, chunk_size: int = DEFAULT_RECEIPT_MIRROR_CHUNK_SIZE
    ) -> None:
        if not self.config.is_membus or not isinstance(rows := receipt.get("records"), list):
            return
        valid = [r for r in rows if isinstance(r, Mapping) and isinstance(r.get("id"), str)]
        step = max(1, int(chunk_size))
        for i in range(0, len(valid), step):
            for r in valid[i : i + step]:
                self.mirror_record(r)
            if self.config.mode == "shadow-async" and self._router is not None:
                self._router.flush_shadow(timeout=5.0)

    def flush(self, timeout: float = 5.0) -> bool:
        return True if self._router is None else bool(self._router.flush_shadow(timeout=timeout))

    def search_records(
        self,
        query: str,
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 25,
        search_mode: str = "auto",
    ) -> dict[str, Any] | None:
        if search_mode == "lexical" or not query.strip():
            return None
        if not self.config.is_membus or self.config.namespace is None:
            if search_mode == "bm25":
                raise ValueError("search_mode='bm25' requires a configured membus-v2 backend")
            return None
        ns, kl = self.config.namespace, list(kinds) if kinds else []
        k1 = kl[0] if len(kl) == 1 else None
        fl = min(200, max(limit * 4, 50) if len(kl) > 1 else limit * 2)
        t0 = time.perf_counter()
        try:
            rh = self._ensure_client()[0].search(
                query, namespace=ns, kind=k1, mode="Bm25", limit=fl
            )
        except _mm.BACKEND_ERRORS as exc:
            if search_mode == "bm25":
                raise
            return {
                "requested": search_mode,
                "engine": "lexical-fallback",
                "namespace": ns,
                "fallback_reason": str(exc),
                "hits": None,
            }
        hits: list[tuple[str, float]] = []
        seen: set[str] = set()
        for h in rh:
            if h.visibility != "Sealed" and h.doc_id.startswith("records/"):
                rid = h.doc_id.removeprefix("records/")
                if (not kl or h.kind in kl) and rid not in seen:
                    seen.add(rid)
                    hits.append((rid, h.score))
        return {
            "requested": search_mode,
            "engine": "membus-bm25",
            "namespace": ns,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 3),
            "hit_count": len(hits),
            "hits": hits,
        }

    def _persist(self, cfg: BackendConfig) -> None:
        upd = dict(self._sys.persisted_descriptor)
        if (blk := cfg.to_descriptor() if cfg.is_membus else None) is not None:
            upd["backend"], self._sys.descriptor["backend"] = blk, dict(blk)
        else:
            upd.pop("backend", None)
            self._sys.descriptor.pop("backend", None)
        _st.write_json(self._sys.root / "system.json", upd)
        self._sys.persisted_descriptor = upd
        self.reload()

    def _target(
        self, url: Any, ns: Any, mode: Any, tok: Any, blobs: Any, dmode: MembusMode
    ) -> BackendConfig:
        c = self.config
        raw: dict[str, Any] = {
            "type": "membus-v2",
            "url": c.url if url is None else url,
            "namespace": c.namespace if ns is None else ns,
            "mode": (c.mode if c.is_membus else dmode) if mode is None else mode,
            "include_blobs": (c.include_blobs if c.is_membus else True) if blobs is None else blobs,
        }
        if (tv := c.token_env if tok is None else tok) is not None:
            raw["token_env"] = tv
        for k in _LKEYS:
            if vs := getattr(c, k):
                raw[k] = list(vs)
        return parse_backend_config(raw)

    def _resp(self, action: str, **extra: Any) -> dict[str, Any]:
        return {
            "format": BACKEND_STATUS_FORMAT,
            "action": action,
            "store_root": str(self._sys.root),
            "backend": self.config.to_descriptor(),
            **extra,
        }

    def status(
        self, *, check_parity: bool = True, verify_blob_bytes: bool = False
    ) -> dict[str, Any]:
        c = self.config
        res = self._resp("status", healthy=True)
        if not c.is_membus or c.url is None or c.namespace is None:
            return res
        res["healthy"], res["health"] = False, None
        with _mm.MembusV2Backend(c.url, token=c.resolve_token(), timeout=10.0) as cli:
            try:
                res["health"], res["healthy"] = cli.health(), True
                if check_parity:
                    res["parity"] = _mm.MembusStoreMirror().verify_parity(
                        self._sys.root,
                        cli,
                        self._mcfg(c, blobs=c.include_blobs),
                        verify_blob_bytes=verify_blob_bytes,
                    )
            except _mm.BACKEND_ERRORS as e:
                res["error"] = str(e)
        if self._router is not None:
            res["live_route_parity"] = self._router.parity_summary(recent_limit=5)
        return res

    def _transition(
        self,
        action: Literal["sync", "cutover"],
        *,
        url: Any = None,
        namespace: Any = None,
        mode: Any = None,
        token_env: Any = None,
        include_blobs: Any = None,
        verify_blob_bytes: bool = False,
        include_cold_records: bool = False,
        inactive_as_metadata_only: bool = False,
        **mirror_kw: Any,
    ) -> dict[str, Any]:
        cut = action == "cutover"
        tgt = self._target(
            url, namespace, mode, token_env, include_blobs, "primary" if cut else "dual-sync"
        )
        if tgt.url is None or tgt.namespace is None:
            raise ValueError("membus-v2 backend requires a nonempty url and namespace")
        mcfg = self._mcfg(
            tgt, blobs=tgt.include_blobs, cold=include_cold_records, im=inactive_as_metadata_only
        )
        with _mm.MembusV2Backend(tgt.url, token=tgt.resolve_token(), timeout=10.0) as cli:
            _abort_open(cli, tgt.namespace, action)
            rep = _mm.MembusStoreMirror().apply(
                self._sys.root,
                cli,
                mcfg,
                contract_cutover=cut,
                verify_blob_bytes=verify_blob_bytes,
                **mirror_kw,
            )
        if cut or tgt != self.config:
            self._persist(tgt)
        return self._resp(action, mirror=rep)

    def sync(self, **kw: Any) -> dict[str, Any]:
        return self._transition("sync", **kw)

    def cutover(self, *, mode: str = "primary", **kw: Any) -> dict[str, Any]:
        if mode not in {"dual-sync", "primary"}:
            raise ValueError("cutover mode must be 'dual-sync' or 'primary'")
        return self._transition("cutover", mode=mode, **kw)

    def rollback(self, *, reason: str | None = None) -> dict[str, Any]:
        prev = self.config.to_descriptor()
        self._persist(BackendConfig("embedded"))
        return self._resp(
            "rollback",
            previous_backend=prev,
            reason=reason or "Rolled back to zero-daemon embedded local files.",
        )


def backend_status(s: Any, **k: Any) -> dict[str, Any]:
    return dict(s.backend_bridge.status(**k))


def backend_sync(s: Any, **k: Any) -> dict[str, Any]:
    return dict(s.backend_bridge.sync(**k))


def backend_cutover(s: Any, **k: Any) -> dict[str, Any]:
    return dict(s.backend_bridge.cutover(**k))


def backend_rollback(s: Any, **k: Any) -> dict[str, Any]:
    return dict(s.backend_bridge.rollback(**k))
