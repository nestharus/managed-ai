"""Request validation, selection pins, lexical ordering, and coverage reporting for Discovery."""

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from managed_ai.core.formats import DISCOVERY_FORMAT as FORMAT

PROJECT_CONTEXT_BYTES = 2 * 1024 * 1024
SCOPES = ("records", "project", "techniques")
PREFIX_SCOPE = {"record": "records", "project": "project", "technique": "techniques"}
SELECTOR = {"records": "records", "project": "project_sources", "techniques": "techniques"}
FIELDS = {
    "query",
    "content",
    "scopes",
    "anchors",
    "relation_labels",
    "direction",
    "hops",
    "limit",
    "scan_limit",
    "max_bytes",
    "include_inactive",
    "scan_order",
    "seed",
    "records_only",
    "exclude_relation_labels",
    "include_content_links",
}
SCAN_ORDERS = ("inventory", "scope-round-robin", "seeded")
SCOPE = (
    "Lexical matches and authored relation routes nominate inspection. "
    "Machine examination, a returned candidate, body delivery, semantic "
    "applicability, actual use and authority are different outcomes. "
    "No returned match establishes no adequate source outside this search."
)


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(name + " must be a list of nonempty strings")
    if len(set(value)) != len(value):
        raise ValueError(name + " contains duplicates")
    return value


def validate_selection_pins(pins: Any) -> tuple[list[str], set[str]]:
    """Validate structure and namespaces of candidate source pins before verification."""
    if not isinstance(pins, list) or len(pins) > 200:
        raise ValueError("source pins must be a list of at most 200 {ref, source} objects")

    refs: list[str] = []
    scopes: set[str] = set()
    for pin in pins:
        if not isinstance(pin, dict) or set(pin) != {"ref", "source"}:
            raise ValueError("each source pin must contain exactly ref and source")
        ref = pin["ref"]
        if not isinstance(ref, str) or not ref or not isinstance(pin["source"], dict):
            raise ValueError("a source pin requires a reference and source identity object")
        scope = PREFIX_SCOPE.get(ref.partition(":")[0])
        if scope is None or ":" not in ref:
            raise ValueError("source pin must use a declared reference namespace")
        if ref in refs:
            raise ValueError("duplicate source pin: " + ref)
        canonical(pin)
        refs.append(ref)
        scopes.add(scope)

    return refs, scopes


def verify_pin_candidates(
    pins: list[dict[str, Any]], candidates: dict[str, dict[str, Any]]
) -> None:
    """Confirm every requested pin matches a verified current candidate identity."""
    for pin in pins:
        candidate = candidates.get(pin["ref"])
        if candidate is None:
            raise ValueError(
                "selected discovery source is missing or exceeds verification bounds: " + pin["ref"]
            )
        if candidate["source"] != pin["source"]:
            raise ValueError("selected discovery source identity changed: " + pin["ref"])
        if candidate.get("selector") is None or not candidate.get("body_identity", {}).get(
            "verified"
        ):
            raise ValueError(
                "selected discovery source is unavailable as verified context text: " + pin["ref"]
            )


def _validate_request_basics(value: Any, deployed: bool) -> tuple[str, list[str], str, str]:
    if not isinstance(value, dict):
        raise ValueError("discovery request must be an object")
    unknown = set(value) - FIELDS
    if unknown:
        raise ValueError("unknown discovery fields: " + ", ".join(sorted(unknown)))

    query = value.get("query", "")
    if not isinstance(query, str):
        raise ValueError("discovery query must be text")
    for flag in ("content", "include_inactive", "records_only", "include_content_links"):
        if flag in value and type(value[flag]) is not bool:
            raise ValueError(flag + " must be Boolean")

    scopes = strings(
        value.get("scopes", list(SCOPES if deployed else ("records", "techniques"))), "scopes"
    )
    if not scopes or set(scopes) - set(SCOPES):
        raise ValueError("scopes must select records, project or techniques")
    if "project" in scopes and not deployed:
        raise ValueError("project discovery requires an explicitly selected deployment")

    direction = value.get("direction", "outgoing")
    if direction not in ("outgoing", "incoming", "both"):
        raise ValueError("direction must be outgoing, incoming or both")
    scan_order = value.get("scan_order", "inventory")
    if scan_order not in SCAN_ORDERS:
        raise ValueError("scan_order must be inventory, scope-round-robin or seeded")

    return query, scopes, direction, scan_order


