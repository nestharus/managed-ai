"""Mechanical graph-health repair planners (zero sealed payload reads, zero data loss)."""

import copy
from collections import deque
from dataclasses import dataclass
from typing import Any

from managed_ai_information.core.storage import encode
from managed_ai_information.store.query.graph_profile_metrics import (
    BYPASS_EXEMPT_PREDICATES,
    DEFAULT_ROOT_KINDS,
)

SUCCESSOR_OUT_PREDICATES: frozenset[str] = frozenset({"consolidated-into", "superseded-by"})
SUCCESSOR_IN_PREDICATES: frozenset[str] = frozenset({"supersedes"})
RETIRE_ALLOWED_KINDS: frozenset[str] = frozenset({"question"})
HUB_MAP_PREDICATES: frozenset[str] = frozenset(
    {"tracks-topic-map", "indexes-synthesis", "indexes-causal-ancestor"}
)
HUB_DELEGATABLE_PREDICATES: frozenset[str] = frozenset(
    {
        "tracks-direction",
        "tracks-claim",
        "tracks-gap",
        "tracks-novel-issue",
        "tracks-frontier-family",
        "tracks-frontier-question",
        "tracks-scoped-finding",
    }
)


@dataclass(slots=True)
class Topology:
    """Compact integer-indexed view of hot record graph topology."""

    n: int
    ids: list[str]
    id_to_idx: dict[str, int]
    kinds: list[str]
    active: list[bool]
    out_edges: list[list[tuple[int, str]]]
    in_edges: list[list[tuple[int, str]]]
    self_loop_nodes: set[int]
    indexed: bool


@dataclass(slots=True)
class RecordPatch:
    """Accumulated reversible relation and active-flag patch for one record."""

    record_id: str
    before_revision: int
    before_sha256: str
    previous_active: bool
    remove_relations: list[dict[str, Any]]
    add_relations: list[dict[str, Any]]
    set_active: bool | None = None


def _compute_active_reachability(
    topo: Topology,
    explicit_roots: list[str],
    *,
    root_kinds: frozenset[str] = DEFAULT_ROOT_KINDS,
) -> tuple[set[int], list[set[int]]]:
    active_indices = [i for i in range(topo.n) if topo.active[i]]
    undirected: list[set[int]] = [set() for _ in range(topo.n)]
    for u in active_indices:
        for v, _pred in topo.out_edges[u]:
            if topo.active[v]:
                undirected[u].add(v)
                undirected[v].add(u)

    root_idxs: list[int] = []
    if explicit_roots:
        for rid in explicit_roots:
            idx = topo.id_to_idx.get(rid)
            if idx is None or not topo.active[idx]:
                raise ValueError(f"explicit root must exist and be active: {rid}")
            if idx not in root_idxs:
                root_idxs.append(idx)
    else:
        root_idxs = [i for i in active_indices if topo.kinds[i] in root_kinds]
        if not root_idxs:
            root_idxs = [
                i for i in active_indices if not any(topo.active[s] for s, _ in topo.in_edges[i])
            ]
        if not root_idxs:
            root_idxs = list(active_indices)

    reachable: set[int] = set(root_idxs)
    queue: deque[int] = deque(root_idxs)
    while queue:
        cur = queue.popleft()
        for nxt in undirected[cur]:
            if nxt not in reachable:
                reachable.add(nxt)
                queue.append(nxt)
    return reachable, undirected


def _resolve_active_successor(topo: Topology, start_idx: int) -> tuple[str, list[int]]:
    """Follow consolidated-into/superseded-by (out) or supersedes (in) with cycle guard."""
    visited: set[int] = {start_idx}
    queue: deque[int] = deque([start_idx])
    active_heads: set[int] = set()
    while queue:
        cur = queue.popleft()
        candidates = [v for v, pred in topo.out_edges[cur] if pred in SUCCESSOR_OUT_PREDICATES]
        candidates.extend(s for s, pred in topo.in_edges[cur] if pred in SUCCESSOR_IN_PREDICATES)
        for nxt in candidates:
            if topo.active[nxt]:
                active_heads.add(nxt)
            elif nxt not in visited:
                visited.add(nxt)
                queue.append(nxt)
    ordered = sorted(active_heads)
    if len(ordered) == 1:
        return "single", ordered
    if len(ordered) > 1:
        return "ambiguous", ordered
    return "none", []


