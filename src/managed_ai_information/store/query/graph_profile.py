"""Native knowledge-graph health profiler over HeadIndex v3 mmap or node summaries."""

from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from managed_ai_information.core.formats import HEAD_INDEX_FORMAT
from managed_ai_information.core.storage import is_ordinary_file_stat
from managed_ai_information.store.protocol.inputs import strings
from managed_ai_information.store.query.graph_profile_alerts import (
    GRAPH_PROFILE_BASELINE_FORMAT as GRAPH_PROFILE_BASELINE_FORMAT,
)
from managed_ai_information.store.query.graph_profile_alerts import (
    compute_phantom_cochange,
    evaluate_graph_alerts,
    load_graph_baseline,
    write_graph_baseline,
)
from managed_ai_information.store.query.graph_profile_metrics import (
    DEFAULT_ROOT_KINDS,
    STRUCTURAL_PREDICATES,
    compute_density_and_qualification,
    percentile,
)

GRAPH_PROFILE_FORMAT = "managed-ai-graph-profile/1"
BYPASS_EXEMPT_PREDICATES = frozenset(
    {
        "supersedes",
        "consolidated-into",
        "revises native navigation from",
        "corresponds to dated origin",
        "grounded in original edition",
        "declared by original source",
    }
)
_MAX_SAMPLE_ITEMS = 25
_MAX_BYPASS_SAMPLES = 50


@dataclass(slots=True)
class _GraphSnapshot:
    n: int
    ids: list[str]
    id_to_idx: dict[str, int]
    kinds: list[str]
    active: list[bool]
    sizes: list[int]
    out_edges: list[list[tuple[int, str]]]
    in_edges: list[list[tuple[int, str]]]
    indexed: bool
    external_edge_count: int
    self_loop_count: int


def _snapshot_from_mmap(view: Any) -> _GraphSnapshot:
    """Build compact integer-indexed graph snapshot directly from HeadIndex v3 mmap."""
    n = view.record_count
    rel_labels = view.rel_labels()
    ids = [view.slot_id(s) for s in range(n)]
    id_to_idx = {ident: s for s, ident in enumerate(ids)}
    kinds = [view.slot_kind_name(s) for s in range(n)]
    active = [view.slot_active(s) for s in range(n)]
    sizes = [view.slot_stat_fast(s)[0] for s in range(n)]

    out_edges: list[list[tuple[int, str]]] = [[] for _ in range(n)]
    in_edges: list[list[tuple[int, str]]] = [[] for _ in range(n)]
    external_edges = 0
    self_loops = 0

    for slot in range(n):
        for rid, t_slot, _t_off, _t_len in view.fwd_edges_raw(slot):
            if t_slot == slot:
                self_loops += 1
            elif t_slot >= 0:
                label = rel_labels[rid]
                out_edges[slot].append((t_slot, label))
                in_edges[t_slot].append((slot, label))
            else:
                external_edges += 1

    return _GraphSnapshot(
        n=n,
        ids=ids,
        id_to_idx=id_to_idx,
        kinds=kinds,
        active=active,
        sizes=sizes,
        out_edges=out_edges,
        in_edges=in_edges,
        indexed=True,
        external_edge_count=external_edges,
        self_loop_count=self_loops,
    )


def _node_size_bytes(system: Any, ident: str, node: dict[str, Any]) -> int:
    raw_size = node.get("size")
    if type(raw_size) is int and raw_size >= 0:
        return raw_size
    try:
        info = system.record_store.path(ident).lstat()
        if is_ordinary_file_stat(info):
            return info.st_size
    except OSError, ValueError:
        pass
    return 0


def _snapshot_from_nodes(
    system: Any, nodes: dict[str, dict[str, Any]], *, indexed: bool
) -> _GraphSnapshot:
    ids = sorted(nodes)
    n = len(ids)
    id_to_idx = {ident: idx for idx, ident in enumerate(ids)}
    kinds = [str(nodes[ident]["kind"]) for ident in ids]
    active = [bool(nodes[ident]["active"]) for ident in ids]
    sizes = [_node_size_bytes(system, ident, nodes[ident]) for ident in ids]

    out_edges: list[list[tuple[int, str]]] = [[] for _ in range(n)]
    in_edges: list[list[tuple[int, str]]] = [[] for _ in range(n)]
    external_edges = 0
    self_loops = 0

    for idx, ident in enumerate(ids):
        for label, target in nodes[ident]["graph"].get("relations", ()):
            t_idx = id_to_idx.get(target)
            if t_idx is None:
                external_edges += 1
            elif t_idx == idx:
                self_loops += 1
            else:
                out_edges[idx].append((t_idx, label))
                in_edges[t_idx].append((idx, label))

    return _GraphSnapshot(
        n=n,
        ids=ids,
        id_to_idx=id_to_idx,
        kinds=kinds,
        active=active,
        sizes=sizes,
        out_edges=out_edges,
        in_edges=in_edges,
        indexed=indexed,
        external_edge_count=external_edges,
        self_loop_count=self_loops,
    )


