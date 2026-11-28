"""Read-only parallel multi-graph cross-domain discovery and N-goal smoothing records.

Federates N distinct graph stores (for example ``writing``, ``marketing``, ``product``,
and ``code``) strictly read-only and non-merging: each store retains its own namespace
and on-disk custody while typed ``CrossGraphRef`` projections support cross-domain
neighborhood/impact queries and ``SmoothingCycle`` tracks N-goal convergence under a
cost budget.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from managed_ai_information.store.protocol.identities import require
from managed_ai_information.store.protocol.inputs import integer, strings, text

MULTI_GRAPH_NEIGHBORS_FORMAT = "managed-ai-multi-graph-neighbors/1"
MULTI_GRAPH_IMPACT_FORMAT = "managed-ai-multi-graph-impact/1"
SMOOTHING_CYCLE_FORMAT = "managed-ai-smoothing-cycle/1"


def _ref_sort_key(ref: CrossGraphRef) -> tuple[str, str, str, str, str, str]:
    return (
        ref.source_graph,
        ref.source_node_id,
        ref.relation,
        ref.target_graph,
        ref.target_node_id,
        ref.basis,
    )


def _require_finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


@dataclass(frozen=True)
class CrossGraphRef:
    """Typed read-only directed link between two distinct graph namespaces."""

    source_graph: str
    source_node_id: str
    relation: str
    target_graph: str
    target_node_id: str
    basis: str

    def __post_init__(self) -> None:
        sg = text(self.source_graph, "source_graph")
        sn = text(self.source_node_id, "source_node_id")
        rel = text(self.relation, "relation")
        tg = text(self.target_graph, "target_graph")
        tn = text(self.target_node_id, "target_node_id")
        bs = text(self.basis, "basis")
        require(sg != tg, "CrossGraphRef must connect two distinct graphs")
        object.__setattr__(self, "source_graph", sg)
        object.__setattr__(self, "source_node_id", sn)
        object.__setattr__(self, "relation", rel)
        object.__setattr__(self, "target_graph", tg)
        object.__setattr__(self, "target_node_id", tn)
        object.__setattr__(self, "basis", bs)

    def to_dict(self) -> dict[str, str]:
        return {
            "source_graph": self.source_graph,
            "source_node_id": self.source_node_id,
            "relation": self.relation,
            "target_graph": self.target_graph,
            "target_node_id": self.target_node_id,
            "basis": self.basis,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CrossGraphRef:
        require(isinstance(data, Mapping), "CrossGraphRef payload must be a mapping")
        return cls(
            source_graph=data.get("source_graph", ""),
            source_node_id=data.get("source_node_id", ""),
            relation=data.get("relation", ""),
            target_graph=data.get("target_graph", ""),
            target_node_id=data.get("target_node_id", ""),
            basis=data.get("basis", ""),
        )


@dataclass(frozen=True)
class SmoothingCycle:
    """One recorded iteration of an N-goal cost-bounded smoothing loop across N graphs."""

    cycle_index: int
    goal_scores: dict[str, float]
    cost_spent: float
    cost_budget: float
    graph_deltas: dict[str, float]
    touched_graphs: tuple[str, ...] = ()
    weights: dict[str, float] | None = None
    format: str = SMOOTHING_CYCLE_FORMAT

    def __post_init__(self) -> None:
        require(self.format == SMOOTHING_CYCLE_FORMAT, "unsupported smoothing cycle format")
        integer(self.cycle_index, "cycle_index", 0)
        require(
            isinstance(self.goal_scores, Mapping) and len(self.goal_scores) > 0,
            "goal_scores must be a nonempty mapping",
        )
        norm_scores = {
            text(k, "goal_id"): _require_finite_float(v, f"goal_scores[{k!r}]")
            for k, v in sorted(self.goal_scores.items())
        }
        spent = _require_finite_float(self.cost_spent, "cost_spent")
        budget = _require_finite_float(self.cost_budget, "cost_budget")
        require(spent >= 0.0, "cost_spent must be >= 0")
        require(budget > 0.0, "cost_budget must be > 0")

        require(isinstance(self.graph_deltas, Mapping), "graph_deltas must be a mapping")
        norm_deltas = {
            text(k, "graph_name"): _require_finite_float(v, f"graph_deltas[{k!r}]")
            for k, v in sorted(self.graph_deltas.items())
        }
        touched = tuple(sorted({text(g, "touched_graph") for g in self.touched_graphs}))

        norm_weights: dict[str, float] | None = None
        if self.weights is not None:
            require(isinstance(self.weights, Mapping), "weights must be a mapping")
            norm_weights = {
                text(k, "weight_goal_id"): _require_finite_float(v, f"weights[{k!r}]")
                for k, v in sorted(self.weights.items())
            }
            require(
                set(norm_weights.keys()) == set(norm_scores.keys()),
                "weights keys must match goal_scores keys",
            )
            require(all(w > 0.0 for w in norm_weights.values()), "weights must be > 0")

        object.__setattr__(self, "goal_scores", norm_scores)
        object.__setattr__(self, "cost_spent", spent)
        object.__setattr__(self, "cost_budget", budget)
        object.__setattr__(self, "graph_deltas", norm_deltas)
        object.__setattr__(self, "touched_graphs", touched)
        object.__setattr__(self, "weights", norm_weights)

    def aggregate_score(self) -> float:
        if self.weights is None:
            return sum(self.goal_scores.values()) / len(self.goal_scores)
        total_w = sum(self.weights.values())
        return sum(self.goal_scores[g] * self.weights[g] for g in self.goal_scores) / total_w

    def min_goal_score(self) -> float:
        return min(self.goal_scores.values())

    def budget_remaining(self) -> float:
        return self.cost_budget - self.cost_spent

    def within_budget(self) -> bool:
        return self.cost_spent <= self.cost_budget

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "format": self.format,
            "cycle_index": self.cycle_index,
            "goal_scores": dict(self.goal_scores),
            "cost_spent": self.cost_spent,
            "cost_budget": self.cost_budget,
            "graph_deltas": dict(self.graph_deltas),
            "touched_graphs": list(self.touched_graphs),
            "aggregate_score": self.aggregate_score(),
            "min_goal_score": self.min_goal_score(),
            "budget_remaining": self.budget_remaining(),
            "within_budget": self.within_budget(),
        }
        if self.weights is not None:
            payload["weights"] = dict(self.weights)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SmoothingCycle:
        require(isinstance(data, Mapping), "SmoothingCycle payload must be a mapping")
        return cls(
            format=str(data.get("format", SMOOTHING_CYCLE_FORMAT)),
            cycle_index=data["cycle_index"],
            goal_scores=dict(data["goal_scores"]),
            cost_spent=data["cost_spent"],
            cost_budget=data["cost_budget"],
            graph_deltas=dict(data.get("graph_deltas", {})),
            touched_graphs=tuple(data.get("touched_graphs", ())),
            weights=dict(data["weights"]) if data.get("weights") is not None else None,
        )


class SmoothingTracker:
    """Tracks ordered ``SmoothingCycle`` convergence for an N-goal optimization problem."""

    def __init__(self, *, enforce_budget: bool = True) -> None:
        self._cycles: list[SmoothingCycle] = []
        self._enforce_budget = enforce_budget

    @property
    def cycles(self) -> tuple[SmoothingCycle, ...]:
        return tuple(self._cycles)

    def record_cycle(self, cycle: SmoothingCycle) -> dict[str, Any]:
        require(isinstance(cycle, SmoothingCycle), "expected SmoothingCycle")
        if self._enforce_budget:
            require(cycle.within_budget(), "smoothing cycle exceeds cost_budget")
        if self._cycles:
            prev = self._cycles[-1]
            require(
                cycle.cycle_index > prev.cycle_index,
                "cycle_index must strictly increase across smoothing cycles",
            )
            require(
                set(cycle.goal_scores.keys()) == set(prev.goal_scores.keys()),
                "goal_scores key set must remain consistent across smoothing cycles",
            )
            require(
                cycle.cost_spent >= prev.cost_spent,
                "cumulative cost_spent must be non-decreasing across smoothing cycles",
            )
            goal_deltas = {
                g: cycle.goal_scores[g] - prev.goal_scores[g] for g in sorted(cycle.goal_scores)
            }
            agg_delta = cycle.aggregate_score() - prev.aggregate_score()
            regressed = [g for g, d in goal_deltas.items() if d < 0.0]
        else:
            goal_deltas = dict.fromkeys(sorted(cycle.goal_scores), 0.0)
            agg_delta = 0.0
            regressed = []
        self._cycles.append(cycle)
        return {
            "cycle_index": cycle.cycle_index,
            "aggregate_score": cycle.aggregate_score(),
            "aggregate_delta": agg_delta,
            "goal_deltas": goal_deltas,
            "regressed_goals": regressed,
            "budget_remaining": cycle.budget_remaining(),
            "within_budget": cycle.within_budget(),
        }

    def is_monotonically_improving(
        self, *, tolerance: float = 0.0, require_pareto: bool = False
    ) -> bool:
        tol = _require_finite_float(tolerance, "tolerance")
        require(tol >= 0.0, "tolerance must be >= 0")
        for prev, curr in zip(self._cycles, self._cycles[1:], strict=False):
            if curr.aggregate_score() + tol < prev.aggregate_score():
                return False
            if require_pareto and any(
                curr.goal_scores[g] + tol < prev.goal_scores[g] for g in curr.goal_scores
            ):
                return False
        return True

    def converged(self, *, min_delta: float = 1e-3, target_floor: float | None = None) -> bool:
        threshold = _require_finite_float(min_delta, "min_delta")
        require(threshold >= 0.0, "min_delta must be >= 0")
        if len(self._cycles) < 2:
            return False
        prev, curr = self._cycles[-2], self._cycles[-1]
        if target_floor is not None:
            floor = _require_finite_float(target_floor, "target_floor")
            if curr.min_goal_score() < floor:
                return False
        max_step = max(abs(curr.goal_scores[g] - prev.goal_scores[g]) for g in curr.goal_scores)
        return max_step <= threshold

    def summary(
        self, *, tolerance: float = 0.0, min_delta: float = 1e-3, target_floor: float | None = None
    ) -> dict[str, Any]:
        if not self._cycles:
            return {
                "cycle_count": 0,
                "goal_ids": [],
                "latest_scores": {},
                "total_deltas": {},
                "cost_spent": 0.0,
                "cost_budget": 0.0,
                "within_budget": True,
                "monotonically_improving": True,
                "pareto_non_regressing": True,
                "converged": False,
            }
        first, latest = self._cycles[0], self._cycles[-1]
        return {
            "cycle_count": len(self._cycles),
            "goal_ids": sorted(latest.goal_scores),
            "latest_scores": dict(latest.goal_scores),
            "total_deltas": {
                g: latest.goal_scores[g] - first.goal_scores[g] for g in sorted(latest.goal_scores)
            },
            "cost_spent": latest.cost_spent,
            "cost_budget": latest.cost_budget,
            "within_budget": all(c.within_budget() for c in self._cycles),
            "monotonically_improving": self.is_monotonically_improving(tolerance=tolerance),
            "pareto_non_regressing": self.is_monotonically_improving(
                tolerance=tolerance, require_pareto=True
            ),
            "converged": self.converged(min_delta=min_delta, target_floor=target_floor),
        }


def _extract_store_nodes(store: Any) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for rec in store.record_store.scan_records():
        ident = str(rec["id"])
        rels: list[tuple[str, str]] = []
        for rel in rec.get("relations", ()):
            if isinstance(rel, Mapping) and "relation" in rel and "target" in rel:
                rels.append((str(rel["relation"]), str(rel["target"])))
        rels.sort()
        nodes[ident] = {
            "id": ident,
            "kind": str(rec.get("kind", "")),
            "revision": int(rec.get("revision", 0)),
            "sha256": str(rec.get("sha256", "")),
            "active": bool(rec.get("active", True)),
            "relations": rels,
            "content": rec.get("content"),
        }
    return nodes


def _pin_for_node(graph_name: str, node: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "graph": graph_name,
        "id": node["id"],
        "kind": node["kind"],
        "revision": node["revision"],
        "sha256": node["sha256"],
        "active": node["active"],
    }


def _parse_qualified_target(
    raw_target: str, known_graphs: Mapping[str, Any], source_graph: str
) -> tuple[str, str] | None:
    candidate = raw_target[len("graph:") :] if raw_target.startswith("graph:") else raw_target
    if ":" not in candidate:
        return None
    prefix, node_id = candidate.split(":", 1)
    if prefix in known_graphs and prefix != source_graph and node_id.strip():
        return prefix, node_id
    return None


class MultiGraphFederation:
    """Read-only, non-merging federation over N distinct graph stores."""

    def __init__(self, graphs: Mapping[str, Any] | None = None) -> None:
        self._graphs: dict[str, Any] = {}
        self._registered_links: list[CrossGraphRef] = []
        self._registered_set: set[CrossGraphRef] = set()
        if graphs:
            for name, store in graphs.items():
                self.register_graph(name, store)

    def register_graph(self, name: str, store: Any) -> None:
        gname = text(name, "graph_name")
        require(gname not in self._graphs, f"graph already registered: {gname}")
        require(hasattr(store, "record_store"), f"graph {gname} must expose record_store")
        self._graphs[gname] = store

    def unregister_graph(self, name: str) -> None:
        gname = text(name, "graph_name")
        require(gname in self._graphs, f"unknown graph: {gname}")
        del self._graphs[gname]
        self._registered_links = [
            ref
            for ref in self._registered_links
            if ref.source_graph != gname and ref.target_graph != gname
        ]
        self._registered_set = set(self._registered_links)

    def graph_names(self) -> list[str]:
        return sorted(self._graphs)

    def add_link(self, ref: CrossGraphRef) -> None:
        require(isinstance(ref, CrossGraphRef), "expected CrossGraphRef")
        require(ref.source_graph in self._graphs, f"unknown source_graph: {ref.source_graph}")
        require(ref.target_graph in self._graphs, f"unknown target_graph: {ref.target_graph}")
        if ref not in self._registered_set:
            self._registered_set.add(ref)
            self._registered_links.append(ref)

    def _snapshot_all(self) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, str]]:
        snapshots: dict[str, dict[str, dict[str, Any]]] = {}
        instance_to_graph: dict[str, str] = {}
        for gname, store in sorted(self._graphs.items()):
            snapshots[gname] = _extract_store_nodes(store)
            inst_id = getattr(store, "instance_id", None)
            if isinstance(inst_id, str) and inst_id.strip():
                instance_to_graph[inst_id] = gname
        return snapshots, instance_to_graph

    def _discover_store_refs(
        self,
        snapshots: Mapping[str, Mapping[str, dict[str, Any]]],
        instance_to_graph: Mapping[str, str],
    ) -> list[CrossGraphRef]:
        discovered: set[CrossGraphRef] = set()
        for gname, nodes in sorted(snapshots.items()):
            for nid, node in sorted(nodes.items()):
                basis = f"record:{gname}:{nid}@r{node['revision']}:{node['sha256']}"
                for label, raw_target in node["relations"]:
                    parsed = _parse_qualified_target(raw_target, self._graphs, gname)
                    if parsed is not None:
                        tgt_graph, tgt_id = parsed
                        discovered.add(
                            CrossGraphRef(
                                source_graph=gname,
                                source_node_id=nid,
                                relation=label,
                                target_graph=tgt_graph,
                                target_node_id=tgt_id,
                                basis=basis,
                            )
                        )
                if node["kind"] == "source-observation" and isinstance(node["content"], Mapping):
                    src_meta = node["content"].get("source")
                    if isinstance(src_meta, Mapping):
                        src_inst = str(src_meta.get("instance_id", ""))
                        src_rec = str(src_meta.get("record_id", ""))
                        obs_graph = instance_to_graph.get(src_inst)
                        if obs_graph and obs_graph != gname and src_rec:
                            obj_sha = str(node["content"].get("object_sha256", node["sha256"]))
                            discovered.add(
                                CrossGraphRef(
                                    source_graph=gname,
                                    source_node_id=nid,
                                    relation="observes-source",
                                    target_graph=obs_graph,
                                    target_node_id=src_rec,
                                    basis=f"observation:{obj_sha}",
                                )
                            )
        return sorted(discovered, key=_ref_sort_key)

    def discover_cross_graph_refs(
        self, *, include_registered: bool = True, include_store_refs: bool = True
    ) -> list[CrossGraphRef]:
        """Return deterministic read-only cross-graph refs across all registered stores."""
        snapshots, instance_to_graph = self._snapshot_all()
        combined: set[CrossGraphRef] = set()
        if include_registered:
            combined.update(self._registered_links)
        if include_store_refs:
            combined.update(self._discover_store_refs(snapshots, instance_to_graph))
        return sorted(combined, key=_ref_sort_key)

    def _build_adjacency(
        self,
        snapshots: Mapping[str, Mapping[str, dict[str, Any]]],
        cross_refs: Iterable[CrossGraphRef],
    ) -> tuple[
        dict[tuple[str, str], list[tuple[tuple[str, str], str, bool, str | None]]],
        dict[tuple[str, str], list[tuple[tuple[str, str], str, bool, str | None]]],
    ]:
        outgoing: dict[tuple[str, str], list[tuple[tuple[str, str], str, bool, str | None]]] = {}
        incoming: dict[tuple[str, str], list[tuple[tuple[str, str], str, bool, str | None]]] = {}

        for gname, nodes in sorted(snapshots.items()):
            for nid, node in sorted(nodes.items()):
                src = (gname, nid)
                for label, raw_target in node["relations"]:
                    if _parse_qualified_target(raw_target, self._graphs, gname) is not None:
                        continue
                    dst = (gname, raw_target)
                    outgoing.setdefault(src, []).append((dst, label, False, None))
                    incoming.setdefault(dst, []).append((src, label, False, None))

        for ref in cross_refs:
            src = (ref.source_graph, ref.source_node_id)
            dst = (ref.target_graph, ref.target_node_id)
            outgoing.setdefault(src, []).append((dst, ref.relation, True, ref.basis))
            incoming.setdefault(dst, []).append((src, ref.relation, True, ref.basis))

        for edges in outgoing.values():
            edges.sort(key=lambda item: (item[0][0], item[0][1], item[1], item[2], item[3] or ""))
        for edges in incoming.values():
            edges.sort(key=lambda item: (item[0][0], item[0][1], item[1], item[2], item[3] or ""))
        return outgoing, incoming

    def neighborhood(
        self,
        graph_name: str,
        node_id: str,
        *,
        direction: str = "both",
        max_hops: int = 2,
        relations: Iterable[str] = (),
        target_graphs: Iterable[str] | None = None,
        active_only: bool = False,
    ) -> dict[str, Any]:
        """Cycle-safe read-only BFS neighborhood across N separate graph stores."""
        gname = text(graph_name, "graph_name")
        nid = text(node_id, "node_id")
        require(gname in self._graphs, f"unknown graph: {gname}")
        require(direction in ("in", "out", "both"), "direction must be in, out or both")
        integer(max_hops, "max_hops", 1)
        require(max_hops <= 6, "max_hops must be between 1 and 6")

        rel_filter = set(strings(list(relations), "relations")) if relations else None
        graph_filter = (
            set(strings(list(target_graphs), "target_graphs"))
            if target_graphs is not None
            else None
        )

        snapshots, instance_to_graph = self._snapshot_all()
        require(nid in snapshots[gname], f"node not found in graph {gname!r}: {nid}")

        cross_refs = sorted(
            set(self._registered_links)
            | set(self._discover_store_refs(snapshots, instance_to_graph)),
            key=_ref_sort_key,
        )
        outgoing, incoming = self._build_adjacency(snapshots, cross_refs)

        origin_key = (gname, nid)
        visited: dict[tuple[str, str], int] = {origin_key: 0}
        queue: deque[tuple[tuple[str, str], int]] = deque([(origin_key, 0)])
        discovered_nodes: list[dict[str, Any]] = []
        traversed_edges: list[dict[str, Any]] = []
        seen_edges: set[tuple[str, str, str, str, str, str, bool]] = set()

        while queue:
            cur, depth = queue.popleft()
            if depth >= max_hops:
                continue
            steps: list[tuple[tuple[str, str], tuple[str, str], str, str, bool, str | None]] = []
            if direction in ("out", "both"):
                for dst, label, is_cross, basis in outgoing.get(cur, ()):
                    steps.append((cur, dst, label, "out", is_cross, basis))
            if direction in ("in", "both"):
                for src, label, is_cross, basis in incoming.get(cur, ()):
                    steps.append((src, cur, label, "in", is_cross, basis))

            for src, dst, label, edge_dir, is_cross, basis in steps:
                if rel_filter is not None and label not in rel_filter:
                    continue
                other = dst if edge_dir == "out" else src
                other_node = snapshots.get(other[0], {}).get(other[1])
                if other_node is None:
                    continue
                if active_only and not other_node["active"]:
                    continue
                ekey = (src[0], src[1], dst[0], dst[1], label, edge_dir, is_cross)
                if ekey not in seen_edges:
                    seen_edges.add(ekey)
                    edge_row: dict[str, Any] = {
                        "from_graph": src[0],
                        "from_id": src[1],
                        "to_graph": dst[0],
                        "to_id": dst[1],
                        "relation": label,
                        "direction": edge_dir,
                        "cross_graph": is_cross,
                        "depth": depth + 1,
                    }
                    if basis is not None:
                        edge_row["basis"] = basis
                    traversed_edges.append(edge_row)
                if other not in visited:
                    visited[other] = depth + 1
                    discovered_nodes.append(
                        dict(_pin_for_node(other[0], other_node), depth=depth + 1)
                    )
                    queue.append((other, depth + 1))

        if graph_filter is not None:
            discovered_nodes = [row for row in discovered_nodes if row["graph"] in graph_filter]
        discovered_nodes.sort(key=lambda r: (r["depth"], r["graph"], r["id"]))

        return {
            "format": MULTI_GRAPH_NEIGHBORS_FORMAT,
            "origin": _pin_for_node(gname, snapshots[gname][nid]),
            "registered_graphs": self.graph_names(),
            "direction": direction,
            "max_hops": max_hops,
            "active_only": active_only,
            "neighbor_count": len(discovered_nodes),
            "edge_count": len(traversed_edges),
            "neighbors": discovered_nodes,
            "edges": traversed_edges,
        }

    def impact(
        self,
        graph_name: str,
        node_id: str,
        *,
        max_hops: int = 3,
        relations: Iterable[str] = (),
        target_graphs: Iterable[str] | None = None,
        active_only: bool = False,
    ) -> dict[str, Any]:
        """Project cross-domain impact of ``(graph_name, node_id)`` without merging graphs."""
        result = self.neighborhood(
            graph_name,
            node_id,
            direction="both",
            max_hops=max_hops,
            relations=relations,
            target_graphs=target_graphs,
            active_only=active_only,
        )
        by_graph: dict[str, list[dict[str, Any]]] = {}
        for row in result["neighbors"]:
            by_graph.setdefault(row["graph"], []).append(row)
        cross_edges = [edge for edge in result["edges"] if edge["cross_graph"]]
        return {
            "format": MULTI_GRAPH_IMPACT_FORMAT,
            "origin": result["origin"],
            "registered_graphs": result["registered_graphs"],
            "max_hops": max_hops,
            "affected_graphs": sorted(by_graph),
            "by_graph": {g: by_graph[g] for g in sorted(by_graph)},
            "cross_graph_edge_count": len(cross_edges),
            "cross_graph_edges": cross_edges,
            "neighbors": result["neighbors"],
            "edges": result["edges"],
            "read_only_non_merging": True,
        }


__all__ = [
    "MULTI_GRAPH_IMPACT_FORMAT",
    "MULTI_GRAPH_NEIGHBORS_FORMAT",
    "SMOOTHING_CYCLE_FORMAT",
    "CrossGraphRef",
    "MultiGraphFederation",
    "SmoothingCycle",
    "SmoothingTracker",
]
