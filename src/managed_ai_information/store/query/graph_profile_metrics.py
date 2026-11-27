"""Predicate-aware density, clustering, qualification, contradicts, and working-set metrics."""

import math
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from typing import Any

DEFAULT_ROOT_KINDS = frozenset({"goal", "field", "frontier"})
DEFAULT_NAVIGATIONAL_KINDS = frozenset(
    {
        "research-gap",
        "research-topic-map",
        "research-current",
        "research-frontier",
        "frontier-family",
        "index",
        "catalogue",
        "map",
        "topic-map",
    }
)
STRUCTURAL_PREDICATES = frozenset(
    {
        "depends-on",
        "supersedes",
        "superseded-by",
        "consolidated-into",
        "maps-to",
        "instantiates",
        "instantiated-by",
    }
)
QUALIFYING_PREDICATES = frozenset(
    {
        "qualifies",
        "qualified-by",
        "scope-restricts",
        "scope-restricted-by",
        "evidenced-by",
        "evidences",
        "supports",
        "supported-by",
        "contradicts",
        "contradicted-by",
        "refines",
        "refined-by",
        "answers",
        "answered-by",
        "partially-answers",
        "partially-answered-by",
        "cross-family-pair",
        "common-mechanism",
        "bears-on",
        "uses-finding",
        "interprets-observation",
        "evaluates",
        "evaluated-by",
    }
)
QUALIFICATION_TARGET_KINDS = frozenset(
    {
        "claim",
        "synthesis",
        "conclusion",
        "finding",
        "hypothesis",
        "principle",
        "architecture",
        "decision",
    }
)
CONTRADICT_RESOLUTION_PREDICATES = frozenset(
    {
        "qualifies",
        "qualified-by",
        "scope-restricts",
        "scope-restricted-by",
        "refines",
        "refined-by",
        "consolidated-into",
        "supersedes",
        "superseded-by",
    }
)
BYPASS_EXEMPT_PREDICATES = frozenset(
    {
        "supersedes",
        "superseded-by",
        "consolidated-into",
        "revises native navigation from",
        "corresponds to dated origin",
        "grounded in original edition",
        "declared by original source",
    }
)

_MAX_SAMPLE_ITEMS = 25
_MAX_CLUSTERING_DEGREE = 512
_MAX_HOP2_EXPANSION_DEGREE = 256


@dataclass(frozen=True, slots=True)
class GraphProfileCalibration:
    root_kinds: frozenset[str] = DEFAULT_ROOT_KINDS
    claim_kinds: frozenset[str] = QUALIFICATION_TARGET_KINDS
    navigational_kinds: frozenset[str] = DEFAULT_NAVIGATIONAL_KINDS
    structural_predicates: frozenset[str] = STRUCTURAL_PREDICATES
    qualifying_predicates: frozenset[str] = QUALIFYING_PREDICATES
    contradict_resolution_predicates: frozenset[str] = CONTRADICT_RESOLUTION_PREDICATES
    bypass_exempt_predicates: frozenset[str] = BYPASS_EXEMPT_PREDICATES
    default_roots: tuple[str, ...] = ()
    desert_min_fan_in: int = 3
    desert_min_qual_ratio: float = 0.2
    sources: tuple[str, ...] = ("defaults",)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": list(self.sources),
            "root_kinds": sorted(self.root_kinds),
            "claim_kinds": sorted(self.claim_kinds),
            "navigational_kinds": sorted(self.navigational_kinds),
            "structural_predicates": sorted(self.structural_predicates),
            "qualifying_predicates": sorted(self.qualifying_predicates),
            "contradict_resolution_predicates": sorted(self.contradict_resolution_predicates),
            "bypass_exempt_predicates": sorted(self.bypass_exempt_predicates),
            "desert_min_fan_in": self.desert_min_fan_in,
            "desert_min_qual_ratio": self.desert_min_qual_ratio,
        }


DEFAULT_CALIBRATION = GraphProfileCalibration()