def _select_roots(
    snap: _GraphSnapshot, active_indices: list[int], explicit_roots: list[str]
) -> tuple[list[int], str]:
    if explicit_roots:
        root_idxs: list[int] = []
        seen: set[int] = set()
        for rid in explicit_roots:
            idx = snap.id_to_idx.get(rid)
            if idx is None:
                raise ValueError("root record not found in hot graph: " + rid)
            if not snap.active[idx]:
                raise ValueError("root record is inactive in hot graph: " + rid)
            if idx not in seen:
                seen.add(idx)
                root_idxs.append(idx)
        return root_idxs, "explicit"

    governing = [i for i in active_indices if snap.kinds[i] in DEFAULT_ROOT_KINDS]
    if governing:
        return governing, "governing_kinds"
    zero_in = [i for i in active_indices if not any(snap.active[s] for s, _ in snap.in_edges[i])]
    if zero_in:
        return zero_in, "zero_indegree_fallback"
    return list(active_indices), "all_active_fallback"


def _compute_orphan_islands(
    snap: _GraphSnapshot, unreachable: list[int], undirected: list[set[int]]
) -> list[dict[str, Any]]:
    unreach_set = set(unreachable)
    visited: set[int] = set()
    islands: list[dict[str, Any]] = []
    for start in unreachable:
        if start in visited:
            continue
        comp: list[int] = []
        queue: deque[int] = deque([start])
        visited.add(start)
        while queue:
            cur = queue.popleft()
            comp.append(cur)
            for nxt in undirected[cur]:
                if nxt in unreach_set and nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        comp_ids = sorted(snap.ids[i] for i in comp)
        islands.append(
            {
                "size": len(comp),
                "total_bytes": sum(snap.sizes[i] for i in comp),
                "sample_ids": comp_ids[:10],
            }
        )
    islands.sort(key=lambda row: (-row["size"], row["sample_ids"][0]))
    return islands


def _compute_discoverability(
    snap: _GraphSnapshot,
    active_indices: list[int],
    undirected: list[set[int]],
    explicit_roots: list[str],
) -> dict[str, Any]:
    root_idxs, root_selection = _select_roots(snap, active_indices, explicit_roots)
    dist = [-1] * snap.n
    queue: deque[int] = deque(root_idxs)
    for r in root_idxs:
        dist[r] = 0

    while queue:
        cur = queue.popleft()
        nxt_d = dist[cur] + 1
        for nxt in undirected[cur]:
            if dist[nxt] == -1:
                dist[nxt] = nxt_d
                queue.append(nxt)

    reachable_dists = sorted(dist[i] for i in active_indices if dist[i] >= 0)
    unreachable = [i for i in active_indices if dist[i] == -1]
    active_count = len(active_indices)
    hist = Counter(reachable_dists)
    islands = _compute_orphan_islands(snap, unreachable, undirected)

    def _ratio(max_hop: int) -> float:
        if not active_count:
            return 1.0
        return round(sum(1 for d in reachable_dists if d <= max_hop) / active_count, 4)

    return {
        "root_selection": root_selection,
        "root_count": len(root_idxs),
        "root_ids": [snap.ids[i] for i in root_idxs[:_MAX_SAMPLE_ITEMS]],
        "reachable_active_count": len(reachable_dists),
        "orphan_count": len(unreachable),
        "isolated_active_count": sum(1 for i in active_indices if not undirected[i]),
        "p50_hops": percentile(reachable_dists, 0.50),
        "p95_hops": percentile(reachable_dists, 0.95),
        "max_hops": max(reachable_dists) if reachable_dists else None,
        "discoverability_2hop": _ratio(2),
        "discoverability_3hop": _ratio(3),
        "discoverability_4hop": _ratio(4),
        "hop_histogram": {str(h): hist[h] for h in sorted(hist)},
        "orphan_island_count": len(islands),
        "orphan_islands": islands[:_MAX_SAMPLE_ITEMS],
        "orphan_sample_ids": sorted(snap.ids[i] for i in unreachable)[:_MAX_SAMPLE_ITEMS],
    }


