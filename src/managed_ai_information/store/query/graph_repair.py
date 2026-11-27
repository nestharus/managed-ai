"""Dry-run-by-default mechanical graph-health repair and reversible receipt rollback."""

import hashlib
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from managed_ai_information.core.formats import BATCH_FORMAT
from managed_ai_information.core.storage import (
    MAX_BYTES,
    atomic_bytes,
    check_ordinary_ancestors,
    encode,
    exclusive,
    is_ordinary_file_stat,
    now,
    read_bytes,
    strict_json_loads,
)
from managed_ai_information.store.protocol.identities import packet_id
from managed_ai_information.store.protocol.inputs import strings
from managed_ai_information.store.query.graph_profile_alerts import resolve_calibration
from managed_ai_information.store.query.graph_repair_ops import (
    RecordPatch,
    Topology,
    build_batch_operations,
    plan_repairs,
)

LINK_SPEC_FORMAT = "managed-ai-graph-health-links/1"
REPAIR_PLAN_FORMAT = "managed-ai-graph-health-repair-plan/1"
REPAIR_RECEIPT_FORMAT = "managed-ai-graph-health-repair-receipt/1"
DEFAULT_ACCOUNT = "graph repair: mechanical zero-loss graph-health maintenance"
DEFAULT_ROLLBACK_ACCOUNT = "graph repair: rollback mechanical repair receipt"


def _strict_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}, got {value!r}")
    return value


def _strict_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean, got {value!r}")
    return value