def request(value: Any, deployed: bool) -> dict[str, Any]:
    """Validate and normalize a discovery query request dictionary."""
    query, scopes, direction, scan_order = _validate_request_basics(value, deployed)

    if scan_order == "seeded":
        seed: int | None = integer(value.get("seed"), "seed", 0, 2**64 - 1)
    else:
        if "seed" in value:
            raise ValueError("seed is only accepted with scan_order=seeded")
        seed = None

    normalized: dict[str, Any] = {
        "query": query,
        "content": value.get("content", False),
        "scopes": scopes,
        "anchors": strings(value.get("anchors", []), "anchors"),
        "relation_labels": strings(value.get("relation_labels", []), "relation_labels"),
        "direction": direction,
        "hops": integer(value.get("hops", 1), "hops", 0, 8),
        "limit": integer(value.get("limit", 25), "limit", 1, 200),
        "scan_limit": integer(value.get("scan_limit", 1000), "scan_limit", 1, 100000),
        "max_bytes": integer(value.get("max_bytes", 1048576), "max_bytes", 1, 33554432),
        "include_inactive": value.get("include_inactive", False),
        "scan_order": scan_order,
    }
    if scan_order == "seeded":
        normalized["seed"] = seed
    if value.get("records_only"):
        normalized["records_only"] = True
    exclude_labels = strings(value.get("exclude_relation_labels", []), "exclude_relation_labels")
    if exclude_labels:
        normalized["exclude_relation_labels"] = exclude_labels
    if value.get("include_content_links"):
        normalized["include_content_links"] = True
    return normalized