def _pop_scc(root: int, tarjan_stack: list[int], on_stack: set[int]) -> list[int]:
    comp: list[int] = []
    while True:
        w = tarjan_stack.pop()
        on_stack.remove(w)
        comp.append(w)
        if w == root:
            return comp


def _iterative_tarjan_sccs(nodes: list[int], adj: dict[int, list[int]]) -> list[list[int]]:
    """Non-recursive Tarjan SCC extraction safe on arbitrarily deep graphs."""
    index_counter = 0
    indices: dict[int, int] = {}
    lowlink: dict[int, int] = {}
    on_stack: set[int] = set()
    tarjan_stack: list[int] = []
    sccs: list[list[int]] = []

    for start in nodes:
        if start in indices:
            continue
        call_stack: list[tuple[int, int]] = [(start, 0)]
        indices[start] = lowlink[start] = index_counter
        index_counter += 1
        tarjan_stack.append(start)
        on_stack.add(start)

        while call_stack:
            v, edge_pos = call_stack[-1]
            neighbors = adj.get(v, ())
            if edge_pos < len(neighbors):
                call_stack[-1] = (v, edge_pos + 1)
                w = neighbors[edge_pos]
                if w not in indices:
                    indices[w] = lowlink[w] = index_counter
                    index_counter += 1
                    tarjan_stack.append(w)
                    on_stack.add(w)
                    call_stack.append((w, 0))
                elif w in on_stack and indices[w] < lowlink[v]:
                    lowlink[v] = indices[w]
                continue

            call_stack.pop()
            if call_stack:
                parent = call_stack[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[v])
            if lowlink[v] == indices[v]:
                comp = _pop_scc(v, tarjan_stack, on_stack)
                if len(comp) >= 2:
                    sccs.append(comp)
    return sccs


def _compute_bottlenecks(
    system: Any,
    snap: _GraphSnapshot,
    active_indices: list[int],
    undirected: list[set[int]],
    *,
    include_cochange: bool,
) -> dict[str, Any]:
    struct_adj: dict[int, list[int]] = {}
    bypasses: list[dict[str, Any]] = []
    for u in active_indices:
        targets: list[int] = []
        for v, pred in snap.out_edges[u]:
            if snap.active[v]:
                if pred in STRUCTURAL_PREDICATES:
                    targets.append(v)
            elif pred not in BYPASS_EXEMPT_PREDICATES:
                bypasses.append(
                    {
                        "from": snap.ids[u],
                        "from_kind": snap.kinds[u],
                        "relation": pred,
                        "to": snap.ids[v],
                        "to_kind": snap.kinds[v],
                    }
                )
        if targets:
            struct_adj[u] = targets

    raw_sccs = _iterative_tarjan_sccs(active_indices, struct_adj)
    scc_rows: list[dict[str, Any]] = []
    total_scc_nodes = 0
    total_locked_bytes = 0
    for comp in raw_sccs:
        comp_set = set(comp)
        locked = sum(snap.sizes[i] for i in comp)
        preds = sorted(
            {
                pred
                for u in comp
                for v, pred in snap.out_edges[u]
                if v in comp_set and pred in STRUCTURAL_PREDICATES
            }
        )
        node_ids = sorted(snap.ids[i] for i in comp)
        total_scc_nodes += len(comp)
        total_locked_bytes += locked
        scc_rows.append(
            {"size": len(comp), "locked_bytes": locked, "predicates": preds, "node_ids": node_ids}
        )

    scc_rows.sort(key=lambda r: (-r["locked_bytes"], -r["size"], r["node_ids"][0]))
    bypasses.sort(key=lambda r: (r["from"], r["relation"], r["to"]))
    cochange = (
        compute_phantom_cochange(system, snap.ids, snap.id_to_idx, snap.active, undirected)
        if include_cochange
        else {
            "enabled": False,
            "batches_scanned": 0,
            "min_co_revisions": 2,
            "phantom_pair_count": 0,
            "phantom_pairs": [],
        }
    )
    return {
        "structural_scc_count": len(scc_rows),
        "structural_scc_node_count": total_scc_nodes,
        "structural_scc_locked_bytes": total_locked_bytes,
        "structural_sccs": scc_rows[:_MAX_SAMPLE_ITEMS],
        "superseded_head_bypass_count": len(bypasses),
        "superseded_head_bypasses": bypasses[:_MAX_BYPASS_SAMPLES],
        "phantom_cochange": cochange,
    }


