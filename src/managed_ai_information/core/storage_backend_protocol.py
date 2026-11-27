"""Shared protocol, dataclasses, typed errors, wire helpers, and validators for StorageBackend."""

import base64
import http.client
import ipaddress
import re
import secrets
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast, runtime_checkable

from managed_ai_information.core import storage as _st

SearchVisibility = Literal["Public", "MetadataOnly", "Sealed"]
type _Conn = http.client.HTTPConnection | http.client.HTTPSConnection

_VIS = frozenset({"Public", "MetadataOnly", "Sealed"})
_SHA_RE = re.compile(r"^[0-9a-f]{64}\Z")
_SEG_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,256}\Z")


class StorageBackendError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, code: str = "backend_error") -> None:
        super().__init__(message)
        self.status, self.code = status, code


class BackendConflictError(StorageBackendError):
    pass


class BackendContendedError(StorageBackendError):
    pass


class BackendUnavailableError(StorageBackendError):
    pass


@dataclass(frozen=True, slots=True)
class BackendRecord:
    namespace: str
    key: str
    value: bytes
    version: int
    visibility: SearchVisibility = "Public"
    metadata: dict[str, Any] = field(default_factory=dict)
    visibility_reported: bool = True


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    namespace: str
    resource: str
    holder: str
    generation: int
    token: str
    expires_at_ms: int

    def to_payload(self, **extra: Any) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "resource": self.resource,
            "holder": self.holder,
            "generation": self.generation,
            "token": self.token,
            **extra,
        }

    @classmethod
    def from_payload(cls, d: Mapping[str, Any]) -> LeaseGrant:
        return cls(
            str(d["namespace"]),
            str(d["resource"]),
            str(d["holder"]),
            int(d["generation"]),
            str(d["token"]),
            int(d["expires_at_ms"]),
        )


@dataclass(frozen=True, slots=True)
class SearchHit:
    namespace: str
    doc_id: str
    kind: str
    title: str
    score: float
    visibility: SearchVisibility = "Public"


@dataclass(frozen=True, slots=True)
class SignalEvent:
    seq: int
    epoch: int
    namespace: str
    key: str
    version: int
    kind: str
    value_hint: bytes | None = None


@runtime_checkable
class StorageBackend(Protocol):
    def put_record(
        self,
        namespace: str,
        key: str,
        value: bytes,
        *,
        expected_version: int | None = None,
        visibility: SearchVisibility = "Public",
        metadata: Mapping[str, Any] | None = None,
    ) -> BackendRecord: ...
    def get_record(self, namespace: str, key: str) -> BackendRecord | None: ...
    def delete_record(
        self, namespace: str, key: str, *, expected_version: int | None = None
    ) -> bool: ...
    def put_blob(self, namespace: str, data: bytes) -> str: ...
    def get_blob(self, namespace: str, sha256_hex: str) -> bytes | None: ...
    def has_blob(self, namespace: str, sha256_hex: str) -> bool: ...
    def search(
        self,
        query: str,
        *,
        namespace: str | None = None,
        kind: str | None = None,
        mode: str = "Bm25",
        limit: int = 20,
    ) -> list[SearchHit]: ...
    def acquire_lease(
        self, namespace: str, resource: str, holder: str, ttl_ms: int
    ) -> LeaseGrant: ...
    def renew_lease(self, grant: LeaseGrant, ttl_ms: int) -> LeaseGrant: ...
    def release_lease(self, grant: LeaseGrant) -> bool: ...
    def validate_lease(self, grant: LeaseGrant) -> bool: ...
    def stream_signals(
        self,
        namespace: str,
        *,
        prefix: str | None = None,
        since_seq: int | None = None,
        mode: str = "each",
        timeout: float | None = None,
    ) -> Iterator[SignalEvent]: ...
    def health(self) -> dict[str, Any]: ...


def validate_sha256(digest: str) -> str:
    if not isinstance(digest, str) or not _SHA_RE.match(digest):
        raise ValueError(f"invalid SHA-256 digest: {digest!r}")
    return digest


