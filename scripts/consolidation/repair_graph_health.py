#!/usr/bin/env python3
"""Dry-run-by-default mechanical graph-health repair CLI (zero data loss, zero sealed reads)."""

import argparse
import copy
import hashlib
import json
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
for _entry in (str(SRC_ROOT), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai_information.core.formats import BATCH_FORMAT  # noqa: E402
from managed_ai_information.core.ownership import packet_id  # noqa: E402
from managed_ai_information.core.storage import (  # noqa: E402
    MAX_BYTES,
    encode,
    exclusive,
    now,
    read_bytes,
)
from managed_ai_information.platform import (  # noqa: E402
    atomic_bytes,
    check_ordinary_ancestors,
    is_ordinary_file_stat,
    strict_json_loads,
)
from managed_ai_information.store.protocol.inputs import strings  # noqa: E402
from managed_ai_information.store.query import graph_query  # noqa: E402
from managed_ai_information.store.query.graph_profile import (  # noqa: E402
    BYPASS_EXEMPT_PREDICATES,
)
from managed_ai_information.store.query.graph_profile_metrics import (  # noqa: E402
    DEFAULT_ROOT_KINDS,
)
from managed_ai_information.store.records.record_batch import put_batch  # noqa: E402
from managed_ai_information.store.system import System  # noqa: E402

REPAIR_PLAN_FORMAT = "managed-ai-graph-health-repair-plan/1"
REPAIR_RECEIPT_FORMAT = "managed-ai-graph-health-repair-receipt/1"
LINK_SPEC_FORMAT = "managed-ai-graph-health-links/1"

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
DEFAULT_ACCOUNT = "repair_graph_health: mechanical zero-loss graph-health maintenance"


@dataclass(slots=True)
class _Topology:
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
class _RecordPatch:
    record_id: str
    before_revision: int
    before_sha256: str
    previous_active: bool
    remove_relations: list[dict[str, Any]]
    add_relations: list[dict[str, Any]]
    set_active: bool | None = None


def _dump_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)


def _open_store(store_root: Path) -> System:
    checked = check_ordinary_ancestors(
        store_root, expect_dir=True, error_prefix="ordinary store root required"
    ).resolve(strict=True)
    return System(checked)


def _read_ordinary_json(path: Path, *, label: str) -> dict[str, Any]:
    checked = check_ordinary_ancestors(
        path, expect_dir=False, error_prefix=f"ordinary {label} file required"
    )
    info = checked.lstat()
    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary {label} file required: {checked}")
    parsed = strict_json_loads(read_bytes(checked, limit=MAX_BYTES), checked, require_object=True)
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object: {checked}")
    return parsed


def _lint_anomaly_count(lint_report: dict[str, Any]) -> int:
    summary = lint_report.get("summary", {})
    if isinstance(summary, dict):
        return sum(int(val) for val in summary.values())
    return int(lint_report.get("anomaly_count", 0))


def _validate_ordinary_output_path(path: Path, *, label: str) -> Path:
    parent = path.parent
    probe = parent
    while not probe.exists() and not probe.is_symlink() and probe != probe.parent:
        probe = probe.parent
    check_ordinary_ancestors(
        probe, expect_dir=True, error_prefix=f"ordinary {label} directory required"
    )
    if parent.exists() or parent.is_symlink():
        checked_parent = check_ordinary_ancestors(
            parent, expect_dir=True, error_prefix=f"ordinary {label} directory required"
        )
        dest = checked_parent / path.name
        try:
            info = dest.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and not is_ordinary_file_stat(info):
            raise ValueError(f"ordinary {label} file required: {dest}")
    return path


def _write_ordinary_json(path: Path, payload: dict[str, Any], *, label: str) -> Path:
    _validate_ordinary_output_path(path, label=label)
    path.parent.mkdir(parents=True, exist_ok=True)
    checked_dir = check_ordinary_ancestors(
        path.parent, expect_dir=True, error_prefix=f"ordinary {label} directory required"
    )
    dest = checked_dir / path.name
    try:
        info = dest.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary {label} file required: {dest}")
    atomic_bytes(dest, encode(payload))
    return dest


