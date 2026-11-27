"""Authored relation extraction and bounded BFS anchor traversal for Discovery."""

from collections import deque
from typing import Any

from managed_ai_information.store.records.head_index import extract_content_record_links
from managed_ai_information.store.records.head_index_bin import FLAG_HAS_CONTENT_LINKS


def _append_content_link_edges(
    discovery: Any,
    item: dict[str, Any],
    result: list[dict[str, Any]],
    excluded: set[str],
) -> None:
    record = item.get("record", {})
    if not (discovery.req.get("include_content_links") and isinstance(record.get("content"), dict)):
        return

    ref = item["ref"]
    content_links = extract_content_record_links(record["content"])
    syn_index = len(record.get("relations", []))

    for field_name, vals in sorted(content_links.items()):
        label = "content:" + field_name
        if label in excluded or (
            discovery.req["relation_labels"] and label not in discovery.req["relation_labels"]
        ):
            continue

        for val in vals:
            target = "record:" + val
            if target not in discovery.entries:
                continue

            key = (ref, syn_index)
            syn_index += 1
            if key not in discovery.relation_rows:
                if len(discovery.relation_rows) >= discovery.req["scan_limit"]:
                    discovery.saturated.add("relation-scan-budget")
                    discovery.frontier.append(
                        {
                            "ref": ref,
                            "reason": "relation-scan-budget",
                            "next_relation_index": syn_index - 1,
                        }
                    )
                    return
                discovery.relation_rows.add(key)

            edge = {
                "from": ref,
                "to": target,
                "relation": label,
                "declared_in": {
                    "ref": ref,
                    "revision": item["source"]["revision"],
                    "record_sha256": item["source"]["record_sha256"],
                },
                "properties": {"channel": "content-link", "field": field_name},
            }
            result.append(edge)
            if key not in discovery.edge_keys:
                discovery.edge_keys.add(key)
                discovery.edges.append(edge)


def _resolve_relation_target(
    discovery: Any, raw_target: Any, ref: str, *, records_only: bool
) -> str | None:
    if not records_only:
        return discovery._resolve(raw_target, ref)
    if not isinstance(raw_target, str) or not raw_target:
        return None
    candidate_ref = (
        raw_target
        if raw_target.startswith("record:")
        else ("record:" + raw_target if ":" not in raw_target else None)
    )
    if candidate_ref is None or candidate_ref not in discovery.entries:
        return None
    return candidate_ref


