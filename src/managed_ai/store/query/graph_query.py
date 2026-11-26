"""Domain-neutral read-only graph query and integrity linter backed by verified HeadIndex."""

from collections import deque
from collections.abc import Callable
from typing import Any

from managed_ai.core.formats import (
    GRAPH_LINKS_FORMAT,
    GRAPH_NEIGHBORS_FORMAT,
    GRAPH_PATH_FORMAT,
    HEAD_INDEX_FORMAT,
)
from managed_ai.store.protocol.identities import require
from managed_ai.store.protocol.inputs import integer, strings, text
from managed_ai.store.query.graph_lint import _lint_mmap as _lint_mmap
from managed_ai.store.query.graph_lint import lint_nodes as _lint_nodes
from managed_ai.store.records.head_index import extract_graph_hints
from managed_ai.store.records.head_index_bin import HeadIndexView


def _load_graph_nodes(system: Any) -> tuple[dict[str, dict[str, Any]], set[str], bool]:
    """Load verified graph metadata from HeadIndex v2 or fall back to scan_records()."""
    store = system.record_store
    cold_ids = set(store.cold_archive.ids())
    loaded = store.indexed_summaries_with_format()
    if loaded is not None:
        summaries, index_format = loaded
        if index_format == HEAD_INDEX_FORMAT:
            return summaries, set(summaries.keys()) | cold_ids, True

    nodes: dict[str, dict[str, Any]] = {}
    for rec in store.scan_records():
        ident = rec["id"]
        nodes[ident] = {
            "id": ident,
            "kind": rec["kind"],
            "revision": rec["revision"],
            "sha256": rec["sha256"],
            "active": rec["active"],
            "graph": extract_graph_hints(rec),
        }
    return nodes, set(nodes.keys()) | cold_ids, False


def _resolve_graph_source[T](
    system: Any,
    *,
    on_mmap: Callable[[HeadIndexView, set[str]], T],
    on_nodes: Callable[[dict[str, dict[str, Any]], set[str], bool], T],
    need_cold_ids: bool = False,
) -> T:
    """Open verified HeadIndex v3 once, dispatching exact-base mmap or materialized nodes."""
    verified = system.record_store.open_verified_index_view()
    if verified is not None:
        with verified:
            cold_ids = set(system.record_store.cold_archive.ids()) if need_cold_ids else set()
            if verified.is_exact_base:
                return on_mmap(verified.view, cold_ids)
            nodes = verified.all_entries()
        all_ids = (set(nodes) | cold_ids) if need_cold_ids else set(nodes)
        return on_nodes(nodes, all_ids, True)

    nodes, all_ids, indexed = _load_graph_nodes(system)
    return on_nodes(nodes, all_ids, indexed)


def _node_pin(node: dict[str, Any]) -> dict[str, Any]:
    return {k: node[k] for k in ("id", "kind", "revision", "sha256", "active")}


def _node_relations(node: dict[str, Any]) -> list[tuple[str, str]]:
    return node["graph"].get("relations", ())


def _build_outgoing(nodes: dict[str, dict[str, Any]]) -> dict[str, list[tuple[str, str]]]:
    outgoing: dict[str, list[tuple[str, str]]] = {}
    for src, node in nodes.items():
        rels = [(label, target) for label, target in _node_relations(node)]
        if len(rels) > 1:
            rels.sort()
        outgoing[src] = rels
    return outgoing


def _build_incoming(
    nodes: dict[str, dict[str, Any]], *, targets: set[str] | None = None
) -> dict[str, list[tuple[str, str]]]:
    incoming: dict[str, list[tuple[str, str]]] = {}
    for src, node in nodes.items():
        for label, target in _node_relations(node):
            if targets is None or target in targets:
                incoming.setdefault(target, []).append((label, src))
    for edges in incoming.values():
        if len(edges) > 1:
            edges.sort()
    return incoming


def _collect_neighbor_steps(
    cur: str,
    direction: str,
    nodes: dict[str, dict[str, Any]],
    outgoing_cache: dict[str, list[tuple[str, str]]],
    incoming: dict[str, list[tuple[str, str]]],
) -> list[tuple[str, str, str, str]]:
    step_edges: list[tuple[str, str, str, str]] = []
    if direction in ("out", "both"):
        cached = outgoing_cache.get(cur)
        if cached is None:
            node = nodes.get(cur)
            cached = sorted(_node_relations(node)) if node is not None else []
            outgoing_cache[cur] = cached
        step_edges.extend((cur, target, label, "out") for label, target in cached)
    if direction in ("in", "both"):
        step_edges.extend((src, cur, label, "in") for label, src in incoming.get(cur, ()))
    return step_edges