def _load_topology(system: System) -> _Topology:
    """Load graph topology from HeadIndex v3 or scan_records with zero sealed D12 reads."""
    store = system.record_store
    verified = store.open_verified_index_view()
    if verified is not None:
        with verified:
            if verified.is_exact_base:
                return _topology_from_mmap(verified.view)
            nodes = verified.all_entries()
        return _topology_from_nodes(nodes, indexed=True)
    scanned: dict[str, dict[str, Any]] = {}
    for entry in store.scan_records():
        scanned[str(entry["id"])] = entry
    return _topology_from_nodes(scanned, indexed=False)


def _topology_from_mmap(view: Any) -> _Topology:
    n = view.record_count
    rel_labels = view.rel_labels()
    ids = [view.slot_id(s) for s in range(n)]
    id_to_idx = {ident: s for s, ident in enumerate(ids)}
    kinds = [view.slot_kind_name(s) for s in range(n)]
    active = [view.slot_active(s) for s in range(n)]
    out_edges: list[list[tuple[int, str]]] = [[] for _ in range(n)]
    in_edges: list[list[tuple[int, str]]] = [[] for _ in range(n)]
    self_loop_nodes: set[int] = set()
    for slot in range(n):
        for rid, t_slot, _t_off, _t_len in view.fwd_edges_raw(slot):
            if t_slot == slot:
                self_loop_nodes.add(slot)
            elif t_slot >= 0:
                label = rel_labels[rid]
                out_edges[slot].append((t_slot, label))
                in_edges[t_slot].append((slot, label))
    return _Topology(n, ids, id_to_idx, kinds, active, out_edges, in_edges, self_loop_nodes, True)


def _node_relations(node: dict[str, Any]) -> list[tuple[str, str]]:
    graph = node.get("graph")
    if isinstance(graph, dict) and "relations" in graph:
        return [(str(label), str(target)) for label, target in graph.get("relations", ())]
    rels: list[tuple[str, str]] = []
    for item in node.get("relations", ()):
        if isinstance(item, dict) and "relation" in item and "target" in item:
            rels.append((str(item["relation"]), str(item["target"])))
    return rels


def _topology_from_nodes(nodes: dict[str, dict[str, Any]], *, indexed: bool) -> _Topology:
    ids = sorted(nodes)
    n = len(ids)
    id_to_idx = {ident: idx for idx, ident in enumerate(ids)}
    kinds = [str(nodes[ident]["kind"]) for ident in ids]
    active = [bool(nodes[ident]["active"]) for ident in ids]
    out_edges: list[list[tuple[int, str]]] = [[] for _ in range(n)]
    in_edges: list[list[tuple[int, str]]] = [[] for _ in range(n)]
    self_loop_nodes: set[int] = set()
    for idx, ident in enumerate(ids):
        for label, target in _node_relations(nodes[ident]):
            t_idx = id_to_idx.get(target)
            if t_idx is None:
                continue
            if t_idx == idx:
                self_loop_nodes.add(idx)
            else:
                out_edges[idx].append((t_idx, label))
                in_edges[t_idx].append((idx, label))
    return _Topology(
        n, ids, id_to_idx, kinds, active, out_edges, in_edges, self_loop_nodes, indexed
    )


