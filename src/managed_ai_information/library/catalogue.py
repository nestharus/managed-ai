"""Verified read-only published source library reader and lexical discovery.

Owns complete-body verification, precomputed module pins (P7-12), streaming
prepared-index construction (P7-2), single-query discovery, and bounded batch
discovery over a shared verified index image.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import DEFAULT_LIBRARY_CONFIG, LibraryConfig
from .contract import SearchScope
from .discovery_ops import (
    assemble_discovery_result,
    batch_json_size,
    literal_search,
    prepare_library_index,
    retained_preparation_metadata,
    run_discover_batch,
)
from .format import (
    check_manifest,
    decoded,
    digest,
    encoded,
    integer,
    need,
    ordinary,
    read_bytes,
    read_bytes_under,
    sha,
    text,
)
from .index import PreparedIndex, RetainedSearch
from .lexical import SEARCH_POLICY, SQLITE_ERRORS, FtsAnalyzer, search_query, sqlite3

_DEFAULT_READ_BYTES = read_bytes

INDEX_FORMAT = DEFAULT_LIBRARY_CONFIG.search.index_format
MAX_INDEX = DEFAULT_LIBRARY_CONFIG.search.max_index_bytes
MAX_BATCH_QUERIES = DEFAULT_LIBRARY_CONFIG.batch.max_queries
MAX_BATCH_INPUT = DEFAULT_LIBRARY_CONFIG.batch.max_input_bytes
MAX_BATCH_RESULT = DEFAULT_LIBRARY_CONFIG.batch.max_result_bytes


def _resolve_read_bytes() -> Callable[[Path | str, int], bytes]:
    return read_bytes


_literal_search = literal_search
__all__ = [
    "INDEX_FORMAT",
    "MAX_BATCH_INPUT",
    "MAX_BATCH_QUERIES",
    "MAX_BATCH_RESULT",
    "MAX_INDEX",
    "Library",
    "batch_json_size",
]


class Library:
    """Verified read-only source library with bounded lexical nomination."""

    def __init__(
        self,
        directory: Path | str,
        *,
        config: LibraryConfig = DEFAULT_LIBRARY_CONFIG,
        _preloaded_raw: tuple[bytes, str] | None = None,
    ) -> None:
        self.config = config
        self.directory = ordinary(directory)
        need(self.directory.is_dir(), "library directory required")

        if _preloaded_raw is not None:
            self.raw, self.sha256 = _preloaded_raw
        else:
            reader = _resolve_read_bytes()
            self.raw = reader(self.directory / "catalogue.json", config.limits.max_manifest_bytes)
            self.sha256 = digest(self.raw)

        self.catalogue = check_manifest(decoded(self.raw), config=config)
        self.bodies = {row["id"]: row for row in self.catalogue["bodies"]}
        self.modules = {row["id"]: row for row in self.catalogue["modules"]}
        self._module_pins = {
            row["id"]: {
                "library_sha256": self.sha256,
                "id": row["id"],
                "sha256": digest(encoded(row)),
            }
            for row in self.catalogue["modules"]
        }
        self._search_cache: RetainedSearch | None = None

    def close(self) -> None:
        if self._search_cache is not None:
            self._search_cache.close()
            self._search_cache = None

    def __enter__(self) -> Library:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        if hasattr(self, "_search_cache"):
            self.close()

    @property
    def prepared_scope(self) -> SearchScope | None:
        return self._search_cache.scope if self._search_cache is not None else None

    def pin(self, row: dict[str, Any], kind: str = "body") -> dict[str, str]:
        if kind == "body":
            return {"library_sha256": self.sha256, "id": row["id"], "sha256": row["sha256"]}

        cached = self._module_pins.get(row["id"])
        if cached is not None and row is self.modules.get(row["id"]):
            return dict(cached)

        return {"library_sha256": self.sha256, "id": row["id"], "sha256": digest(encoded(row))}

    def _read_verified_body_bytes(self, ident: str) -> bytes:
        need(ident in self.bodies, "body unavailable in this library: " + ident)
        row = self.bodies[ident]
        limit = self.config.limits.max_body_bytes
        reader = _resolve_read_bytes()

        if reader is _DEFAULT_READ_BYTES:
            data = read_bytes_under(self.directory, row["object"], limit)
        else:
            data = reader(self.directory / row["object"], limit)

        need(
            len(data) == row["bytes"] and digest(data) == row["sha256"],
            "body differs from declared source: " + ident,
        )
        return data

    def body_text(self, ident: str) -> str:
        """Read, hash-verify, and UTF-8-decode one complete body in a single pass (P7-12)."""
        return self._read_verified_body_bytes(ident).decode("utf-8")

    def body(self, ident: str) -> bytes:
        """Read, hash-verify, and UTF-8-validate one complete body, returning raw bytes."""
        data = self._read_verified_body_bytes(ident)
        data.decode("utf-8")
        return data

    def show(self, ident: str) -> dict[str, Any]:
        need(ident in self.bodies, "body unavailable in this library: " + ident)
        row = self.bodies[ident]
        return {"pin": self.pin(row), "entry": row, "text": self.body_text(ident)}

    def verify(self) -> dict[str, Any]:
        total = 0
        max_pkg = self.config.limits.max_package_bytes

        for ident in self.bodies:
            total += len(self.body(ident))
            need(total <= max_pkg, "library exceeds total verified byte bound")

        return {
            "library_sha256": self.sha256,
            "edition": self.catalogue["edition"],
            "bodies": len(self.bodies),
            "modules": len(self.modules),
            "body_bytes": total,
            "scope": (
                "Exact complete bodies and declared required closure; no semantic adequacy claim."
            ),
        }

    def search_scope(self, scan_limit: int, max_bytes: int) -> tuple[list[str], list[str], int]:
        integer(scan_limit, "scan limit", 1, self.config.limits.max_bodies)
        integer(max_bytes, "search bytes", 1, self.config.limits.max_package_bytes)

        selected: list[str] = []
        omitted: list[str] = []
        total = 0

        for ident, row in self.bodies.items():
            if len(selected) >= scan_limit or row["bytes"] > max_bytes - total:
                omitted.append(ident)
            else:
                selected.append(ident)
                total += row["bytes"]

        return selected, omitted, total

    def index_basis(self, scan_limit: int, max_bytes: int) -> dict[str, Any]:
        need(sqlite3 is not None, "SQLite support unavailable; use ordinary BM25 discovery")

        selected, _, total = self.search_scope(scan_limit, max_bytes)
        return {
            "format": self.config.search.index_format,
            "library_sha256": self.sha256,
            "policy": SEARCH_POLICY,
            "backend": "fts5",
            "analyzer": FtsAnalyzer.identity(self.config.search),
            "bodies": selected,
            "modules": list(self.modules),
            "body_bytes": total,
            "limits": {"bodies": scan_limit, "body_bytes": max_bytes},
        }

    def prepare_index(
        self,
        output: Path | str,
        scan_limit: int = DEFAULT_LIBRARY_CONFIG.search.default_scan_limit,
        max_bytes: int = DEFAULT_LIBRARY_CONFIG.search.default_max_bytes,
    ) -> dict[str, Any]:
        """Build a fresh immutable derivative outside the delivered library."""
        return prepare_library_index(self, output, scan_limit, max_bytes, sqlite_mod=sqlite3)

    def discover(
        self,
        query: str,
        limit: int = DEFAULT_LIBRARY_CONFIG.search.default_limit,
        scan_limit: int = DEFAULT_LIBRARY_CONFIG.search.default_scan_limit,
        max_bytes: int = DEFAULT_LIBRARY_CONFIG.search.default_max_bytes,
        backend: str = "bm25",
        index: Path | str | None = None,
        index_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Verify each examined body on ordinary searches, even with warm indexes."""
        return self._discover(
            query,
            limit,
            scan_limit,
            max_bytes,
            backend,
            index,
            index_sha256,
            allow_retained_scope=False,
        )

    def discover_retained_scope(
        self,
        query: str,
        limit: int = DEFAULT_LIBRARY_CONFIG.search.default_limit,
        scan_limit: int = DEFAULT_LIBRARY_CONFIG.search.default_scan_limit,
        max_bytes: int = DEFAULT_LIBRARY_CONFIG.search.default_max_bytes,
    ) -> dict[str, Any]:
        """Query the retained FTS5 observation, acquiring bodies on a scope miss."""
        return self._discover(
            query,
            limit,
            scan_limit,
            max_bytes,
            "fts5",
            None,
            None,
            allow_retained_scope=True,
        )

    def _run_search_backend(
        self,
        parsed: dict[str, Any],
        scope: SearchScope,
        documents: list[dict[str, Any]],
        module_documents: list[dict[str, Any]],
        *,
        searchable: bool,
        reusable: bool,
        backend: str,
        index: Path | str | None,
        index_sha256: str | None,
        scan_limit: int,
        max_bytes: int,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], bool, int]:
        if not searchable:
            return {}, {}, False, 0

        reader = _resolve_read_bytes()

        if index is not None:
            if index_sha256 is None:
                raise ValueError(
                    "prepared index path and expected digest must be supplied together"
                )
            prepared = PreparedIndex(
                index,
                index_sha256,
                self.index_basis(scan_limit, max_bytes),
                config=self.config,
                reader=reader,
                sqlite_mod=sqlite3,
            )
            try:
                body_hits = prepared.search("body_docs", parsed)
                mod_hits = prepared.search("module_docs", parsed)
                return body_hits, mod_hits, True, prepared.bytes
            finally:
                prepared.close()

        if backend == "literal":
            return (
                _literal_search(documents, parsed["terms"]),
                _literal_search(module_documents, parsed["terms"]),
                False,
                0,
            )

        if not reusable:
            self.close()
            self._search_cache = RetainedSearch(
                scope,
                documents,
                module_documents,
                sqlite_mod=sqlite3,
                sqlite_errors=SQLITE_ERRORS,
                config=self.config,
            )

        cache = self._search_cache
        if cache is None:
            raise RuntimeError("retained search cache was not initialized")
        body_hits, mod_hits = cache.search(parsed)
        return body_hits, mod_hits, reusable, 0

    def _discover(
        self,
        query: str,
        limit: int,
        scan_limit: int,
        max_bytes: int,
        backend: str,
        index: Path | str | None,
        index_sha256: str | None,
        *,
        allow_retained_scope: bool,
    ) -> dict[str, Any]:
        cfg = self.config.search
        text(query, "query", cfg.max_query_bytes)
        integer(limit, "result limit", 1, cfg.max_limit)
        integer(scan_limit, "scan limit", 1, self.config.limits.max_bodies)
        integer(max_bytes, "search bytes", 1, self.config.limits.max_package_bytes)

        need(
            (index is None) == (index_sha256 is None),
            "prepared index path and expected digest must be supplied together",
        )
        need(index is None or backend == "fts5", "prepared index requires --backend fts5")
        if index is not None:
            sha(index_sha256)

        parsed = search_query(query, backend, config=cfg)
        searchable = (
            bool(parsed["terms"] or parsed["phrases"])
            or query in self.bodies
            or query in self.modules
        )
        examined, omitted, byte_count = (
            self.search_scope(scan_limit, max_bytes) if searchable else ([], list(self.bodies), 0)
        )

        scope = SearchScope(self.sha256, SEARCH_POLICY, backend, tuple(examined))
        reusable = self.prepared_scope == scope

        if allow_retained_scope and searchable and reusable and self._search_cache is not None:
            matches, module_matches = self._search_cache.search(parsed)
            return assemble_discovery_result(
                self,
                query,
                parsed,
                matches,
                module_matches,
                examined,
                omitted,
                byte_count,
                (limit, scan_limit, max_bytes),
                backend,
                retained_preparation_metadata(),
                indexed=True,
            )

        documents: list[dict[str, Any]] = []
        if index is None:
            for ident in examined:
                body_str = self.body_text(ident)
                if not reusable:
                    documents.append(dict(self.bodies[ident], text=body_str))

        module_documents = [
            {"id": row["id"], "title": row["title"], "summary": row["purpose"], "text": ""}
            for row in self.modules.values()
        ]

        matches, module_matches, reused, index_bytes = self._run_search_backend(
            parsed,
            scope,
            documents,
            module_documents,
            searchable=searchable,
            reusable=reusable,
            backend=backend,
            index=index,
            index_sha256=index_sha256,
            scan_limit=scan_limit,
            max_bytes=max_bytes,
        )

        preparation = {
            "storage": "pinned external contentless index"
            if index is not None
            else "in-memory Library instance only",
            "reused": reused,
            "index_sha256": index_sha256,
            "index_read_bytes": index_bytes,
            "index_verified": bool(index_bytes),
            "source_verification": (
                "No canonical source bodies read on this call. Indexed nomination relies on the "
                "selected build; "
                "index_verified states whether its bytes and basis were checked. Use show or "
                "verify "
                "for current exact acquisition."
                if index is not None
                else "Every examined complete body read and hash-verified on this call."
            ),
        }
        return assemble_discovery_result(
            self,
            query,
            parsed,
            matches,
            module_matches,
            examined,
            omitted,
            byte_count,
            (limit, scan_limit, max_bytes),
            backend,
            preparation,
            indexed=index is not None,
        )

    def discover_batch(
        self,
        queries: list[str],
        *,
        index: Path | str,
        index_sha256: str,
        limit: int = DEFAULT_LIBRARY_CONFIG.search.default_limit,
        scan_limit: int = DEFAULT_LIBRARY_CONFIG.search.default_scan_limit,
        max_bytes: int = DEFAULT_LIBRARY_CONFIG.search.default_max_bytes,
        max_result_bytes: int = DEFAULT_LIBRARY_CONFIG.batch.max_result_bytes,
    ) -> dict[str, Any]:
        """Nominate bounded questions from one explicitly shared, verified index image."""
        return run_discover_batch(
            self,
            queries,
            index,
            index_sha256,
            (limit, scan_limit, max_bytes, max_result_bytes),
            _resolve_read_bytes(),
            sqlite3,
            PreparedIndex,
        )
