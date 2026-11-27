"""Prepared SQLite FTS5 index building, batch discovery, and ranking assembly for Library."""

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from managed_ai_information.platform import publish_exclusive, sha256_file, sync_dir

from .format import encoded, integer, need, ordinary, sha, text
from .lexical import SEARCH_POLICY, FtsAnalyzer, search_query

_BATCH_ENCODER = json.JSONEncoder(sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)


def batch_json_size(value: Any, maximum: int, extra_indent: int = 0) -> int:
    """Count the actual indented UTF-8 representation using C-accelerated encoding."""
    rendered = _BATCH_ENCODER.encode(value)
    size = extra_indent + len(rendered.encode("utf-8")) + extra_indent * rendered.count("\n")
    need(size <= maximum, "discovery batch exceeds its JSON byte bound")
    return size


def literal_search(items: list[dict[str, Any]], terms: list[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for row in items:
        surface = "\n".join(row[k] for k in ("id", "title", "summary", "text")).casefold()
        hits = [term for term in terms if term in surface]
        if hits:
            found[row["id"]] = {"score": len(hits), "matched_terms": hits}
    return found


def retained_preparation_metadata() -> dict[str, Any]:
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
            "Retained until a different corpus replaces this catalogue's index, or close() ends "
            "the instance."
        ),
    }


