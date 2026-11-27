"""Lexical search index engines over verified library bodies."""

import math
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import DEFAULT_LIBRARY_CONFIG, LibraryConfig
from .contract import SearchScope
from .format import digest, need, read_bytes, sha
from .lexical import TOKEN, FtsAnalyzer, LexicalStore


class SearchIndex:
    """Disposable in-memory candidate preparation over already verified bodies."""

    def __init__(
        self,
        rows: list[dict[str, Any]],
        backend: str,
        *,
        config: LibraryConfig = DEFAULT_LIBRARY_CONFIG,
    ) -> None:
        self.backend, self.config = backend, config
        self.db: LexicalStore | None = None
        self.ids = {r["id"] for r in rows}
        self._phrase_rows: list[tuple[str, tuple[str, str, str, str]]] | None = None
        self._fields: dict[str, list[str]] | None = None
        if backend == "fts5":
            st = FtsAnalyzer.new_store()
            FtsAnalyzer.create(st, "docs", config=config.search)
            for r in rows:
                FtsAnalyzer.add(st, "docs", r["id"], r["id"], r["title"], r["summary"], r["text"])
            self.db = st
            return
        self.frequencies: dict[str, Counter[str]] = {}
        self.df: Counter[str] = Counter()
        self.lengths: dict[str, int] = {}
        raw_s: list[tuple[str, tuple[str, str, str, str]]] = []
        for r in rows:
            i, fq = r["id"], Counter[str]()
            for k in ("id", "title", "summary", "text"):
                fq.update(TOKEN.findall(r[k].casefold()))
            self.frequencies[i], self.lengths[i] = fq, sum(fq.values())
            for t in fq:
                self.df[t] += 1
            raw_s.append((i, (r["id"], r["title"], r["summary"], r["text"])))
        self._phrase_rows = raw_s
        self.average = sum(self.lengths.values()) / max(1, len(rows)) or 1.0

    @property
    def fields(self) -> dict[str, list[str]]:
        if self._fields is None:
            self._fields = {
                i: [" " + " ".join(TOKEN.findall(p.casefold())) + " " for p in ps]
                for i, ps in (self._phrase_rows or ())
            }
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
        pc: dict[str, dict[str, int]] = {}
        pdf: dict[str, int] = {}
        if parsed["phrases"]:
            sf = self.fields
            for ph in parsed["phrases"]:
                pat = re.compile("(?=" + re.escape(" " + ph + " ") + ")")
                cs = {i: sum(len(pat.findall(f)) for f in fl) for i, fl in sf.items()}
                pc[ph], pdf[ph] = cs, sum(c > 0 for c in cs.values())
        res: dict[str, dict[str, Any]] = {}
        n = len(self.ids)
        for i, fq in self.frequencies.items():
            norm = 1.2 * (0.25 + 0.75 * self.lengths[i] / self.average)
            hits, sc = [], 0.0
            for t in parsed["terms"]:
                if f := fq[t]:
                    hits.append(t)
                    sc += (
                        math.log1p((n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                        * f
                        * 2.2
                        / (f + norm)
                    )
            for ph in parsed["phrases"]:
                if f := pc[ph][i]:
                    hits.append(ph)
                    sc += math.log1p((n - pdf[ph] + 0.5) / (pdf[ph] + 0.5)) * f * 2.2 / (f + norm)
            if hits:
                res[i] = {"score": sc, "matched_terms": hits}
        return res


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
    ) -> None:
        self.db: LexicalStore | None = None
        self.temporary: Any = None
        self.config = config
        data = reader(path, config.search.max_index_bytes)
        need(digest(data) == sha(expected_sha256), "prepared index differs from expected digest")
        self.bytes, self.sha256, self.basis = len(data), expected_sha256, basis
        try:
            st = LexicalStore.deserialize(
                data,
                max_uncompressed_bytes=max(config.search.max_index_bytes * 4, 64 * 1024 * 1024),
            )
            need(
                st.basis == basis,
                "prepared index basis differs; rebuild for this catalogue, analyzer and scope",
            )
            for tn, ids in (("body_docs", basis["bodies"]), ("module_docs", basis["modules"])):
                need(
                    st.table(tn).keys == list(range(1, len(ids) + 1)),
                    "prepared index row coverage differs",
                )
            self.db = st
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
            ids[r - 1]: h
            for r, h in FtsAnalyzer.search(self.db, table, units, config=self.config.search).items()
        }

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None

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
        config: LibraryConfig = DEFAULT_LIBRARY_CONFIG,
    ) -> None:
        self.scope = scope
        self.bodies = SearchIndex(documents, scope.backend, config=config)
        try:
            self.modules = SearchIndex(modules, scope.backend, config=config)
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