@dataclass(slots=True)
class _PlanCtx:
    system: Any
    topo: Topology
    patches: dict[str, RecordPatch]
    rec_cache: dict[str, dict[str, Any]]
    actions: list[dict[str, Any]]

    def show(self, rid: str) -> dict[str, Any]:
        rec = self.rec_cache.get(rid)
        if rec is None:
            rec = self.system.record_store.show(rid)
            self.rec_cache[rid] = rec
        return rec

    def patch(self, rid: str) -> tuple[RecordPatch, dict[str, Any]]:
        rec = self.show(rid)
        patch = self.patches.get(rid)
        if patch is None:
            raw_rev = rec["revision"]
            if type(raw_rev) is not int or raw_rev < 1:
                raise ValueError(f"record {rid} has invalid revision: {raw_rev!r}")
            patch = RecordPatch(rid, raw_rev, str(rec["sha256"]), bool(rec["active"]), [], [], None)
            self.patches[rid] = patch
        return patch, rec


def _append_unique_rel(bucket: list[dict[str, Any]], rel: dict[str, Any]) -> None:
    encoded = encode(rel)
    if all(encode(item) != encoded for item in bucket):
        bucket.append(copy.deepcopy(rel))


def _plan_self_loops(ctx: _PlanCtx) -> int:
    count = 0
    for idx in sorted(ctx.topo.self_loop_nodes, key=lambda i: ctx.topo.ids[i]):
        if not ctx.topo.active[idx]:
            continue
        rid = ctx.topo.ids[idx]
        patch, rec = ctx.patch(rid)
        for rel in rec.get("relations", []):
            if rel.get("target") == rid:
                _append_unique_rel(patch.remove_relations, rel)
                ctx.actions.append(
                    {
                        "action": "remove_self_loop",
                        "record_id": rid,
                        "relation": str(rel.get("relation", "")),
                        "removed_relation": copy.deepcopy(rel),
                    }
                )
                count += 1
    return count


def _plan_single_bypass_edge(ctx: _PlanCtx, u: int, v: int, active_idx: int, pred: str) -> int:
    rid, old_target, new_target = ctx.topo.ids[u], ctx.topo.ids[v], ctx.topo.ids[active_idx]
    patch, rec = ctx.patch(rid)
    target_rec = ctx.show(new_target)
    existing_pairs = {
        (str(r.get("relation", "")), str(r.get("target", "")))
        for r in rec.get("relations", []) + patch.add_relations
    }
    matched = [
        r
        for r in rec.get("relations", [])
        if r.get("relation") == pred and r.get("target") == old_target
    ]
    changed = 0
    for rel in matched:
        _append_unique_rel(patch.remove_relations, rel)
        base = {
            "record_id": rid,
            "relation": pred,
            "prior_target": old_target,
            "active_target": new_target,
            "removed_relation": copy.deepcopy(rel),
        }
        if (pred, new_target) in existing_pairs:
            ctx.actions.append({"action": "dedup_repointed_bypass", **base})
        else:
            updated = copy.deepcopy(rel)
            updated["target"] = new_target
            if "revision" in updated:
                target_rev = target_rec["revision"]
                if type(target_rev) is not int or target_rev < 1:
                    raise ValueError(f"target record {new_target} has invalid revision")
                updated["revision"] = target_rev
            if "sha256" in updated:
                updated["sha256"] = str(target_rec["sha256"])
            _append_unique_rel(patch.add_relations, updated)
            existing_pairs.add((pred, new_target))
            ctx.actions.append(
                {"action": "repoint_bypass", **base, "added_relation": copy.deepcopy(updated)}
            )
        changed += 1
    return changed