def validate_identifier(value: str, label: str) -> str:
    bad = not isinstance(value, str) or not value or ".." in value or value.startswith("/")
    if bad or not _SEG_RE.match(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def validate_visibility(visibility: str) -> SearchVisibility:
    if visibility not in _VIS:
        raise ValueError(f"unsupported search visibility: {visibility!r}")
    return cast(SearchVisibility, visibility)


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_endpoint_url(base_url: str) -> urllib.parse.SplitResult:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("membus endpoint URL must be a non-empty string")
    if any(ord(c) < 32 or ord(c) == 127 for c in base_url):
        raise ValueError("membus endpoint URL contains control characters")
    p = urllib.parse.urlsplit(base_url.rstrip("/"))
    if p.scheme not in ("http", "https"):
        raise ValueError(f"unsupported membus endpoint scheme: {p.scheme!r}")
    if p.username is not None or p.password is not None or not p.hostname:
        raise ValueError("membus endpoint URL must specify host without embedded credentials")
    if p.scheme == "http" and not is_loopback_host(p.hostname):
        raise ValueError(f"non-loopback membus endpoint requires https: {p.hostname!r}")
    return p


def now_ms() -> int:
    return int(time.time() * 1000)


def quote_segment(seg: str) -> str:
    return urllib.parse.quote(seg, safe="")


def encode_expected_version(v: int | None) -> Any:
    if v is None:
        return "Any"
    if isinstance(v, bool) or v < 0:
        raise ValueError(f"invalid expected_version: {v!r}")
    return "Absent" if v == 0 else {"Version": v}


def build_search_op(
    ns: str, key: str, value: bytes, vis: SearchVisibility, meta: Mapping[str, Any]
) -> dict[str, Any]:
    if vis == "Sealed":
        return {"Delete": {"namespace": ns, "doc_id": key}}
    return {
        "Index": {
            "namespace": ns,
            "doc_id": key,
            "kind": str(meta.get("kind", "record")),
            "title": str(meta.get("title", key)),
            "tags": list(meta.get("tags", ())),
            "body": value.decode("utf-8", errors="replace") if vis == "Public" else "",
            "visibility": vis,
        }
    }


def build_collection_registration(
    namespace: str,
    *,
    schema_version: int = 1,
    schema_bytes: bytes = b"{}",
    store_profile: str = "Mixed",
    expected_version: int | None = None,
) -> dict[str, Any]:
    ns = validate_identifier(namespace, "namespace")
    if isinstance(schema_version, bool) or schema_version < 1:
        raise ValueError(f"schema_version must be >= 1, got {schema_version!r}")
    return {
        "expected_version": expected_version,
        "descriptor": {
            "namespace": ns,
            "store_profile": store_profile,
            "schema_version": schema_version,
            "schema_sha256": list(bytes.fromhex(_st.sha(schema_bytes))),
            "schema_bytes": base64.b64encode(schema_bytes).decode("ascii"),
            "declared_indexes": [],
            "retention_policy": {"max_age_secs": None, "max_entries": None},
            "migration_state": "Active",
            "compatible_versions": [schema_version],
            "compatibility_mode": "Strict",
        },
    }


def map_http_error(status: int, raw: bytes) -> StorageBackendError:
    code, msg = "http_error", raw.decode("utf-8", errors="replace")
    try:
        p = _st.strict_json_loads(raw, "membus_error", require_object=False)
        if isinstance(p, dict):
            code, msg = str(p.get("error", code)), str(p.get("message", msg))
    except UnicodeDecodeError, ValueError:
        pass
    low = code.lower()
    if status in (423, 429) or "lease" in low or "contend" in low:
        return BackendContendedError(msg, status=status, code=code)
    if status == 409 or "conflict" in low or "version" in low:
        return BackendConflictError(msg, status=status, code=code)
    if status in (502, 503, 504):
        return BackendUnavailableError(msg, status=status, code=code)
    return StorageBackendError(msg, status=status, code=code)


def parse_sse_signals(stream: Any, *, max_line_bytes: int = _st.MAX_BYTES) -> Iterator[SignalEvent]:
    ev = "message"
    lines: list[str] = []
    while rl := stream.readline(max_line_bytes + 1):
        if len(rl) > max_line_bytes:
            raise StorageBackendError("SSE line exceeds byte limit", code="overflow")
        ln = rl.decode("utf-8").rstrip("\r\n")
        if ln == "":
            if lines and ev == "signal":
                p = _st.strict_json_loads(
                    "\n".join(lines).encode(), "sse_event", require_object=False
                )
                if isinstance(p, dict):
                    k = bytes(p["key"]).decode() if isinstance(p["key"], list) else str(p["key"])
                    hr = p.get("value_hint")
                    yield SignalEvent(
                        int(p["seq"]),
                        int(p["epoch"]),
                        str(p["namespace"]),
                        k,
                        int(p["version"]),
                        str(p["kind"]).lower(),
                        bytes(hr) if isinstance(hr, list) else None,
                    )
            ev = "message"
            lines = []
        elif ln.startswith("event:"):
            ev = ln[7:] if ln.startswith("event: ") else ln[6:]
        elif ln.startswith("data:"):
            lines.append(ln[6:] if ln.startswith("data: ") else ln[5:])


def encode_local_record(
    ns: str, key: str, ver: int, vis: SearchVisibility, meta: Mapping[str, Any], value: bytes
) -> dict[str, Any]:
    return {
        "namespace": ns,
        "key": key,
        "version": ver,
        "visibility": vis,
        "metadata": dict(meta),
        "value_b64": base64.b64encode(value).decode("ascii"),
    }


def decode_local_record(d: Mapping[str, Any]) -> BackendRecord:
    raw = base64.b64decode(str(d["value_b64"]).encode("ascii"))
    vis = validate_visibility(str(d.get("visibility", "Public")))
    return BackendRecord(
        str(d["namespace"]), str(d["key"]), raw, int(d["version"]), vis, dict(d.get("metadata", {}))
    )


def ensure_ordinary_dir(path: Path) -> Path:
    probe = path
    while not probe.exists() and not probe.is_symlink() and probe != probe.parent:
        probe = probe.parent
    _st.check_ordinary_ancestors(probe, expect_dir=True, error_prefix="ordinary directory required")
    path.mkdir(parents=True, exist_ok=True)
    return _st.check_ordinary_ancestors(path, expect_dir=True, error_prefix="ordinary dir required")


def ensure_ordinary_leaf_or_missing(path: Path) -> None:
    probe = path.parent
    while not probe.exists() and not probe.is_symlink() and probe != probe.parent:
        probe = probe.parent
    _st.check_ordinary_ancestors(probe, expect_dir=True, error_prefix="ordinary directory required")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not _st.is_ordinary_file_stat(info):
        raise ValueError(f"ordinary file required: {path}")


_STALE_ERRS = (
    http.client.RemoteDisconnected,
    http.client.CannotSendRequest,
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
)
_REUSE_ERR_STATUSES = frozenset({404, 409, 422, 423, 429})


class KeepAliveHTTPPool:
    def __init__(self, opener: Callable[[float | None], _Conn], *, max_idle: int = 4) -> None:
        self._opener, self._max_idle = opener, max(1, max_idle)
        self._lock, self._idle = threading.RLock(), deque[_Conn]()

    def _acquire(self) -> tuple[_Conn, bool]:
        with self._lock:
            if self._idle:
                return self._idle.pop(), True
        return self._opener(None), False

    def _release(self, conn: _Conn, *, reusable: bool) -> None:
        if reusable:
            with self._lock:
                if len(self._idle) < self._max_idle:
                    self._idle.append(conn)
                    return
        conn.close()

    def close(self) -> None:
        with self._lock:
            cs = list(self._idle)
            self._idle.clear()
        for c in cs:
            c.close()

    def request(
        self,
        method: str,
        full_path: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        max_bytes: int = _st.MAX_BYTES,
    ) -> tuple[int, bytes]:
        hdrs = dict(headers)
        hdrs.setdefault("Connection", "keep-alive")
        for att in range(2):
            conn, reused = self._acquire()
            reusable = False
            try:
                conn.request(method, full_path, body=body, headers=hdrs)
                resp = conn.getresponse()
                if resp.status >= 400:
                    eb = resp.read(65536)
                    reusable = (
                        (not resp.will_close)
                        and resp.isclosed()
                        and resp.status in _REUSE_ERR_STATUSES
                    )
                    raise map_http_error(resp.status, eb)
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise StorageBackendError("membus response exceeds byte limit", code="overflow")
                reusable = (not resp.will_close) and resp.isclosed()
                return resp.status, data
            except StorageBackendError:
                raise
            except _STALE_ERRS as err:
                if reused and att == 0:
                    continue
                raise BackendUnavailableError(str(err), status=0, code="unavailable") from None
            except (http.client.HTTPException, TimeoutError, OSError) as err:
                raise BackendUnavailableError(str(err), status=0, code="unavailable") from None
            finally:
                self._release(conn, reusable=reusable)
        raise BackendUnavailableError("membus request failed after reconnect", status=0)


class InMemoryMirrorBackend:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._vers: dict[tuple[str, str], int] = {}
        self._blobs: set[tuple[str, str]] = set()
        self._leases: dict[tuple[str, str], LeaseGrant] = {}
        self._gens: dict[tuple[str, str], int] = {}

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
        ns, k, vis = (
            validate_identifier(namespace, "namespace"),
            validate_identifier(key, "key"),
            validate_visibility(visibility),
        )
        if len(value) > _st.MAX_BYTES:
            raise ValueError("record payload exceeds per-object limit")
        with self._lock:
            cur = self._vers.get((ns, k), 0)
            if expected_version is not None and cur != expected_version:
                raise BackendConflictError(
                    f"expected version {expected_version}, found {cur}", status=409
                )
            self._vers[(ns, k)] = cur + 1
        return BackendRecord(ns, k, value, cur + 1, vis, dict(metadata) if metadata else {})

    def get_record(self, namespace: str, key: str) -> BackendRecord | None:
        validate_identifier(namespace, "namespace")
        validate_identifier(key, "key")
        return None

    def delete_record(
        self, namespace: str, key: str, *, expected_version: int | None = None
    ) -> bool:
        ns, k = validate_identifier(namespace, "namespace"), validate_identifier(key, "key")
        with self._lock:
            if (cur := self._vers.get((ns, k))) is None:
                return False
            if expected_version is not None and cur != expected_version:
                raise BackendConflictError(
                    f"expected version {expected_version}, found {cur}", status=409
                )
            self._vers.pop((ns, k), None)
            return True

    def put_blob(self, namespace: str, data: bytes) -> str:
        ns = validate_identifier(namespace, "namespace")
        if len(data) > _st.MAX_BYTES:
            raise ValueError("blob exceeds per-object limit")
        d = _st.sha(data)
        with self._lock:
            self._blobs.add((ns, d))
        return d

    def get_blob(self, namespace: str, sha256_hex: str) -> bytes | None:
        validate_identifier(namespace, "namespace")
        validate_sha256(sha256_hex)
        return None

    def has_blob(self, namespace: str, sha256_hex: str) -> bool:
        ns, d = validate_identifier(namespace, "namespace"), validate_sha256(sha256_hex)
        with self._lock:
            return (ns, d) in self._blobs

    def search(
        self,
        query: str,
        *,
        namespace: str | None = None,
        kind: str | None = None,
        mode: str = "Bm25",
        limit: int = 20,
    ) -> list[SearchHit]:
        del query, namespace, kind, mode, limit
        return []

    def acquire_lease(self, namespace: str, resource: str, holder: str, ttl_ms: int) -> LeaseGrant:
        ns, res = validate_identifier(namespace, "namespace"), validate_identifier(resource, "res")
        if not holder or ttl_ms <= 0:
            raise ValueError("lease holder and positive ttl_ms required")
        with self._lock:
            nv = now_ms()
            if (cur := self._leases.get((ns, res))) is not None and cur.expires_at_ms > nv:
                raise BackendContendedError(f"lease held for {ns}/{res}", status=409)
            gen = self._gens.get((ns, res), 0) + 1
            self._gens[(ns, res)] = gen
            g = LeaseGrant(ns, res, holder, gen, secrets.token_hex(16), nv + ttl_ms)
            self._leases[(ns, res)] = g
            return g

    def renew_lease(self, grant: LeaseGrant, ttl_ms: int) -> LeaseGrant:
        if ttl_ms <= 0:
            raise ValueError("positive ttl_ms required")
        with self._lock:
            if not self.validate_lease(grant):
                raise BackendContendedError("lease is not active", status=409)
            ren = replace(grant, expires_at_ms=now_ms() + ttl_ms)
            self._leases[(grant.namespace, grant.resource)] = ren
            return ren

    def release_lease(self, grant: LeaseGrant) -> bool:
        with self._lock:
            if not self.validate_lease(grant):
                return False
            self._leases.pop((grant.namespace, grant.resource), None)
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
        del prefix, since_seq, mode, timeout
        validate_identifier(namespace, "namespace")
        return iter(())

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "backend": "in-memory-mirror"}
