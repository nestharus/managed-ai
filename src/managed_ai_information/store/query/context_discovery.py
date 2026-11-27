"""Bounded, source-qualified discovery for the cooperative local agent system.

The host supplies useful questions, relation meanings and applicability. This
module searches declared local scopes and follows authored record relations; it
does not infer authority, semantic completeness or a universal dependency graph.
No persistent index is maintained. The returned selectors are candidates for a
later context, not a claim that a receiving participant has read their bodies.
"""

import copy
from typing import Any

from managed_ai_information.core.ownership import packet_id
from managed_ai_information.platform import strict_json_loads
from managed_ai_information.store.protocol.inputs import parse_input_bytes
from managed_ai_information.store.query.discovery_records import (
    inspect_record,
    prepare_record_contexts,
    read_record_bounded,
    read_verified_record_raw,
    verified_record_meta,
)
from managed_ai_information.store.query.discovery_report import (
    FIELDS,
    FORMAT,
    PREFIX_SCOPE,
    PROJECT_CONTEXT_BYTES,
    SCAN_ORDERS,
    SCOPE,
    SCOPES,
    SELECTOR,
    build_coverage,
    build_lexical_order,
    build_ranked_recovery,
    build_search_result,
    canonical,
    digest,
    integer,
    request,
    strings,
    validate_selection_pins,
    verify_pin_candidates,
)
from managed_ai_information.store.query.discovery_traversal import (
    build_incoming_edges,
    extract_record_edges,
    traverse_anchors,
)

__all__ = [
    "FIELDS",
    "FORMAT",
    "PREFIX_SCOPE",
    "PROJECT_CONTEXT_BYTES",
    "SCAN_ORDERS",
    "SCOPE",
    "SCOPES",
    "SELECTOR",
    "Discovery",
    "canonical",
    "digest",
    "integer",
    "request",
    "strings",
]

# Minimum valid native record JSON byte count (mandatory fields + 64-hex sha256 exceed 200B).
_MIN_RECORD_JSON_BYTES = 128
_MAX_ZERO_PROGRESS_RECORD_BYTE_SKIPS = 64


def _is_valid_project_member_path(name: Any) -> bool:
    if not isinstance(name, str) or not name or "\0" in name or "\\" in name:
        return False
    if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
        return False
    return all(part != ".." and not (len(part) >= 2 and part[1] == ":") for part in name.split("/"))