def build_prepared_sqlite(
    library: Any, staged: Path, basis: dict[str, Any], *, sqlite_mod: Any
) -> None:
    if sqlite_mod is None:
        raise ValueError("SQLite support unavailable; use ordinary BM25 discovery")
    db = sqlite_mod.connect(staged)
    try:
        db.execute("PRAGMA page_size=4096")
        db.execute(f"PRAGMA max_page_count={library.config.search.max_index_bytes // 4096}")
        db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        db.execute("INSERT INTO metadata VALUES(?,?)", ("basis", encoded(basis).decode("utf-8")))
        for table in ("body_docs", "module_docs"):
            FtsAnalyzer.create(db, table, config=library.config.search)
        db.executemany(
            "INSERT INTO body_docs(rowid,id,title,summary,body) VALUES(?,?,?,?,?)",
            (
                (
                    i,
                    ident,
                    (r := library.bodies[ident])["title"],
                    r["summary"],
                    library.body_text(ident),
                )
                for i, ident in enumerate(basis["bodies"], 1)
            ),
        )
        db.executemany(
            "INSERT INTO module_docs(rowid,id,title,summary,body) VALUES(?,?,?,?,?)",
            (
                (i, r["id"], r["title"], r["purpose"], "")
                for i, r in enumerate(library.modules.values(), 1)
            ),
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


def prepare_library_index(
    library: Any,
    output: Path | str,
    scan_limit: int,
    max_bytes: int,
    *,
    sqlite_mod: Any,
) -> dict[str, Any]:
    """Build a fresh immutable derivative outside the delivered library."""
    basis = library.index_basis(scan_limit, max_bytes)
    target = ordinary(output).resolve()
    library_directory = ordinary(library.directory).resolve()

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

        build_prepared_sqlite(library, staged, basis, sqlite_mod=sqlite_mod)

        staged_size = staged.lstat().st_size
        need(
            staged_size <= library.config.search.max_index_bytes,
            "file exceeds byte bound: " + str(staged),
        )
        index_sha = sha256_file(staged)

        with staged.open("r+b") as stream:
            os.fsync(stream.fileno())

        publish_exclusive(staged, target, error_message="prepared index output already exists")
        sync_dir(target.parent)
        _, omitted, _ = library.search_scope(scan_limit, max_bytes)

        return {
            "format": "knowledge-search-preparation/1",
            "library_sha256": library.sha256,
            "index": {
                "path": str(target),
                "sha256": index_sha,
                "bytes": staged_size,
                "format": library.config.search.index_format,
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


def _parse_batch_questions(
    library: Any,
    queries: list[str],
    index: Path | str,
    index_sha256: str,
    limits: tuple[int, int, int, int],
) -> list[tuple[str, dict[str, Any], bool]]:
    limit, scan_limit, max_bytes, max_result_bytes = limits
    bcfg, scfg = library.config.batch, library.config.search
    need(
        isinstance(queries, list) and 1 <= len(queries) <= bcfg.max_queries,
        f"queries must be a list of 1 to {bcfg.max_queries} questions",
    )
    integer(limit, "result limit", 1, scfg.max_limit)
    integer(scan_limit, "scan limit", 1, library.config.limits.max_bodies)
    integer(max_bytes, "search bytes", 1, library.config.limits.max_package_bytes)
    integer(max_result_bytes, "batch result bytes", 1, bcfg.max_result_bytes)
    need(index is not None, "prepared index path is required for a batch")
    sha(index_sha256)
    questions: list[tuple[str, dict[str, Any], bool]] = []
    for query in queries:
        text(query, "query", scfg.max_query_bytes)
        parsed = search_query(query, "fts5", config=scfg)
        searchable = (
            bool(parsed["terms"] or parsed["phrases"])
            or query in library.bodies
            or query in library.modules
        )
        questions.append((query, parsed, searchable))
    batch_json_size(queries, bcfg.max_input_bytes - 1)
    return questions


def assemble_discovery_result(
    library: Any,
    query: str,
    parsed: dict[str, Any],
    matches: dict[str, dict[str, Any]],
    module_matches: dict[str, dict[str, Any]],
    examined: list[str],
    omitted: list[str],
    byte_count: int,
    limits: tuple[int, int, int],
    backend: str,
    preparation: dict[str, Any],
    *,
    indexed: bool,
) -> dict[str, Any]:
    """Shared ranking and coverage assembly for ordinary and batch discovery."""
    limit, scan_limit, max_bytes = limits
    rows: list[dict[str, Any]] = []
    if query in examined:
        matches.setdefault(query, {"score": 0.0, "matched_terms": []})
    if query in library.modules:
        module_matches.setdefault(query, {"score": 0.0, "matched_terms": []})

    for ident, match in matches.items():
        row = library.bodies[ident]
        rows.append(
            {
                "pin": library.pin(row),
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

    for row in library.modules.values():
        hits = [x for x in row["members"] if x in matched_ids]
        if hits or row["id"] in module_matches:
            match = module_matches.get(row["id"], {"score": 0.0, "matched_terms": []})
            modules.append(
                {
                    "pin": library.pin(row, "module"),
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
        "library_sha256": library.sha256,
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


def run_discover_batch(
    library: Any,
    queries: list[str],
    index: Path | str,
    index_sha256: str,
    limits: tuple[int, int, int, int],
    reader: Callable[[Path | str, int], bytes],
    sqlite_mod: Any,
    prepared_cls: type[Any],
) -> dict[str, Any]:
    """Nominate bounded questions from one explicitly shared, verified index image."""
    limit, scan_limit, max_bytes, max_result_bytes = limits
    questions = _parse_batch_questions(library, queries, index, index_sha256, limits)

    prepared: Any = None
    selected: list[str] = []
    omitted: list[str] = []
    byte_count = 0

    try:
        if any(searchable for _, _, searchable in questions):
            prepared = prepared_cls(
                index,
                index_sha256,
                library.index_basis(scan_limit, max_bytes),
                config=library.config,
                reader=reader,
                sqlite_mod=sqlite_mod,
            )
            selected, omitted, byte_count = library.search_scope(scan_limit, max_bytes)

        bcfg = library.config.batch
        scfg = library.config.search
        result: dict[str, Any] = {
            "format": "knowledge-discovery-batch/1",
            "library_sha256": library.sha256,
            "preparation": {
                "storage": "one private prepared index image for this batch",
                "index_sha256": index_sha256,
                "index_verified": prepared is not None,
                "index_opens": int(prepared is not None),
                "index_read_bytes": prepared.bytes if prepared is not None else 0,
                "canonical_body_read_bytes": 0,
                "lifetime": "This call only; the shared image is closed before return or refusal.",
                "source_verification": (
                    "No canonical source bodies read. Searchable questions share one verified "
                    "derivative and its exact catalogue/analyzer/scope basis. The selected build "
                    "warrants the postings; use show or verify for current exact acquisition."
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
                examined, excluded, body_bytes = [], list(library.bodies), 0

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
                    "needed, are accounted for once in the enclosing batch. Use show or verify for "
                    "current exact acquisition."
                ),
            }
            found = assemble_discovery_result(
                library,
                query,
                parsed,
                matches,
                module_matches,
                examined,
                excluded,
                body_bytes,
                (limit, scan_limit, max_bytes),
                "fts5",
                preparation,
                indexed=True,
            )

            separator_bytes = 4 if not result["results"] else 2
            result_bytes += separator_bytes + batch_json_size(
                found, max_result_bytes - result_bytes - separator_bytes, extra_indent=4
            )
            result["results"].append(found)

        return result
    finally:
        if prepared is not None:
            prepared.close()