def _strict_rel_list(value: Any, label: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{label} must be a list of relation objects")
    return list(value)


def _require_nonempty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value.strip()


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


def _topology_from_mmap(view: Any) -> Topology:
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
    return Topology(n, ids, id_to_idx, kinds, active, out_edges, in_edges, self_loop_nodes, True)


def _node_relations(node: dict[str, Any]) -> list[tuple[str, str]]:
    graph = node.get("graph")
    if isinstance(graph, dict) and "relations" in graph:
        return [(str(label), str(target)) for label, target in graph.get("relations", ())]
    return [
        (str(item["relation"]), str(item["target"]))
        for item in node.get("relations", ())
        if isinstance(item, dict) and "relation" in item and "target" in item
    ]


def _topology_from_nodes(nodes: dict[str, dict[str, Any]], *, indexed: bool) -> Topology:
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
    return Topology(n, ids, id_to_idx, kinds, active, out_edges, in_edges, self_loop_nodes, indexed)


def load_topology(system: Any) -> Topology:
    """Load graph topology from HeadIndex v3 or scan_records with zero sealed payload reads."""
    store = system.record_store
    verified = store.open_verified_index_view()
    if verified is not None:
        with verified:
            if verified.is_exact_base:
                return _topology_from_mmap(verified.view)
            nodes = verified.all_entries()
        return _topology_from_nodes(nodes, indexed=True)
    scanned = {str(entry["id"]): entry for entry in store.scan_records()}
    return _topology_from_nodes(scanned, indexed=False)


def load_link_spec(link_file: Path) -> list[dict[str, str]]:
    """Load and strictly validate an explicit reviewed island-link specification."""
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
        src = packet_id(_require_nonempty_str(row.get("from"), f"link spec entry #{idx} 'from'"))
        tgt = packet_id(
            _require_nonempty_str(row.get("target"), f"link spec entry #{idx} 'target'")
        )
        rel = _require_nonempty_str(row.get("relation"), f"link spec entry #{idx} 'relation'")
        if src == tgt:
            raise ValueError(f"link spec entry #{idx} must not create a self-loop on {src}")
        entry = {"from": src, "relation": rel, "target": tgt}
        if "warrant" in row:
            entry["warrant"] = _require_nonempty_str(
                row["warrant"], f"link spec entry #{idx} 'warrant'"
            )
        parsed.append(entry)
    return parsed


def _run_post_lint(
    system: Any, lint_fn: Callable[[Any], dict[str, Any]] | None
) -> tuple[int, dict[str, Any]]:
    if lint_fn is not None:
        report = lint_fn(system)
        summary = report.get("summary")
        if isinstance(summary, dict):
            total = sum(_strict_int(val, f"lint summary {key}") for key, val in summary.items())
            return total, dict(summary)
        return _strict_int(report.get("anomaly_count", 0), "lint anomaly_count"), {}
    topo = load_topology(system)
    dup_edges = sum(len(e) - len(set(e)) for e in topo.out_edges if len(e) != len(set(e)))
    summary = {"self_loop_nodes": len(topo.self_loop_nodes), "duplicate_edges": dup_edges}
    return sum(summary.values()), summary


def _build_receipt_records(
    operations: list[dict[str, Any]],
    patches: dict[str, RecordPatch],
    batch_receipt: dict[str, Any],
) -> list[dict[str, Any]]:
    rows_by_id = {str(row["id"]): row for row in batch_receipt["records"]}
    records: list[dict[str, Any]] = []
    for op in operations:
        rid = str(op["id"])
        patch = patches[rid]
        row = rows_by_id[rid]
        records.append(
            {
                "id": rid,
                "before_revision": _strict_int(
                    row["before_revision"], "before_revision", minimum=1
                ),
                "before_sha256": str(row["before_sha256"]),
                "after_revision": _strict_int(row["after_revision"], "after_revision", minimum=1),
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
    return records


def repair_graph(
    system: Any,
    *,
    apply: bool = False,
    receipt_path: Path | None = None,
    link_spec_path: Path | None = None,
    profile_config_path: Path | None = None,
    roots: list[str] | tuple[str, ...] = (),
    remove_self_loops: bool = True,
    repoint_actionable_bypasses: bool = True,
    retire_stranded_questions: bool = True,
    retire_retired_prefix_questions: bool = False,
    delegate_redundant_hub_edges: bool = False,
    hub_ids: list[str] | tuple[str, ...] = (),
    account: str = DEFAULT_ACCOUNT,
    acquire_lock: bool = True,
    lint_fn: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Plan (dry-run default) or atomically apply zero-loss mechanical graph-health repairs."""
    cal = resolve_calibration(profile_config_path=profile_config_path)
    explicit_roots = strings(list(roots), "roots") if roots else list(cal.default_roots)
    explicit_hubs = strings(list(hub_ids), "hub_ids") if hub_ids else []
    links = load_link_spec(link_spec_path) if link_spec_path is not None else []
    profile_str = str(profile_config_path) if profile_config_path is not None else None

    def _compute() -> tuple[dict[str, RecordPatch], dict[str, Any], list[dict[str, Any]]]:
        patches, summary = plan_repairs(
            system,
            load_topology(system),
            explicit_roots=explicit_roots,
            root_kinds=cal.root_kinds,
            bypass_exempt_predicates=cal.bypass_exempt_predicates,
            remove_self_loops=remove_self_loops,
            repoint_actionable_bypasses=repoint_actionable_bypasses,
            retire_stranded_questions=retire_stranded_questions,
            retire_retired_prefix_questions=retire_retired_prefix_questions,
            delegate_redundant_hub_edges=delegate_redundant_hub_edges,
            hub_ids=explicit_hubs,
            links=links,
        )
        return patches, summary, build_batch_operations(patches)

    if not apply:
        _patches, summary, operations = _compute()
        return {
            "format": REPAIR_PLAN_FORMAT,
            "instance_id": system.instance_id,
            "applied": False,
            "dry_run": True,
            "written_count": 0,
            "batch_id": None,
            "receipt_path": None,
            "lint_anomalies": None,
            "profile_config": profile_str,
            "calibration_sources": list(cal.sources),
            "operation_ids": [op["id"] for op in operations],
            **summary,
        }

    lock_cm = exclusive(system.ex.root / "owner.lock") if acquire_lock else nullcontext()
    with lock_cm:
        patches, summary, operations = _compute()
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
                "profile_config": profile_str,
                "calibration_sources": list(cal.sources),
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
        batch_receipt = system.put_batch(
            {
                "format": BATCH_FORMAT,
                "id": batch_id,
                "instance_id": system.instance_id,
                "pin_predecessors": True,
                "account": account,
                "operations": operations,
            }
        )
        system.reindex()
        lint_anomalies, lint_summary = _run_post_lint(system, lint_fn)
        receipt_records = _build_receipt_records(operations, patches, batch_receipt)
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
        "lint_summary": lint_summary,
        "profile_config": profile_str,
        "calibration_sources": list(cal.sources),
        "operation_ids": [op["id"] for op in operations],
        **summary,
    }


def _build_inverse_ops(system: Any, records: list[Any]) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    for idx, row in enumerate(records):
        if not isinstance(row, dict):
            raise ValueError(f"repair receipt record #{idx} must be an object")
        rid = packet_id(_require_nonempty_str(row.get("id"), f"repair receipt record #{idx} 'id'"))
        expected_after_rev = _strict_int(
            row.get("after_revision"), f"repair receipt record #{idx} after_revision", minimum=1
        )
        expected_after_sha = _require_nonempty_str(
            row.get("after_sha256"), f"repair receipt record #{idx} after_sha256"
        )
        previous_active = _strict_bool(
            row.get("previous_active"), f"repair receipt record #{idx} previous_active"
        )
        new_active = _strict_bool(row.get("new_active"), f"repair receipt record #{idx} new_active")
        added_rels = _strict_rel_list(
            row.get("added_relations"), f"repair receipt record #{idx} added_relations"
        )
        removed_rels = _strict_rel_list(
            row.get("removed_relations"), f"repair receipt record #{idx} removed_relations"
        )
        current = system.record_store.show(rid)
        cur_rev = _strict_int(current.get("revision"), f"current head {rid} revision", minimum=1)
        if cur_rev != expected_after_rev or str(current["sha256"]) != expected_after_sha:
            raise ValueError(
                f"cannot rollback {rid}: current head rev {cur_rev} differs from "
                f"receipt after_revision {expected_after_rev}"
            )
        op: dict[str, Any] = {
            "id": rid,
            "mode": "patch",
            "expected_revision": cur_rev,
            "expected_sha256": str(current["sha256"]),
        }
        if added_rels:
            op["remove_relations"] = added_rels
        if removed_rels:
            op["add_relations"] = removed_rels
        if previous_active != new_active:
            op["set_active"] = previous_active
        if len(op) > 4:
            ops.append(op)
    return ops


def rollback_graph_repair(
    system: Any,
    receipt_path: Path,
    *,
    apply: bool = False,
    account: str = DEFAULT_ROLLBACK_ACCOUNT,
    acquire_lock: bool = True,
    lint_fn: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Preview or atomically invert a previously written repair receipt via put_batch."""
    receipt = _read_ordinary_json(receipt_path, label="repair receipt")
    if receipt.get("format") != REPAIR_RECEIPT_FORMAT:
        raise ValueError(f"unsupported repair receipt format: {receipt.get('format')!r}")
    if receipt.get("instance_id") != system.instance_id:
        raise ValueError("repair receipt belongs to a different store instance")
    records = receipt.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("repair receipt contains no reversible records")

    if not apply:
        ops = _build_inverse_ops(system, records)
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

    lock_cm = exclusive(system.ex.root / "owner.lock") if acquire_lock else nullcontext()
    with lock_cm:
        ops = _build_inverse_ops(system, records)
        batch_hash = hashlib.sha256(encode(ops)).hexdigest()[:12]
        batch_id = packet_id(f"graph-health-rollback-{batch_hash}")
        system.put_batch(
            {
                "format": BATCH_FORMAT,
                "id": batch_id,
                "instance_id": system.instance_id,
                "pin_predecessors": True,
                "account": account,
                "operations": ops,
            }
        )
        system.reindex()
        lint_anomalies, lint_summary = _run_post_lint(system, lint_fn)

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
        "lint_summary": lint_summary,
        "rollback_ids": [op["id"] for op in ops],
    }


def dispatch_repair(
    system: Any, args: Any, *, lint_fn: Callable[[Any], dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Dispatch parsed CLI arguments for `managed-ai-information graph repair`."""

    def _csv_list(attr: str) -> list[str]:
        raw = getattr(args, attr, None) or []
        return [part.strip() for item in raw for part in item.split(",") if part.strip()]

    if args.rollback is not None:
        if args.link_spec or args.receipt or args.profile_config:
            raise ValueError(
                "--rollback cannot be combined with --link-spec, --receipt, or --profile-config"
            )
        return rollback_graph_repair(
            system,
            args.rollback,
            apply=bool(args.apply),
            account=args.account if args.account is not None else DEFAULT_ROLLBACK_ACCOUNT,
            acquire_lock=False,
            lint_fn=lint_fn,
        )
    return repair_graph(
        system,
        apply=bool(args.apply),
        receipt_path=args.receipt,
        link_spec_path=args.link_spec,
        profile_config_path=args.profile_config,
        roots=_csv_list("roots"),
        remove_self_loops=not bool(args.no_self_loops),
        repoint_actionable_bypasses=not bool(args.no_repoint_bypasses),
        retire_stranded_questions=not bool(args.no_retire_stranded_questions),
        retire_retired_prefix_questions=bool(args.retire_retired_prefix_questions),
        delegate_redundant_hub_edges=bool(args.delegate_redundant_hub_edges),
        hub_ids=_csv_list("hub_ids"),
        account=args.account if args.account is not None else DEFAULT_ACCOUNT,
        acquire_lock=False,
        lint_fn=lint_fn,
    )