def _plan_actionable_bypasses(
    ctx: _PlanCtx, *, bypass_exempt_predicates: frozenset[str] = BYPASS_EXEMPT_PREDICATES
) -> tuple[int, int, list[dict[str, Any]]]:
    repointed = 0
    skipped_historical = 0
    skipped_ambiguous: list[dict[str, Any]] = []
    cache_succ: dict[int, tuple[str, list[int]]] = {}

    for u in range(ctx.topo.n):
        if not ctx.topo.active[u]:
            continue
        rid = ctx.topo.ids[u]
        seen_edges: set[tuple[int, str]] = set()
        for v, pred in ctx.topo.out_edges[u]:
            if ctx.topo.active[v] or pred in bypass_exempt_predicates or (v, pred) in seen_edges:
                continue
            seen_edges.add((v, pred))
            status, heads = cache_succ.get(v) or _resolve_active_successor(ctx.topo, v)
            cache_succ[v] = (status, heads)
            if status == "none":
                skipped_historical += 1
            elif status == "ambiguous":
                skipped_ambiguous.append(
                    {
                        "from": rid,
                        "relation": pred,
                        "inactive_target": ctx.topo.ids[v],
                        "candidate_active_heads": [ctx.topo.ids[h] for h in heads],
                    }
                )
            elif heads[0] != u:
                repointed += _plan_single_bypass_edge(ctx, u, v, heads[0], pred)
    return repointed, skipped_historical, skipped_ambiguous


def _plan_explicit_links(ctx: _PlanCtx, links: list[dict[str, str]]) -> tuple[int, int, set[str]]:
    added = 0
    already_linked = 0
    linked_ids: set[str] = set()
    for link in links:
        src, tgt, rel_name = link["from"], link["target"], link["relation"]
        src_idx, tgt_idx = ctx.topo.id_to_idx.get(src), ctx.topo.id_to_idx.get(tgt)
        if src_idx is None or not ctx.topo.active[src_idx]:
            raise ValueError(f"link source must exist and be active: {src}")
        if tgt_idx is None or not ctx.topo.active[tgt_idx]:
            raise ValueError(f"link target must exist and be active: {tgt}")
        linked_ids.update((src, tgt))
        patch, rec = ctx.patch(src)
        existing_pairs = {
            (str(r.get("relation", "")), str(r.get("target", "")))
            for r in rec.get("relations", []) + patch.add_relations
        }
        if (rel_name, tgt) in existing_pairs:
            already_linked += 1
            continue
        new_rel: dict[str, Any] = {"relation": rel_name, "target": tgt}
        if "warrant" in link:
            new_rel["warrant"] = link["warrant"]
        _append_unique_rel(patch.add_relations, new_rel)
        ctx.actions.append(
            {
                "action": "link_island",
                "record_id": src,
                "relation": rel_name,
                "target": tgt,
                "added_relation": copy.deepcopy(new_rel),
            }
        )
        added += 1
    return added, already_linked, linked_ids


def _plan_stranded_question_retirements(
    ctx: _PlanCtx,
    reachable: set[int],
    undirected: list[set[int]],
    protected_ids: frozenset[str],
    linked_ids: set[str],
) -> int:
    retired = 0
    for idx in range(ctx.topo.n):
        if (
            not ctx.topo.active[idx]
            or idx in reachable
            or ctx.topo.kinds[idx] not in RETIRE_ALLOWED_KINDS
        ):
            continue
        rid = ctx.topo.ids[idx]
        if rid in protected_ids or rid in linked_ids or undirected[idx]:
            continue
        retired_parents = sorted(
            {ctx.topo.ids[s] for s, _ in ctx.topo.in_edges[idx] if not ctx.topo.active[s]}
        )
        if not retired_parents:
            continue
        patch, _ = ctx.patch(rid)
        patch.set_active = False
        ctx.actions.append(
            {
                "action": "retire_stranded_question_stub",
                "record_id": rid,
                "kind": ctx.topo.kinds[idx],
                "retired_parents": retired_parents,
                "note": (
                    "Soft-retired isolated question stub whose only incoming references come "
                    "from inactive parent records (zero content deleted; reversible via receipt)."
                ),
            }
        )
        retired += 1
    return retired


