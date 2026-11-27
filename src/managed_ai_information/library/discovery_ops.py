"""Prepared inverted index building, batch discovery, and ranking assembly for Library."""

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from managed_ai_information.platform import publish_exclusive, sha256_file, sync_dir

from .format import integer, need, ordinary, sha, text
from .lexical import SEARCH_POLICY, FtsAnalyzer, search_query

_ENC = json.JSONEncoder(sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)


def batch_json_size(value: Any, maximum: int, extra_indent: int = 0) -> int:
    r = _ENC.encode(value)
    sz = extra_indent + len(r.encode("utf-8")) + extra_indent * r.count("\n")
    need(sz <= maximum, "discovery batch exceeds its JSON byte bound")
    return sz


def literal_search(items: list[dict[str, Any]], terms: list[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for row in items:
        s = "\n".join(row[k] for k in ("id", "title", "summary", "text")).casefold()
        if hits := [t for t in terms if t in s]:
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
            "No canonical source bodies read on this call. The same in-memory inverted indexes "
            "retain bodies read and hash-verified when this catalogue and selected corpus were "
            "prepared. Use show for fresh exact acquisition."
        ),
        "lifetime": (
            "Retained until a different corpus replaces this catalogue's index, or close() ends "
            "the instance."
        ),
    }


def build_prepared_index_bytes(library: Any, basis: dict[str, Any]) -> bytes:
    st = FtsAnalyzer.new_store()
    try:
        for t in ("body_docs", "module_docs"):
            FtsAnalyzer.create(st, t, config=library.config.search)
        for i, ident in enumerate(basis["bodies"], 1):
            r = library.bodies[ident]
            FtsAnalyzer.add(
                st, "body_docs", i, ident, r["title"], r["summary"], library.body_text(ident)
            )
        for i, r in enumerate(library.modules.values(), 1):
            FtsAnalyzer.add(st, "module_docs", i, r["id"], r["title"], r["purpose"], "")
        return st.serialize(basis)
    finally:
        st.close()


def prepare_library_index(
    library: Any, output: Path | str, scan_limit: int, max_bytes: int
) -> dict[str, Any]:
    basis = library.index_basis(scan_limit, max_bytes)
    tgt, ldir = ordinary(output).resolve(), ordinary(library.directory).resolve()
    need(tgt.parent.is_dir(), "prepared index parent directory must exist")
    need(ldir != tgt and ldir not in tgt.parents, "prepared index must be outside the library")
    need(not tgt.exists(), "prepared index output already exists")
    staged: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".knowledge-index-", dir=tgt.parent)
        os.close(fd)
        staged = Path(name)
        pl = build_prepared_index_bytes(library, basis)
        need(
            len(pl) <= library.config.search.max_index_bytes,
            "file exceeds byte bound: " + str(staged),
        )
        with staged.open("wb") as s:
            s.write(pl)
            s.flush()
            os.fsync(s.fileno())
        sz, isha = staged.lstat().st_size, sha256_file(staged)
        publish_exclusive(staged, tgt, error_message="prepared index output already exists")
        sync_dir(tgt.parent)
        _, om, _ = library.search_scope(scan_limit, max_bytes)
        return {
            "format": "knowledge-search-preparation/1",
            "library_sha256": library.sha256,
            "index": {
                "path": str(tgt),
                "sha256": isha,
                "bytes": sz,
                "format": library.config.search.index_format,
            },
            "basis": basis,
            "omitted_body_ids": om,
            "source_verification": (
                "Every indexed complete body read and hash-verified during this build."
            ),
            "scope": (
                "Disposable lexical derivative, not source custody. Select this actual build "
                "receipt; a self-declared index digest does not warrant its construction. Exact "
                "acquisition still uses show."
            ),
        }
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _parse_qs(
    lib: Any, qs: list[str], index: Path | str, isha: str, limits: tuple[int, int, int, int]
) -> list[tuple[str, dict[str, Any], bool]]:
    lim, slim, mb, mrb = limits
    bc, sc = lib.config.batch, lib.config.search
    need(
        isinstance(qs, list) and 1 <= len(qs) <= bc.max_queries,
        f"queries must be a list of 1 to {bc.max_queries} questions",
    )
    integer(lim, "result limit", 1, sc.max_limit)
    integer(slim, "scan limit", 1, lib.config.limits.max_bodies)
    integer(mb, "search bytes", 1, lib.config.limits.max_package_bytes)
    integer(mrb, "batch result bytes", 1, bc.max_result_bytes)
    need(index is not None, "prepared index path is required for a batch")
    sha(isha)
    out: list[tuple[str, dict[str, Any], bool]] = []
    for q in qs:
        text(q, "query", sc.max_query_bytes)
        p = search_query(q, "fts5", config=sc)
        out.append((q, p, bool(p["terms"] or p["phrases"]) or q in lib.bodies or q in lib.modules))
    batch_json_size(qs, bc.max_input_bytes - 1)
    return out