class Discovery:
    def __init__(self, system: Any) -> None:
        self.records = system.record_store
        self.techniques = system.techniques
        self.projects = system.projects

    def verify_selection(self, pins: Any) -> dict[str, Any]:
        """Resolve exact candidate pins before a later context uses their selectors."""
        refs, scopes = validate_selection_pins(pins)
        if not pins:
            return {"verified": [], "context_request": {}}

        current = self.search(
            {
                "anchors": refs,
                "scopes": [s for s in SCOPES if s in scopes],
                "hops": 0,
                "limit": 200,
                "scan_limit": 100000,
                "max_bytes": 33554432,
                "include_inactive": True,
            }
        )
        candidates = {c["ref"]: c for c in current["candidates"]}
        verify_pin_candidates(pins, candidates)

        verified = strict_json_loads(canonical(pins), "source pins", require_object=False)
        return {"verified": verified, "context_request": current["context_request"]}

    def _init_search_state(self, supplied: dict[str, Any]) -> None:
        self.req: dict[str, Any] = request(supplied, self.projects.current is not None)
        self.entries: dict[str, dict[str, Any]] = {}
        self.by_scope: dict[str, list[str]] = {scope: [] for scope in SCOPES}
        self.catalogue: dict[str, Any] | None = None
        self.examined: set[str] = set()
        self.loaded: dict[str, dict[str, Any]] = {}
        self.bodies: dict[str, str] = {}
        self.body_info: dict[str, dict[str, Any]] = {}
        self.body_failures: dict[str, str] = {}
        self.body_objects: dict[
            tuple[str, str, int], tuple[dict[str, Any], str | None, str | None]
        ] = {}
        self.reused_body_references = 0
        self.examined_bytes = 0
        self.skipped: list[dict[str, str]] = []
        self.unresolved: list[dict[str, Any]] = []
        self.frontier: list[dict[str, Any]] = []
        self._skips: set[tuple[str, str]] = set()
        self._unresolved: set[tuple[str, str, str | None]] = set()
        self.relation_rows: set[tuple[str, int]] = set()
        self.edges: list[dict[str, Any]] = []
        self.edge_keys: set[tuple[str, int]] = set()
        self.edge_cache: dict[str, list[dict[str, Any]]] = {}
        self.candidates: dict[str, dict[str, Any]] = {}
        self.incoming: dict[str, list[dict[str, Any]]] | None = None
        self.lexical_nomination: dict[str, Any] | None = None
        self.saturated: set[str] = set()
        self._records_fd: int | None = None
        self._records_dir_str: str | None = None
        self._verified_index: Any = None
        self._record_order_rank: dict[str, int] | None = None
        self._project_selection_copy: Any = (
            copy.deepcopy(self.projects.current.get("selection"))
            if self.projects.current is not None
            else None
        )
        self._catalogue_snapshot_copy: Any = None

    def _build_coverage(
        self,
        all_candidates: list[dict[str, Any]],
        shown: list[dict[str, Any]],
        omitted: list[str],
    ) -> dict[str, Any]:
        return build_coverage(self, all_candidates, shown, omitted)

    def _build_ranked_recovery(self) -> dict[str, Any] | None:
        return build_ranked_recovery(self.req)

    def search(self, supplied: dict[str, Any]) -> dict[str, Any]:
        """Return a bounded discovery report without writing or invoking effects."""
        self._init_search_state(supplied)
        fd_ctx, verified_ctx = prepare_record_contexts(self)

        with fd_ctx as records_fd, verified_ctx as verified_index:
            self._records_fd = records_fd
            self._verified_index = verified_index
            try:
                self._inventory()
                if self.req["anchors"]:
                    self._traverse()
                if self.req["query"].strip() or not self.req["anchors"]:
                    self._lexical()

                all_candidates = list(self.candidates.values())
                shown = all_candidates[: self.req["limit"]]
                selectors: dict[str, list[str]] = {key: [] for key in SELECTOR.values()}
                for candidate in shown:
                    self._selector(candidate)
                    for key, values in (candidate.get("selector") or {}).items():
                        selectors[key].extend(v for v in values if v not in selectors[key])
            finally:
                self._records_fd = None
                self._records_dir_str = None
                self._verified_index = None

        omitted = [c["ref"] for c in all_candidates[self.req["limit"] :]]
        if omitted:
            self.saturated.add("result-limit")
        return build_search_result(self, all_candidates, shown, selectors, omitted)

    def _inventory_project(self) -> None:
        for row in sorted(self.projects.current["project_files"], key=lambda r: r["path"]):
            name = row["path"]
            if not _is_valid_project_member_path(name):
                raise ValueError("invalid accepted-project member path")
            ref = "project:" + name
            if ref in self.entries:
                raise ValueError("duplicate accepted-project member")
            self.entries[ref] = {"scope": "project", "id": name, "row": row}
            self.by_scope["project"].append(ref)

    def _inventory_techniques(self) -> None:
        catalogue = self.techniques.index()
        self.catalogue = catalogue
        self._catalogue_snapshot_copy = copy.deepcopy(catalogue.get("snapshot"))
        entries = catalogue.get("entries", [])
        if not isinstance(entries, list):
            raise ValueError("catalogue entries must be a list")
        for row in sorted(entries, key=lambda r: r["id"]):
            ref = "technique:" + row["id"]
            if ref in self.entries:
                raise ValueError("duplicate catalogue entry")
            self.entries[ref] = {"scope": "techniques", "id": row["id"], "row": row}
            self.by_scope["techniques"].append(ref)

    def _inventory(self) -> None:
        if "records" in self.req["scopes"]:
            for ident in self.records.ids():
                ref = "record:" + ident
                self.entries[ref] = {"scope": "records", "id": ident}
                self.by_scope["records"].append(ref)
        if "project" in self.req["scopes"]:
            self._inventory_project()
        if "techniques" in self.req["scopes"]:
            self._inventory_techniques()

    def _skip(self, ref: str, reason: str) -> None:
        key = (ref, reason)
        if key not in self._skips:
            self._skips.add(key)
            self.skipped.append({"ref": ref, "reason": reason})

    def _unresolve(self, target: str, reason: str, declared_in: str | None = None) -> None:
        key = (target, reason, declared_in)
        if key not in self._unresolved:
            self._unresolved.add(key)
            self.unresolved.append({"target": target, "reason": reason, "declared_in": declared_in})

    def _resolve(self, target: Any, declared_in: str | None = None) -> str | None:
        if not isinstance(target, str) or not target:
            self._unresolve(str(target), "nonempty-reference-required", declared_in)
            return None
        if ":" not in target and "record:" + target in self.entries:
            target = "record:" + target

        scope = PREFIX_SCOPE.get(target.partition(":")[0])
        if scope is None:
            self._unresolve(target, "unknown-reference-or-undeclared-bare-record", declared_in)
            return None
        if scope not in self.req["scopes"]:
            self._unresolve(target, "outside-requested-scopes", declared_in)
            return None
        if target not in self.entries:
            self._unresolve(target, "absent-from-selected-inventory", declared_in)
            return None
        return target

    def _budget_bytes(self, ref: str, count: Any) -> bool:
        if type(count) is not int or count < 0:
            raise ValueError("source byte count must be a nonnegative integer")
        if self.examined_bytes + count > self.req["max_bytes"]:
            self.saturated.add("body-byte-budget")
            self._skip(ref, "body-byte-budget")
            return False
        self.examined_bytes += count
        return True

    def _read_record_bounded(self, ref: str, ident: str) -> tuple[dict[str, Any], bytes] | None:
        return read_record_bounded(self, ref, ident)

    def _verified_record_meta(
        self, ident: str
    ) -> tuple[int, Any, str, int, str, str, bytes, bool] | None:
        return verified_record_meta(self, ident)

    def _read_verified_record_raw(
        self, ident: str, expected_size: int, expected_raw_sha_bin: bytes
    ) -> tuple[bytes, dict[str, Any] | None]:
        return read_verified_record_raw(self, ident, expected_size, expected_raw_sha_bin)

    def _ensure_record_loaded(self, item: dict[str, Any]) -> dict[str, Any]:
        """Lazily parse the cryptographically verified record JSON for relation traversal."""
        if "record" in item:
            return item["record"]
        body_text = self._body(item["ref"])
        if "record" in item:
            return item["record"]
        if body_text is None:
            raise ValueError("verified record body unavailable for " + item["ref"])
        full_path = self.records.work / "records" / (packet_id(item["id"]) + ".json")
        record = parse_input_bytes(body_text.encode("utf-8"), full_path)
        item["record"] = record
        return record

    def _inspect(self, ref: str) -> dict[str, Any] | None:
        if ref in self.loaded:
            return self.loaded[ref]
        if ref in self.examined:
            return None
        if len(self.examined) >= self.req["scan_limit"]:
            self.saturated.add("entry-scan-budget")
            self._skip(ref, "entry-scan-budget")
            return None

        self.examined.add(ref)
        entry = self.entries[ref]
        scope, ident = entry["scope"], entry["id"]

        if scope == "records":
            value = inspect_record(self, ref, ident)
            if value is None:
                return None
        elif scope == "project":
            row = entry["row"]
            src = {
                "scope": scope,
                "path": ident,
                "sha256": row["sha256"],
                "bytes": row["bytes"],
                "selection": copy.deepcopy(self._project_selection_copy),
            }
            value = {
                "ref": ref,
                "scope": scope,
                "id": ident,
                "source": src,
                "title": ident,
                "metadata": ident,
            }
        else:
            row = entry["row"]
            metadata = " ".join(
                str(row.get(k, ""))
                for k in ("id", "title", "kind", "family", "source_path", "purpose")
            )
            src = {
                "scope": scope,
                "id": ident,
                "sha256": row["sha256"],
                "bytes": row["bytes"],
                "catalogue_snapshot": copy.deepcopy(self._catalogue_snapshot_copy),
                "source_root": row.get("source_root"),
                "source_path": row.get("source_path"),
            }
            value = {
                "ref": ref,
                "scope": scope,
                "id": ident,
                "source": src,
                "title": row.get("title", ident),
                "metadata": metadata,
            }

        self.loaded[ref] = value
        return value

    def _decode_and_retain_body(
        self, ref: str, raw: bytes, object_key: tuple[str, str, int] | None
    ) -> str | None:
        info: dict[str, Any] = {
            "bytes": len(raw),
            "sha256": digest(raw),
            "verified": True,
            "text": False,
        }
        failure: str | None = None
        body: str | None = None
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            body, failure = None, "not-utf8-text"
        if body is not None and "\x00" in body:
            body, failure = None, "contains-nul-not-context-text"

        info["text"] = failure is None
        if object_key is not None:
            self.body_objects[object_key] = (info, body, failure)
        return self._retain_body(ref, info, body, failure)

    def _body_record(self, ref: str, ident: str) -> str | None:
        item = self.loaded.get(ref)
        if item is None or "_raw_sha_bin" not in item:
            return None
        raw, parsed = self._read_verified_record_raw(ident, item["_size"], item["_raw_sha_bin"])
        text = raw.decode("utf-8")
        self.bodies[ref] = text
        if parsed is not None:
            item["record"] = parsed
        return text

    def _technique_object_key(self, ref: str, row: dict[str, Any]) -> tuple[str, str, int] | None:
        if row.get("availability") == "sealed" and ref not in self.req["anchors"]:
            self.body_failures[ref] = "sealed-source"
            self._skip(ref, "sealed-source")
            return None
        if not isinstance(row.get("body_path"), str) or not isinstance(row.get("sha256"), str):
            raise ValueError("catalogue body path and digest must be text")
        if type(row.get("bytes")) is not int or row["bytes"] < 0:
            raise ValueError("source byte count must be a nonnegative integer")
        return (row["body_path"], row["sha256"], row["bytes"])

    def _body(self, ref: str) -> str | None:
        if ref in self.bodies:
            return self.bodies[ref]
        if ref in self.body_failures:
            return None

        entry = self.entries[ref]
        row, scope = entry.get("row"), entry["scope"]
        if scope == "records":
            return self._body_record(ref, entry["id"])
        if row is None:
            return None
        if scope == "project" and row["bytes"] > PROJECT_CONTEXT_BYTES:
            self.body_failures[ref] = "exceeds-project-context-body-bound"
            self._skip(ref, self.body_failures[ref])
            return None

        object_key: tuple[str, str, int] | None = None
        if scope == "techniques":
            object_key = self._technique_object_key(ref, row)
            if object_key is None:
                return None
            if object_key in self.body_objects:
                self.reused_body_references += 1
                return self._retain_body(ref, *self.body_objects[object_key])

        if not self._budget_bytes(ref, row["bytes"]):
            self.body_failures[ref] = "body-byte-budget"
            return None

        raw = (
            self.projects.body(row)
            if scope == "project"
            else self.techniques.body(row, byte_limit=row["bytes"])
        )
        if len(raw) != row["bytes"] or digest(raw) != row["sha256"]:
            raise ValueError("discovery source binding changed: " + ref)
        return self._decode_and_retain_body(ref, raw, object_key)

    def _retain_body(
        self, ref: str, info: dict[str, Any], body: str | None, failure: str | None
    ) -> str | None:
        self.body_info[ref] = dict(info)
        if failure is not None or body is None:
            reason = failure or "source-not-available-as-context-text"
            self.body_failures[ref] = reason
            self._skip(ref, reason)
            return None
        self.bodies[ref] = body
        return body

    def _candidate(self, item: dict[str, Any], match: dict[str, Any]) -> None:
        ref = item["ref"]
        if ref not in self.candidates:
            self.candidates[ref] = {k: item[k] for k in ("ref", "title", "source")}
            self.candidates[ref]["matches"] = []
        if match not in self.candidates[ref]["matches"]:
            self.candidates[ref]["matches"].append(match)

    def _selector(self, candidate: dict[str, Any]) -> None:
        ref = candidate["ref"]
        entry = self.entries[ref]
        body = self._body(ref)
        if body is None:
            candidate["selector"] = None
            candidate["acquisition_needed"] = self.body_failures.get(
                ref, "source-not-available-as-context-text"
            )
        else:
            candidate["selector"] = {SELECTOR[entry["scope"]]: [entry["id"]]}
            candidate["body_identity"] = self.body_info[ref]
            candidate["acquisition_needed"] = None

    def _lexical_order(self) -> list[str]:
        refs, nomination = build_lexical_order(self.by_scope, self.req)
        self.lexical_nomination = nomination
        return refs

    def _record_skip_exhausted(
        self, scope: str, ref: str, zero_progress_skips: int
    ) -> tuple[bool, int]:
        if scope != "records" or (ref, "body-byte-budget") not in self._skips:
            return False, zero_progress_skips
        if self.req["max_bytes"] < _MIN_RECORD_JSON_BYTES:
            return True, zero_progress_skips
        zero_progress_skips += 1
        return zero_progress_skips >= _MAX_ZERO_PROGRESS_RECORD_BYTE_SKIPS, zero_progress_skips

    @staticmethod
    def _terms_match(terms: list[str], raw_text: str) -> bool:
        if all(t in raw_text for t in terms):
            return True
        if not any("-" in t or "_" in t for t in terms):
            return False
        norm_text = raw_text.replace("_", "-")
        return all((t in raw_text) or (t.replace("_", "-") in norm_text) for t in terms)

    def _match_lexical_item(self, ref: str, item: dict[str, Any], terms: list[str]) -> None:
        metadata = item["metadata"].casefold()
        metadata_match = self._terms_match(terms, metadata)
        body = self._body(ref) if (terms and not metadata_match and self.req["content"]) else None
        searchable = metadata + ("\n" + body.casefold() if body is not None else "")
        if metadata_match or self._terms_match(terms, searchable):
            self._candidate(
                item,
                {
                    "kind": "lexical" if terms else "inventory",
                    "terms": terms,
                    "channel": "metadata" if metadata_match else "metadata-and-complete-body",
                },
            )

    def _lexical(self) -> None:
        terms = self.req["query"].casefold().split()
        records_only_scope = self.req["scopes"] == ["records"]
        records_byte_exhausted = False
        zero_progress_skips = 0

        for ref in self._lexical_order():
            if len(self.examined) >= self.req["scan_limit"] and ref not in self.examined:
                self.saturated.add("entry-scan-budget")
                self._skip(ref, "entry-scan-budget")
                break

            scope = self.entries[ref]["scope"]
            remaining = self.req["max_bytes"] - self.examined_bytes
            if (
                scope == "records"
                and ref not in self.examined
                and (records_byte_exhausted or remaining < _MIN_RECORD_JSON_BYTES)
            ):
                self.saturated.add("body-byte-budget")
                if records_only_scope:
                    break
                continue

            item = self._inspect(ref)
            if item is None:
                exhausted, zero_progress_skips = self._record_skip_exhausted(
                    scope, ref, zero_progress_skips
                )
                if exhausted:
                    records_byte_exhausted = True
                    if records_only_scope:
                        break
                continue

            if scope == "records":
                zero_progress_skips = 0
            self._match_lexical_item(ref, item, terms)

    def _record_edges(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        return extract_record_edges(self, item)

    def _incoming(self) -> dict[str, list[dict[str, Any]]]:
        return build_incoming_edges(self)

    def _traverse(self) -> None:
        traverse_anchors(self)
