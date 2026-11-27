"""Shared protocol, dataclasses, typed errors, and validators for StorageBackend adapters."""

import ipaddress
import re
import time
import urllib.parse
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast, runtime_checkable

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