def _mmap_neighbor_steps(
    view: HeadIndexView, cur_slot: int, cur_id: str, direction: str, id_cache: dict[int, str]
) -> list[tuple[str, str, str, str, int]]:
    step_edges: list[tuple[str, str, str, str, int]] = []
    if direction in ("out", "both"):
        out_edges = view.fwd_edges(cur_slot)
        if len(out_edges) > 1:
            out_edges.sort(key=lambda row: (row[0], row[1]))
        step_edges.extend((cur_id, tgt, lbl, "out", slot) for lbl, tgt, slot in out_edges)
    if direction in ("in", "both"):
        for label, src_slot in view.rev_edges(cur_slot):
            src_id = id_cache.get(src_slot)
            if src_id is None:
                src_id = view.slot_id(src_slot)
                id_cache[src_slot] = src_id
            step_edges.append((src_id, cur_id, label, "in", src_slot))
    return step_edges


def _neighbors_response(
    system: Any,
    origin: dict[str, Any],
    direction: str,
    hops: int,
    active_only: bool,
    records_only: bool,
    indexed: bool,
    discovered_nodes: list[dict[str, Any]],
    traversed_edges: list[dict[str, Any]],
) -> dict[str, Any]:
    discovered_nodes.sort(key=lambda r: (r["depth"], r["id"]))
    return {
        "format": GRAPH_NEIGHBORS_FORMAT,
        "instance_id": system.instance_id,
        "origin": origin,
        "direction": direction,
        "hops": hops,
        "active_only": active_only,
        "records_only": records_only,
        "indexed": indexed,
        "neighbor_count": len(discovered_nodes),
        "edge_count": len(traversed_edges),
        "neighbors": discovered_nodes,
        "edges": traversed_edges,
    }


def _record_edge(
    seen_edges: set[tuple[str, str, str, str]],
    traversed_edges: list[dict[str, Any]],
    src: str,
    dst: str,
    label: str,
    edge_dir: str,
    depth: int,
) -> None:
    ekey = (src, dst, label, edge_dir)
    if ekey not in seen_edges:
        seen_edges.add(ekey)
        traversed_edges.append(
            {"from": src, "to": dst, "relation": label, "direction": edge_dir, "depth": depth}
        )


def _neighbors_mmap(
    system: Any,
    view: HeadIndexView,
    cold_ids: set[str],
    record_id: str,
    *,
    direction: str,
    hops: int,
    rel_filter: set[str] | None,
    active_only: bool,
    records_only: bool,
) -> dict[str, Any]:
    origin_slot = view.slot_for_id(record_id)
    require(origin_slot >= 0, "record not found in hot graph: " + record_id)

    visited = {origin_slot: 0}
    queue: deque[tuple[int, int]] = deque([(origin_slot, 0)])
    discovered_nodes: list[dict[str, Any]] = []
    traversed_edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str, str]] = set()
    id_cache: dict[int, str] = {origin_slot: record_id}

    while queue:
        cur_slot, depth = queue.popleft()
        if depth >= hops:
            continue
        cur_id = id_cache.setdefault(cur_slot, view.slot_id(cur_slot))
        for src, dst, label, edge_dir, other_slot in _mmap_neighbor_steps(
            view, cur_slot, cur_id, direction, id_cache
        ):
            if rel_filter is not None and label not in rel_filter:
                continue
            other = dst if edge_dir == "out" else src
            if records_only and other_slot < 0 and other not in cold_ids:
                continue
            if active_only and (other_slot < 0 or not view.slot_active(other_slot)):
                continue
            _record_edge(seen_edges, traversed_edges, src, dst, label, edge_dir, depth + 1)
            if other_slot >= 0 and other_slot not in visited:
                visited[other_slot] = depth + 1
                discovered_nodes.append(dict(view.slot_pin(other_slot), depth=depth + 1))
                queue.append((other_slot, depth + 1))

    return _neighbors_response(
        system,
        view.slot_pin(origin_slot),
        direction,
        hops,
        active_only,
        records_only,
        True,
        discovered_nodes,
        traversed_edges,
    )


