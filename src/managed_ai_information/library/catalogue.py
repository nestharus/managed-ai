"""Verified read-only published source library reader and lexical discovery.

Owns complete-body verification, precomputed module pins (P7-12), streaming
prepared-index construction (P7-2), single-query discovery, and bounded batch
discovery over a shared verified index image.
"""

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from managed_ai_information.platform import publish_exclusive, sha256_file, sync_dir

from .config import DEFAULT_LIBRARY_CONFIG, LibraryConfig
from .contract import SearchScope
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
_BATCH_ENCODER = json.JSONEncoder(sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)

INDEX_FORMAT = DEFAULT_LIBRARY_CONFIG.search.index_format
MAX_INDEX = DEFAULT_LIBRARY_CONFIG.search.max_index_bytes
MAX_BATCH_QUERIES = DEFAULT_LIBRARY_CONFIG.batch.max_queries
MAX_BATCH_INPUT = DEFAULT_LIBRARY_CONFIG.batch.max_input_bytes
MAX_BATCH_RESULT = DEFAULT_LIBRARY_CONFIG.batch.max_result_bytes


def _resolve_read_bytes() -> Callable[[Path | str, int], bytes]:
    return read_bytes


def batch_json_size(value: Any, maximum: int, extra_indent: int = 0) -> int:
    """Count the actual indented UTF-8 representation using C-accelerated encoding."""
    rendered = _BATCH_ENCODER.encode(value)
    size = extra_indent + len(rendered.encode("utf-8")) + extra_indent * rendered.count("\n")
    need(size <= maximum, "discovery batch exceeds its JSON byte bound")
    return size


