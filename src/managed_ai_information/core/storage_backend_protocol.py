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

from managed_ai_information.core.storage import (
    MAX_BYTES,
    check_ordinary_ancestors,
    is_ordinary_file_stat,
    sha,
    strict_json_loads,
)

SearchVisibility = Literal["Public", "MetadataOnly", "Sealed"]

_VISIBILITIES: frozenset[str] = frozenset({"Public", "MetadataOnly", "Sealed"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,256}\Z")


class StorageBackendError(RuntimeError):
    """Base error for pluggable storage backend operations."""

    def __init__(self, message: str, *, status: int = 0, code: str = "backend_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class BackendConflictError(StorageBackendError):
    """Optimistic concurrency / revision conflict on a backend mutation."""


class BackendContendedError(StorageBackendError):
    """Lease or admission contention on a backend resource."""


class BackendUnavailableError(StorageBackendError):
    """Backend transport failure, timeout, or upstream unavailability."""


@dataclass(frozen=True, slots=True)
class BackendRecord:
    """Versioned key/value record stored by a StorageBackend."""

    namespace: str
    key: str
    value: bytes
    version: int
    visibility: SearchVisibility = "Public"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    """Fenced exclusive lease grant carrying monotonic generation and secret token."""

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
    def from_payload(cls, data: Mapping[str, Any]) -> LeaseGrant:
        return cls(
            str(data["namespace"]),
            str(data["resource"]),
            str(data["holder"]),
            int(data["generation"]),
            str(data["token"]),
            int(data["expires_at_ms"]),
        )


@dataclass(frozen=True, slots=True)
class SearchHit:
    """Single search match returned by a StorageBackend."""

    namespace: str
    doc_id: str
    kind: str
    title: str
    score: float
    visibility: SearchVisibility = "Public"


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """Ordered mutation or control signal emitted by a StorageBackend."""

    seq: int
    epoch: int
    namespace: str
    key: str
    version: int
    kind: str
    value_hint: bytes | None = None


@runtime_checkable
class StorageBackend(Protocol):
    """Pure-stdlib storage, search, lease, and signal backend contract."""

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
    if not isinstance(digest, str) or not _SHA256_RE.match(digest):
        raise ValueError(f"invalid SHA-256 digest: {digest!r}")
    return digest


def validate_identifier(value: str, label: str) -> str:
    bad = not isinstance(value, str) or not value or ".." in value or value.startswith("/")
    if bad or not _SAFE_SEGMENT_RE.match(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def validate_visibility(visibility: str) -> SearchVisibility:
    if visibility not in _VISIBILITIES:
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
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in base_url):
        raise ValueError("membus endpoint URL contains control characters")
    parsed = urllib.parse.urlsplit(base_url.rstrip("/"))
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported membus endpoint scheme: {parsed.scheme!r}")
    if parsed.username is not None or parsed.password is not None or not parsed.hostname:
        raise ValueError("membus endpoint URL must specify host without embedded credentials")
    if parsed.scheme == "http" and not is_loopback_host(parsed.hostname):
        raise ValueError(f"non-loopback membus endpoint requires https: {parsed.hostname!r}")
    return parsed


def now_ms() -> int:
    return int(time.time() * 1000)


def quote_segment(seg: str) -> str:
    return urllib.parse.quote(seg, safe="")


def encode_expected_version(expected_version: int | None) -> Any:
    if expected_version is None:
        return "Any"
    if isinstance(expected_version, bool) or expected_version < 0:
        raise ValueError(f"invalid expected_version: {expected_version!r}")
    if expected_version == 0:
        return "Absent"
    return {"Version": expected_version}


def build_search_op(
    ns: str, key: str, value: bytes, vis: SearchVisibility, meta: Mapping[str, Any]
) -> dict[str, Any]:
    """Build a Membus v2 SearchLogOp payload that never indexes Sealed or MetadataOnly bodies."""
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
    """Build a Membus v2 POST /v2/catalog/collections registration payload."""
    ns = validate_identifier(namespace, "namespace")
    if isinstance(schema_version, bool) or schema_version < 1:
        raise ValueError(f"schema_version must be >= 1, got {schema_version!r}")
    digest_bytes = list(bytes.fromhex(sha(schema_bytes)))
    return {
        "expected_version": expected_version,
        "descriptor": {
            "namespace": ns,
            "store_profile": store_profile,
            "schema_version": schema_version,
            "schema_sha256": digest_bytes,
            "schema_bytes": base64.b64encode(schema_bytes).decode("ascii"),
            "declared_indexes": [],
            "retention_policy": {"max_age_secs": None, "max_entries": None},
            "migration_state": "Active",
            "compatible_versions": [schema_version],
            "compatibility_mode": "Strict",
        },
    }


def map_http_error(status: int, raw: bytes) -> StorageBackendError:
    """Map Membus v2 HTTP status + error code to typed StorageBackendError."""
    code, message = "http_error", raw.decode("utf-8", errors="replace")
    try:
        parsed = strict_json_loads(raw, "membus_error", require_object=False)
        if isinstance(parsed, dict):
            code = str(parsed.get("error", code))
            message = str(parsed.get("message", message))
    except UnicodeDecodeError, ValueError:
        pass
    low = code.lower()
    # Check lease/contention BEFORE generic 409 because live membus returns 409 + lease_held.
    if status in (423, 429) or "lease" in low or "contend" in low:
        return BackendContendedError(message, status=status, code=code)
    if status == 409 or "conflict" in low or "version" in low:
        return BackendConflictError(message, status=status, code=code)
    if status in (502, 503, 504):
        return BackendUnavailableError(message, status=status, code=code)
    return StorageBackendError(message, status=status, code=code)


def parse_sse_signals(stream: Any, *, max_line_bytes: int = MAX_BYTES) -> Iterator[SignalEvent]:
    """Parse bounded WHATWG SSE signal events from a Membus v2 stream."""
    event_name = "message"
    data_lines: list[str] = []
    while raw_line := stream.readline(max_line_bytes + 1):
        if len(raw_line) > max_line_bytes:
            raise StorageBackendError("SSE line exceeds byte limit", code="overflow")
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if line == "":
            if data_lines and event_name == "signal":
                p = strict_json_loads(
                    "\n".join(data_lines).encode(), "sse_event", require_object=False
                )
                if isinstance(p, dict):
                    k = bytes(p["key"]).decode() if isinstance(p["key"], list) else str(p["key"])
                    hint_raw = p.get("value_hint")
                    hint = bytes(hint_raw) if isinstance(hint_raw, list) else None
                    yield SignalEvent(
                        int(p["seq"]),
                        int(p["epoch"]),
                        str(p["namespace"]),
                        k,
                        int(p["version"]),
                        str(p["kind"]).lower(),
                        hint,
                    )
            event_name, data_lines = "message", []
        elif line.startswith("event:"):
            event_name = line[7:] if line.startswith("event: ") else line[6:]
        elif line.startswith("data:"):
            data_lines.append(line[6:] if line.startswith("data: ") else line[5:])


def encode_local_record(
    namespace: str,
    key: str,
    version: int,
    visibility: SearchVisibility,
    metadata: Mapping[str, Any],
    value: bytes,
) -> dict[str, Any]:
    """Encode an EmbeddedFileBackend KV record payload for JSON persistence."""
    return {
        "namespace": namespace,
        "key": key,
        "version": version,
        "visibility": visibility,
        "metadata": dict(metadata),
        "value_b64": base64.b64encode(value).decode("ascii"),
    }


def decode_local_record(doc: Mapping[str, Any]) -> BackendRecord:
    """Decode a persisted EmbeddedFileBackend KV record JSON object."""
    raw = base64.b64decode(str(doc["value_b64"]).encode("ascii"))
    vis = validate_visibility(str(doc.get("visibility", "Public")))
    return BackendRecord(
        str(doc["namespace"]),
        str(doc["key"]),
        raw,
        int(doc["version"]),
        vis,
        dict(doc.get("metadata", {})),
    )


def ensure_ordinary_dir(path: Path) -> Path:
    """Create and verify an ordinary (non-symlink/non-reparse) directory."""
    probe = path
    while not probe.exists() and not probe.is_symlink() and probe != probe.parent:
        probe = probe.parent
    check_ordinary_ancestors(probe, expect_dir=True, error_prefix="ordinary directory required")
    path.mkdir(parents=True, exist_ok=True)
    return check_ordinary_ancestors(path, expect_dir=True, error_prefix="ordinary dir required")


def ensure_ordinary_leaf_or_missing(path: Path) -> None:
    """Reject non-ordinary existing leaf files (symlinks, directories, FIFOs)."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary file required: {path}")


_STALE_KEEPALIVE_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.CannotSendRequest,
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
)
_REUSABLE_ERROR_STATUSES: frozenset[int] = frozenset({404, 409, 422, 423, 429})


class KeepAliveHTTPPool:
    """Thread-safe persistent HTTP/1.1 connection pool with single-retry stale reconnect."""

    def __init__(
        self,
        opener: Callable[[float | None], http.client.HTTPConnection | http.client.HTTPSConnection],
        *,
        max_idle: int = 4,
    ) -> None:
        self._opener = opener
        self._max_idle = max(1, max_idle)
        self._lock = threading.RLock()
        self._idle: deque[http.client.HTTPConnection | http.client.HTTPSConnection] = deque()

    def _acquire(
        self,
    ) -> tuple[http.client.HTTPConnection | http.client.HTTPSConnection, bool]:
        with self._lock:
            if self._idle:
                return self._idle.pop(), True
        return self._opener(None), False

    def _release(
        self,
        conn: http.client.HTTPConnection | http.client.HTTPSConnection,
        *,
        reusable: bool,
    ) -> None:
        if reusable:
            with self._lock:
                if len(self._idle) < self._max_idle:
                    self._idle.append(conn)
                    return
        conn.close()

    def close(self) -> None:
        with self._lock:
            conns = list(self._idle)
            self._idle.clear()
        for conn in conns:
            conn.close()

    def request(
        self,
        method: str,
        full_path: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        max_bytes: int = MAX_BYTES,
    ) -> tuple[int, bytes]:
        req_headers = dict(headers)
        req_headers.setdefault("Connection", "keep-alive")
        for attempt in range(2):
            conn, reused = self._acquire()
            reusable = False
            try:
                conn.request(method, full_path, body=body, headers=req_headers)
                resp = conn.getresponse()
                if resp.status >= 400:
                    err_body = resp.read(65536)
                    reusable = (
                        (not resp.will_close)
                        and resp.isclosed()
                        and resp.status in _REUSABLE_ERROR_STATUSES
                    )
                    raise map_http_error(resp.status, err_body)
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise StorageBackendError("membus response exceeds byte limit", code="overflow")
                reusable = (not resp.will_close) and resp.isclosed()
                return resp.status, data
            except StorageBackendError:
                raise
            except _STALE_KEEPALIVE_ERRORS as err:
                if reused and attempt == 0:
                    continue
                raise BackendUnavailableError(str(err), status=0, code="unavailable") from None
            except (http.client.HTTPException, TimeoutError, OSError) as err:
                raise BackendUnavailableError(str(err), status=0, code="unavailable") from None
            finally:
                self._release(conn, reusable=reusable)
        raise BackendUnavailableError("membus request failed after reconnect", status=0)


class InMemoryMirrorBackend:
    """Zero-disk-fsync digest-only in-RAM Route A backend for MembusStoreMirror.apply()."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._versions: dict[tuple[str, str], int] = {}
        self._blob_digests: set[tuple[str, str]] = set()
        self._leases: dict[tuple[str, str], LeaseGrant] = {}
        self._generations: dict[tuple[str, str], int] = {}

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
        with self._lock:
            cur = self._versions.get((ns, k), 0)
            if expected_version is not None and cur != expected_version:
                raise BackendConflictError(
                    f"expected version {expected_version}, found {cur}", status=409
                )
            nxt = cur + 1
            self._versions[(ns, k)] = nxt
        return BackendRecord(ns, k, value, nxt, vis, dict(metadata) if metadata else {})

    def get_record(self, namespace: str, key: str) -> BackendRecord | None:
        validate_identifier(namespace, "namespace")
        validate_identifier(key, "key")
        return None

    def delete_record(
        self, namespace: str, key: str, *, expected_version: int | None = None
    ) -> bool:
        ns, k = validate_identifier(namespace, "namespace"), validate_identifier(key, "key")
        with self._lock:
            cur = self._versions.get((ns, k))
            if cur is None:
                return False
            if expected_version is not None and cur != expected_version:
                raise BackendConflictError(
                    f"expected version {expected_version}, found {cur}", status=409
                )
            self._versions.pop((ns, k), None)
            return True

    def put_blob(self, namespace: str, data: bytes) -> str:
        ns = validate_identifier(namespace, "namespace")
        if len(data) > MAX_BYTES:
            raise ValueError("blob exceeds per-object limit")
        digest = sha(data)
        with self._lock:
            self._blob_digests.add((ns, digest))
        return digest

    def get_blob(self, namespace: str, sha256_hex: str) -> bytes | None:
        validate_identifier(namespace, "namespace")
        validate_sha256(sha256_hex)
        return None

    def has_blob(self, namespace: str, sha256_hex: str) -> bool:
        ns, digest = validate_identifier(namespace, "namespace"), validate_sha256(sha256_hex)
        with self._lock:
            return (ns, digest) in self._blob_digests

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
            now_val = now_ms()
            cur = self._leases.get((ns, res))
            if cur is not None and cur.expires_at_ms > now_val:
                raise BackendContendedError(f"lease held for {ns}/{res}", status=409)
            gen = self._generations.get((ns, res), 0) + 1
            self._generations[(ns, res)] = gen
            grant = LeaseGrant(ns, res, holder, gen, secrets.token_hex(16), now_val + ttl_ms)
            self._leases[(ns, res)] = grant
            return grant

    def renew_lease(self, grant: LeaseGrant, ttl_ms: int) -> LeaseGrant:
        if ttl_ms <= 0:
            raise ValueError("positive ttl_ms required")
        with self._lock:
            if not self.validate_lease(grant):
                raise BackendContendedError("lease is not active", status=409)
            renewed = replace(grant, expires_at_ms=now_ms() + ttl_ms)
            self._leases[(grant.namespace, grant.resource)] = renewed
            return renewed

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