def _neighbors_nodes(
    system: Any,
    nodes: dict[str, dict[str, Any]],
    all_ids: set[str],
    record_id: str,
    *,
    direction: str,
    hops: int,
    rel_filter: set[str] | None,
    active_only: bool,
    records_only: bool,
    indexed: bool,
) -> dict[str, Any]:
    require(record_id in nodes, "record not found in hot graph: " + record_id)

    outgoing_cache: dict[str, list[tuple[str, str]]] = {}
    incoming = _build_incoming(nodes) if direction in ("in", "both") else {}
    visited = {record_id: 0}
    queue = deque([(record_id, 0)])
    discovered_nodes: list[dict[str, Any]] = []
    traversed_edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str, str]] = set()

    while queue:
        cur, depth = queue.popleft()
        if depth >= hops:
            continue
        for src, dst, label, edge_dir in _collect_neighbor_steps(
            cur, direction, nodes, outgoing_cache, incoming
        ):
            if rel_filter is not None and label not in rel_filter:
                continue
            other = dst if edge_dir == "out" else src
            other_node = nodes.get(other)
            if records_only and other not in all_ids:
                continue
            if active_only and (other_node is None or not other_node["active"]):
                continue
            _record_edge(seen_edges, traversed_edges, src, dst, label, edge_dir, depth + 1)
            if other_node is not None and other not in visited:
                visited[other] = depth + 1
                discovered_nodes.append(dict(_node_pin(other_node), depth=depth + 1))
                queue.append((other, depth + 1))

    return _neighbors_response(
        system,
        _node_pin(nodes[record_id]),
        direction,
        hops,
        active_only,
        records_only,
        indexed,
        discovered_nodes,
        traversed_edges,
    )


def neighbors(
    system: Any,
    record_id: str,
    *,
    direction: str = "both",
    hops: int = 1,
    relations: list[str] | tuple[str, ...] = (),
    active_only: bool = False,
    records_only: bool = False,
) -> dict[str, Any]:
    """Return bounded BFS neighborhood around one record ID."""
    text(record_id, "record_id")
    require(direction in ("in", "out", "both"), "direction must be in, out or both")
    integer(hops, "hops", 1)
    require(hops <= 4, "hops must be between 1 and 4")
    rel_filter = set(strings(list(relations), "relations")) if relations else None

    return _resolve_graph_source(
        system,
        on_mmap=lambda view, cold_ids: _neighbors_mmap(
            system,
            view,
            cold_ids,
            record_id,
            direction=direction,
            hops=hops,
            rel_filter=rel_filter,
            active_only=active_only,
            records_only=records_only,
        ),
        on_nodes=lambda nodes, all_ids, indexed: _neighbors_nodes(
            system,
            nodes,
            all_ids,
            record_id,
            direction=direction,
            hops=hops,
            rel_filter=rel_filter,
            active_only=active_only,
            records_only=records_only,
            indexed=indexed,
        ),
        need_cold_ids=True,
    )


def _links_response(
    system: Any,
    target: str,
    target_head: dict[str, Any] | None,
    active_only: bool,
    indexed: bool,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "format": GRAPH_LINKS_FORMAT,
        "instance_id": system.instance_id,
        "target": target,
        "target_head": target_head,
        "active_only": active_only,
        "indexed": indexed,
        "matched": len(rows),
        "links": rows,
    }


def _links_mmap(
    system: Any,
    view: HeadIndexView,
    *,
    target: str,
    rel_filter: set[str] | None,
    active_only: bool,
) -> dict[str, Any]:
    target_slot = view.slot_for_id(target)
    edges = view.rev_edges(target_slot) if target_slot >= 0 else view.ext_rev_edges(target)
    target_head = view.slot_pin(target_slot) if target_slot >= 0 else None

    rows: list[dict[str, Any]] = []
    pin_cache: dict[int, dict[str, Any]] = {}
    for label, src_slot in edges:
        if rel_filter is not None and label not in rel_filter:
            continue
        if active_only and not view.slot_active(src_slot):
            continue
        pin = pin_cache.get(src_slot)
        if pin is None:
            pin = view.slot_pin(src_slot)
            pin_cache[src_slot] = pin
        rows.append({"from": pin, "relation": label, "to": target})

    return _links_response(system, target, target_head, active_only, True, rows)