def build_lexical_order(
    by_scope: dict[str, list[str]], req: dict[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    """Compute deterministic lexical scan order and its signed nomination metadata."""
    scopes = [by_scope[s] for s in req["scopes"]]
    mode = req["scan_order"]
    if mode == "scope-round-robin":
        refs = [
            rows[index]
            for index in range(max(map(len, scopes), default=0))
            for rows in scopes
            if index < len(rows)
        ]
    else:
        refs = [ref for rows in scopes for ref in rows]
        if mode == "seeded":
            refs.sort(key=lambda ref: (digest(canonical({"seed": req["seed"], "ref": ref})), ref))

    nomination = {
        "policy": mode,
        "algorithm": (
            "sha256-canonical-seed-ref-sort/1"
            if mode == "seeded"
            else "scope-round-robin/1"
            if mode == "scope-round-robin"
            else "requested-scopes-sorted-identities/1"
        ),
        "seed": req.get("seed"),
        "entries": len(refs),
        "order_sha256": digest(canonical(refs)),
        "scope": (
            "Order nominates the full declared lexical inventory after anchors. "
            "Entry examination, body acquisition and returned candidates remain "
            "separately bounded. "
            "A fixed seed repeats the same order; changed membership can change its prefix. "
            "Seeded hash sorting is pseudorandom exploration, not a uniform-sampling, "
            "independent-trial, semantic-relevance or complete-gap-detection guarantee. "
            "All policies enumerate metadata; seeded sorting costs O(N log N)."
        ),
    }
    return refs, nomination


def build_coverage(
    discovery: Any,
    all_candidates: list[dict[str, Any]],
    shown: list[dict[str, Any]],
    omitted: list[str],
) -> dict[str, Any]:
    """Assemble the full discovery coverage, accounting, and binding report."""
    req = discovery.req
    unexamined = {
        scope: [ref for ref in discovery.by_scope[scope] if ref not in discovery.examined]
        for scope in req["scopes"]
    }
    project = discovery.projects.current
    search_mode = (
        "anchor-neighborhood-and-lexical"
        if req["anchors"] and req["query"].strip()
        else "anchor-neighborhood"
        if req["anchors"]
        else "lexical"
        if req["query"].strip()
        else "inventory"
    )
    return {
        "requested_scopes": req["scopes"],
        "unavailable_default_scopes": [] if project else ["project"],
        "inventory_counts": {s: len(discovery.by_scope[s]) for s in req["scopes"]},
        "inventory": {s: discovery.by_scope[s] for s in req["scopes"]},
        "examined_entries": len(discovery.examined),
        "examined_entries_by_scope": {
            s: len(discovery.by_scope[s]) - len(unexamined[s]) for s in req["scopes"]
        },
        "lexical_nomination": discovery.lexical_nomination,
        "examined_relation_rows": len(discovery.relation_rows),
        "examined_body_bytes": discovery.examined_bytes,
        "examined_body_reference_bytes": sum(row["bytes"] for row in discovery.body_info.values()),
        "examined_body_sources": len(discovery.body_info) - discovery.reused_body_references,
        "reused_body_references": discovery.reused_body_references,
        "body_accounting": (
            "examined_body_bytes charges each acquired bound source object once "
            "within this query. examined_body_reference_bytes counts its bytes "
            "for each verified reference. Reuse requires the same catalogue "
            "body path, expected digest and byte count; no cross-query cache."
        ),
        "unexamined": unexamined,
        "unexamined_count": sum(map(len, unexamined.values())),
        "skipped": discovery.skipped,
        "unresolved_references": discovery.unresolved,
        "traversal_frontier": discovery.frontier,
        "omitted_candidate_refs": omitted,
        "matched_candidates": len(all_candidates),
        "shown_candidates": len(shown),
        "limits": {k: req[k] for k in ("hops", "limit", "scan_limit", "max_bytes")},
        "saturated": sorted(discovery.saturated),
        "search_mode": search_mode,
        "project_selection": copy.deepcopy(discovery._project_selection_copy),
        "project_manifest_sha256": (
            None if project is None else project.get("selection", {}).get("manifest_sha256")
        ),
        "catalogue_snapshot": copy.deepcopy(discovery._catalogue_snapshot_copy),
        "catalogue_metadata_sha256": (
            None if discovery.catalogue is None else digest(canonical(discovery.catalogue))
        ),
        "record_membership_sha256": digest(canonical(discovery.by_scope["records"])),
        "binding_scope": (
            "Catalogue metadata digest uses sorted compact UTF-8 JSON. Record membership "
            "binds visible IDs only; revisions of unexamined records were not read. "
            "Project identity comes from the selected verified deployment manifest. "
            "Complete examined body identities are reported separately."
        ),
        "scope": SCOPE,
    }


def build_ranked_recovery(req: dict[str, Any]) -> dict[str, Any] | None:
    """Build optional read-only BM25 library recovery invocation when lexical search is empty."""
    engine = Path(__file__).resolve().parents[2]
    cli_path = engine / "library" / "cli.py"
    lib_dir = engine / "library" / "library"
    if not (cli_path.is_file() and (lib_dir / "catalogue.json").is_file()):
        return None

    query = req["query"]
    recovery: dict[str, Any] = {
        "operation": "ranked-published-knowledge",
        "scope": (
            "Optional separate, read-only BM25 search of the installed knowledge library. "
            "Local records and project files stay in the app search. Inspect the returned "
            "source owner before using a nomination."
        ),
    }
    if len(query.encode("utf-8")) <= 2048 and "\x00" not in query:
        recovery["argv"] = [
            sys.executable,
            "-B",
            str(cli_path),
            "--library",
            str(lib_dir),
            "discover",
            "--backend",
            "bm25",
            "--query=" + query,
            "--limit",
            str(min(req["limit"], 100)),
            "--scan-limit",
            str(min(req["scan_limit"], 2048)),
            "--max-bytes",
            str(req["max_bytes"]),
        ]
    else:
        recovery["input_needed"] = "Supply a ranked query of at most 2048 UTF-8 bytes without NUL."
    return recovery


def build_search_result(
    discovery: Any,
    all_candidates: list[dict[str, Any]],
    shown: list[dict[str, Any]],
    selectors: dict[str, list[str]],
    omitted: list[str],
) -> dict[str, Any]:
    """Assemble the complete discovery result envelope, attaching BM25 recovery if applicable."""
    coverage = build_coverage(discovery, all_candidates, shown, omitted)
    result: dict[str, Any] = {
        "format": FORMAT,
        "request": copy.deepcopy(discovery.req),
        "candidates": shown,
        "context_request": {k: v for k, v in selectors.items() if v},
        "context_request_scope": (
            "Candidate selectors only: not automatically applicable, read by a "
            "participant, or authorized for an effect. Retain this discovery "
            "report and compare each expected source identity when preparing "
            "a later context. A changed source needs an explicit current use."
        ),
        "source_bindings": {c["ref"]: copy.deepcopy(c["source"]) for c in shown},
        "examined_bodies": {k: dict(v) for k, v in discovery.body_info.items()},
        "coverage": coverage,
        "ordering": (
            "Requested anchors and bounded breadth-first relations, then lexical "
            + discovery.req["scan_order"]
            + " order; not a relevance ranking."
        ),
    }
    if (
        not shown
        and coverage["search_mode"] == "lexical"
        and "techniques" in discovery.req["scopes"]
    ):
        recovery = build_ranked_recovery(discovery.req)
        if recovery is not None:
            result["recovery"] = recovery
    return result
