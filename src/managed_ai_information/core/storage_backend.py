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

from managed_ai_information.core.storage import (
    MAX_BYTES,
    ObjectStore,
    atomic_bytes,
    encode,
    is_ordinary_file_stat,
    read_json,
    sha,
    strict_json_loads,
)
from managed_ai_information.core.storage_backend_protocol import (
    BackendConflictError,
    BackendContendedError,
    BackendRecord,
    BackendUnavailableError,
    LeaseGrant,
    SearchHit,
    SearchVisibility,
    SignalEvent,
    StorageBackend,
    StorageBackendError,
    build_collection_registration,
    build_search_op,
    decode_local_record,
    encode_expected_version,
    encode_local_record,
    ensure_ordinary_dir,
    ensure_ordinary_leaf_or_missing,
    map_http_error,
    now_ms,
    parse_sse_signals,
    quote_segment,
    validate_endpoint_url,
    validate_identifier,
    validate_sha256,
    validate_visibility,
)

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
        self.root = ensure_ordinary_dir(Path(root))
        self.objects_dir = ensure_ordinary_dir(self.root / "exchange/objects")
        self.kv_dir = ensure_ordinary_dir(self.root / "work/backend-kv")
        self._state_path = self.root / "work/backend-state.json"
        self._objects = ObjectStore(self.objects_dir)
        self._lock = threading.RLock()
        self._leases: dict[tuple[str, str], LeaseGrant] = {}
        self._generations: dict[tuple[str, str], int] = {}
        self._search_docs: dict[tuple[str, str], dict[str, Any]] = {}
        self._signals: deque[SignalEvent] = deque(maxlen=max_signals)
        self._seq = 0
        self._reload_persisted_state()

    def _reload_persisted_state(self) -> None:
        if self._state_path.exists() or self._state_path.is_symlink():
            doc = read_json(self._state_path)
            self._seq = int(doc.get("seq", 0))
            for row in doc.get("generations", ()):
                self._generations[(str(row["namespace"]), str(row["resource"]))] = int(
                    row["generation"]
                )
            for row in doc.get("leases", ()):
                grant = LeaseGrant.from_payload(row)
                self._leases[(grant.namespace, grant.resource)] = grant
        for entry in sorted(self.kv_dir.iterdir(), key=lambda p: p.name):
            if entry.name.startswith("."):
                continue
            info = entry.lstat()
            if not is_ordinary_file_stat(info):
                raise ValueError(f"ordinary KV record file required: {entry}")
            rec = self._read_unlocked(entry)
            if rec is not None:
                self._index_local_record(
                    rec.namespace, rec.key, rec.value, rec.visibility, rec.metadata
                )

    def _persist_state_unlocked(self) -> None:
        ensure_ordinary_leaf_or_missing(self._state_path)
        gens = [
            {"namespace": ns, "resource": res, "generation": g}
            for (ns, res), g in sorted(self._generations.items())
        ]
        leases = [g.to_payload(expires_at_ms=g.expires_at_ms) for g in self._leases.values()]
        atomic_bytes(
            self._state_path, encode({"seq": self._seq, "generations": gens, "leases": leases})
        )

    def _record_path(self, namespace: str, key: str) -> Path:
        ns = validate_identifier(namespace, "namespace")
        k = validate_identifier(key, "key")
        return self.kv_dir / sha(f"{ns}\0{k}".encode())

    def _emit_signal(self, namespace: str, key: str, version: int, kind: str) -> None:
        self._seq += 1
        self._signals.append(SignalEvent(self._seq, 1, namespace, key, version, kind))
        self._persist_state_unlocked()

    def _index_local_record(
        self, ns: str, key: str, value: bytes, vis: SearchVisibility, meta: Mapping[str, Any]
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

    def _read_unlocked(self, path: Path) -> BackendRecord | None:
        try:
            return decode_local_record(read_json(path))
        except FileNotFoundError:
            return None

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
        vis = validate_visibility(visibility)
        if len(value) > MAX_BYTES:
            raise ValueError("record payload exceeds per-object limit")
        meta = dict(metadata) if metadata else {}
        with self._lock:
            path = self._record_path(namespace, key)
            ensure_ordinary_leaf_or_missing(path)
            current = self._read_unlocked(path)
            cur_ver = 0 if current is None else current.version
            if expected_version is not None and cur_ver != expected_version:
                raise BackendConflictError(
                    f"version mismatch for {namespace}/{key}", status=409, code="conflict"
                )
            next_ver = cur_ver + 1
            atomic_bytes(
                path,
                encode(encode_local_record(namespace, key, next_ver, vis, meta, value)),
            )
            self._index_local_record(namespace, key, value, vis, meta)
            self._emit_signal(namespace, key, next_ver, "put")
            return BackendRecord(namespace, key, value, next_ver, vis, meta)

    def get_record(self, namespace: str, key: str) -> BackendRecord | None:
        with self._lock:
            return self._read_unlocked(self._record_path(namespace, key))

    def delete_record(
        self, namespace: str, key: str, *, expected_version: int | None = None
    ) -> bool:
        with self._lock:
            path = self._record_path(namespace, key)
            ensure_ordinary_leaf_or_missing(path)
            current = self._read_unlocked(path)
            cur_ver = 0 if current is None else current.version
            if expected_version is not None and cur_ver != expected_version:
                raise BackendConflictError(
                    f"version mismatch for {namespace}/{key}", status=409, code="conflict"
                )
            if current is None:
                return False
            path.unlink()
            self._search_docs.pop((namespace, key), None)
            self._emit_signal(namespace, key, cur_ver + 1, "delete")
            return True

    def put_blob(self, namespace: str, data: bytes) -> str:
        validate_identifier(namespace, "namespace")
        return self._objects.put(data)

    def get_blob(self, namespace: str, sha256_hex: str) -> bytes | None:
        validate_identifier(namespace, "namespace")
        key = validate_sha256(sha256_hex)
        return self._objects.read(key) if self._objects.has(key) else None

    def has_blob(self, namespace: str, sha256_hex: str) -> bool:
        validate_identifier(namespace, "namespace")
        return self._objects.has(validate_sha256(sha256_hex))

    def search(
        self,
        query: str,
        *,
        namespace: str | None = None,
        kind: str | None = None,
        mode: str = "Bm25",
        limit: int = 20,
    ) -> list[SearchHit]:
        del mode
        needle = query.strip().lower()
        if not needle or limit <= 0:
            return []
        with self._lock:
            docs = list(self._search_docs.values())
        hits = [
            SearchHit(
                d["namespace"], d["doc_id"], d["kind"], d["title"], float(cnt), d["visibility"]
            )
            for d in docs
            if (namespace is None or d["namespace"] == namespace)
            and (kind is None or d["kind"] == kind)
            and (cnt := f"{d['title']}\n{d['body']}".lower().count(needle)) > 0
        ]
        hits.sort(key=lambda h: (-h.score, h.doc_id))
        return hits[:limit]

    def acquire_lease(self, namespace: str, resource: str, holder: str, ttl_ms: int) -> LeaseGrant:
        ns = validate_identifier(namespace, "namespace")
        res = validate_identifier(resource, "resource")
        if not holder or ttl_ms <= 0:
            raise ValueError("lease holder and positive ttl_ms required")
        with self._lock:
            now_val = now_ms()
            existing = self._leases.get((ns, res))
            if existing is not None and existing.expires_at_ms > now_val:
                raise BackendContendedError(
                    f"lease already held for {ns}/{res}", status=409, code="lease_held"
                )
            gen = self._generations.get((ns, res), 0) + 1
            self._generations[(ns, res)] = gen
            grant = LeaseGrant(ns, res, holder, gen, secrets.token_hex(16), now_val + ttl_ms)
            self._leases[(ns, res)] = grant
            self._persist_state_unlocked()
            return grant

    def renew_lease(self, grant: LeaseGrant, ttl_ms: int) -> LeaseGrant:
        if ttl_ms <= 0:
            raise ValueError("positive ttl_ms required")
        with self._lock:
            if not self.validate_lease(grant):
                raise BackendContendedError("lease is not active", status=409, code="lease_expired")
            renewed = replace(grant, expires_at_ms=now_ms() + ttl_ms)
            self._leases[(grant.namespace, grant.resource)] = renewed
            self._persist_state_unlocked()
            return renewed

    def release_lease(self, grant: LeaseGrant) -> bool:
        with self._lock:
            if not self.validate_lease(grant):
                return False
            self._leases.pop((grant.namespace, grant.resource), None)
            self._persist_state_unlocked()
            return True

    def validate_lease(self, grant: LeaseGrant) -> bool:
        with self._lock:
            cur = self._leases.get((grant.namespace, grant.resource))
            return cur == grant and cur.expires_at_ms > now_ms()

    def stream_signals(
        self,
        namespace: str,
        *,
        prefix: str | None = None,
        since_seq: int | None = None,
        mode: str = "each",
        timeout: float | None = None,
    ) -> Iterator[SignalEvent]:
        del mode, timeout
        ns = validate_identifier(namespace, "namespace")
        with self._lock:
            snapshot = list(self._signals)
        for ev in snapshot:
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
        self._parsed = validate_endpoint_url(base_url)
        self.base_url, self._token, self.timeout = base_url.rstrip("/"), token, timeout

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
        max_bytes: int = MAX_BYTES,
    ) -> tuple[int, bytes]:
        extra = {"Content-Type": content_type} if content_type else None
        full_path = f"{self._parsed.path.rstrip('/')}{path}"
        conn = self._open_connection()
        try:
            conn.request(method, full_path, body=body, headers=self._headers(extra))
            resp = conn.getresponse()
            if resp.status >= 400:
                raise map_http_error(resp.status, resp.read(65536))
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise StorageBackendError("membus response exceeds byte limit", code="overflow")
            return resp.status, data
        except StorageBackendError:
            raise
        except (http.client.HTTPException, TimeoutError, OSError) as err:
            raise BackendUnavailableError(str(err), status=0, code="unavailable") from None
        finally:
            conn.close()

    def _request_json(self, method: str, path: str, payload: Any | None = None) -> Any:
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode("utf-8")
        _, raw = self._request_raw(
            method, path, body=body, content_type=None if payload is None else "application/json"
        )
        if not raw:
            return None
        try:
            return strict_json_loads(raw, path, require_object=False)
        except (UnicodeDecodeError, ValueError) as err:
            raise StorageBackendError(f"invalid JSON from membus: {err}", code="protocol") from None

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
        ns = validate_identifier(namespace, "namespace")
        k = validate_identifier(key, "key")
        vis = validate_visibility(visibility)
        if len(value) > MAX_BYTES:
            raise ValueError("record payload exceeds per-object limit")
        meta = dict(metadata) if metadata else {}
        body: dict[str, Any] = {
            "ack": "Fast",
            "expected": encode_expected_version(expected_version),
            "value_b64": base64.b64encode(value).decode("ascii"),
            "visibility": vis,
            "search_op": build_search_op(ns, k, value, vis, meta),
        }
        res = self._request_json("PUT", f"/v2/kv/{quote_segment(ns)}/{quote_segment(k)}", body)
        vers = res.get("versions", [1])
        return BackendRecord(ns, k, value, int(vers[0]) if vers else 1, vis, meta)

    def get_record(self, namespace: str, key: str) -> BackendRecord | None:
        ns, k = validate_identifier(namespace, "namespace"), validate_identifier(key, "key")
        try:
            res = self._request_json("GET", f"/v2/kv/{quote_segment(ns)}/{quote_segment(k)}")
        except StorageBackendError as err:
            if err.status == 404:
                return None
            raise
        raw_val = res["value"]
        val = bytes(raw_val) if isinstance(raw_val, list) else base64.b64decode(str(raw_val))
        vis = validate_visibility(str(res.get("visibility", "Public")))
        return BackendRecord(
            ns, k, val, int(res["meta"]["version"]), vis, dict(res.get("metadata", {}))
        )

    def delete_record(
        self, namespace: str, key: str, *, expected_version: int | None = None
    ) -> bool:
        ns, k = validate_identifier(namespace, "namespace"), validate_identifier(key, "key")
        body = {
            "ack": "Fast",
            "expected": encode_expected_version(expected_version),
            "search_op": {"Delete": {"namespace": ns, "doc_id": k}},
        }
        try:
            self._request_json("DELETE", f"/v2/kv/{quote_segment(ns)}/{quote_segment(k)}", body)
            return True
        except StorageBackendError as err:
            if err.status == 404:
                return False
            raise

    def put_blob(self, namespace: str, data: bytes) -> str:
        ns = validate_identifier(namespace, "namespace")
        if len(data) > MAX_BYTES:
            raise ValueError("blob exceeds per-object limit")
        expected_sha = sha(data)
        _, raw = self._request_raw(
            "PUT",
            f"/v2/blobs/{quote_segment(ns)}?ack=fast",
            body=data,
            content_type="application/octet-stream",
        )
        digest = validate_sha256(str(strict_json_loads(raw, "put_blob")["sha256_hex"]))
        if digest != expected_sha:
            raise StorageBackendError("membus returned mismatched blob digest", code="integrity")
        return digest

    def get_blob(self, namespace: str, sha256_hex: str) -> bytes | None:
        ns, digest = validate_identifier(namespace, "namespace"), validate_sha256(sha256_hex)
        try:
            _, raw = self._request_raw(
                "GET", f"/v2/blobs/{quote_segment(ns)}/{quote_segment(digest)}"
            )
        except StorageBackendError as err:
            if err.status == 404:
                return None
            raise
        if sha(raw) != digest:
            raise StorageBackendError("membus blob digest mismatch", code="integrity")
        return raw

    def has_blob(self, namespace: str, sha256_hex: str) -> bool:
        ns, digest = validate_identifier(namespace, "namespace"), validate_sha256(sha256_hex)
        try:
            status, _ = self._request_raw(
                "HEAD", f"/v2/blobs/{quote_segment(ns)}/{quote_segment(digest)}"
            )
            return status == 200
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
    ) -> list[SearchHit]:
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
            if (vis := validate_visibility(str(r.get("visibility", "Public")))) != "Sealed"
        ]

    def acquire_lease(self, namespace: str, resource: str, holder: str, ttl_ms: int) -> LeaseGrant:
        body = {"namespace": namespace, "resource": resource, "holder": holder, "ttl_ms": ttl_ms}
        return LeaseGrant.from_payload(self._request_json("POST", "/v2/leases/acquire", body))

    def renew_lease(self, grant: LeaseGrant, ttl_ms: int) -> LeaseGrant:
        return LeaseGrant.from_payload(
            self._request_json("POST", "/v2/leases/renew", grant.to_payload(ttl_ms=ttl_ms))
        )

    def release_lease(self, grant: LeaseGrant) -> bool:
        try:
            return bool(self._request_json("POST", "/v2/leases/release", grant.to_payload())["ok"])
        except StorageBackendError as err:
            if err.status == 404 or isinstance(err, BackendContendedError):
                return False
            raise

    def validate_lease(self, grant: LeaseGrant) -> bool:
        return bool(self._request_json("POST", "/v2/leases/validate", grant.to_payload())["ok"])

    def stream_signals(
        self,
        namespace: str,
        *,
        prefix: str | None = None,
        since_seq: int | None = None,
        mode: str = "each",
        timeout: float | None = None,
    ) -> Iterator[SignalEvent]:
        ns = validate_identifier(namespace, "namespace")
        params: dict[str, str] = {"mode": mode}
        if prefix is not None:
            params["prefix"] = prefix
        if since_seq is not None:
            params["since_seq"] = str(since_seq)
        qs = urllib.parse.urlencode(params)
        path = f"{self._parsed.path.rstrip('/')}/v2/signals/{quote_segment(ns)}/stream?{qs}"
        conn = self._open_connection(timeout=timeout)
        try:
            conn.request("GET", path, headers=self._headers({"Accept": "text/event-stream"}))
            resp = conn.getresponse()
            if resp.status != 200:
                raise map_http_error(resp.status, resp.read(65536))
            yield from parse_sse_signals(resp)
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
        payload = build_collection_registration(
            namespace,
            schema_version=schema_version,
            schema_bytes=schema_bytes,
            store_profile=store_profile,
            expected_version=expected_version,
        )
        return dict(self._request_json("POST", "/v2/catalog/collections", payload))

    def get_collection(self, namespace: str) -> dict[str, Any] | None:
        ns = validate_identifier(namespace, "namespace")
        try:
            return dict(self._request_json("GET", f"/v2/catalog/collections/{quote_segment(ns)}"))
        except StorageBackendError as err:
            if err.status == 404:
                return None
            raise

    def _migration_post(self, namespace: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        ns = validate_identifier(namespace, "namespace")
        url_path = f"/v2/catalog/collections/{quote_segment(ns)}/migrations/{action}"
        return dict(self._request_json("POST", url_path, body))

    def begin_migration(
        self,
        namespace: str,
        *,
        expected_version: int,
        target_version: int,
        target_sha256: str,
        target_schema_bytes: bytes = b"",
    ) -> dict[str, Any]:
        sha_bytes = list(bytes.fromhex(validate_sha256(target_sha256)))
        return self._migration_post(
            namespace,
            "begin",
            {
                "expected_version": expected_version,
                "target_version": target_version,
                "target_sha256": sha_bytes,
                "target_schema_bytes": list(target_schema_bytes),
            },
        )

    def advance_migration(
        self, namespace: str, *, target_version: int, verified_through: int
    ) -> dict[str, Any]:
        return self._migration_post(
            namespace,
            "advance",
            {"target_version": target_version, "verified_through": verified_through},
        )

    def commit_migration(
        self, namespace: str, *, target_version: int, verified_through: int
    ) -> dict[str, Any]:
        return self._migration_post(
            namespace,
            "commit",
            {"target_version": target_version, "verified_through": verified_through},
        )

    def abort_migration(
        self, namespace: str, *, target_version: int, reason: str
    ) -> dict[str, Any]:
        del reason
        return self._migration_post(namespace, "abort", {"target_version": target_version})

    def health(self) -> dict[str, Any]:
        return dict(self._request_json("GET", "/v2/health"))