def _compute_active_reachability(
    topo: _Topology, explicit_roots: list[str]
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
        root_idxs = [i for i in active_indices if topo.kinds[i] in DEFAULT_ROOT_KINDS]
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


def _resolve_active_successor(topo: _Topology, start_idx: int) -> tuple[str, list[int]]:
    """Follow consolidated-into (out) / supersedes (in) from an inactive node with cycle guard."""
    visited: set[int] = {start_idx}
    queue: deque[int] = deque([start_idx])
    active_heads: set[int] = set()
    while queue:
        cur = queue.popleft()
        candidates: list[int] = [
            v for v, pred in topo.out_edges[cur] if pred in SUCCESSOR_OUT_PREDICATES
        ]
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


def _load_link_spec(link_file: Path) -> list[dict[str, str]]:
    doc = _read_ordinary_json(link_file, label="link spec")
    if doc.get("format") != LINK_SPEC_FORMAT:
        raise ValueError(f"unsupported link spec format: {doc.get('format')!r}")
    raw_links = doc.get("links")
    if not isinstance(raw_links, list):
        raise ValueError("link spec requires a 'links' list")
    parsed: list[dict[str, str]] = []
    for idx, row in enumerate(raw_links):
        if not isinstance(row, dict):
            raise ValueError(f"link spec entry #{idx} must be an object")
        extra = set(row) - {"from", "relation", "target", "warrant"}
        if extra:
            raise ValueError(f"unexpected link spec keys at #{idx}: {sorted(extra)}")
        src = packet_id(str(row.get("from", "")))
        tgt = packet_id(str(row.get("target", "")))
        rel = str(row.get("relation", "")).strip()
        if not rel:
            raise ValueError(f"link spec entry #{idx} requires nonempty 'relation'")
        if src == tgt:
            raise ValueError(f"link spec entry #{idx} must not create a self-loop on {src}")
        entry = {"from": src, "relation": rel, "target": tgt}
        if "warrant" in row:
            warrant = str(row["warrant"]).strip()
            if not warrant:
                raise ValueError(f"link spec entry #{idx} warrant must be nonempty if supplied")
            entry["warrant"] = warrant
        parsed.append(entry)
    return parsed


def _get_or_create_patch(
    system: System, patches: dict[str, _RecordPatch], rec_cache: dict[str, dict[str, Any]], rid: str
) -> tuple[_RecordPatch, dict[str, Any]]:
    rec = rec_cache.get(rid)
    if rec is None:
        rec = system.record_store.show(rid)
        rec_cache[rid] = rec
    patch = patches.get(rid)
    if patch is None:
        patch = _RecordPatch(
            record_id=rid,
            before_revision=int(rec["revision"]),
            before_sha256=str(rec["sha256"]),
            previous_active=bool(rec["active"]),
            remove_relations=[],
            add_relations=[],
            set_active=None,
        )
        patches[rid] = patch
    return patch, rec


def _append_unique_rel(bucket: list[dict[str, Any]], rel: dict[str, Any]) -> None:
    encoded = encode(rel)
    if all(encode(item) != encoded for item in bucket):
        bucket.append(copy.deepcopy(rel))


def _plan_self_loops(
    system: System,
    topo: _Topology,
    patches: dict[str, _RecordPatch],
    rec_cache: dict[str, dict[str, Any]],
    actions: list[dict[str, Any]],
) -> int:
    count = 0
    for idx in sorted(topo.self_loop_nodes, key=lambda i: topo.ids[i]):
        if not topo.active[idx]:
            continue
        rid = topo.ids[idx]
        patch, rec = _get_or_create_patch(system, patches, rec_cache, rid)
        for rel in rec.get("relations", []):
            if rel.get("target") == rid:
                _append_unique_rel(patch.remove_relations, rel)
                actions.append(
                    {
                        "action": "remove_self_loop",
                        "record_id": rid,
                        "relation": str(rel.get("relation", "")),
                        "removed_relation": copy.deepcopy(rel),
                    }
                )
                count += 1
    return count


def _plan_actionable_bypasses(
    system: System,
    topo: _Topology,
    patches: dict[str, _RecordPatch],
    rec_cache: dict[str, dict[str, Any]],
    actions: list[dict[str, Any]],
) -> tuple[int, int, list[dict[str, Any]]]:
    repointed = 0
    skipped_historical = 0
    skipped_ambiguous: list[dict[str, Any]] = []
    cache_succ: dict[int, tuple[str, list[int]]] = {}

    for u in range(topo.n):
        if not topo.active[u]:
            continue
        rid = topo.ids[u]
        seen_edges: set[tuple[int, str]] = set()
        for v, pred in topo.out_edges[u]:
            if topo.active[v] or pred in BYPASS_EXEMPT_PREDICATES or (v, pred) in seen_edges:
                continue
            seen_edges.add((v, pred))
            status, heads = cache_succ.get(v) or _resolve_active_successor(topo, v)
            cache_succ[v] = (status, heads)
            if status == "none":
                skipped_historical += 1
                continue
            if status == "ambiguous":
                skipped_ambiguous.append(
                    {
                        "from": rid,
                        "relation": pred,
                        "inactive_target": topo.ids[v],
                        "candidate_active_heads": [topo.ids[h] for h in heads],
                    }
                )
                continue
            active_idx = heads[0]
            if active_idx == u:
                continue
            repointed += _plan_single_bypass_edge(
                system, topo, u, v, active_idx, pred, patches, rec_cache, actions
            )
    return repointed, skipped_historical, skipped_ambiguous


def _plan_single_bypass_edge(
    system: System,
    topo: _Topology,
    u: int,
    v: int,
    active_idx: int,
    pred: str,
    patches: dict[str, _RecordPatch],
    rec_cache: dict[str, dict[str, Any]],
    actions: list[dict[str, Any]],
) -> int:
    rid = topo.ids[u]
    old_target = topo.ids[v]
    new_target = topo.ids[active_idx]
    patch, rec = _get_or_create_patch(system, patches, rec_cache, rid)
    target_rec = rec_cache.get(new_target)
    if target_rec is None:
        target_rec = system.record_store.show(new_target)
        rec_cache[new_target] = target_rec

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
        if (pred, new_target) in existing_pairs:
            actions.append(
                {
                    "action": "dedup_repointed_bypass",
                    "record_id": rid,
                    "relation": pred,
                    "prior_target": old_target,
                    "active_target": new_target,
                    "removed_relation": copy.deepcopy(rel),
                }
            )
        else:
            updated = copy.deepcopy(rel)
            updated["target"] = new_target
            if "revision" in updated:
                updated["revision"] = int(target_rec["revision"])
            if "sha256" in updated:
                updated["sha256"] = str(target_rec["sha256"])
            _append_unique_rel(patch.add_relations, updated)
            existing_pairs.add((pred, new_target))
            actions.append(
                {
                    "action": "repoint_bypass",
                    "record_id": rid,
                    "relation": pred,
                    "prior_target": old_target,
                    "active_target": new_target,
                    "removed_relation": copy.deepcopy(rel),
                    "added_relation": copy.deepcopy(updated),
                }
            )
        changed += 1
    return changed


def _plan_explicit_links(
    system: System,
    topo: _Topology,
    links: list[dict[str, str]],
    patches: dict[str, _RecordPatch],
    rec_cache: dict[str, dict[str, Any]],
    actions: list[dict[str, Any]],
) -> tuple[int, int, set[str]]:
    added = 0
    already_linked = 0
    linked_ids: set[str] = set()
    for link in links:
        src = link["from"]
        tgt = link["target"]
        rel_name = link["relation"]
        src_idx = topo.id_to_idx.get(src)
        tgt_idx = topo.id_to_idx.get(tgt)
        if src_idx is None or not topo.active[src_idx]:
            raise ValueError(f"link source must exist and be active: {src}")
        if tgt_idx is None or not topo.active[tgt_idx]:
            raise ValueError(f"link target must exist and be active: {tgt}")
        linked_ids.add(src)
        linked_ids.add(tgt)
        patch, rec = _get_or_create_patch(system, patches, rec_cache, src)
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
        actions.append(
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
    system: System,
    topo: _Topology,
    reachable: set[int],
    undirected: list[set[int]],
    protected_ids: frozenset[str],
    linked_ids: set[str],
    patches: dict[str, _RecordPatch],
    rec_cache: dict[str, dict[str, Any]],
    actions: list[dict[str, Any]],
) -> int:
    retired = 0
    for idx in range(topo.n):
        if not topo.active[idx] or idx in reachable:
            continue
        if topo.kinds[idx] not in RETIRE_ALLOWED_KINDS:
            continue
        rid = topo.ids[idx]
        if rid in protected_ids or rid in linked_ids or undirected[idx]:
            continue
        retired_parents = sorted({topo.ids[s] for s, _ in topo.in_edges[idx] if not topo.active[s]})
        if not retired_parents:
            continue
        patch, _ = _get_or_create_patch(system, patches, rec_cache, rid)
        patch.set_active = False
        actions.append(
            {
                "action": "retire_stranded_question_stub",
                "record_id": rid,
                "kind": topo.kinds[idx],
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
    system: System,
    topo: _Topology,
    reachable: set[int],
    undirected: list[set[int]],
    protected_ids: frozenset[str],
    linked_ids: set[str],
    patches: dict[str, _RecordPatch],
    rec_cache: dict[str, dict[str, Any]],
    actions: list[dict[str, Any]],
) -> int:
    inactive_issues = [
        topo.ids[i]
        for i in range(topo.n)
        if not topo.active[i] and topo.kinds[i] == "research-issue"
    ]
    active_issues = [
        topo.ids[i] for i in range(topo.n) if topo.active[i] and topo.kinds[i] == "research-issue"
    ]
    retired = 0
    for idx in range(topo.n):
        if not topo.active[idx] or idx in reachable or undirected[idx]:
            continue
        if topo.kinds[idx] not in RETIRE_ALLOWED_KINDS:
            continue
        rid = topo.ids[idx]
        if rid in protected_ids or rid in linked_ids:
            continue
        if rid in patches and patches[rid].set_active is False:
            continue
        if not rid.startswith("g60-named-") or "-question-" not in rid:
            continue
        study_prefix = rid.split("-question-", 1)[0] + "-"
        matched_retired = sorted(m for m in inactive_issues if m.startswith(study_prefix))
        if not matched_retired or any(m.startswith(study_prefix) for m in active_issues):
            continue
        rec = rec_cache.get(rid)
        if rec is None:
            rec = system.record_store.show(rid)
            rec_cache[rid] = rec
        content = rec.get("content")
        if int(rec["revision"]) != 1 or not isinstance(content, dict):
            continue
        if content.get("selection") != "open_conditional":
            continue
        patch, _ = _get_or_create_patch(system, patches, rec_cache, rid)
        patch.set_active = False
        actions.append(
            {
                "action": "retire_retired_prefix_question_stub",
                "record_id": rid,
                "kind": topo.kinds[idx],
                "study_prefix": study_prefix.rstrip("-"),
                "retired_issues": matched_retired,
                "note": (
                    "Soft-retired isolated rev=1 open_conditional question stub whose parent "
                    "named study research-issue is already inactive (zero content deleted)."
                ),
            }
        )
        retired += 1
    return retired


def _plan_redundant_hub_delegations(
    system: System,
    topo: _Topology,
    undirected: list[set[int]],
    hub_ids: list[str],
    patches: dict[str, _RecordPatch],
    rec_cache: dict[str, dict[str, Any]],
    actions: list[dict[str, Any]],
) -> int:
    if hub_ids:
        hub_idxs: list[int] = []
        for hid in hub_ids:
            idx = topo.id_to_idx.get(hid)
            if idx is None or not topo.active[idx]:
                raise ValueError(f"hub record must exist and be active: {hid}")
            if idx not in hub_idxs:
                hub_idxs.append(idx)
    else:
        hub_idxs = [
            u
            for u in range(topo.n)
            if topo.active[u]
            and any(topo.active[v] and pred == "tracks-topic-map" for v, pred in topo.out_edges[u])
        ]

    working_undirected = [set(neighbors) for neighbors in undirected]
    delegated = 0
    for h_idx in sorted(hub_idxs, key=lambda i: topo.ids[i]):
        map_idxs = {
            v for v, pred in topo.out_edges[h_idx] if topo.active[v] and pred in HUB_MAP_PREDICATES
        }
        if not map_idxs:
            continue
        covered_by_maps: set[int] = set()
        for m_idx in map_idxs:
            covered_by_maps.update(working_undirected[m_idx])
        covered_by_maps.discard(h_idx)
        covered_by_maps.difference_update(map_idxs)
        if not covered_by_maps:
            continue

        hid = topo.ids[h_idx]
        rec = rec_cache.get(hid)
        if rec is None:
            rec = system.record_store.show(hid)
            rec_cache[hid] = rec
        for rel in rec.get("relations", []):
            pred = str(rel.get("relation", ""))
            target = str(rel.get("target", ""))
            if pred not in HUB_DELEGATABLE_PREDICATES:
                continue
            t_idx = topo.id_to_idx.get(target)
            if t_idx is None or t_idx not in covered_by_maps:
                continue
            covering_maps = sorted(topo.ids[m] for m in map_idxs if t_idx in working_undirected[m])
            if not covering_maps:
                continue
            patch, _ = _get_or_create_patch(system, patches, rec_cache, hid)
            _append_unique_rel(patch.remove_relations, rel)
            working_undirected[h_idx].discard(t_idx)
            working_undirected[t_idx].discard(h_idx)
            actions.append(
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


def _build_batch_operations(patches: dict[str, _RecordPatch]) -> list[dict[str, Any]]:
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


def _plan_repairs(
    system: System,
    *,
    explicit_roots: list[str],
    remove_self_loops: bool,
    repoint_actionable_bypasses: bool,
    retire_stranded_questions: bool,
    retire_retired_prefix_questions: bool,
    delegate_redundant_hub_edges: bool,
    hub_ids: list[str],
    links: list[dict[str, str]],
) -> tuple[dict[str, _RecordPatch], dict[str, Any]]:
    topo = _load_topology(system)
    reachable, undirected = _compute_active_reachability(topo, explicit_roots)
    protected = frozenset(getattr(system.domain_selection, "protected_record_ids", frozenset()))
    patches: dict[str, _RecordPatch] = {}
    rec_cache: dict[str, dict[str, Any]] = {}
    actions: list[dict[str, Any]] = []

    self_loops_removed = (
        _plan_self_loops(system, topo, patches, rec_cache, actions) if remove_self_loops else 0
    )
    bypasses_repointed = 0
    skipped_historical = 0
    skipped_ambiguous: list[dict[str, Any]] = []
    if repoint_actionable_bypasses:
        bypasses_repointed, skipped_historical, skipped_ambiguous = _plan_actionable_bypasses(
            system, topo, patches, rec_cache, actions
        )
    links_added, already_linked, linked_ids = _plan_explicit_links(
        system, topo, links, patches, rec_cache, actions
    )
    stranded_retired = (
        _plan_stranded_question_retirements(
            system,
            topo,
            reachable,
            undirected,
            protected,
            linked_ids,
            patches,
            rec_cache,
            actions,
        )
        if retire_stranded_questions
        else 0
    )
    retired_prefix_questions = (
        _plan_retired_prefix_question_retirements(
            system,
            topo,
            reachable,
            undirected,
            protected,
            linked_ids,
            patches,
            rec_cache,
            actions,
        )
        if retire_retired_prefix_questions
        else 0
    )
    hub_edges_delegated = (
        _plan_redundant_hub_delegations(
            system,
            topo,
            undirected,
            hub_ids,
            patches,
            rec_cache,
            actions,
        )
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
        "records_to_patch": len(_build_batch_operations(patches)),
        "actions": actions,
    }
    return patches, summary


def repair_graph_health(
    store_root: Path,
    *,
    apply: bool = False,
    receipt_path: Path | None = None,
    link_file: Path | None = None,
    roots: list[str] | tuple[str, ...] = (),
    remove_self_loops: bool = True,
    repoint_actionable_bypasses: bool = True,
    retire_stranded_questions: bool = True,
    retire_retired_prefix_questions: bool = False,
    delegate_redundant_hub_edges: bool = False,
    hub_ids: list[str] | tuple[str, ...] = (),
    account: str = DEFAULT_ACCOUNT,
) -> dict[str, Any]:
    """Plan (dry-run default) or atomically apply zero-loss mechanical graph-health repairs."""
    system = _open_store(store_root)
    explicit_roots = strings(list(roots), "roots") if roots else []
    explicit_hubs = strings(list(hub_ids), "hub_ids") if hub_ids else []
    links = _load_link_spec(link_file) if link_file is not None else []

    if not apply:
        patches, summary = _plan_repairs(
            system,
            explicit_roots=explicit_roots,
            remove_self_loops=remove_self_loops,
            repoint_actionable_bypasses=repoint_actionable_bypasses,
            retire_stranded_questions=retire_stranded_questions,
            retire_retired_prefix_questions=retire_retired_prefix_questions,
            delegate_redundant_hub_edges=delegate_redundant_hub_edges,
            hub_ids=explicit_hubs,
            links=links,
        )
        operations = _build_batch_operations(patches)
        return {
            "format": REPAIR_PLAN_FORMAT,
            "instance_id": system.instance_id,
            "applied": False,
            "dry_run": True,
            "written_count": 0,
            "batch_id": None,
            "receipt_path": None,
            "lint_anomalies": None,
            "operation_ids": [op["id"] for op in operations],
            **summary,
        }

    with exclusive(system.ex.root / "owner.lock"):
        patches, summary = _plan_repairs(
            system,
            explicit_roots=explicit_roots,
            remove_self_loops=remove_self_loops,
            repoint_actionable_bypasses=repoint_actionable_bypasses,
            retire_stranded_questions=retire_stranded_questions,
            retire_retired_prefix_questions=retire_retired_prefix_questions,
            delegate_redundant_hub_edges=delegate_redundant_hub_edges,
            hub_ids=explicit_hubs,
            links=links,
        )
        operations = _build_batch_operations(patches)
        if not operations:
            return {
                "format": REPAIR_PLAN_FORMAT,
                "instance_id": system.instance_id,
                "applied": False,
                "dry_run": False,
                "written_count": 0,
                "batch_id": None,
                "receipt_path": None,
                "lint_anomalies": None,
                "operation_ids": [],
                **summary,
            }

        batch_hash = hashlib.sha256(encode(operations)).hexdigest()[:12]
        batch_id = packet_id(f"graph-health-repair-{batch_hash}")
        dest_receipt = (
            receipt_path
            if receipt_path is not None
            else system.work / "repairs" / f"{batch_id}.json"
        )
        _validate_ordinary_output_path(dest_receipt, label="repair receipt")
        spec = {
            "format": BATCH_FORMAT,
            "id": batch_id,
            "instance_id": system.instance_id,
            "pin_predecessors": True,
            "account": account,
            "operations": operations,
        }
        batch_receipt = put_batch(system, spec)
        system.reindex()
        lint_report = graph_query.lint(system)
        lint_anomalies = _lint_anomaly_count(lint_report)

        rows_by_id = {str(r["id"]): r for r in batch_receipt["records"]}
        receipt_records: list[dict[str, Any]] = []
        for op in operations:
            rid = str(op["id"])
            patch = patches[rid]
            row = rows_by_id[rid]
            receipt_records.append(
                {
                    "id": rid,
                    "before_revision": int(row["before_revision"]),
                    "before_sha256": str(row["before_sha256"]),
                    "after_revision": int(row["after_revision"]),
                    "after_sha256": str(row["after_sha256"]),
                    "pinned_object": row["pinned_object"],
                    "removed_relations": patch.remove_relations,
                    "added_relations": patch.add_relations,
                    "previous_active": patch.previous_active,
                    "new_active": (
                        patch.previous_active if patch.set_active is None else patch.set_active
                    ),
                }
            )

        receipt_payload: dict[str, Any] = {
            "format": REPAIR_RECEIPT_FORMAT,
            "instance_id": system.instance_id,
            "batch_id": batch_id,
            "recorded_at": now(),
            "account": account,
            "records": receipt_records,
            "actions": summary["actions"],
            "counts": {
                "written_count": len(operations),
                "self_loops_removed": summary["self_loops_removed"],
                "bypasses_repointed": summary["bypasses_repointed"],
                "stranded_questions_retired": summary["stranded_questions_retired"],
                "retired_prefix_questions_retired": summary["retired_prefix_questions_retired"],
                "hub_edges_delegated": summary["hub_edges_delegated"],
                "explicit_links_added": summary["explicit_links_added"],
                "lint_anomalies": lint_anomalies,
            },
        }
        written_receipt = _write_ordinary_json(
            dest_receipt, receipt_payload, label="repair receipt"
        )

    return {
        "format": REPAIR_RECEIPT_FORMAT,
        "instance_id": system.instance_id,
        "applied": True,
        "dry_run": False,
        "written_count": len(operations),
        "batch_id": batch_id,
        "receipt_path": str(written_receipt),
        "lint_anomalies": lint_anomalies,
        "lint_summary": lint_report.get("summary", {}),
        "operation_ids": [op["id"] for op in operations],
        **summary,
    }


def rollback_graph_repair(
    store_root: Path,
    receipt_path: Path,
    *,
    apply: bool = False,
    account: str = "repair_graph_health: rollback mechanical repair receipt",
) -> dict[str, Any]:
    """Preview or atomically invert a previously written repair receipt via put_batch."""
    system = _open_store(store_root)
    receipt = _read_ordinary_json(receipt_path, label="repair receipt")
    if receipt.get("format") != REPAIR_RECEIPT_FORMAT:
        raise ValueError(f"unsupported repair receipt format: {receipt.get('format')!r}")
    if receipt.get("instance_id") != system.instance_id:
        raise ValueError("repair receipt belongs to a different store instance")
    records = receipt.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("repair receipt contains no reversible records")

    def _build_inverse_ops() -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        for row in records:
            rid = packet_id(str(row["id"]))
            current = system.record_store.show(rid)
            if current["revision"] != int(row["after_revision"]) or current["sha256"] != str(
                row["after_sha256"]
            ):
                raise ValueError(
                    f"cannot rollback {rid}: current head rev {current['revision']} differs from "
                    f"receipt after_revision {row['after_revision']}"
                )
            op: dict[str, Any] = {
                "id": rid,
                "mode": "patch",
                "expected_revision": int(current["revision"]),
                "expected_sha256": str(current["sha256"]),
            }
            if row.get("added_relations"):
                op["remove_relations"] = list(row["added_relations"])
            if row.get("removed_relations"):
                op["add_relations"] = list(row["removed_relations"])
            if bool(row["previous_active"]) != bool(row["new_active"]):
                op["set_active"] = bool(row["previous_active"])
            if len(op) > 4:
                ops.append(op)
        return ops

    if not apply:
        ops = _build_inverse_ops()
        return {
            "format": REPAIR_PLAN_FORMAT,
            "mode": "rollback",
            "instance_id": system.instance_id,
            "applied": False,
            "dry_run": True,
            "source_batch_id": str(receipt.get("batch_id", "")),
            "written_count": 0,
            "rollback_ids": [op["id"] for op in ops],
        }

    with exclusive(system.ex.root / "owner.lock"):
        ops = _build_inverse_ops()
        batch_hash = hashlib.sha256(encode(ops)).hexdigest()[:12]
        batch_id = packet_id(f"graph-health-rollback-{batch_hash}")
        put_batch(
            system,
            {
                "format": BATCH_FORMAT,
                "id": batch_id,
                "instance_id": system.instance_id,
                "pin_predecessors": True,
                "account": account,
                "operations": ops,
            },
        )
        system.reindex()
        lint_report = graph_query.lint(system)
        lint_anomalies = _lint_anomaly_count(lint_report)

    return {
        "format": REPAIR_RECEIPT_FORMAT,
        "mode": "rollback",
        "instance_id": system.instance_id,
        "applied": True,
        "dry_run": False,
        "source_batch_id": str(receipt.get("batch_id", "")),
        "batch_id": batch_id,
        "written_count": len(ops),
        "lint_anomalies": lint_anomalies,
        "lint_summary": lint_report.get("summary", {}),
        "rollback_ids": [op["id"] for op in ops],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repair_graph_health.py",
        allow_abbrev=False,
        description=(
            "Dry-run-by-default mechanical graph-health repair CLI "
            "(self-loop dedup, unambiguous bypass re-pointing, stranded retired-parent "
            "question soft-retirement, explicit island linking, and receipt rollback)."
        ),
    )
    parser.add_argument("--store", required=True, help="Target managed-ai store root directory.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit repairs atomically under owner.lock (default is read-only dry-run).",
    )
    parser.add_argument("--receipt", default=None, help="Output path for reversible receipt JSON.")
    parser.add_argument(
        "--link-file", default=None, help="Explicit reviewed island-link JSON specification."
    )
    parser.add_argument(
        "--roots", nargs="*", default=[], help="Optional explicit active root record IDs."
    )
    parser.add_argument("--no-self-loops", action="store_true", help="Skip self-loop cleanup.")
    parser.add_argument(
        "--no-repoint-bypasses", action="store_true", help="Skip unambiguous bypass re-pointing."
    )
    parser.add_argument(
        "--no-retire-stranded-questions",
        action="store_true",
        help="Skip stranded retired-parent question soft-retirement.",
    )
    parser.add_argument(
        "--retire-retired-prefix-questions",
        action="store_true",
        help=(
            "Opt-in: soft-retire isolated rev=1 open_conditional g60-named-* question stubs "
            "whose parent named research-issue is already inactive (sparing selection='open')."
        ),
    )
    parser.add_argument(
        "--delegate-redundant-hub-edges",
        action="store_true",
        help=(
            "Opt-in: trim redundant flat tracks-* leaf edges from hubs when the target is "
            "already 1-hop reachable via one of that hub's tracked topic-maps."
        ),
    )
    parser.add_argument(
        "--hub-ids",
        nargs="*",
        default=[],
        help="Optional explicit hub IDs for --delegate-redundant-hub-edges.",
    )
    parser.add_argument(
        "--rollback",
        default=None,
        help="Path to a repair receipt JSON to preview (or invert with --apply).",
    )
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help="Batch attribution note.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    store_root = Path(args.store)
    try:
        if args.rollback:
            if args.link_file or args.receipt:
                raise ValueError("--rollback cannot be combined with --link-file or --receipt")
            result = rollback_graph_repair(
                store_root,
                Path(args.rollback),
                apply=bool(args.apply),
                account=str(args.account),
            )
        else:
            result = repair_graph_health(
                store_root,
                apply=bool(args.apply),
                receipt_path=Path(args.receipt) if args.receipt else None,
                link_file=Path(args.link_file) if args.link_file else None,
                roots=list(args.roots),
                remove_self_loops=not bool(args.no_self_loops),
                repoint_actionable_bypasses=not bool(args.no_repoint_bypasses),
                retire_stranded_questions=not bool(args.no_retire_stranded_questions),
                retire_retired_prefix_questions=bool(args.retire_retired_prefix_questions),
                delegate_redundant_hub_edges=bool(args.delegate_redundant_hub_edges),
                hub_ids=list(args.hub_ids),
                account=str(args.account),
            )
    except (OSError, ValueError) as exc:
        print(_dump_json({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(_dump_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