def percentile(sorted_vals: list[int] | list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    rank = min(len(sorted_vals) - 1, max(0, math.ceil(q * len(sorted_vals)) - 1))
    return float(sorted_vals[rank])


def _compute_clustering(active_indices: list[int], undirected: list[set[int]]) -> dict[str, Any]:
    total_coeff = 0.0
    evaluated = 0
    capped_hubs = 0
    for u in active_indices:
        nbrs = undirected[u]
        deg = len(nbrs)
        if deg < 2:
            continue
        if deg > _MAX_CLUSTERING_DEGREE:
            capped_hubs += 1
            continue
        links = sum(len(nbrs & undirected[v]) for v in nbrs)
        total_coeff += links / (deg * (deg - 1))
        evaluated += 1
    active_count = len(active_indices)
    return {
        "avg_local_clustering": round(total_coeff / evaluated, 4) if evaluated else 0.0,
        "global_avg_clustering": round(total_coeff / active_count, 4) if active_count else 0.0,
        "clustering_evaluated_nodes": evaluated,
        "clustering_capped_hubs": capped_hubs,
    }


def _compute_unresolved_contradicts(
    snap: Any, active_indices: list[int], resolution_preds: frozenset[str]
) -> dict[str, Any]:
    res_neighbors: dict[int, set[int]] = defaultdict(set)
    contradict_edges: list[tuple[int, int]] = []
    for u in active_indices:
        for v, pred in snap.out_edges[u]:
            if not snap.active[v]:
                continue
            if pred == "contradicts":
                contradict_edges.append((u, v))
            elif pred == "contradicted-by":
                contradict_edges.append((v, u))
            elif pred in resolution_preds:
                res_neighbors[u].add(v)
                res_neighbors[v].add(u)

    unresolved: list[tuple[int, int]] = []
    adj: dict[int, set[int]] = defaultdict(set)
    for u, v in contradict_edges:
        if v not in res_neighbors[u] and not (res_neighbors[u] & res_neighbors[v]):
            unresolved.append((u, v))
            adj[u].add(v)
            adj[v].add(u)

    visited: set[int] = set()
    clusters: list[dict[str, Any]] = []
    for node in sorted(adj):
        if node in visited:
            continue
        comp: set[int] = set()
        queue: deque[int] = deque([node])
        visited.add(node)
        while queue:
            cur = queue.popleft()
            comp.add(cur)
            for nxt in adj[cur]:
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        comp_edges = sorted(
            (
                {"from": snap.ids[u], "to": snap.ids[v]}
                for u, v in unresolved
                if u in comp and v in comp
            ),
            key=lambda e: (e["from"], e["to"]),
        )
        comp_ids = sorted(snap.ids[i] for i in comp)
        clusters.append(
            {
                "size": len(comp_ids),
                "edge_count": len(comp_edges),
                "node_ids": comp_ids,
                "edges": comp_edges,
            }
        )
    clusters.sort(key=lambda c: (-c["size"], c["node_ids"][0]))
    return {
        "total_active_contradicts_edges": len(contradict_edges),
        "unresolved_contradicts_edge_count": len(unresolved),
        "unresolved_contradicts_cluster_count": len(clusters),
        "unresolved_contradicts_clusters": clusters[:_MAX_SAMPLE_ITEMS],
    }


def _compute_working_set(
    snap: Any,
    active_indices: list[int],
    undirected: list[set[int]],
    qual_incident: list[int],
) -> dict[str, Any]:
    hop1_list: list[int] = []
    hop2_list: list[int] = []
    ratios: list[float] = []
    heaviest: list[dict[str, Any]] = []
    hop2_exact = True

    for u in active_indices:
        nbr1 = undirected[u]
        b1 = snap.sizes[u] + sum(snap.sizes[v] for v in nbr1)
        hop1_list.append(b1)
        if len(nbr1) <= _MAX_HOP2_EXPANSION_DEGREE:
            hop2_nodes = set(nbr1) | {u}
            for v in nbr1:
                hop2_nodes.update(undirected[v])
            b2 = sum(snap.sizes[x] for x in hop2_nodes)
        else:
            hop2_exact = False
            b2 = b1
        hop2_list.append(b2)
        sig_ratio = round(qual_incident[u] / (b1 / 1024.0), 4) if b1 > 0 else 0.0
        ratios.append(sig_ratio)
        heaviest.append(
            {
                "id": snap.ids[u],
                "kind": snap.kinds[u],
                "degree": len(nbr1),
                "hop1_bytes": b1,
                "hop2_bytes": b2,
                "qualifying_edges": qual_incident[u],
                "signal_to_byte_ratio": sig_ratio,
            }
        )

    hop1_list.sort()
    hop2_list.sort()
    ratios.sort()
    heaviest.sort(key=lambda row: (-row["hop1_bytes"], row["id"]))
    return {
        "hop1_bytes_p50": percentile(hop1_list, 0.50),
        "hop1_bytes_p95": percentile(hop1_list, 0.95),
        "hop1_bytes_max": hop1_list[-1] if hop1_list else 0,
        "hop2_bytes_p50": percentile(hop2_list, 0.50),
        "hop2_bytes_p95": percentile(hop2_list, 0.95),
        "hop2_bytes_max": hop2_list[-1] if hop2_list else 0,
        "hop2_bytes_exact": hop2_exact,
        "signal_to_byte_ratio_p50": percentile(ratios, 0.50),
        "signal_to_byte_ratio_mean": round(sum(ratios) / len(ratios), 4) if ratios else 0.0,
        "heaviest_working_sets": heaviest[:10],
    }


def _collect_edge_stats(
    snap: Any, active_indices: list[int], cal: GraphProfileCalibration
) -> dict[str, Any]:
    pred_counts: Counter[str] = Counter()
    kind_out: Counter[str] = Counter()
    kind_in: Counter[str] = Counter()
    kind_qual: Counter[str] = Counter()
    kind_struct: Counter[str] = Counter()
    fan_in = [0] * snap.n
    qual_incident = [0] * snap.n
    struct_incident = [0] * snap.n
    counts = {"structural": 0, "qualifying": 0, "other": 0}
    struct_preds = cal.structural_predicates
    qual_preds = cal.qualifying_predicates

    for u in active_indices:
        ukind = snap.kinds[u]
        for v, pred in snap.out_edges[u]:
            if not snap.active[v]:
                continue
            pred_counts[pred] += 1
            kind_out[ukind] += 1
            kind_in[snap.kinds[v]] += 1
            fan_in[v] += 1
            if pred in struct_preds:
                counts["structural"] += 1
                struct_incident[u] += 1
                struct_incident[v] += 1
                kind_struct[ukind] += 1
            elif pred in qual_preds:
                counts["qualifying"] += 1
                qual_incident[u] += 1
                qual_incident[v] += 1
                kind_qual[ukind] += 1
            else:
                counts["other"] += 1

    return {
        "pred_counts": pred_counts,
        "kind_out": kind_out,
        "kind_in": kind_in,
        "kind_qual": kind_qual,
        "kind_struct": kind_struct,
        "fan_in": fan_in,
        "qual_incident": qual_incident,
        "struct_incident": struct_incident,
        "counts": counts,
    }


def _find_qualification_deserts(
    snap: Any,
    active_indices: list[int],
    fan_in: list[int],
    qual_incident: list[int],
    struct_incident: list[int],
    cal: GraphProfileCalibration,
) -> tuple[list[dict[str, Any]], int]:
    excluded = cal.navigational_kinds | cal.root_kinds
    nav_count = sum(1 for u in active_indices if snap.kinds[u] in cal.navigational_kinds)
    min_fi = cal.desert_min_fan_in
    min_ratio = cal.desert_min_qual_ratio
    ratio_fi = max(min_fi + 2, 5)
    deserts: list[dict[str, Any]] = [
        {
            "id": snap.ids[u],
            "kind": snap.kinds[u],
            "fan_in": fan_in[u],
            "qualifying_edges": qual_incident[u],
            "structural_edges": struct_incident[u],
            "size_bytes": snap.sizes[u],
        }
        for u in active_indices
        if snap.kinds[u] not in excluded
        and (
            (fan_in[u] >= min_fi and qual_incident[u] == 0)
            or (fan_in[u] >= ratio_fi and qual_incident[u] / fan_in[u] < min_ratio)
        )
    ]
    deserts.sort(key=lambda row: (-row["fan_in"], row["qualifying_edges"], row["id"]))
    return deserts, nav_count


def _kind_role(kind: str, cal: GraphProfileCalibration) -> str:
    if kind in cal.root_kinds:
        return "root"
    if kind in cal.navigational_kinds:
        return "navigational"
    if kind in cal.claim_kinds:
        return "claim"
    return "other"


def compute_density_and_qualification(
    snap: Any,
    active_indices: list[int],
    undirected: list[set[int]],
    calibration: GraphProfileCalibration = DEFAULT_CALIBRATION,
) -> dict[str, Any]:
    """Compute predicate-aware density, entropy, clustering, qualification, and working set."""
    stats = _collect_edge_stats(snap, active_indices, calibration)
    pred_counts: Counter[str] = stats["pred_counts"]
    counts = stats["counts"]
    active_nodes = len(active_indices)
    active_edges = counts["structural"] + counts["qualifying"] + counts["other"]

    entropy = (
        -sum((cnt / active_edges) * math.log2(cnt / active_edges) for cnt in pred_counts.values())
        if active_edges > 0
        else 0.0
    )
    max_entropy = math.log2(len(pred_counts)) if len(pred_counts) > 1 else 0.0

    def _cat(pred: str) -> str:
        if pred in calibration.structural_predicates:
            return "structural"
        return "qualifying" if pred in calibration.qualifying_predicates else "other"

    kind_totals = Counter(snap.kinds)
    kind_active = Counter(snap.kinds[i] for i in active_indices)
    per_predicate = {
        pred: {
            "count": cnt,
            "density": round(cnt / active_nodes, 4) if active_nodes else 0.0,
            "category": _cat(pred),
        }
        for pred, cnt in sorted(pred_counts.items())
    }
    per_kind = {
        kind: {
            "role": _kind_role(kind, calibration),
            "total": kind_totals[kind],
            "active": kind_active[kind],
            "out_edges": stats["kind_out"][kind],
            "in_edges": stats["kind_in"][kind],
            "edge_density": round(stats["kind_out"][kind] / kind_active[kind], 4)
            if kind_active[kind]
            else 0.0,
            "structural_edges": stats["kind_struct"][kind],
            "qualifying_edges": stats["kind_qual"][kind],
        }
        for kind in sorted(kind_totals)
    }

    target_nodes = [
        i
        for i in active_indices
        if snap.kinds[i] in calibration.claim_kinds
        and snap.kinds[i] not in calibration.navigational_kinds
    ]
    target_scope = "claim_and_synthesis_kinds"
    if not target_nodes:
        excluded = calibration.root_kinds | calibration.navigational_kinds
        target_nodes = [i for i in active_indices if snap.kinds[i] not in excluded]
        target_scope = "all_active_substantive_non_roots"

    qual_incident: list[int] = stats["qual_incident"]
    deserts, nav_count = _find_qualification_deserts(
        snap,
        active_indices,
        stats["fan_in"],
        qual_incident,
        stats["struct_incident"],
        calibration,
    )
    return {
        "active_edge_count": active_edges,
        "global_edge_density": round(active_edges / active_nodes, 4) if active_nodes else 0.0,
        "structural_edge_count": counts["structural"],
        "structural_edge_density": round(counts["structural"] / active_nodes, 4)
        if active_nodes
        else 0.0,
        "qualifying_edge_count": counts["qualifying"],
        "qualifying_edge_density": round(counts["qualifying"] / active_nodes, 4)
        if active_nodes
        else 0.0,
        "other_edge_count": counts["other"],
        "other_edge_density": round(counts["other"] / active_nodes, 4) if active_nodes else 0.0,
        "qualifying_to_structural_ratio": round(
            counts["qualifying"] / max(counts["structural"], 1), 4
        ),
        "predicate_mix_entropy": round(entropy, 4),
        "predicate_mix_entropy_normalized": round(entropy / max_entropy, 4)
        if max_entropy > 0
        else 0.0,
        "clustering": _compute_clustering(active_indices, undirected),
        "qualification_target_scope": target_scope,
        "qualification_target_count": len(target_nodes),
        "navigational_active_count": nav_count,
        "qualification_density": round(
            sum(qual_incident[i] for i in target_nodes) / len(target_nodes), 4
        )
        if target_nodes
        else 0.0,
        "overall_qualification_density": round(
            sum(qual_incident[i] for i in active_indices) / active_nodes, 4
        )
        if active_nodes
        else 0.0,
        "qualification_desert_count": len(deserts),
        "qualification_deserts": deserts[:_MAX_SAMPLE_ITEMS],
        "contradicts": _compute_unresolved_contradicts(
            snap, active_indices, calibration.contradict_resolution_predicates
        ),
        "working_set": _compute_working_set(snap, active_indices, undirected, qual_incident),
        "per_predicate": per_predicate,
        "per_kind": per_kind,
    }
