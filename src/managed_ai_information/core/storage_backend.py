"""Embedded local StorageBackend adapter and pure-stdlib Membus v2 HTTP/JSON/SSE client."""

import base64
import http.client
import json
import secrets
import threading
import urllib.parse
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from managed_ai_information.core import storage as _st
from managed_ai_information.core import storage_backend_protocol as _sbp

BackendConflictError = _sbp.BackendConflictError
BackendContendedError = _sbp.BackendContendedError
BackendRecord = _sbp.BackendRecord
BackendUnavailableError = _sbp.BackendUnavailableError
LeaseGrant = _sbp.LeaseGrant
SearchHit = _sbp.SearchHit
SearchVisibility = _sbp.SearchVisibility
SignalEvent = _sbp.SignalEvent
StorageBackend = _sbp.StorageBackend
StorageBackendError = _sbp.StorageBackendError

__all__ = [
    "BackendConflictError",
    "BackendContendedError",
    "BackendRecord",
    "BackendUnavailableError",
    "EmbeddedFileBackend",
    "LeaseGrant",
    "MembusV2Backend",
    "SearchHit",
    "SearchVisibility",
    "SignalEvent",
    "StorageBackend",
    "StorageBackendError",
]


class EmbeddedFileBackend:
    """Zero-dependency local StorageBackend over ObjectStore + workspace files."""

    def __init__(self, root: Path, *, max_signals: int = 4096) -> None:
        self.root = _sbp.ensure_ordinary_dir(Path(root))
        self.objects_dir = _sbp.ensure_ordinary_dir(self.root / "exchange/objects")
        self.kv_dir = _sbp.ensure_ordinary_dir(self.root / "work/backend-kv")
        self._state_path = self.root / "work/backend-state.json"
        self._objects = _st.ObjectStore(self.objects_dir)
        self._lock = threading.RLock()
        self._leases: dict[tuple[str, str], _sbp.LeaseGrant] = {}
        self._generations: dict[tuple[str, str], int] = {}
        self._search_docs: dict[tuple[str, str], dict[str, Any]] = {}
        self._signals: deque[_sbp.SignalEvent] = deque(maxlen=max_signals)
        self._seq = 0
        self._reload_persisted_state()

    def _reload_persisted_state(self) -> None:
        if self._state_path.exists() or self._state_path.is_symlink():
            doc = _st.read_json(self._state_path)
            self._seq = int(doc.get("seq", 0))
            for r in doc.get("generations", ()):
                self._generations[(str(r["namespace"]), str(r["resource"]))] = int(r["generation"])
            for r in doc.get("leases", ()):
                g = LeaseGrant.from_payload(r)
                self._leases[(g.namespace, g.resource)] = g
        for e in sorted(self.kv_dir.iterdir(), key=lambda p: p.name):
            if e.name.startswith("."):
                continue
            if not _st.is_ordinary_file_stat(e.lstat()):
                raise ValueError(f"ordinary KV record file required: {e}")
            if (rec := self._read_unlocked(e)) is not None:
                self._index_local_record(
                    rec.namespace, rec.key, rec.value, rec.visibility, rec.metadata
                )

    def _persist_state_unlocked(self) -> None:
        _sbp.ensure_ordinary_leaf_or_missing(self._state_path)
        gens = [
            {"namespace": ns, "resource": res, "generation": g}
            for (ns, res), g in sorted(self._generations.items())
        ]
        leases = [g.to_payload(expires_at_ms=g.expires_at_ms) for g in self._leases.values()]
        _st.atomic_bytes(
            self._state_path, _st.encode({"seq": self._seq, "generations": gens, "leases": leases})
        )

    def _record_path(self, namespace: str, key: str) -> Path:
        ns = _sbp.validate_identifier(namespace, "namespace")
        k = _sbp.validate_identifier(key, "key")
        return self.kv_dir / _st.sha(f"{ns}\0{k}".encode())

    def _emit_signal(self, namespace: str, key: str, version: int, kind: str) -> None:
        self._seq += 1
        self._signals.append(SignalEvent(self._seq, 1, namespace, key, version, kind))
        self._persist_state_unlocked()

    def _index_local_record(
        self, ns: str, key: str, value: bytes, vis: _sbp.SearchVisibility, meta: Mapping[str, Any]
    ) -> None:
        if vis == "Sealed":
            self._search_docs.pop((ns, key), None)
            return
        body = value.decode("utf-8", errors="replace") if vis == "Public" else ""
        self._search_docs[(ns, key)] = {
            "namespace": ns,
            "doc_id": key,
            "kind": str(meta.get("kind", "record")),
            "title": str(meta.get("title", key)),
            "body": body,
            "visibility": vis,
        }

    def _read_unlocked(self, path: Path) -> _sbp.BackendRecord | None:
        try:
            return _sbp.decode_local_record(_st.read_json(path))
        except FileNotFoundError:
            return None

    def put_record(
        self,
        namespace: str,
        key: str,
        value: bytes,
        *,
        expected_version: int | None = None,
        visibility: _sbp.SearchVisibility = "Public",
        metadata: Mapping[str, Any] | None = None,
    ) -> _sbp.BackendRecord:
        vis = _sbp.validate_visibility(visibility)
        if len(value) > _st.MAX_BYTES:
            raise ValueError("record payload exceeds per-object limit")
        meta = dict(metadata) if metadata else {}
        with self._lock:
            path = self._record_path(namespace, key)
            _sbp.ensure_ordinary_leaf_or_missing(path)
            cur = self._read_unlocked(path)
            cv = 0 if cur is None else cur.version
            if expected_version is not None and cv != expected_version:
                raise BackendConflictError(
                    f"version mismatch for {namespace}/{key}", status=409, code="conflict"
                )
            nv = cv + 1
            _st.atomic_bytes(
                path, _st.encode(_sbp.encode_local_record(namespace, key, nv, vis, meta, value))
            )
            self._index_local_record(namespace, key, value, vis, meta)
            self._emit_signal(namespace, key, nv, "put")
            return BackendRecord(namespace, key, value, nv, vis, meta)

    def get_record(self, namespace: str, key: str) -> _sbp.BackendRecord | None:
        with self._lock:
            return self._read_unlocked(self._record_path(namespace, key))

    def delete_record(
        self, namespace: str, key: str, *, expected_version: int | None = None
    ) -> bool:
        with self._lock:
            path = self._record_path(namespace, key)
            _sbp.ensure_ordinary_leaf_or_missing(path)
            cur = self._read_unlocked(path)
            cv = 0 if cur is None else cur.version
            if expected_version is not None and cv != expected_version:
                raise BackendConflictError(
                    f"version mismatch for {namespace}/{key}", status=409, code="conflict"
                )
            if cur is None:
                return False
            path.unlink()
            self._search_docs.pop((namespace, key), None)
            self._emit_signal(namespace, key, cv + 1, "delete")
            return True

    def put_blob(self, namespace: str, data: bytes) -> str:
        _sbp.validate_identifier(namespace, "namespace")
        return self._objects.put(data)

    def get_blob(self, namespace: str, sha256_hex: str) -> bytes | None:
        _sbp.validate_identifier(namespace, "namespace")
        k = _sbp.validate_sha256(sha256_hex)
        return self._objects.read(k) if self._objects.has(k) else None

    def has_blob(self, namespace: str, sha256_hex: str) -> bool:
        _sbp.validate_identifier(namespace, "namespace")
        return self._objects.has(_sbp.validate_sha256(sha256_hex))

    def search(
        self,
        query: str,
        *,
        namespace: str | None = None,
        kind: str | None = None,
        mode: str = "Bm25",
        limit: int = 20,
    ) -> list[_sbp.SearchHit]:
        del mode
        needle = query.strip().lower()
        if not needle or limit <= 0:
            return []
        with self._lock:
            docs = list(self._search_docs.values())
        hits = [
            SearchHit(d["namespace"], d["doc_id"], d["kind"], d["title"], float(c), d["visibility"])
            for d in docs
            if (namespace is None or d["namespace"] == namespace)
            and (kind is None or d["kind"] == kind)
            and (c := f"{d['title']}\n{d['body']}".lower().count(needle)) > 0
        ]
        hits.sort(key=lambda h: (-h.score, h.doc_id))
        return hits[:limit]

    def acquire_lease(
        self, namespace: str, resource: str, holder: str, ttl_ms: int
    ) -> _sbp.LeaseGrant:
        ns = _sbp.validate_identifier(namespace, "namespace")
        res = _sbp.validate_identifier(resource, "resource")
        if not holder or ttl_ms <= 0:
            raise ValueError("lease holder and positive ttl_ms required")
        with self._lock:
            now_v = _sbp.now_ms()
            if (ex := self._leases.get((ns, res))) is not None and ex.expires_at_ms > now_v:
                raise BackendContendedError(
                    f"lease already held for {ns}/{res}", status=409, code="lease_held"
                )
            gen = self._generations.get((ns, res), 0) + 1
            self._generations[(ns, res)] = gen
            g = LeaseGrant(ns, res, holder, gen, secrets.token_hex(16), now_v + ttl_ms)
            self._leases[(ns, res)] = g
            self._persist_state_unlocked()
            return g

    def renew_lease(self, grant: _sbp.LeaseGrant, ttl_ms: int) -> _sbp.LeaseGrant:
        if ttl_ms <= 0:
            raise ValueError("positive ttl_ms required")
        with self._lock:
            if not self.validate_lease(grant):
                raise BackendContendedError("lease is not active", status=409, code="lease_expired")
            ren = replace(grant, expires_at_ms=_sbp.now_ms() + ttl_ms)
            self._leases[(grant.namespace, grant.resource)] = ren
            self._persist_state_unlocked()
            return ren

    def release_lease(self, grant: _sbp.LeaseGrant) -> bool:
        with self._lock:
            if not self.validate_lease(grant):
                return False
            self._leases.pop((grant.namespace, grant.resource), None)
            self._persist_state_unlocked()
            return True

    def validate_lease(self, grant: _sbp.LeaseGrant) -> bool:
        with self._lock:
            cur = self._leases.get((grant.namespace, grant.resource))
            return cur == grant and cur.expires_at_ms > _sbp.now_ms()

    def stream_signals(
        self,
        namespace: str,
        *,
        prefix: str | None = None,
        since_seq: int | None = None,
        mode: str = "each",
        timeout: float | None = None,
    ) -> Iterator[_sbp.SignalEvent]:
        del mode, timeout
        ns = _sbp.validate_identifier(namespace, "namespace")
        with self._lock:
            snap = list(self._signals)
        for ev in snap:
            if (
                ev.namespace == ns
                and (since_seq is None or ev.seq > since_seq)
                and (prefix is None or ev.key.startswith(prefix))
            ):
                yield ev

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "backend": "embedded-file",
            "root": str(self.root),
            "seq": self._seq,
        }