def _links_nodes(
    system: Any,
    nodes: dict[str, dict[str, Any]],
    *,
    target: str,
    rel_filter: set[str] | None,
    active_only: bool,
    indexed: bool,
) -> dict[str, Any]:
    matched_edges = sorted(
        (label, src)
        for src, node in nodes.items()
        if not active_only or node["active"]
        for label, dst in _node_relations(node)
        if dst == target and (rel_filter is None or label in rel_filter)
    )
    rows = [
        {"from": _node_pin(nodes[src]), "relation": label, "to": target}
        for label, src in matched_edges
    ]
    target_head = _node_pin(nodes[target]) if target in nodes else None
    return _links_response(system, target, target_head, active_only, indexed, rows)


def links(
    system: Any,
    *,
    target: str,
    relations: list[str] | tuple[str, ...] = (),
    active_only: bool = False,
) -> dict[str, Any]:
    """Indexed reverse-edge lookup returning all inbound edges pointing to target."""
    text(target, "target")
    rel_filter = set(strings(list(relations), "relations")) if relations else None

    return _resolve_graph_source(
        system,
        on_mmap=lambda view, _cold: _links_mmap(
            system, view, target=target, rel_filter=rel_filter, active_only=active_only
        ),
        on_nodes=lambda nodes, _all, indexed: _links_nodes(
            system,
            nodes,
            target=target,
            rel_filter=rel_filter,
            active_only=active_only,
            indexed=indexed,
        ),
    )


def _path_response(
    system: Any,
    src: str,
    dst: str,
    max_hops: int,
    active_only: bool,
    indexed: bool,
    *,
    found: bool = False,
    hops: int | None = None,
    path_nodes: list[dict[str, Any]] | None = None,
    path_edges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "format": GRAPH_PATH_FORMAT,
        "instance_id": system.instance_id,
        "source": src,
        "target": dst,
        "max_hops": max_hops,
        "active_only": active_only,
        "indexed": indexed,
        "found": found,
        "hops": hops,
        "nodes": [] if path_nodes is None else path_nodes,
        "edges": [] if path_edges is None else path_edges,
    }


def _bfs_shortest_path(
    nodes: dict[str, dict[str, Any]],
    src: str,
    dst: str,
    *,
    max_hops: int,
    active_only: bool,
    rel_filter: set[str] | None,
) -> tuple[bool, dict[str, tuple[str, str]]]:
    parent: dict[str, tuple[str, str]] = {}
    depth_map: dict[str, int] = {src: 0}
    queue = deque([src])

    while queue:
        cur = queue.popleft()
        cur_depth = depth_map[cur]
        if cur_depth >= max_hops:
            continue
        cur_edges = sorted(_node_relations(nodes[cur]))
        for label, nxt in cur_edges:
            if rel_filter is not None and label not in rel_filter:
                continue
            nxt_node = nodes.get(nxt)
            if nxt_node is None or (active_only and not nxt_node["active"]) or nxt in depth_map:
                continue
            depth_map[nxt] = cur_depth + 1
            parent[nxt] = (cur, label)
            if nxt == dst:
                return True, parent
            queue.append(nxt)
    return False, parent


def _bfs_shortest_path_mmap(
    view: HeadIndexView,
    src_slot: int,
    dst_slot: int,
    *,
    max_hops: int,
    active_only: bool,
    rel_filter: set[str] | None,
) -> tuple[bool, dict[int, tuple[int, str]]]:
    parent: dict[int, tuple[int, str]] = {}
    depth_map: dict[int, int] = {src_slot: 0}
    queue: deque[int] = deque([src_slot])

    while queue:
        cur_slot = queue.popleft()
        cur_depth = depth_map[cur_slot]
        if cur_depth >= max_hops:
            continue
        out_edges = view.fwd_edges(cur_slot)
        if len(out_edges) > 1:
            out_edges.sort(key=lambda row: (row[0], row[1]))
        for label, _target_str, nxt_slot in out_edges:
            if rel_filter is not None and label not in rel_filter:
                continue
            if (
                nxt_slot < 0
                or (active_only and not view.slot_active(nxt_slot))
                or nxt_slot in depth_map
            ):
                continue
            depth_map[nxt_slot] = cur_depth + 1
            parent[nxt_slot] = (cur_slot, label)
            if nxt_slot == dst_slot:
                return True, parent
            queue.append(nxt_slot)
    return False, parent