def _profile_snapshot(
    system: Any,
    snap: _GraphSnapshot,
    *,
    roots: list[str] | tuple[str, ...] = (),
    include_cochange: bool = False,
    baseline_path: Path | None = None,
    write_baseline_path: Path | None = None,
    check_alerts: bool = False,
) -> dict[str, Any]:
    explicit_roots = strings(list(roots), "roots") if roots else []
    active_indices = [i for i in range(snap.n) if snap.active[i]]
    undirected: list[set[int]] = [set() for _ in range(snap.n)]
    for u in active_indices:
        for v, _pred in snap.out_edges[u]:
            if snap.active[v]:
                undirected[u].add(v)
                undirected[v].add(u)

    discoverability = _compute_discoverability(snap, active_indices, undirected, explicit_roots)
    density = compute_density_and_qualification(snap, active_indices, undirected)
    bottlenecks = _compute_bottlenecks(
        system, snap, active_indices, undirected, include_cochange=include_cochange
    )
    report: dict[str, Any] = {
        "format": GRAPH_PROFILE_FORMAT,
        "instance_id": system.instance_id,
        "indexed": snap.indexed,
        "summary": {
            "total_nodes": snap.n,
            "active_nodes": len(active_indices),
            "inactive_nodes": snap.n - len(active_indices),
            "active_edges": density["active_edge_count"],
            "external_edges": snap.external_edge_count,
            "self_loops": snap.self_loop_count,
            "total_active_bytes": sum(snap.sizes[i] for i in active_indices),
        },
        "discoverability": discoverability,
        "density_and_qualification": density,
        "bottlenecks": bottlenecks,
    }

    loaded_baseline = load_graph_baseline(baseline_path) if baseline_path is not None else None
    alerts = evaluate_graph_alerts(report, baseline=loaded_baseline)
    report["baseline_compared"] = str(baseline_path) if baseline_path is not None else None
    report["alerts"] = alerts
    report["alert_count"] = len(alerts)
    report["error_alert_count"] = sum(1 for a in alerts if a["severity"] == "error")
    report["warning_alert_count"] = sum(1 for a in alerts if a["severity"] == "warning")
    report["alerts_passed"] = len(alerts) == 0

    if write_baseline_path is not None:
        if check_alerts and alerts:
            report["baseline_written"] = None
            report["baseline_write_skipped_reason"] = "alerts_triggered"
        else:
            write_graph_baseline(write_baseline_path, report)
            report["baseline_written"] = str(write_baseline_path)
    return report


def _load_snapshot(system: Any) -> _GraphSnapshot:
    store = system.record_store
    verified = store.open_verified_index_view()
    if verified is not None:
        with verified:
            if verified.is_exact_base:
                return _snapshot_from_mmap(verified.view)
            nodes = verified.all_entries()
        return _snapshot_from_nodes(system, nodes, indexed=True)

    loaded = store.indexed_summaries_with_format()
    if loaded is not None:
        summaries, index_format = loaded
        if index_format == HEAD_INDEX_FORMAT:
            return _snapshot_from_nodes(system, summaries, indexed=True)

    nodes = {}
    for rec in store.scan_records():
        raw_rels = rec.get("relations")
        rels: list[tuple[str, str]] = []
        seen_rels: set[tuple[str, str]] = set()
        if isinstance(raw_rels, list):
            for item in raw_rels:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("relation"), str)
                    and isinstance(item.get("target"), str)
                ):
                    pair = (item["relation"], item["target"])
                    if pair not in seen_rels:
                        seen_rels.add(pair)
                        rels.append(pair)
        nodes[rec["id"]] = {
            "id": rec["id"],
            "kind": rec["kind"],
            "revision": rec["revision"],
            "sha256": rec["sha256"],
            "active": rec["active"],
            "graph": {"relations": rels},
        }
    return _snapshot_from_nodes(system, nodes, indexed=False)


def profile(
    system: Any,
    *,
    roots: list[str] | tuple[str, ...] = (),
    include_cochange: bool = False,
    baseline_path: Path | None = None,
    write_baseline_path: Path | None = None,
    check_alerts: bool = False,
) -> dict[str, Any]:
    """Compute knowledge-graph health metrics over HeadIndex v3 mmap or scan_records fallback."""
    return _profile_snapshot(
        system,
        _load_snapshot(system),
        roots=roots,
        include_cochange=include_cochange,
        baseline_path=baseline_path,
        write_baseline_path=write_baseline_path,
        check_alerts=check_alerts,
    )
