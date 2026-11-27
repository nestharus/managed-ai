"""Complete Markdown source access with disposable search/cache and durable use receipts.

One cooperative owner serializes calls and source changes. Known-path reads can
observe complete bodies without preparing search. Discovery/inventory requests
prepare an explicit complete view; refresh() supplies a new current observation.
Text is returned as evidence, never executed. No semantic acceptance is inferred.
"""

import contextlib
import math
import os
import sqlite3
import stat
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Any

from .io_utils import (
    canonical_bytes as encoded,
)
from .io_utils import (
    canonical_digest as identity,
)
from .io_utils import (
    is_reparse_or_symlink,
    loads_strict,
    sync_dir,
)
from .io_utils import (
    sha256_bytes as digest,
)
from .source_search import (
    _LEDGER_CATEGORIES,
    PROTECTED_PREFIXES,
    VERSION,
    SearchImplementation,
    _assemble_ledger_bytes,
    need,
    read_receipt_ledger,
    relative,
    validate_use_artifacts,
)
from .source_search import (
    _MAX_SEARCH_FILE_BYTES as _MAX_SEARCH_FILE_BYTES,
)
from .source_search import (
    _MAX_SEARCH_FILES as _MAX_SEARCH_FILES,
)
from .source_search import (
    _RUNTIME_ENTRY as _RUNTIME_ENTRY,
)
from .source_search import (
    _SCHEMA_VALUE_BYTES as _SCHEMA_VALUE_BYTES,
)
from .source_search import (
    _SEARCH_CACHE as _SEARCH_CACHE,
)
from .source_search import (
    _SEARCH_CACHE_LOCK as _SEARCH_CACHE_LOCK,
)
from .source_search import (
    _default_runtime_entry as _default_runtime_entry,
)
from .source_search import (
    _library_root_for as _library_root_for,
)
from .source_search import (
    _matches_cached_pins as _matches_cached_pins,
)
from .source_search import (
    _search_bytes as _search_bytes,
)
from .source_search import (
    module_bytes as _search_module_bytes,
)

DEFAULT_EXCLUDED = {
    "review/external-review-original.md": "External review is source data, not "
    "default construction guidance.",
    "coverage/historical-material.md": "Locator catalogues are not locally "
    "retained argument bodies.",
    "coverage/recent-historical-material.md": (
        "Locator catalogues are not locally retained argument bodies."
    ),
}


