"""Domain-native contracts for the published source library and lexical search engines."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, Protocol, runtime_checkable


@dataclass(frozen=True)
class LibraryPin:
    """Content-addressed pin to one body or module within a published library."""

    library_sha256: str
    id: str
    sha256: str


class SearchScope(NamedTuple):
    """Immutable identity of a prepared lexical search corpus."""

    catalogue_sha256: str
    policy: str
    backend: str
    body_ids: tuple[str, ...]


@runtime_checkable
class SearchBackend(Protocol):
    """Disposable lexical search engine over verified source bodies."""

    def search(self, parsed: dict[str, Any]) -> dict[str, Any]: ...

    def close(self) -> None: ...


@runtime_checkable
class LibraryReader(Protocol):
    """Verified read-only access and lexical nomination over a source library."""

    directory: Path
    sha256: str

    def verify(self) -> dict[str, Any]: ...

    def body(self, ident: str) -> bytes: ...

    def body_text(self, ident: str) -> str: ...

    def show(self, ident: str) -> dict[str, Any]: ...

    def prepare_index(
        self,
        output: Path | str,
        scan_limit: int = 512,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> dict[str, Any]: ...

    def discover(
        self,
        query: str,
        limit: int = 20,
        scan_limit: int = 512,
        max_bytes: int = 8 * 1024 * 1024,
        backend: str = "bm25",
        index: Path | str | None = None,
        index_sha256: str | None = None,
    ) -> dict[str, Any]: ...

    def discover_batch(
        self,
        queries: list[str],
        *,
        index: Path | str,
        index_sha256: str,
        limit: int = 20,
        scan_limit: int = 512,
        max_bytes: int = 8 * 1024 * 1024,
        max_result_bytes: int = 8 * 1024 * 1024,
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


@runtime_checkable
class SourceProducerService(Protocol):
    """Catalogue-pinned FTS5 discovery service with explicit lifecycle."""

    directory: Path

    def handle(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def close(self) -> None: ...
