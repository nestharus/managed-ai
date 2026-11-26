"""Lexical search index engines over verified library bodies.

Separates in-memory BM25/FTS5 indexing (SearchIndex, RetainedSearch) and
pinned external SQLite FTS5 derivative queries (PreparedIndex) from catalogue
custody. Implements lazy phrase-surface materialization (P7-12) so BM25 term
queries avoid retaining duplicated full-text field copies in RAM.
"""

import math
import re
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import DEFAULT_LIBRARY_CONFIG, LibraryConfig
from .contract import SearchScope
from .format import decoded, digest, need, read_bytes, sha
from .lexical import SQLITE_ERRORS, TOKEN, FtsAnalyzer, sqlite3

_UNSET: Any = object()


class SearchIndex:
    """Disposable in-memory candidate preparation over already verified bodies."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        backend: str,
        *,
        sqlite_mod: Any = _UNSET,
        sqlite_errors: tuple[type[BaseException], ...] | None = None,
        config: LibraryConfig = DEFAULT_LIBRARY_CONFIG,
    ) -> None:
        self.backend = backend
        self.config = config
        self.db: Any = None
        self.ids = {row["id"] for row in rows}
        self._phrase_rows: list[tuple[str, tuple[str, str, str, str]]] | None = None
        self._fields: dict[str, list[str]] | None = None

        sql = sqlite3 if sqlite_mod is _UNSET else sqlite_mod
        sql_errs = SQLITE_ERRORS if sqlite_errors is None else sqlite_errors

        if backend == "fts5":
            if sql is None:
                raise ValueError("SQLite support unavailable; use --backend bm25")
            try:
                self.db = sql.connect(":memory:")
                FtsAnalyzer.create(self.db, "docs", config=config.search)
                self.db.executemany(
                    "INSERT INTO docs VALUES(?,?,?,?)",
                    [(r["id"], r["title"], r["summary"], r["text"]) for r in rows],
                )
            except sql_errs as exc:
                self.close()
                raise ValueError(
                    "SQLite FTS5 preparation unavailable; use --backend bm25: " + str(exc)
                ) from exc
            return

        self.frequencies: dict[str, Counter[str]] = {}
        self.df: Counter[str] = Counter()
        self.lengths: dict[str, int] = {}
        raw_surfaces: list[tuple[str, tuple[str, str, str, str]]] = []

        for row in rows:
            ident = row["id"]
            freq: Counter[str] = Counter()
            for field_key in ("id", "title", "summary", "text"):
                freq.update(TOKEN.findall(row[field_key].casefold()))
            self.frequencies[ident] = freq
            self.lengths[ident] = sum(freq.values())
            for term in freq:
                self.df[term] += 1
            raw_surfaces.append((ident, (row["id"], row["title"], row["summary"], row["text"])))

        # Defer building padded token strings until a quoted phrase query arrives (P7-12).
        self._phrase_rows = raw_surfaces
        self.average = sum(self.lengths.values()) / max(1, len(rows)) or 1.0

    @property
    def fields(self) -> dict[str, list[str]]:
        """Lazily materialize tokenized phrase-search surfaces on first phrase query."""
        if self._fields is None:
            built: dict[str, list[str]] = {}
            for ident, parts in self._phrase_rows or ():
                built[ident] = [" " + " ".join(TOKEN.findall(p.casefold())) + " " for p in parts]
            self._fields = built
            self._phrase_rows = None
        return self._fields

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self) -> SearchIndex:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def search(self, parsed: dict[str, Any]) -> dict[str, dict[str, Any]]:
        units = parsed["terms"] + parsed["phrases"]
        if not units:
            return {}

        if self.backend == "fts5":
            return FtsAnalyzer.search(self.db, "docs", units, config=self.config.search)

        phrase_counts: dict[str, dict[str, int]] = {}
        phrase_df: dict[str, int] = {}

        if parsed["phrases"]:
            surfaces = self.fields
            for phrase in parsed["phrases"]:
                pattern = re.compile("(?=" + re.escape(" " + phrase + " ") + ")")
                counts = {
                    ident: sum(len(pattern.findall(field)) for field in field_list)
                    for ident, field_list in surfaces.items()
                }
                phrase_counts[phrase] = counts
                phrase_df[phrase] = sum(count > 0 for count in counts.values())

        result: dict[str, dict[str, Any]] = {}
        count = len(self.ids)

        for ident, frequencies in self.frequencies.items():
            norm = 1.2 * (0.25 + 0.75 * self.lengths[ident] / self.average)
            hits: list[str] = []
            score = 0.0

            for term in parsed["terms"]:
                frequency = frequencies[term]
                if frequency:
                    hits.append(term)
                    idf = math.log1p((count - self.df[term] + 0.5) / (self.df[term] + 0.5))
                    score += idf * frequency * 2.2 / (frequency + norm)

            for phrase in parsed["phrases"]:
                frequency = phrase_counts[phrase][ident]
                if frequency:
                    hits.append(phrase)
                    idf = math.log1p((count - phrase_df[phrase] + 0.5) / (phrase_df[phrase] + 0.5))
                    score += idf * frequency * 2.2 / (frequency + norm)

            if hits:
                result[ident] = {"score": score, "matched_terms": hits}

        return result


class PreparedIndex:
    """Query exact pinned derivative bytes, without claiming fresh source reads."""

    def __init__(
        self,
        path: Path | str,
        expected_sha256: str,
        basis: dict[str, Any],
        *,
        config: LibraryConfig = DEFAULT_LIBRARY_CONFIG,
        reader: Callable[[Path | str, int], bytes] = read_bytes,
        sqlite_mod: Any = _UNSET,
    ) -> None:
        self.db: Any = None
        self.temporary: tempfile.TemporaryDirectory[str] | None = None
        self.config = config
        sql = sqlite3 if sqlite_mod is _UNSET else sqlite_mod

        if sql is None:
            raise ValueError("SQLite support unavailable; use ordinary BM25 discovery")
        data = reader(path, config.search.max_index_bytes)
        need(digest(data) == sha(expected_sha256), "prepared index differs from expected digest")

        self.bytes = len(data)
        self.sha256 = expected_sha256
        self.basis = basis

        try:
            self.db = sql.connect(":memory:")
            if hasattr(self.db, "deserialize"):
                self.db.deserialize(data)
            else:
                self.db.close()
                self.db = None
                self.temporary = tempfile.TemporaryDirectory(prefix="knowledge-index-")
                copied = Path(self.temporary.name) / "verified.sqlite"
                copied.write_bytes(data)
                self.db = sql.connect(copied.as_uri() + "?mode=ro", uri=True)

            self.db.execute("PRAGMA query_only=ON")
            self.db.execute("PRAGMA trusted_schema=OFF")

            row = self.db.execute("SELECT value FROM metadata WHERE key='basis'").fetchone()
            need(
                row is not None
                and isinstance(row[0], str)
                and len(row[0].encode("utf-8")) <= config.limits.max_manifest_bytes,
                "invalid prepared index basis",
            )
            need(
                decoded(row[0]) == basis,
                "prepared index basis differs; rebuild for this catalogue, analyzer and scope",
            )

            for query_sql, identities in (
                ("SELECT rowid FROM body_docs ORDER BY rowid", basis["bodies"]),
                ("SELECT rowid FROM module_docs ORDER BY rowid", basis["modules"]),
            ):
                found = [r[0] for r in self.db.execute(query_sql)]
                need(
                    found == list(range(1, len(identities) + 1)),
                    "prepared index row coverage differs",
                )
        except Exception:
            self.close()
            raise

    def search(self, table: str, parsed: dict[str, Any]) -> dict[str, dict[str, Any]]:
        need(table in {"body_docs", "module_docs"}, "invalid prepared index table")
        units = parsed["terms"] + parsed["phrases"]
        if not units:
            return {}

        ids = self.basis["bodies" if table == "body_docs" else "modules"]
        return {
            ids[row - 1]: hit
            for row, hit in FtsAnalyzer.search(
                self.db, table, units, config=self.config.search
            ).items()
        }

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None

        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None

    def __enter__(self) -> PreparedIndex:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


class RetainedSearch:
    """Paired in-memory body and module indexes bound to one SearchScope."""

    def __init__(
        self,
        scope: SearchScope,
        documents: list[dict[str, Any]],
        modules: list[dict[str, Any]],
        *,
        sqlite_mod: Any = _UNSET,
        sqlite_errors: tuple[type[BaseException], ...] | None = None,
        config: LibraryConfig = DEFAULT_LIBRARY_CONFIG,
    ) -> None:
        self.scope = scope
        self.bodies = SearchIndex(
            documents,
            scope.backend,
            sqlite_mod=sqlite_mod,
            sqlite_errors=sqlite_errors,
            config=config,
        )
        try:
            self.modules = SearchIndex(
                modules,
                scope.backend,
                sqlite_mod=sqlite_mod,
                sqlite_errors=sqlite_errors,
                config=config,
            )
        except Exception:
            self.bodies.close()
            raise

    def search(
        self, parsed: dict[str, Any]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        return self.bodies.search(parsed), self.modules.search(parsed)

    def close(self) -> None:
        self.bodies.close()
        self.modules.close()