def _plan_retired_prefix_question_retirements(
    ctx: _PlanCtx,
    reachable: set[int],
    undirected: list[set[int]],
    protected_ids: frozenset[str],
    linked_ids: set[str],
) -> int:
    topo = ctx.topo
    inactive_parents = [
        topo.ids[i]
        for i in range(topo.n)
        if not topo.active[i] and topo.kinds[i] not in RETIRE_ALLOWED_KINDS
    ]
    active_parents = [
        topo.ids[i]
        for i in range(topo.n)
        if topo.active[i] and topo.kinds[i] not in RETIRE_ALLOWED_KINDS
    ]
    retired = 0
    for idx in range(topo.n):
        if not topo.active[idx] or idx in reachable or undirected[idx]:
            continue
        if topo.kinds[idx] not in RETIRE_ALLOWED_KINDS:
            continue
        rid = topo.ids[idx]
        if rid in protected_ids or rid in linked_ids or "-question-" not in rid:
            continue
        if rid in ctx.patches and ctx.patches[rid].set_active is False:
            continue
        study_prefix = rid.split("-question-", 1)[0] + "-"
        matched_retired = sorted(m for m in inactive_parents if m.startswith(study_prefix))
        if not matched_retired or any(m.startswith(study_prefix) for m in active_parents):
            continue
        rec = ctx.show(rid)
        content = rec.get("content")
        if type(rec.get("revision")) is not int or rec["revision"] != 1:
            continue
        if not isinstance(content, dict) or content.get("selection") != "open_conditional":
            continue
        patch, _ = ctx.patch(rid)
        patch.set_active = False
        ctx.actions.append(
            {
                "action": "retire_retired_prefix_question_stub",
                "record_id": rid,
                "kind": topo.kinds[idx],
                "study_prefix": study_prefix.rstrip("-"),
                "retired_parents": matched_retired,
                "note": (
                    "Soft-retired isolated rev=1 open_conditional question stub whose parent "
                    "prefix records are inactive (zero content deleted; reversible via receipt)."
                ),
            }
        )
        retired += 1
    return retired


def _resolve_hub_indices(topo: Topology, hub_ids: list[str]) -> list[int]:
    if hub_ids:
        hub_idxs: list[int] = []
        for hid in hub_ids:
            idx = topo.id_to_idx.get(hid)
            if idx is None or not topo.active[idx]:
                raise ValueError(f"hub record must exist and be active: {hid}")
            if idx not in hub_idxs:
                hub_idxs.append(idx)
        return hub_idxs
    return [
        u
        for u in range(topo.n)
        if topo.active[u]
        and any(topo.active[v] and pred == "tracks-topic-map" for v, pred in topo.out_edges[u])
    ]


def _plan_redundant_hub_delegations(
    ctx: _PlanCtx, undirected: list[set[int]], hub_ids: list[str]
) -> int:
    topo = ctx.topo
    working_undirected = [set(neighbors) for neighbors in undirected]
    delegated = 0
    for h_idx in sorted(_resolve_hub_indices(topo, hub_ids), key=lambda i: topo.ids[i]):
        map_idxs = {
            v for v, pred in topo.out_edges[h_idx] if topo.active[v] and pred in HUB_MAP_PREDICATES
        }
        if not map_idxs:
            continue
        covered_by_maps = (
            set().union(*(working_undirected[m] for m in map_idxs)) - map_idxs - {h_idx}
        )
        if not covered_by_maps:
            continue
        hid = topo.ids[h_idx]
        for rel in ctx.show(hid).get("relations", []):
            pred, target = str(rel.get("relation", "")), str(rel.get("target", ""))
            t_idx = topo.id_to_idx.get(target)
            if (
                pred not in HUB_DELEGATABLE_PREDICATES
                or t_idx is None
                or t_idx not in covered_by_maps
            ):
                continue
            covering_maps = sorted(topo.ids[m] for m in map_idxs if t_idx in working_undirected[m])
            if not covering_maps:
                continue
            patch, _ = ctx.patch(hid)
            _append_unique_rel(patch.remove_relations, rel)
            working_undirected[h_idx].discard(t_idx)
            working_undirected[t_idx].discard(h_idx)
            ctx.actions.append(
                {
                    "action": "delegate_redundant_hub_edge",
                    "record_id": hid,
                    "relation": pred,
                    "target": target,
                    "covered_by_maps": covering_maps,
                    "removed_relation": copy.deepcopy(rel),
                }
            )
            delegated += 1
    return delegated


