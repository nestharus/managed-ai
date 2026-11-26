"""Domain-native structural protocols for the Managed AI store package."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ObjectRepository(Protocol):
    """Content-addressed object store backing immutable packets and records."""

    directory: Path

    def has(self, key: str) -> bool: ...

    def read(
        self,
        key: str,
        *,
        retained: bool = False,
        expected_bytes: int | None = None,
        dir_fd: int | None = None,
    ) -> bytes: ...

    def put(self, data: bytes) -> str: ...

    def entries(self) -> list[tuple[str, Path, bool, int]]: ...


@runtime_checkable
class RecordRepository(Protocol):
    """Verified native record head repository."""

    format: str

    def ids(self) -> list[str]: ...

    def has_record(self, ident: str) -> bool: ...

    def show(self, ident: str) -> dict[str, Any]: ...

    def read(
        self,
        ident: str,
        *,
        byte_limit: int | None = None,
        dir_fd: int | None = None,
    ) -> tuple[dict[str, Any], bytes]: ...

    def put(self, record: dict[str, Any], expected: int) -> dict[str, Any]: ...

    def scan_records(
        self,
        idents: list[str] | None = None,
        *,
        max_workers: int = 16,
    ) -> list[dict[str, Any]]: ...

    def indexed_summaries(
        self,
        *,
        max_workers: int = 16,
    ) -> dict[str, dict[str, Any]] | None: ...

    def reindex(self, *, max_workers: int = 16) -> dict[str, Any]: ...


@runtime_checkable
class CatalogueReader(Protocol):
    """Verified reader for installed and domain-selected knowledge catalogues."""

    def index(self) -> dict[str, Any]: ...

    def search(
        self,
        ident: str | None = None,
        query: str = '',
        kind: str | None = None,
        limit: int = 25,
        offset: int = 0,
        content: bool = False,
    ) -> dict[str, Any]: ...

    def body(
        self,
        entry: dict[str, Any],
        *,
        byte_limit: int | None = None,
        validated_root: bool = False,
    ) -> bytes: ...


@runtime_checkable
class StoreService(Protocol):
    """Public native store runtime facade consumed by CLIs and domain plugins."""

    root: Path
    instance_id: str | None
    work: Path

    def show(self, ident: str) -> dict[str, Any]: ...

    def put(self, record: dict[str, Any], expected: int) -> dict[str, Any]: ...

    def status(self, *, verify_all_basis: bool = False) -> dict[str, Any]: ...

    def discover(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def compose(
        self,
        record: str,
        focus: str | None = None,
        limit: int = 20,
        pair_limit: int = 200,
    ) -> dict[str, Any]: ...

    def reindex(self) -> dict[str, Any]: ...
