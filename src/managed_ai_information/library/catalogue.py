"""Verified read-only published source library reader and lexical discovery."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import discovery_ops as _dops
from . import format as _fmt
from .config import DEFAULT_LIBRARY_CONFIG, LibraryConfig
from .contract import SearchScope
from .index import PreparedIndex, RetainedSearch
from .lexical import SEARCH_POLICY, FtsAnalyzer, search_query

_DEFAULT_READ_BYTES = _fmt.read_bytes
INDEX_FORMAT = DEFAULT_LIBRARY_CONFIG.search.index_format
MAX_INDEX = DEFAULT_LIBRARY_CONFIG.search.max_index_bytes
MAX_BATCH_QUERIES = DEFAULT_LIBRARY_CONFIG.batch.max_queries
MAX_BATCH_INPUT = DEFAULT_LIBRARY_CONFIG.batch.max_input_bytes
MAX_BATCH_RESULT = DEFAULT_LIBRARY_CONFIG.batch.max_result_bytes
batch_json_size = _dops.batch_json_size
_literal_search = _dops.literal_search
read_bytes = _fmt.read_bytes
__all__ = [
    "INDEX_FORMAT",
    "MAX_BATCH_INPUT",
    "MAX_BATCH_QUERIES",
    "MAX_BATCH_RESULT",
    "MAX_INDEX",
    "Library",
    "batch_json_size",
]


def _resolve_read_bytes() -> Callable[[Path | str, int], bytes]:
    return read_bytes


class Library:
    """Verified read-only source library with bounded lexical nomination."""

    def __init__(
        self,
        directory: Path | str,
        *,
        config: LibraryConfig = DEFAULT_LIBRARY_CONFIG,
        _preloaded_raw: tuple[bytes, str] | None = None,
        membus_backend: Any | None = None,
        membus_namespace: str | None = None,
    ) -> None:
        self.config = config
        self.directory = _fmt.ordinary(directory)
        _fmt.need(self.directory.is_dir(), "library directory required")
        self._membus_backend, self._membus_namespace = membus_backend, membus_namespace
        if _preloaded_raw is not None:
            self.raw, self.sha256 = _preloaded_raw
        else:
            self.raw = _resolve_read_bytes()(
                self.directory / "catalogue.json", config.limits.max_manifest_bytes
            )
            self.sha256 = _fmt.digest(self.raw)
        self.catalogue = _fmt.check_manifest(_fmt.decoded(self.raw), config=config)
        self.bodies = {r["id"]: r for r in self.catalogue["bodies"]}
        self.modules = {r["id"]: r for r in self.catalogue["modules"]}
        self._module_pins = {
            r["id"]: {
                "library_sha256": self.sha256,
                "id": r["id"],
                "sha256": _fmt.digest(_fmt.encoded(r)),
            }
            for r in self.catalogue["modules"]
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
        if (c := self._module_pins.get(row["id"])) is not None and row is self.modules.get(
            row["id"]
        ):
            return dict(c)
        return {
            "library_sha256": self.sha256,
            "id": row["id"],
            "sha256": _fmt.digest(_fmt.encoded(row)),
        }

    def _read_body(self, ident: str) -> bytes:
        _fmt.need(ident in self.bodies, "body unavailable in this library: " + ident)
        row, lim, rdr = self.bodies[ident], self.config.limits.max_body_bytes, _resolve_read_bytes()
        data = (
            _fmt.read_bytes_under(self.directory, row["object"], lim)
            if rdr is _DEFAULT_READ_BYTES
            else rdr(self.directory / row["object"], lim)
        )
        _fmt.need(
            len(data) == row["bytes"] and _fmt.digest(data) == row["sha256"],
            "body differs from declared source: " + ident,
        )
        return data

    def body_text(self, ident: str) -> str:
        return self._read_body(ident).decode("utf-8")

    def body(self, ident: str) -> bytes:
        data = self._read_body(ident)
        data.decode("utf-8")
        return data

    def show(self, ident: str) -> dict[str, Any]:
        _fmt.need(ident in self.bodies, "body unavailable in this library: " + ident)
        row = self.bodies[ident]
        return {"pin": self.pin(row), "entry": row, "text": self.body_text(ident)}

    def verify(self) -> dict[str, Any]:
        tot, mp = 0, self.config.limits.max_package_bytes
        for i in self.bodies:
            tot += len(self.body(i))
            _fmt.need(tot <= mp, "library exceeds total verified byte bound")
        return {
            "library_sha256": self.sha256,
            "edition": self.catalogue["edition"],
            "bodies": len(self.bodies),
            "modules": len(self.modules),
            "body_bytes": tot,
            "scope": (
                "Exact complete bodies and declared required closure; no semantic adequacy claim."
            ),
        }

    def search_scope(self, scan_limit: int, max_bytes: int) -> tuple[list[str], list[str], int]:
        _fmt.integer(scan_limit, "scan limit", 1, self.config.limits.max_bodies)
        _fmt.integer(max_bytes, "search bytes", 1, self.config.limits.max_package_bytes)
        sel: list[str] = []
        om: list[str] = []
        tot = 0
        for i, r in self.bodies.items():
            if len(sel) >= scan_limit or r["bytes"] > max_bytes - tot:
                om.append(i)
            else:
                sel.append(i)
                tot += r["bytes"]
        return sel, om, tot

    def index_basis(self, scan_limit: int, max_bytes: int) -> dict[str, Any]:
        sel, _, tot = self.search_scope(scan_limit, max_bytes)
        return {
            "format": self.config.search.index_format,
            "library_sha256": self.sha256,
            "policy": SEARCH_POLICY,
            "backend": "fts5",
            "analyzer": FtsAnalyzer.identity(self.config.search),
            "bodies": sel,
            "modules": list(self.modules),
            "body_bytes": tot,
            "limits": {"bodies": scan_limit, "body_bytes": max_bytes},
        }

    def prepare_index(
        self,
        output: Path | str,
        scan_limit: int = DEFAULT_LIBRARY_CONFIG.search.default_scan_limit,
        max_bytes: int = DEFAULT_LIBRARY_CONFIG.search.default_max_bytes,
    ) -> dict[str, Any]:
        return _dops.prepare_library_index(self, output, scan_limit, max_bytes)

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
        return self._discover(
            query, limit, scan_limit, max_bytes, backend, index, index_sha256, allow_retained=False
        )

    def discover_retained_scope(
        self,
        query: str,
        limit: int = DEFAULT_LIBRARY_CONFIG.search.default_limit,
        scan_limit: int = DEFAULT_LIBRARY_CONFIG.search.default_scan_limit,
        max_bytes: int = DEFAULT_LIBRARY_CONFIG.search.default_max_bytes,
    ) -> dict[str, Any]:
        return self._discover(
            query, limit, scan_limit, max_bytes, "fts5", None, None, allow_retained=True
        )

    def _search_membus(
        self, q: str, parsed: dict[str, Any], ex: list[str], lim: int
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        cli = self._membus_backend
        if cli is None or not self._membus_namespace:
            raise ValueError(
                "membus backend requires configured membus_backend and membus_namespace"
            )
        ex_s, us = set(ex), parsed["terms"] + parsed["phrases"]
        bh: dict[str, dict[str, Any]] = {}
        mh: dict[str, dict[str, Any]] = {}
        for h in cli.search(
            q, namespace=self._membus_namespace, mode="Bm25", limit=max(lim * 4, 50)
        ):
            if h.visibility == "Sealed":
                continue
            r = self.bodies.get(h.doc_id) if h.doc_id in ex_s else self.modules.get(h.doc_id)
            if r is None:
                continue
            desc = r.get("summary", r.get("purpose", ""))
            sn = str(getattr(h, "snippet", "") or "")
            hay = f"{h.doc_id} {h.title or ''} {sn} {r.get('title', '')} {desc}".casefold()
            (bh if h.doc_id in ex_s else mh)[h.doc_id] = {
                "score": float(h.score),
                "matched_terms": [u for u in us if u.casefold() in hay],
            }
        return bh, mh

    def _run_backend(
        self,
        q: str,
        parsed: dict[str, Any],
        scope: SearchScope,
        docs: list[dict[str, Any]],
        mdocs: list[dict[str, Any]],
        flags: tuple[bool, bool],
        backend: str,
        idx: tuple[Path | str | None, str | None],
        lims: tuple[int, int, int],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], bool, int]:
        searchable, reusable = flags
        index, isha = idx
        lim, slim, mb = lims
        if not searchable:
            return {}, {}, False, 0
        if index is not None:
            if isha is None:
                raise ValueError(
                    "prepared index path and expected digest must be supplied together"
                )
            with PreparedIndex(
                index,
                isha,
                self.index_basis(slim, mb),
                config=self.config,
                reader=_resolve_read_bytes(),
            ) as p:
                return p.search("body_docs", parsed), p.search("module_docs", parsed), True, p.bytes
        if backend == "membus":
            return *self._search_membus(q, parsed, list(scope.body_ids), lim), False, 0
        if backend == "literal":
            ts = parsed["terms"]
            return _literal_search(docs, ts), _literal_search(mdocs, ts), False, 0
        if not reusable:
            self.close()
            self._search_cache = RetainedSearch(scope, docs, mdocs, config=self.config)
        if self._search_cache is None:
            raise RuntimeError("retained search cache was not initialized")
        return *self._search_cache.search(parsed), reusable, 0

    def _discover(
        self,
        q: str,
        lim: int,
        slim: int,
        mb: int,
        backend: str,
        index: Path | str | None,
        isha: str | None,
        *,
        allow_retained: bool,
    ) -> dict[str, Any]:
        cfg = self.config.search
        _fmt.text(q, "query", cfg.max_query_bytes)
        _fmt.integer(lim, "result limit", 1, cfg.max_limit)
        _fmt.integer(slim, "scan limit", 1, self.config.limits.max_bodies)
        _fmt.integer(mb, "search bytes", 1, self.config.limits.max_package_bytes)
        _fmt.need(
            (index is None) == (isha is None),
            "prepared index path and expected digest must be supplied together",
        )
        _fmt.need(index is None or backend == "fts5", "prepared index requires --backend fts5")
        if index is not None:
            _fmt.sha(isha)
        parsed = search_query(q, backend, config=cfg)
        searchable = (
            bool(parsed["terms"] or parsed["phrases"]) or q in self.bodies or q in self.modules
        )
        ex, om, bc = self.search_scope(slim, mb) if searchable else ([], list(self.bodies), 0)
        scope = SearchScope(self.sha256, SEARCH_POLICY, backend, tuple(ex))
        reusable = self.prepared_scope == scope
        if allow_retained and searchable and reusable and self._search_cache is not None:
            m, mm = self._search_cache.search(parsed)
            return _dops.assemble_discovery_result(
                self,
                q,
                parsed,
                m,
                mm,
                ex,
                om,
                bc,
                (lim, slim, mb),
                backend,
                _dops.retained_preparation_metadata(),
                indexed=True,
            )
        docs = (
            [
                dict(self.bodies[i], text=self.body_text(i))
                if not reusable
                else (self.body_text(i), None)[1]
                for i in ex
            ]
            if (index is None and backend != "membus")
            else []
        )
        docs_clean = [d for d in docs if d is not None]
        mdocs = [
            {"id": r["id"], "title": r["title"], "summary": r["purpose"], "text": ""}
            for r in self.modules.values()
        ]
        m, mm, reused, ib = self._run_backend(
            q,
            parsed,
            scope,
            docs_clean,
            mdocs,
            (searchable, reusable),
            backend,
            (index, isha),
            (lim, slim, mb),
        )
        indexed = index is not None or backend == "membus"
        prep = {
            "storage": "pinned external contentless index"
            if index is not None
            else "membus-v2 Tantivy BM25"
            if backend == "membus"
            else "in-memory Library instance only",
            "reused": reused,
            "index_sha256": isha,
            "index_read_bytes": ib,
            "index_verified": bool(ib),
            "source_verification": (
                "No canonical source bodies read on this call. Indexed nomination relies on the "
                "selected build; index_verified states whether its bytes and basis were checked. "
                "Use show or verify for current exact acquisition."
                if indexed
                else "Every examined complete body read and hash-verified on this call."
            ),
        }
        return _dops.assemble_discovery_result(
            self, q, parsed, m, mm, ex, om, bc, (lim, slim, mb), backend, prep, indexed=indexed
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
        return _dops.run_discover_batch(
            self,
            queries,
            index,
            index_sha256,
            (limit, scan_limit, max_bytes, max_result_bytes),
            _resolve_read_bytes(),
            PreparedIndex,
        )