def assemble_discovery_result(
    lib: Any,
    q: str,
    parsed: dict[str, Any],
    matches: dict[str, dict[str, Any]],
    mmatches: dict[str, dict[str, Any]],
    ex: list[str],
    om: list[str],
    bc: int,
    limits: tuple[int, int, int],
    backend: str,
    prep: dict[str, Any],
    *,
    indexed: bool,
) -> dict[str, Any]:
    lim, slim, mb = limits
    if q in ex:
        matches.setdefault(q, {"score": 0.0, "matched_terms": []})
    if q in lib.modules:
        mmatches.setdefault(q, {"score": 0.0, "matched_terms": []})
    rows: list[dict[str, Any]] = [
        {
            "pin": lib.pin(lib.bodies[i]),
            "title": lib.bodies[i]["title"],
            "summary": lib.bodies[i]["summary"],
            **m,
            "bytes": lib.bodies[i]["bytes"],
            "exact_identity": q == i,
        }
        for i, m in matches.items()
    ]
    rows.sort(key=lambda r: (not r["exact_identity"], -r["score"], r["pin"]["id"]))
    mids = {r["pin"]["id"] for r in rows}
    mods: list[dict[str, Any]] = []
    for r in lib.modules.values():
        hits = [x for x in r["members"] if x in mids]
        if hits or r["id"] in mmatches:
            mods.append(
                {
                    "pin": lib.pin(r, "module"),
                    "title": r["title"],
                    "purpose": r["purpose"],
                    "matching_members": hits,
                    "members": len(r["members"]),
                    **mmatches.get(r["id"], {"score": 0.0, "matched_terms": []}),
                }
            )
    if backend != "literal":
        mods.sort(
            key=lambda r: (
                r["pin"]["id"] != q,
                -r["score"],
                -len(r["matching_members"]),
                r["pin"]["id"],
            )
        )
    return {
        "format": "knowledge-discovery/1",
        "library_sha256": lib.sha256,
        "bodies": rows[:lim],
        "modules": mods[:lim],
        "query": {
            "text": q,
            "backend": backend,
            "policy": SEARCH_POLICY,
            **parsed,
            "empty_after_filtering": not (parsed["terms"] or parsed["phrases"]),
        },
        "preparation": prep,
        "coverage": {
            "examined_bodies": len(ex),
            "examined_body_bytes": 0 if indexed else bc,
            "body_examination": (
                "prepared text-index representations" if indexed else "complete source bodies"
            ),
            "indexed_body_bytes": bc if indexed else 0,
            "verified_bodies": 0 if indexed else len(ex),
            "matched_bodies": len(rows),
            "omitted_body_count": len(om),
            "omitted_body_ids": om,
            "body_results_truncated": len(rows) > lim,
            "module_results_truncated": len(mods) > lim,
            "limits": {"results": lim, "bodies": slim, "body_bytes": mb},
        },
        "scope": (
            "Ranked lexical candidates and authored module membership nominate routes within the "
            "examined scope. Scores are not relevance probabilities. Complete-body reading, "
            "semantic fit and effect authority remain separate."
        ),
    }


def run_discover_batch(
    lib: Any,
    queries: list[str],
    index: Path | str,
    isha: str,
    limits: tuple[int, int, int, int],
    reader: Callable[[Path | str, int], bytes],
    prepared_cls: type[Any],
) -> dict[str, Any]:
    lim, slim, mb, mrb = limits
    qs = _parse_qs(lib, queries, index, isha, limits)
    prep: Any = None
    sel, om, bc = [], [], 0
    try:
        if any(s for _, _, s in qs):
            prep = prepared_cls(
                index, isha, lib.index_basis(slim, mb), config=lib.config, reader=reader
            )
            sel, om, bc = lib.search_scope(slim, mb)
        bcfg, scfg = lib.config.batch, lib.config.search
        res: dict[str, Any] = {
            "format": "knowledge-discovery-batch/1",
            "library_sha256": lib.sha256,
            "preparation": {
                "storage": "one private prepared index image for this batch",
                "index_sha256": isha,
                "index_verified": prep is not None,
                "index_opens": int(prep is not None),
                "index_read_bytes": prep.bytes if prep is not None else 0,
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
                "result_bytes": mrb,
            },
            "results": [],
        }
        rb = batch_json_size(res, mrb - 1) + 1
        for q, p, s in qs:
            if s and prep is not None:
                m, mm = prep.search("body_docs", p), prep.search("module_docs", p)
                ex, exc, bb = sel, om, bc
            else:
                m, mm, ex, exc, bb = {}, {}, [], list(lib.bodies), 0
            qprep = {
                "storage": "private index image shared within this explicit batch",
                "reused": s,
                "index_sha256": isha,
                "index_read_bytes": 0,
                "index_verified": s,
                "index_acquisition": (
                    "Shared batch preparation; zero additional image reads for this question."
                    if s
                    else (
                        "No searchable units or exact identity; this question did not require "
                        "the image."
                    )
                ),
                "source_verification": (
                    "No canonical source bodies read. Image verification and acquisition, when "
                    "needed, are accounted for once in the enclosing batch. Use show or verify "
                    "for current exact acquisition."
                ),
            }
            found = assemble_discovery_result(
                lib, q, p, m, mm, ex, exc, bb, (lim, slim, mb), "fts5", qprep, indexed=True
            )
            sep = 4 if not res["results"] else 2
            rb += sep + batch_json_size(found, mrb - rb - sep, extra_indent=4)
            res["results"].append(found)
        return res
    finally:
        if prep is not None:
            prep.close()