def build_batch_operations(patches: dict[str, RecordPatch]) -> list[dict[str, Any]]:
    """Convert accumulated record patches into deterministic put_batch patch operations."""
    operations: list[dict[str, Any]] = []
    for rid in sorted(patches):
        patch = patches[rid]
        op: dict[str, Any] = {
            "id": rid,
            "mode": "patch",
            "expected_revision": patch.before_revision,
            "expected_sha256": patch.before_sha256,
        }
        if patch.remove_relations:
            op["remove_relations"] = patch.remove_relations
        if patch.add_relations:
            op["add_relations"] = patch.add_relations
        if patch.set_active is not None:
            op["set_active"] = patch.set_active
        if len(op) > 4:
            operations.append(op)
    return operations


def plan_repairs(
    system: Any,
    topo: Topology,
    *,
    explicit_roots: list[str],
    root_kinds: frozenset[str],
    bypass_exempt_predicates: frozenset[str],
    remove_self_loops: bool,
    repoint_actionable_bypasses: bool,
    retire_stranded_questions: bool,
    retire_retired_prefix_questions: bool,
    delegate_redundant_hub_edges: bool,
    hub_ids: list[str],
    links: list[dict[str, str]],
) -> tuple[dict[str, RecordPatch], dict[str, Any]]:
    """Compute dry-run or pre-commit repair patches and summary metrics."""
    reachable, undirected = _compute_active_reachability(
        topo, explicit_roots, root_kinds=root_kinds
    )
    protected = frozenset(getattr(system.domain_selection, "protected_record_ids", frozenset()))
    ctx = _PlanCtx(system=system, topo=topo, patches={}, rec_cache={}, actions=[])

    self_loops_removed = _plan_self_loops(ctx) if remove_self_loops else 0
    bypasses_repointed, skipped_historical, skipped_ambiguous = (
        _plan_actionable_bypasses(ctx, bypass_exempt_predicates=bypass_exempt_predicates)
        if repoint_actionable_bypasses
        else (0, 0, [])
    )
    links_added, already_linked, linked_ids = _plan_explicit_links(ctx, links)
    stranded_retired = (
        _plan_stranded_question_retirements(ctx, reachable, undirected, protected, linked_ids)
        if retire_stranded_questions
        else 0
    )
    retired_prefix_questions = (
        _plan_retired_prefix_question_retirements(ctx, reachable, undirected, protected, linked_ids)
        if retire_retired_prefix_questions
        else 0
    )
    hub_edges_delegated = (
        _plan_redundant_hub_delegations(ctx, undirected, hub_ids)
        if delegate_redundant_hub_edges
        else 0
    )
    summary = {
        "indexed": topo.indexed,
        "total_nodes": topo.n,
        "active_nodes_before": sum(1 for flag in topo.active if flag),
        "self_loops_removed": self_loops_removed,
        "bypasses_repointed": bypasses_repointed,
        "bypasses_skipped_historical": skipped_historical,
        "bypasses_skipped_ambiguous_count": len(skipped_ambiguous),
        "bypasses_skipped_ambiguous": skipped_ambiguous[:25],
        "stranded_questions_retired": stranded_retired,
        "retired_prefix_questions_retired": retired_prefix_questions,
        "hub_edges_delegated": hub_edges_delegated,
        "explicit_links_added": links_added,
        "explicit_links_already_present": already_linked,
        "records_to_patch": len(build_batch_operations(ctx.patches)),
        "actions": ctx.actions,
    }
    return ctx.patches, summary