def extract_record_edges(discovery: Any, item: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract declared relation edges (and optional content links) from one inspected record."""
    ref = item["ref"]
    if ref in discovery.edge_cache:
        return discovery.edge_cache[ref]

    result: list[dict] = []
    excluded = set(discovery.req.get("exclude_relation_labels", ()))
    allowed_labels = discovery.req["relation_labels"]
    records_only = bool(discovery.req.get("records_only", False))
    record = (
        discovery._ensure_record_loaded(item)
        if item.get("scope") == "records" and "record" not in item
        else item.get("record", {})
    )

    for index, relation in enumerate(record.get("relations", [])):
        key = (ref, index)
        if key not in discovery.relation_rows:
            if len(discovery.relation_rows) >= discovery.req["scan_limit"]:
                discovery.saturated.add("relation-scan-budget")
                discovery.frontier.append(
                    {
                        "ref": ref,
                        "reason": "relation-scan-budget",
                        "next_relation_index": index,
                    }
                )
                break
            discovery.relation_rows.add(key)

        if not isinstance(relation, dict) or not isinstance(relation.get("relation"), str):
            discovery._unresolve(str(relation), "malformed-declared-relation", ref)
            continue

        label = relation["relation"]
        if label in excluded or (allowed_labels and label not in allowed_labels):
            continue

        target = _resolve_relation_target(
            discovery, relation.get("target"), ref, records_only=records_only
        )
        if target is None:
            continue

        edge = {
            "from": ref,
            "to": target,
            "relation": label,
            "declared_in": {
                "ref": ref,
                "revision": item["source"]["revision"],
                "record_sha256": item["source"]["record_sha256"],
            },
            "properties": {k: v for k, v in relation.items() if k not in ("relation", "target")},
        }
        result.append(edge)
        if key not in discovery.edge_keys:
            discovery.edge_keys.add(key)
            discovery.edges.append(edge)

    _append_content_link_edges(discovery, item, result, excluded)
    discovery.edge_cache[ref] = result
    return result


def build_incoming_edges(discovery: Any) -> dict[str, list[dict[str, Any]]]:
    if discovery.incoming is None:
        discovery.incoming = {}
        for ref in discovery.by_scope["records"]:
            item = discovery._inspect(ref)
            if item:
                for edge in extract_record_edges(discovery, item):
                    discovery.incoming.setdefault(edge["to"], []).append(edge)
    return discovery.incoming


def _add_rev_edge_sources(
    view: Any,
    edges: list[tuple[str, int]],
    label_ok: Any,
    skip_base: set[str],
    src_ids: set[str],
) -> None:
    for label, src_slot in edges:
        if not label_ok(label):
            continue
        ident = view.slot_id(src_slot)
        if ident not in skip_base:
            src_ids.add(ident)


def _scan_base_content_link_sources(
    view: Any,
    bare: str,
    label_ok: Any,
    skip_base: set[str],
    src_ids: set[str],
) -> None:
    for slot in range(view.record_count):
        _, _, _, slot_flags = view.slot_stat_fast(slot)
        if not (slot_flags & FLAG_HAS_CONTENT_LINKS):
            continue
        ident = view.slot_id(slot)
        if ident in skip_base or ident in src_ids:
            continue
        clinks = view.slot_graph_aux(slot, slot_flags)["content_links"]
        if any(label_ok("content:" + name) and bare in vals for name, vals in clinks.items()):
            src_ids.add(ident)


def _scan_overlay_incoming_sources(
    overlay: dict[str, dict[str, Any]],
    dels: set[str],
    match_targets: set[str],
    bare: str | None,
    include_content_links: bool,
    label_ok: Any,
    src_ids: set[str],
) -> None:
    for ident, entry in overlay.items():
        if ident in dels:
            continue
        raw_graph = entry.get("graph")
        graph: dict[str, Any] = raw_graph if isinstance(raw_graph, dict) else {}
        if any(
            tgt in match_targets and label_ok(label) for label, tgt in graph.get("relations", ())
        ):
            src_ids.add(ident)
            continue
        if include_content_links and bare is not None:
            clinks = graph.get("content_links") or {}
            if any(label_ok("content:" + name) and bare in vals for name, vals in clinks.items()):
                src_ids.add(ident)


def _collect_indexed_incoming_sources(discovery: Any, target_ref: str) -> list[str]:
    """Use HeadIndex v3 Reverse CSR (+ delta/overrides) to locate incoming source IDs."""
    verified = discovery._verified_index
    view = verified.view
    excluded = set(discovery.req.get("exclude_relation_labels", ()))
    allowed_labels = set(discovery.req["relation_labels"])
    include_content_links = bool(discovery.req.get("include_content_links"))

    def label_ok(label: str) -> bool:
        return label not in excluded and (not allowed_labels or label in allowed_labels)

    dels = verified.removed | view.delta_dels
    overlay = dict(view.delta_puts)
    overlay.update(verified.overrides)
    skip_base = dels | set(overlay)

    src_ids: set[str] = set()
    bare = target_ref[7:] if target_ref.startswith("record:") else None
    _add_rev_edge_sources(view, view.ext_rev_edges(target_ref), label_ok, skip_base, src_ids)

    if bare is not None:
        target_slot = view.slot_for_id(bare)
        if target_slot >= 0:
            _add_rev_edge_sources(view, view.rev_edges(target_slot), label_ok, skip_base, src_ids)
        _add_rev_edge_sources(view, view.ext_rev_edges(bare), label_ok, skip_base, src_ids)
        if include_content_links:
            _scan_base_content_link_sources(view, bare, label_ok, skip_base, src_ids)

    match_targets = {target_ref} if bare is None else {target_ref, bare}
    _scan_overlay_incoming_sources(
        overlay, dels, match_targets, bare, include_content_links, label_ok, src_ids
    )

    if discovery._record_order_rank is None:
        discovery._record_order_rank = {
            ref[7:]: idx for idx, ref in enumerate(discovery.by_scope["records"])
        }
    rank = discovery._record_order_rank
    return sorted((ident for ident in src_ids if ident in rank), key=rank.__getitem__)


def incoming_edges_for(discovery: Any, target_ref: str) -> list[dict[str, Any]]:
    """Return verified incoming edges for target_ref, using HeadIndex v3 Reverse CSR if open."""
    if discovery._verified_index is None:
        return build_incoming_edges(discovery).get(target_ref, [])

    out: list[dict[str, Any]] = []
    for src_id in _collect_indexed_incoming_sources(discovery, target_ref):
        item = discovery._inspect("record:" + src_id)
        if item is None:
            continue
        for edge in extract_record_edges(discovery, item):
            if edge["to"] == target_ref:
                out.append(edge)
    return out


def traverse_anchors(discovery: Any) -> None:
    """Breadth-first relation traversal starting from requested discovery anchors."""
    queue: deque[tuple[str, int]] = deque()
    visited: set[str] = set()

    for anchor in discovery.req["anchors"]:
        ref = discovery._resolve(anchor)
        if ref is None:
            continue
        item = discovery._inspect(ref)
        if item is None:
            discovery._unresolve(anchor, "anchor-not-examined-or-inactive")
            continue
        discovery._candidate(item, {"kind": "anchor", "requested": anchor})
        if ref not in visited:
            queue.append((ref, 0))
            visited.add(ref)

    while queue:
        ref, depth = queue.popleft()
        item = discovery.loaded[ref]
        if depth >= discovery.req["hops"]:
            discovery.frontier.append(
                {
                    "ref": ref,
                    "reason": "hop-bound",
                    "depth": depth,
                    "scope": "Not expanded; this does not assert that further relations exist.",
                }
            )
            continue

        edges: list[tuple[dict[str, Any], str, str]] = []
        if discovery.req["direction"] in ("outgoing", "both") and item["scope"] == "records":
            edges.extend((e, e["to"], "outgoing") for e in extract_record_edges(discovery, item))
        if discovery.req["direction"] in ("incoming", "both"):
            edges.extend((e, e["from"], "incoming") for e in incoming_edges_for(discovery, ref))

        for edge, target, direction in edges:
            other = discovery._inspect(target)
            if other is None:
                discovery._unresolve(target, "endpoint-not-examined-or-inactive", edge["from"])
                continue
            discovery._candidate(
                other,
                {
                    "kind": "declared-relation",
                    "edge": edge,
                    "direction": direction,
                    "depth": depth + 1,
                },
            )
            if target not in visited:
                visited.add(target)
                queue.append((target, depth + 1))