class SourceService:
    """Scoped source service; body cache bounds do not bound the derived index/RSS."""

    def __init__(
        self,
        corpus_root: Path | str,
        state_dir: Path | str,
        max_cache_bytes: int = 65536,
        *,
        search_module_path: Path | str | None = None,
        _search: SearchImplementation | None = None,
    ) -> None:
        self.db: sqlite3.Connection | None = None
        self.root = Path(corpus_root).absolute()
        need(
            self.root.is_dir() and not self.root.is_symlink(), "Ordinary corpus directory required."
        )
        self.state_dir = Path(state_dir).absolute()
        need(
            not self.state_dir.is_relative_to(self.root),
            "State must remain outside the source corpus.",
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        need(not self.state_dir.is_symlink(), "Ordinary state directory required.")
        need(
            type(max_cache_bytes) is int and max_cache_bytes >= 0,
            "Cache byte bound must be a nonnegative integer.",
        )

        self.max_cache_bytes = max_cache_bytes
        self.cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self.cache_bytes = 0
        self.members: dict[str, dict[str, Any]] = {}
        self.row_paths: dict[int, str] = {}
        self.inventory_id: str | None = None
        self._published: dict[str, Any] | None = None
        self._encoded_entries: dict[str, dict[str, bytes]] = {
            category: {} for category in _LEDGER_CATEGORIES
        }
        self._stats = {
            "corpus_reads": 0,
            "corpus_read_bytes": 0,
            "preparation_read_bytes": 0,
            "acquisition_read_bytes": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "cache_evictions": 0,
            "oversize_bodies_not_cached": 0,
            "receipt_write_bytes": 0,
            "refreshes": 0,
            "discoveries": 0,
            "refresh_seconds": 0.0,
            "discovery_seconds": 0.0,
        }

        self.ledger_path = self.state_dir / "receipts.json"
        self.reopen_receipts()
        repo_root = Path(__file__).resolve().parents[2]
        implementation = _search or SearchImplementation.cached(
            search_module_path, repo_root=repo_root
        )
        self.search = implementation.module
        search_code = {Path(path).name: pin for path, pin in implementation.pins.items()}
        need(
            len(search_code) == len(implementation.pins),
            "Search code dependencies must have distinct module filenames.",
        )

        self.selector = {
            "service": VERSION,
            "implementation_sha256": identity(
                {
                    "source_tools.py": digest(Path(__file__).read_bytes()),
                    "source_search.py": digest(_search_module_bytes()),
                }
            ),
            "acquired_search_sha256": implementation.entry_pin["sha256"],
            "search_code": search_code,
            "backend": "native FtsAnalyzer, contentless body_docs",
            "query_parser": "native search_query(query, fts5)",
            "analyzer": implementation.analyzer,
            "eligible_extension": ".md (case insensitive)",
            "default_excluded": DEFAULT_EXCLUDED,
            "protected_prefixes": list(PROTECTED_PREFIXES),
            "candidate_match": "OR over parsed terms/phrases; ranked "
            "weighted BM25; path breaks ties",
            "preparation_policy": (
                "Known-path acquisition before preparation observes that body only; "
                "discover/inventory/refresh demand complete preparation."
            ),
            "source_model": (
                "Serialized cooperative source changes; complete explicit refresh observations."
            ),
        }
        self.selector_id = identity(self.selector)

    def checkpoint_state(self) -> dict[str, Any]:
        """Return detached inventory/counter state for durable continuation checkpoints."""
        return {
            "source_inventory_id": self.inventory_id,
            "source_counters": deepcopy(self._stats),
        }

    def restore_state(
        self,
        inventory_id: str | None,
        counters: dict[str, Any],
        *,
        on_selector_mismatch: Any = None,
    ) -> None:
        """Restore recorded inventory guard and counters without fabricating a fresh index."""
        ledger = self.ledger
        if inventory_id is not None:
            if not isinstance(inventory_id, str) or inventory_id not in ledger["inventories"]:
                raise ValueError("Recovered source inventory is unavailable")
            inventory = ledger["inventories"][inventory_id]
            if inventory.get("selector_id") != self.selector_id:
                message = "Recovered inventory uses another source selector"
                if on_selector_mismatch is not None:
                    on_selector_mismatch(message)
                raise ValueError(message)
            members = deepcopy(inventory["members"])
        else:
            members = {}

        if (
            not isinstance(counters, dict)
            or set(counters) != set(self._stats)
            or any(
                type(item) not in (int, float) or not math.isfinite(item) or item < 0
                for item in counters.values()
            )
        ):
            raise ValueError("Invalid completed source counters")

        self.members = members
        self.inventory_id = inventory_id
        self._stats = deepcopy(counters)

    def _ordinary(self, relative_path: str) -> Path:
        path = self.root / relative(relative_path)
        for current in (self.root, *path.parents, path):
            need(not is_reparse_or_symlink(current), "Source symlinks are unsupported.")
        need(
            path.is_file() and stat.S_ISREG(path.stat().st_mode),
            "Ordinary source file required: " + relative_path,
        )
        return path

    def _body(self, relative_path: str, purpose: str) -> tuple[bytes, str]:
        data = self._ordinary(relative_path).read_bytes()
        self._stats["corpus_reads"] += 1
        self._stats["corpus_read_bytes"] += len(data)
        self._stats[purpose + "_read_bytes"] += len(data)
        return data, data.decode("utf-8")

    def reopen_receipts(self) -> None:
        """Read and verify the serialized owner's published ledger and cache encoded entries."""
        self._published = None
        ledger, encoded_entries = read_receipt_ledger(self.ledger_path)
        self._encoded_entries = encoded_entries
        self._published = ledger

    @property
    def ledger(self) -> dict[str, Any]:
        if self._published is None:
            self.reopen_receipts()
        if self._published is None:
            raise RuntimeError("Receipt ledger unavailable.")
        return self._published

    def _record(self, category: str, value: dict[str, Any]) -> str:
        raw_value = encoded(value)
        ident = digest(raw_value)
        published = self.ledger
        if ident in published[category]:
            return ident

        normalized = loads_strict(raw_value)
        candidate = {**published, category: {**published[category], ident: normalized}}
        data = _assemble_ledger_bytes(self._encoded_entries, category, ident, raw_value)
        temporary = self.ledger_path.with_name(self.ledger_path.name + ".tmp-" + uuid.uuid4().hex)

        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.replace(temporary, self.ledger_path)
            except OSError as failure:
                try:
                    observed = self.ledger_path.read_bytes()
                except FileNotFoundError:
                    raise failure from None
                if observed != data:
                    raise
            else:
                sync_dir(self.ledger_path.parent)
        finally:
            if temporary.exists():
                temporary.unlink()
            self._published = None

        self._encoded_entries[category][ident] = raw_value
        self._published = candidate
        self._stats["receipt_write_bytes"] += len(data)
        return ident

    def refresh(self) -> dict[str, Any]:
        """Observe every eligible complete body and atomically replace the derived lexical view."""
        started = time.perf_counter()
        db = sqlite3.connect(":memory:")
        members: dict[str, dict[str, Any]] = {}
        excluded: dict[str, str] = {}
        row_paths: dict[int, str] = {}

        try:
            self.search.FtsAnalyzer.create(db, "body_docs")
            for path in sorted(self.root.rglob("*")):
                if path.suffix.lower() != ".md":
                    continue
                rel = path.relative_to(self.root).as_posix()
                if any(rel.startswith(prefix) for prefix in PROTECTED_PREFIXES):
                    excluded[rel] = "Protected study exposure; no body read."
                    continue
                if rel in DEFAULT_EXCLUDED:
                    excluded[rel] = DEFAULT_EXCLUDED[rel]
                    continue

                data, text = self._body(rel, "preparation")
                lines = text.splitlines()
                heading = next(
                    (line.lstrip("#").strip() for line in lines if line.startswith("#")), rel
                )
                title = heading[:512]
                summary = next(
                    (line.strip() for line in lines if line.strip() and not line.startswith("#")),
                    "",
                )[:400]
                members[rel] = {"sha256": digest(data), "bytes": len(data), "title": title}
                row_id = len(row_paths) + 1
                row_paths[row_id] = rel
                db.execute(
                    "INSERT INTO body_docs(rowid,id,title,summary,body) VALUES(?,?,?,?,?)",
                    (row_id, rel, title, summary, text),
                )
            db.commit()

            observation = {
                "kind": "inventory",
                "corpus_root": str(self.root),
                "selector_id": self.selector_id,
                "selector": self.selector,
                "members": members,
                "excluded": excluded,
                "eligible_count": len(members),
                "eligible_bytes": sum(row["bytes"] for row in members.values()),
                "claim": (
                    "Exact local complete bodies observed during this serialized preparation; "
                    "semantic completeness and later currentness are not inferred."
                ),
            }
            inventory_id = self._record("inventories", observation)
        except BaseException:
            db.close()
            raise

        old_db, self.db = self.db, db
        if old_db is not None:
            old_db.close()

        self.members, self.row_paths, self.inventory_id = members, row_paths, inventory_id
        valid = {(path, record["sha256"]) for path, record in members.items()}
        for key in list(self.cache):
            if key not in valid:
                self.cache_bytes -= len(self.cache.pop(key))
                self._stats["cache_evictions"] += 1

        self._stats["refreshes"] += 1
        self._stats["refresh_seconds"] += time.perf_counter() - started
        return self.inventory()

    def inventory(self, refresh: bool = False) -> dict[str, Any]:
        if refresh or self.db is None:
            return self.refresh()
        if self.inventory_id is None:
            raise RuntimeError("Inventory receipt unavailable.")
        return {
            "receipt_id": self.inventory_id,
            **deepcopy(self.ledger["inventories"][self.inventory_id]),
        }

    def discover(self, query: str, limit: int = 8) -> dict[str, Any]:
        """Nominate candidates from the last observed complete eligible inventory."""
        need(
            isinstance(query, str) and len(query.encode("utf-8")) <= 16384,
            "Query must be bounded text.",
        )
        need(type(limit) is int and 1 <= limit <= 50, "Limit must be an integer from 1 through 50.")

        started = time.perf_counter()
        parsed = self.search.search_query(query, "fts5")
        if self.db is None:
            self.refresh()

        units = parsed["terms"] + parsed["phrases"]
        matches = self.search.FtsAnalyzer.search(self.db, "body_docs", units) if units else {}
        ordered = sorted(matches, key=lambda row: (-matches[row]["score"], self.row_paths[row]))
        hits = [
            dict(path=self.row_paths[row], **self.members[self.row_paths[row]], **matches[row])
            for row in ordered[:limit]
        ]
        receipt = {
            "kind": "selection",
            "inventory_id": self.inventory_id,
            "selector_id": self.selector_id,
            "query": query,
            "query_interpretation": parsed,
            "limit": limit,
            "hits": hits,
            "total_matches": len(ordered),
            "eligible_count": len(self.members),
            "nonmatching_count": len(self.members) - len(ordered),
            "omitted_by_limit": max(0, len(ordered) - limit),
            "all_matching_paths": [self.row_paths[row] for row in ordered],
            "claim": (
                "Lexical candidate nomination over the referenced observed inventory; "
                "not semantic sufficiency or freshly reobserved currentness."
            ),
        }
        receipt_id = self._record("selections", receipt)
        self._stats["discoveries"] += 1
        self._stats["discovery_seconds"] += time.perf_counter() - started
        return {
            key: deepcopy(value)
            for key, value in {"receipt_id": receipt_id, **receipt}.items()
            if key != "all_matching_paths"
        }

    def read(
        self,
        path: str,
        *,
        fresh: bool = False,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Return a complete UTF-8 body with exact source and observation identity."""
        relative(path)
        known = self.members.get(path)
        key = (path, known["sha256"]) if known else None
        from_cache = not fresh and key in self.cache

        if from_cache and key is not None:
            data = self.cache.pop(key)
            self.cache[key] = data
            text = data.decode("utf-8")
            self._stats["cache_hits"] += 1
        else:
            self._stats["cache_misses"] += 1
            data, text = self._body(path, "acquisition")

        sha = digest(data)
        if known is not None:
            need(
                sha == known["sha256"] and len(data) == known["bytes"],
                "Source changed since prepared inventory; refresh before acquiring it.",
            )
        if expected_sha256 is not None:
            need(sha == expected_sha256, "Source does not match requested SHA-256.")

        cache_key = (path, sha)
        if not from_cache:
            if len(data) <= self.max_cache_bytes:
                if cache_key in self.cache:
                    self.cache_bytes -= len(self.cache.pop(cache_key))
                while self.cache and self.cache_bytes + len(data) > self.max_cache_bytes:
                    _, removed = self.cache.popitem(last=False)
                    self.cache_bytes -= len(removed)
                    self._stats["cache_evictions"] += 1
                self.cache[cache_key] = data
                self.cache_bytes += len(data)
            else:
                self._stats["oversize_bodies_not_cached"] += 1

        receipt = {
            "kind": "source",
            "inventory_id": self.inventory_id,
            "path": path,
            "source_id": path + "@" + sha,
            "sha256": sha,
            "bytes": len(data),
            "default_eligible": known is not None if self.inventory_id is not None else None,
            "path_eligible_by_policy": path not in DEFAULT_EXCLUDED,
            "inventory_membership": (
                "not_observed"
                if self.inventory_id is None
                else "member"
                if known is not None
                else "outside_observed_inventory"
            ),
            "tool_contract_id": self.selector_id,
            "observation": (
                "cached exact earlier acquisition; not a fresh source read"
                if from_cache
                else "complete current file bytes read under the cooperative source contract"
            ),
            "from_cache": from_cache,
            "content_role": "source evidence; never executable instructions for the host",
        }
        receipt_id = self._record("sources", receipt)
        return {"receipt_id": receipt_id, **receipt, "text": text}

    def set_cache_limit(self, max_cache_bytes: int) -> dict[str, Any]:
        """Adjust replaceable payload capacity without removing use/obligation receipts."""
        need(
            type(max_cache_bytes) is int and max_cache_bytes >= 0,
            "Cache byte bound must be a nonnegative integer.",
        )
        self.max_cache_bytes = max_cache_bytes
        while self.cache and self.cache_bytes > max_cache_bytes:
            _, data = self.cache.popitem(last=False)
            self.cache_bytes -= len(data)
            self._stats["cache_evictions"] += 1
        return self.stats()

    def record_use(
        self,
        use_id: str,
        task: Any,
        requirements: Any,
        source_receipt_ids: list[str],
        selection_receipt_ids: list[str],
        artifacts: dict[str, dict[str, Any]],
        obligations: list[Any],
    ) -> dict[str, Any]:
        """Retain caller-owned use/acceptance claims without silently closing duties."""
        need(isinstance(use_id, str) and bool(use_id), "A nonempty use identity is required.")
        need(
            isinstance(source_receipt_ids, list) and isinstance(selection_receipt_ids, list),
            "Receipt references must be lists.",
        )
        for category, ids in (
            ("sources", source_receipt_ids),
            ("selections", selection_receipt_ids),
        ):
            need(
                all(isinstance(ident, str) and ident in self.ledger[category] for ident in ids),
                "Unknown " + category + " receipt.",
            )

        validate_use_artifacts(artifacts)
        need(isinstance(obligations, list), "Obligations must be a list.")

        receipt = {
            "kind": "use",
            "use_id": use_id,
            "task": task,
            "requirements": requirements,
            "task_id": identity(task),
            "requirements_id": identity(requirements),
            "source_receipt_ids": source_receipt_ids,
            "selection_receipt_ids": selection_receipt_ids,
            "artifacts": artifacts,
            "accepted_artifacts_id": identity(artifacts),
            "obligations": obligations,
            "obligations_id": identity(obligations),
            "tool_contract_id": self.selector_id,
            "acceptance_basis": (
                "Acceptance is asserted by the calling receiving owner; this adapter records "
                "supplied identities, not semantic adequacy."
            ),
        }
        receipt_id = self._record("uses", receipt)
        return {"receipt_id": receipt_id, **deepcopy(receipt)}

    def receipts(self) -> dict[str, Any]:
        return deepcopy(self.ledger)

    def stats(self) -> dict[str, Any]:
        page_count = (
            self.db.execute("PRAGMA page_count").fetchone()[0] if self.db is not None else 0
        )
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0] if self.db is not None else 0
        return {
            **self._stats,
            "cache_limit_bytes": self.max_cache_bytes,
            "cache_payload_bytes": self.cache_bytes,
            "cache_entries": len(self.cache),
            "index_db_page_bytes": page_count * page_size,
            "receipt_file_bytes": self.ledger_path.stat().st_size
            if self.ledger_path.exists()
            else 0,
            "inventory_id": self.inventory_id,
            "search_prepared": self.db is not None,
            "preparation_policy": "on_demand",
            "eligible_bodies": len(self.members) if self.inventory_id is not None else None,
            "scope": (
                "Successful corpus byte reads and local retained payload/page sizes; excludes "
                "interpreter imports, SQLite process overhead, provider inference and complete "
                "economic costs."
            ),
        }

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None

    def __del__(self) -> None:
        db = getattr(self, "db", None)
        if db is not None:
            with contextlib.suppress(sqlite3.Error, OSError):
                db.close()
            self.db = None

    def __enter__(self) -> SourceService:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