def _literal_search(items: list[dict[str, Any]], terms: list[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for row in items:
        surface = "\n".join(row[k] for k in ("id", "title", "summary", "text")).casefold()
        hits = [term for term in terms if term in surface]
        if hits:
            found[row["id"]] = {"score": len(hits), "matched_terms": hits}
    return found


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

    def _build_prepared_sqlite(self, staged: Path, basis: dict[str, Any]) -> None:
        if sqlite3 is None:
            raise ValueError("SQLite support unavailable; use ordinary BM25 discovery")
        max_index = self.config.search.max_index_bytes
        db = sqlite3.connect(staged)

        try:
            db.execute("PRAGMA page_size=4096")
            db.execute("PRAGMA max_page_count=" + str(max_index // 4096))
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute(
                "INSERT INTO metadata VALUES(?,?)", ("basis", encoded(basis).decode("utf-8"))
            )

            for table in ("body_docs", "module_docs"):
                FtsAnalyzer.create(db, table, config=self.config.search)

            for number, ident in enumerate(basis["bodies"], 1):
                row = self.bodies[ident]
                body_str = self.body_text(ident)
                db.execute(
                    "INSERT INTO body_docs(rowid,id,title,summary,body) VALUES(?,?,?,?,?)",
                    (number, ident, row["title"], row["summary"], body_str),
                )

            for number, row in enumerate(self.modules.values(), 1):
                db.execute(
                    "INSERT INTO module_docs(rowid,id,title,summary,body) VALUES(?,?,?,?,?)",
                    (number, row["id"], row["title"], row["purpose"], ""),
                )

            for table in ("body_docs", "module_docs"):
                db.execute(f"INSERT INTO {table}({table}) VALUES('optimize')")
                db.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check')")

            need(
                db.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
                "prepared index integrity check failed",
            )
            db.commit()
            db.execute("VACUUM")
        finally:
            db.close()

    def prepare_index(
        self,
        output: Path | str,
        scan_limit: int = DEFAULT_LIBRARY_CONFIG.search.default_scan_limit,
        max_bytes: int = DEFAULT_LIBRARY_CONFIG.search.default_max_bytes,
    ) -> dict[str, Any]:
        """Build a fresh immutable derivative outside the delivered library."""
        basis = self.index_basis(scan_limit, max_bytes)
        target = ordinary(output).resolve()
        library_directory = ordinary(self.directory).resolve()

        need(target.parent.is_dir(), "prepared index parent directory must exist")
        need(
            library_directory != target and library_directory not in target.parents,
            "prepared index must be outside the library",
        )
        need(not target.exists(), "prepared index output already exists")

        staged: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(prefix=".knowledge-index-", dir=target.parent)
            os.close(descriptor)
            staged = Path(name)

            self._build_prepared_sqlite(staged, basis)

            staged_size = staged.lstat().st_size
            need(
                staged_size <= self.config.search.max_index_bytes,
                "file exceeds byte bound: " + str(staged),
            )
            index_sha = sha256_file(staged)

            with staged.open("r+b") as stream:
                os.fsync(stream.fileno())

            publish_exclusive(staged, target, error_message="prepared index output already exists")
            sync_dir(target.parent)
            _, omitted, _ = self.search_scope(scan_limit, max_bytes)

            return {
                "format": "knowledge-search-preparation/1",
                "library_sha256": self.sha256,
                "index": {
                    "path": str(target),
                    "sha256": index_sha,
                    "bytes": staged_size,
                    "format": self.config.search.index_format,
                },
                "basis": basis,
                "omitted_body_ids": omitted,
                "source_verification": "Every indexed complete body read and "
                "hash-verified during this build.",
                "scope": (
                    "Disposable lexical derivative, not source custody. Select this actual build "
                    "receipt; "
                    "a self-declared index digest does not warrant its construction. Exact "
                    "acquisition still uses show."
                ),
            }
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
                Path(str(staged) + "-journal").unlink(missing_ok=True)

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

    def _retained_preparation_metadata(self) -> dict[str, Any]:
        return {
            "storage": "in-memory Library instance only",
            "reused": True,
            "index_sha256": None,
            "index_read_bytes": 0,
            "index_verified": False,
            "source_verification": (
                "No canonical source bodies read on this call. The same in-memory FTS5 indexes "
                "retain bodies read and hash-verified when this catalogue and selected corpus were "
                "prepared. Use show for fresh exact acquisition."
            ),
            "lifetime": (
                "Retained until a different corpus replaces this catalogue's index, or close() "
                "ends "
                "the instance."
            ),
        }

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
            return self._discovery_result(
                query,
                parsed,
                matches,
                module_matches,
                examined,
                omitted,
                byte_count,
                limit=limit,
                scan_limit=scan_limit,
                max_bytes=max_bytes,
                backend=backend,
                preparation=self._retained_preparation_metadata(),
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
        return self._discovery_result(
            query,
            parsed,
            matches,
            module_matches,
            examined,
            omitted,
            byte_count,
            limit=limit,
            scan_limit=scan_limit,
            max_bytes=max_bytes,
            backend=backend,
            preparation=preparation,
            indexed=index is not None,
        )

    def _validate_batch_inputs(
        self,
        queries: list[str],
        index: Path | str,
        index_sha256: str,
        limit: int,
        scan_limit: int,
        max_bytes: int,
        max_result_bytes: int,
    ) -> list[tuple[str, dict[str, Any], bool]]:
        bcfg = self.config.batch
        scfg = self.config.search

        need(
            isinstance(queries, list) and 1 <= len(queries) <= bcfg.max_queries,
            f"queries must be a list of 1 to {bcfg.max_queries} questions",
        )
        integer(limit, "result limit", 1, scfg.max_limit)
        integer(scan_limit, "scan limit", 1, self.config.limits.max_bodies)
        integer(max_bytes, "search bytes", 1, self.config.limits.max_package_bytes)
        integer(max_result_bytes, "batch result bytes", 1, bcfg.max_result_bytes)
        need(index is not None, "prepared index path is required for a batch")
        sha(index_sha256)

        questions: list[tuple[str, dict[str, Any], bool]] = []
        for query in queries:
            text(query, "query", scfg.max_query_bytes)
            parsed = search_query(query, "fts5", config=scfg)
            searchable = (
                bool(parsed["terms"] or parsed["phrases"])
                or query in self.bodies
                or query in self.modules
            )
            questions.append((query, parsed, searchable))

        batch_json_size(queries, bcfg.max_input_bytes - 1)
        return questions

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
        questions = self._validate_batch_inputs(
            queries,
            index,
            index_sha256,
            limit,
            scan_limit,
            max_bytes,
            max_result_bytes,
        )

        prepared: PreparedIndex | None = None
        selected: list[str] = []
        omitted: list[str] = []
        byte_count = 0

        try:
            if any(searchable for _, _, searchable in questions):
                reader = _resolve_read_bytes()
                prepared = PreparedIndex(
                    index,
                    index_sha256,
                    self.index_basis(scan_limit, max_bytes),
                    config=self.config,
                    reader=reader,
                    sqlite_mod=sqlite3,
                )
                selected, omitted, byte_count = self.search_scope(scan_limit, max_bytes)

            bcfg = self.config.batch
            scfg = self.config.search
            result: dict[str, Any] = {
                "format": "knowledge-discovery-batch/1",
                "library_sha256": self.sha256,
                "preparation": {
                    "storage": "one private prepared index image for this batch",
                    "index_sha256": index_sha256,
                    "index_verified": prepared is not None,
                    "index_opens": int(prepared is not None),
                    "index_read_bytes": prepared.bytes if prepared is not None else 0,
                    "canonical_body_read_bytes": 0,
                    "lifetime": "This call only; the shared image is "
                    "closed before return or refusal.",
                    "source_verification": (
                        "No canonical source bodies read. Searchable questions share one verified "
                        "derivative "
                        "and its exact catalogue/analyzer/scope basis. The selected build warrants "
                        "the postings; "
                        "use show or verify for current exact acquisition."
                    ),
                },
                "limits": {
                    "queries": bcfg.max_queries,
                    "query_bytes": scfg.max_query_bytes,
                    "input_bytes": bcfg.max_input_bytes,
                    "result_bytes": max_result_bytes,
                },
                "results": [],
            }

            result_bytes = batch_json_size(result, max_result_bytes - 1) + 1

            for query, parsed, searchable in questions:
                if searchable and prepared is not None:
                    matches = prepared.search("body_docs", parsed)
                    module_matches = prepared.search("module_docs", parsed)
                    examined, excluded, body_bytes = selected, omitted, byte_count
                else:
                    matches, module_matches = {}, {}
                    examined, excluded, body_bytes = [], list(self.bodies), 0

                preparation = {
                    "storage": "private index image shared within this explicit batch",
                    "reused": searchable,
                    "index_sha256": index_sha256,
                    "index_read_bytes": 0,
                    "index_verified": searchable,
                    "index_acquisition": (
                        "Shared batch preparation; zero additional image reads for this question."
                        if searchable
                        else (
                            "No searchable units or exact identity; this question did not require "
                            "the image."
                        )
                    ),
                    "source_verification": (
                        "No canonical source bodies read. Image verification and acquisition, when "
                        "needed, "
                        "are accounted for once in the enclosing batch. Use show or verify for "
                        "current exact acquisition."
                    ),
                }
                found = self._discovery_result(
                    query,
                    parsed,
                    matches,
                    module_matches,
                    examined,
                    excluded,
                    body_bytes,
                    limit=limit,
                    scan_limit=scan_limit,
                    max_bytes=max_bytes,
                    backend="fts5",
                    preparation=preparation,
                    indexed=True,
                )

                separator_bytes = 4 if not result["results"] else 2
                result_bytes += separator_bytes + batch_json_size(
                    found,
                    max_result_bytes - result_bytes - separator_bytes,
                    extra_indent=4,
                )
                result["results"].append(found)

            return result
        finally:
            if prepared is not None:
                prepared.close()

    def _discovery_result(
        self,
        query: str,
        parsed: dict[str, Any],
        matches: dict[str, dict[str, Any]],
        module_matches: dict[str, dict[str, Any]],
        examined: list[str],
        omitted: list[str],
        byte_count: int,
        *,
        limit: int,
        scan_limit: int,
        max_bytes: int,
        backend: str,
        preparation: dict[str, Any],
        indexed: bool,
    ) -> dict[str, Any]:
        """Shared ranking and coverage assembly for ordinary and batch discovery."""
        rows: list[dict[str, Any]] = []
        if query in examined:
            matches.setdefault(query, {"score": 0.0, "matched_terms": []})
        if query in self.modules:
            module_matches.setdefault(query, {"score": 0.0, "matched_terms": []})

        for ident, match in matches.items():
            row = self.bodies[ident]
            rows.append(
                {
                    "pin": self.pin(row),
                    "title": row["title"],
                    "summary": row["summary"],
                    **match,
                    "bytes": row["bytes"],
                    "exact_identity": query == ident,
                }
            )

        rows.sort(key=lambda row: (not row["exact_identity"], -row["score"], row["pin"]["id"]))
        matched_ids = {row["pin"]["id"] for row in rows}
        modules: list[dict[str, Any]] = []

        for row in self.modules.values():
            hits = [x for x in row["members"] if x in matched_ids]
            if hits or row["id"] in module_matches:
                match = module_matches.get(row["id"], {"score": 0.0, "matched_terms": []})
                modules.append(
                    {
                        "pin": self.pin(row, "module"),
                        "title": row["title"],
                        "purpose": row["purpose"],
                        "matching_members": hits,
                        "members": len(row["members"]),
                        **match,
                    }
                )

        if backend != "literal":
            modules.sort(
                key=lambda row: (
                    row["pin"]["id"] != query,
                    -row["score"],
                    -len(row["matching_members"]),
                    row["pin"]["id"],
                )
            )

        return {
            "format": "knowledge-discovery/1",
            "library_sha256": self.sha256,
            "bodies": rows[:limit],
            "modules": modules[:limit],
            "query": {
                "text": query,
                "backend": backend,
                "policy": SEARCH_POLICY,
                **parsed,
                "empty_after_filtering": not (parsed["terms"] or parsed["phrases"]),
            },
            "preparation": preparation,
            "coverage": {
                "examined_bodies": len(examined),
                "examined_body_bytes": 0 if indexed else byte_count,
                "body_examination": "prepared text-index representations"
                if indexed
                else "complete source bodies",
                "indexed_body_bytes": byte_count if indexed else 0,
                "verified_bodies": 0 if indexed else len(examined),
                "matched_bodies": len(rows),
                "omitted_body_count": len(omitted),
                "omitted_body_ids": omitted,
                "body_results_truncated": len(rows) > limit,
                "module_results_truncated": len(modules) > limit,
                "limits": {"results": limit, "bodies": scan_limit, "body_bytes": max_bytes},
            },
            "scope": (
                "Ranked lexical candidates and authored module membership nominate routes within "
                "the examined scope. "
                "Scores are not relevance probabilities. Complete-body reading, semantic fit and "
                "effect authority remain separate."
            ),
        }