class MembusV2Backend:
    """Pure-stdlib HTTP/JSON/SSE StorageBackend client targeting membus v2 /v2/* routes."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 15.0) -> None:
        self._parsed = _sbp.validate_endpoint_url(base_url)
        self.base_url, self._token, self.timeout = base_url.rstrip("/"), token, timeout
        self._pool = _sbp.KeepAliveHTTPPool(self._open_connection)

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> MembusV2Backend:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        out = {"Authorization": f"Bearer {self._token}"} if self._token is not None else {}
        if extra:
            out.update(extra)
        return out

    def _open_connection(
        self, timeout: float | None = None
    ) -> http.client.HTTPConnection | http.client.HTTPSConnection:
        host = self._parsed.hostname or "127.0.0.1"
        port = self._parsed.port or (443 if self._parsed.scheme == "https" else 80)
        eff = self.timeout if timeout is None else timeout
        cls = (
            http.client.HTTPSConnection
            if self._parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        return cls(host, port, timeout=eff)

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        max_bytes: int = _st.MAX_BYTES,
    ) -> tuple[int, bytes]:
        extra = {"Content-Type": content_type} if content_type else None
        fp = f"{self._parsed.path.rstrip('/')}{path}"
        return self._pool.request(
            method, fp, headers=self._headers(extra), body=body, max_bytes=max_bytes
        )

    def _request_json(self, method: str, path: str, payload: Any | None = None) -> Any:
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode("utf-8")
        _, raw = self._request_raw(
            method, path, body=body, content_type=None if payload is None else "application/json"
        )
        if not raw:
            return None
        try:
            return _st.strict_json_loads(raw, path, require_object=False)
        except (UnicodeDecodeError, ValueError) as err:
            raise StorageBackendError(f"invalid JSON from membus: {err}", code="protocol") from None

    def put_record(
        self,
        namespace: str,
        key: str,
        value: bytes,
        *,
        expected_version: int | None = None,
        visibility: _sbp.SearchVisibility = "Public",
        metadata: Mapping[str, Any] | None = None,
    ) -> _sbp.BackendRecord:
        ns = _sbp.validate_identifier(namespace, "namespace")
        k = _sbp.validate_identifier(key, "key")
        vis = _sbp.validate_visibility(visibility)
        if len(value) > _st.MAX_BYTES:
            raise ValueError("record payload exceeds per-object limit")
        meta = dict(metadata) if metadata else {}
        body: dict[str, Any] = {
            "ack": "Fast",
            "expected": _sbp.encode_expected_version(expected_version),
            "value_b64": base64.b64encode(value).decode("ascii"),
            "visibility": vis,
            "search_op": _sbp.build_search_op(ns, k, value, vis, meta),
        }
        p = f"/v2/kv/{_sbp.quote_segment(ns)}/{_sbp.quote_segment(k)}"
        vers = self._request_json("PUT", p, body).get("versions", [1])
        return BackendRecord(ns, k, value, int(vers[0]) if vers else 1, vis, meta)

    def get_record(self, namespace: str, key: str) -> _sbp.BackendRecord | None:
        ns = _sbp.validate_identifier(namespace, "namespace")
        k = _sbp.validate_identifier(key, "key")
        try:
            res = self._request_json(
                "GET", f"/v2/kv/{_sbp.quote_segment(ns)}/{_sbp.quote_segment(k)}"
            )
        except StorageBackendError as err:
            if err.status == 404:
                return None
            raise
        rv = res["value"]
        val = bytes(rv) if isinstance(rv, list) else base64.b64decode(str(rv))
        vis = _sbp.validate_visibility(str(res.get("visibility", "Public")))
        return BackendRecord(
            ns,
            k,
            val,
            int(res["meta"]["version"]),
            vis,
            dict(res.get("metadata", {})),
            visibility_reported="visibility" in res,
        )

    def delete_record(
        self, namespace: str, key: str, *, expected_version: int | None = None
    ) -> bool:
        ns = _sbp.validate_identifier(namespace, "namespace")
        k = _sbp.validate_identifier(key, "key")
        body = {
            "ack": "Fast",
            "expected": _sbp.encode_expected_version(expected_version),
            "search_op": {"Delete": {"namespace": ns, "doc_id": k}},
        }
        try:
            self._request_json(
                "DELETE", f"/v2/kv/{_sbp.quote_segment(ns)}/{_sbp.quote_segment(k)}", body
            )
            return True
        except StorageBackendError as err:
            if err.status == 404:
                return False
            raise

    def put_blob(self, namespace: str, data: bytes) -> str:
        ns = _sbp.validate_identifier(namespace, "namespace")
        if len(data) > _st.MAX_BYTES:
            raise ValueError("blob exceeds per-object limit")
        exp = _st.sha(data)
        _, raw = self._request_raw(
            "PUT",
            f"/v2/blobs/{_sbp.quote_segment(ns)}?ack=fast",
            body=data,
            content_type="application/octet-stream",
        )
        d = _sbp.validate_sha256(str(_st.strict_json_loads(raw, "put_blob")["sha256_hex"]))
        if d != exp:
            raise StorageBackendError("membus returned mismatched blob digest", code="integrity")
        return d

    def get_blob(self, namespace: str, sha256_hex: str) -> bytes | None:
        ns = _sbp.validate_identifier(namespace, "namespace")
        d = _sbp.validate_sha256(sha256_hex)
        try:
            _, raw = self._request_raw(
                "GET", f"/v2/blobs/{_sbp.quote_segment(ns)}/{_sbp.quote_segment(d)}"
            )
        except StorageBackendError as err:
            if err.status == 404:
                return None
            raise
        if _st.sha(raw) != d:
            raise StorageBackendError("membus blob digest mismatch", code="integrity")
        return raw

    def has_blob(self, namespace: str, sha256_hex: str) -> bool:
        ns = _sbp.validate_identifier(namespace, "namespace")
        d = _sbp.validate_sha256(sha256_hex)
        try:
            st, _ = self._request_raw(
                "HEAD", f"/v2/blobs/{_sbp.quote_segment(ns)}/{_sbp.quote_segment(d)}"
            )
            return st == 200
        except StorageBackendError as err:
            if err.status == 404:
                return False
            raise

    def search(
        self,
        query: str,
        *,
        namespace: str | None = None,
        kind: str | None = None,
        mode: str = "Bm25",
        limit: int = 20,
    ) -> list[_sbp.SearchHit]:
        if not query.strip() or limit <= 0:
            return []
        res = self._request_json(
            "POST",
            "/v2/search/query",
            {"query": query, "mode": mode, "namespace": namespace, "kind": kind, "limit": limit},
        )
        return [
            SearchHit(
                str(r["namespace"]),
                str(r["doc_id"]),
                str(r.get("kind", "record")),
                str(r.get("title", r["doc_id"])),
                float(r.get("score", 0.0)),
                vis,
            )
            for r in res.get("hits", ())
            if (vis := _sbp.validate_visibility(str(r.get("visibility", "Public")))) != "Sealed"
        ]

    def acquire_lease(
        self, namespace: str, resource: str, holder: str, ttl_ms: int
    ) -> _sbp.LeaseGrant:
        b = {"namespace": namespace, "resource": resource, "holder": holder, "ttl_ms": ttl_ms}
        return LeaseGrant.from_payload(self._request_json("POST", "/v2/leases/acquire", b))

    def renew_lease(self, grant: _sbp.LeaseGrant, ttl_ms: int) -> _sbp.LeaseGrant:
        b = grant.to_payload(ttl_ms=ttl_ms)
        return LeaseGrant.from_payload(self._request_json("POST", "/v2/leases/renew", b))

    def release_lease(self, grant: _sbp.LeaseGrant) -> bool:
        try:
            return bool(self._request_json("POST", "/v2/leases/release", grant.to_payload())["ok"])
        except StorageBackendError as err:
            if err.status == 404 or isinstance(err, BackendContendedError):
                return False
            raise

    def validate_lease(self, grant: _sbp.LeaseGrant) -> bool:
        return bool(self._request_json("POST", "/v2/leases/validate", grant.to_payload())["ok"])

    def stream_signals(
        self,
        namespace: str,
        *,
        prefix: str | None = None,
        since_seq: int | None = None,
        mode: str = "each",
        timeout: float | None = None,
    ) -> Iterator[_sbp.SignalEvent]:
        ns = _sbp.validate_identifier(namespace, "namespace")
        params: dict[str, str] = {"mode": mode}
        if prefix is not None:
            params["prefix"] = prefix
        if since_seq is not None:
            params["since_seq"] = str(since_seq)
        qs = urllib.parse.urlencode(params)
        path = f"{self._parsed.path.rstrip('/')}/v2/signals/{_sbp.quote_segment(ns)}/stream?{qs}"
        conn = self._open_connection(timeout=timeout)
        try:
            conn.request("GET", path, headers=self._headers({"Accept": "text/event-stream"}))
            resp = conn.getresponse()
            if resp.status != 200:
                raise _sbp.map_http_error(resp.status, resp.read(65536))
            yield from _sbp.parse_sse_signals(resp)
        except StorageBackendError:
            raise
        except (http.client.HTTPException, TimeoutError, OSError, UnicodeDecodeError) as err:
            raise BackendUnavailableError(str(err), status=0, code="unavailable") from None
        finally:
            conn.close()

    def register_collection(
        self,
        namespace: str,
        *,
        schema_version: int = 1,
        schema_bytes: bytes = b"{}",
        store_profile: str = "Mixed",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        pl = _sbp.build_collection_registration(
            namespace,
            schema_version=schema_version,
            schema_bytes=schema_bytes,
            store_profile=store_profile,
            expected_version=expected_version,
        )
        return dict(self._request_json("POST", "/v2/catalog/collections", pl))

    def get_collection(self, namespace: str) -> dict[str, Any] | None:
        ns = _sbp.validate_identifier(namespace, "namespace")
        try:
            return dict(
                self._request_json("GET", f"/v2/catalog/collections/{_sbp.quote_segment(ns)}")
            )
        except StorageBackendError as err:
            if err.status == 404:
                return None
            raise

    def _mig(self, namespace: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        ns = _sbp.validate_identifier(namespace, "namespace")
        return dict(
            self._request_json(
                "POST",
                f"/v2/catalog/collections/{_sbp.quote_segment(ns)}/migrations/{action}",
                body,
            )
        )

    def begin_migration(
        self,
        namespace: str,
        *,
        expected_version: int,
        target_version: int,
        target_sha256: str,
        target_schema_bytes: bytes = b"",
    ) -> dict[str, Any]:
        return self._mig(
            namespace,
            "begin",
            {
                "expected_version": expected_version,
                "target_version": target_version,
                "target_sha256": list(bytes.fromhex(_sbp.validate_sha256(target_sha256))),
                "target_schema_bytes": list(target_schema_bytes),
            },
        )

    def advance_migration(
        self, namespace: str, *, target_version: int, verified_through: int
    ) -> dict[str, Any]:
        return self._mig(
            namespace,
            "advance",
            {"target_version": target_version, "verified_through": verified_through},
        )

    def commit_migration(
        self, namespace: str, *, target_version: int, verified_through: int
    ) -> dict[str, Any]:
        return self._mig(
            namespace,
            "commit",
            {"target_version": target_version, "verified_through": verified_through},
        )

    def abort_migration(
        self, namespace: str, *, target_version: int, reason: str
    ) -> dict[str, Any]:
        del reason
        return self._mig(namespace, "abort", {"target_version": target_version})

    def health(self) -> dict[str, Any]:
        return dict(self._request_json("GET", "/v2/health"))