def _reconstruct_path[K](
    src: K,
    dst: K,
    parent: dict[K, tuple[K, str]],
    to_id: Callable[[K], str],
    to_pin: Callable[[K], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    keys: list[K] = []
    edges: list[dict[str, Any]] = []
    cur = dst
    while cur != src:
        keys.append(cur)
        prev, label = parent[cur]
        edges.append({"from": to_id(prev), "to": to_id(cur), "relation": label})
        cur = prev
    keys.append(src)
    keys.reverse()
    edges.reverse()
    return [to_pin(k) for k in keys], edges


def _shortest_path_mmap(
    system: Any,
    view: HeadIndexView,
    src: str,
    dst: str,
    *,
    max_hops: int,
    active_only: bool,
    rel_filter: set[str] | None,
) -> dict[str, Any]:
    src_slot, dst_slot = view.slot_for_id(src), view.slot_for_id(dst)
    require(src_slot >= 0, "source record not found: " + src)
    require(dst_slot >= 0, "destination record not found: " + dst)

    if active_only and (not view.slot_active(src_slot) or not view.slot_active(dst_slot)):
        return _path_response(system, src, dst, max_hops, active_only, True)
    if src_slot == dst_slot:
        return _path_response(
            system,
            src,
            dst,
            max_hops,
            active_only,
            True,
            found=True,
            hops=0,
            path_nodes=[view.slot_pin(src_slot)],
        )

    found, parent = _bfs_shortest_path_mmap(
        view, src_slot, dst_slot, max_hops=max_hops, active_only=active_only, rel_filter=rel_filter
    )
    if not found:
        return _path_response(system, src, dst, max_hops, active_only, True)

    path_nodes, path_edges = _reconstruct_path(
        src_slot, dst_slot, parent, view.slot_id, view.slot_pin
    )
    return _path_response(
        system,
        src,
        dst,
        max_hops,
        active_only,
        True,
        found=True,
        hops=len(path_edges),
        path_nodes=path_nodes,
        path_edges=path_edges,
    )


def _shortest_path_nodes(
    system: Any,
    nodes: dict[str, dict[str, Any]],
    src: str,
    dst: str,
    *,
    max_hops: int,
    active_only: bool,
    rel_filter: set[str] | None,
    indexed: bool,
) -> dict[str, Any]:
    require(src in nodes, "source record not found: " + src)
    require(dst in nodes, "destination record not found: " + dst)

    if active_only and (not nodes[src]["active"] or not nodes[dst]["active"]):
        return _path_response(system, src, dst, max_hops, active_only, indexed)
    if src == dst:
        return _path_response(
            system,
            src,
            dst,
            max_hops,
            active_only,
            indexed,
            found=True,
            hops=0,
            path_nodes=[_node_pin(nodes[src])],
        )

    found, parent = _bfs_shortest_path(
        nodes, src, dst, max_hops=max_hops, active_only=active_only, rel_filter=rel_filter
    )
    if not found:
        return _path_response(system, src, dst, max_hops, active_only, indexed)

    path_nodes, path_edges = _reconstruct_path(
        src, dst, parent, lambda k: k, lambda k: _node_pin(nodes[k])
    )
    return _path_response(
        system,
        src,
        dst,
        max_hops,
        active_only,
        indexed,
        found=True,
        hops=len(path_edges),
        path_nodes=path_nodes,
        path_edges=path_edges,
    )


def shortest_path(
    system: Any,
    src: str,
    dst: str,
    *,
    max_hops: int = 6,
    active_only: bool = False,
    relations: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Deterministic shortest directed record-to-record path via BFS."""
    text(src, "src")
    text(dst, "dst")
    integer(max_hops, "max_hops", 1)
    require(max_hops <= 6, "max_hops must be between 1 and 6")
    rel_filter = set(strings(list(relations), "relations")) if relations else None

    return _resolve_graph_source(
        system,
        on_mmap=lambda view, _cold: _shortest_path_mmap(
            system,
            view,
            src,
            dst,
            max_hops=max_hops,
            active_only=active_only,
            rel_filter=rel_filter,
        ),
        on_nodes=lambda nodes, _all, indexed: _shortest_path_nodes(
            system,
            nodes,
            src,
            dst,
            max_hops=max_hops,
            active_only=active_only,
            rel_filter=rel_filter,
            indexed=indexed,
        ),
    )


def lint(system: Any) -> dict[str, Any]:
    """Detect structural graph anomalies using domain-neutral rules and optional vocabulary."""
    return _resolve_graph_source(
        system,
        on_mmap=lambda view, cold_ids: _lint_mmap(system, view, cold_ids),
        on_nodes=lambda nodes, all_ids, indexed: _lint_nodes(
            system, nodes, all_ids, indexed=indexed, incoming_builder=_build_incoming
        ),
        need_cold_ids=True,
    )
