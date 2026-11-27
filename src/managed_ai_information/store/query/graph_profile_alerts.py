"""Baselines, localized alerting, and phantom co-revision coupling for graph profiles."""

import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from managed_ai_information.core.storage import (
    atomic_bytes,
    check_ordinary_ancestors,
    is_ordinary_file_stat,
    read_bytes,
    strict_json_loads,
)
from managed_ai_information.store.protocol.identities import require

GRAPH_PROFILE_BASELINE_FORMAT = "managed-ai-graph-profile-baseline/1"
_MAX_SAMPLE_ITEMS = 25
_MAX_RECEIPT_BYTES = 8 * 1024 * 1024


def _extract_receipt_record_ids(doc: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for field in ("records", "written", "applied", "results", "record_ids"):
        items = doc.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str) and item:
                found.add(item)
            elif isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
                found.add(item["id"])
    return found


def _scan_cochange_groups(system: Any) -> list[tuple[str, set[str]]]:
    """Read ordinary batch receipts without touching sealed objects."""
    groups: list[tuple[str, set[str]]] = []
    batch_dir = system.work / ".system" / "batches"
    if not batch_dir.is_dir():
        return groups
    try:
        check_ordinary_ancestors(batch_dir, expect_dir=True, error_prefix="ordinary batch dir")
        with os.scandir(batch_dir) as entries:
            paths = sorted(
                Path(e.path) for e in entries if e.name.endswith(".json") and not e.is_symlink()
            )
    except OSError, ValueError:
        return groups

    for path in paths:
        try:
            info = path.lstat()
            if not is_ordinary_file_stat(info) or info.st_size > _MAX_RECEIPT_BYTES:
                continue
            doc = strict_json_loads(
                read_bytes(path, limit=_MAX_RECEIPT_BYTES), path, require_object=True
            )
        except OSError, UnicodeDecodeError, ValueError:
            continue
        rids = _extract_receipt_record_ids(doc)
        if len(rids) >= 2:
            groups.append((f"batch:{path.stem}", rids))
    return groups


def compute_phantom_cochange(
    system: Any,
    ids: list[str],
    id_to_idx: dict[str, int],
    active: list[bool],
    undirected: list[set[int]],
    *,
    min_count: int = 2,
) -> dict[str, Any]:
    """Detect active record pairs repeatedly co-revised in batches with zero explicit edge."""
    groups = _scan_cochange_groups(system)
    pair_sources: dict[tuple[int, int], list[str]] = defaultdict(list)

    for source_label, rids in groups:
        idxs = sorted(
            idx for rid in rids if (idx := id_to_idx.get(rid)) is not None and active[idx]
        )
        if len(idxs) < 2 or len(idxs) > 128:
            continue
        for pos, u in enumerate(idxs):
            nbrs = undirected[u]
            for v in idxs[pos + 1 :]:
                if v not in nbrs:
                    pair_sources[(u, v)].append(source_label)

    pairs: list[dict[str, Any]] = [
        {
            "a": ids[u],
            "b": ids[v],
            "co_revision_count": len(sources),
            "sources": sources[:10],
        }
        for (u, v), sources in pair_sources.items()
        if len(sources) >= min_count
    ]
    pairs.sort(key=lambda row: (-row["co_revision_count"], row["a"], row["b"]))
    return {
        "enabled": True,
        "batches_scanned": len(groups),
        "min_co_revisions": min_count,
        "phantom_pair_count": len(pairs),
        "phantom_pairs": pairs[:_MAX_SAMPLE_ITEMS],
    }


def _baseline_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    disc = report["discoverability"]
    dens = report["density_and_qualification"]
    bot = report["bottlenecks"]
    return {
        "format": GRAPH_PROFILE_BASELINE_FORMAT,
        "instance_id": report["instance_id"],
        "captured_at": datetime.now(UTC).isoformat(),
        "metrics": {
            "active_node_count": report["summary"]["active_nodes"],
            "active_edge_count": dens["active_edge_count"],
            "discoverability_2hop": disc["discoverability_2hop"],
            "discoverability_3hop": disc["discoverability_3hop"],
            "discoverability_4hop": disc["discoverability_4hop"],
            "p95_hops": disc["p95_hops"],
            "orphan_count": disc["orphan_count"],
            "global_edge_density": dens["global_edge_density"],
            "qualifying_edge_density": dens["qualifying_edge_density"],
            "qualification_density": dens["qualification_density"],
            "qualification_desert_count": dens["qualification_desert_count"],
            "unresolved_contradicts_cluster_count": dens["contradicts"][
                "unresolved_contradicts_cluster_count"
            ],
            "hop1_bytes_p95": dens["working_set"]["hop1_bytes_p95"],
            "signal_to_byte_ratio_p50": dens["working_set"]["signal_to_byte_ratio_p50"],
            "structural_scc_count": bot["structural_scc_count"],
            "structural_scc_locked_bytes": bot["structural_scc_locked_bytes"],
            "superseded_head_bypass_count": bot["superseded_head_bypass_count"],
            "phantom_pair_count": bot["phantom_cochange"]["phantom_pair_count"],
        },
    }


def _validate_baseline_path(target: Path, *, for_write: bool) -> Path:
    path = Path(target)
    ancestor = path.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if ancestor.exists():
        check_ordinary_ancestors(
            ancestor, expect_dir=True, error_prefix="ordinary graph profile baseline path required"
        )
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not for_write:
            raise ValueError(f"graph profile baseline not found: {path}") from None
        return path
    if not is_ordinary_file_stat(info):
        raise ValueError(f"graph profile baseline must be an ordinary file: {path}")
    return path


def load_graph_baseline(path: Path) -> dict[str, Any]:
    checked = _validate_baseline_path(path, for_write=False)
    doc = strict_json_loads(
        read_bytes(checked, limit=_MAX_RECEIPT_BYTES), checked, require_object=True
    )
    require(
        doc.get("format") == GRAPH_PROFILE_BASELINE_FORMAT and isinstance(doc.get("metrics"), dict),
        "unsupported graph profile baseline format",
    )
    return doc


def write_graph_baseline(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    checked = _validate_baseline_path(path, for_write=True)
    checked.parent.mkdir(parents=True, exist_ok=True)
    check_ordinary_ancestors(
        checked.parent,
        expect_dir=True,
        error_prefix="ordinary graph profile baseline directory required",
    )
    snapshot = _baseline_snapshot(report)
    raw = (json.dumps(snapshot, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    atomic_bytes(checked, raw)
    return snapshot


def _alert(
    severity: str,
    rule: str,
    metric: str,
    baseline: Any,
    current: Any,
    offending_ids: list[str],
) -> dict[str, Any]:
    return {
        "severity": severity,
        "rule": rule,
        "metric": metric,
        "baseline": baseline,
        "current": current,
        "offending_ids": offending_ids[:_MAX_SAMPLE_ITEMS],
    }


def _default_alerts(
    disc: dict[str, Any],
    dens: dict[str, Any],
    bot: dict[str, Any],
    ids_map: dict[str, list[str]],
) -> list[dict[str, Any]]:
    checks = (
        (
            "error",
            "structural_scc_detected",
            "structural_scc_count",
            bot["structural_scc_count"],
            ids_map["scc"],
        ),
        (
            "error",
            "superseded_head_bypass_detected",
            "superseded_head_bypass_count",
            bot["superseded_head_bypass_count"],
            ids_map["bypass"],
        ),
        (
            "warning",
            "unresolved_contradicts_cluster",
            "unresolved_contradicts_cluster_count",
            dens["contradicts"]["unresolved_contradicts_cluster_count"],
            ids_map["contradict"],
        ),
    )
    alerts = [
        _alert(sev, rule, metric, 0, val, ids) for sev, rule, metric, val, ids in checks if val > 0
    ]
    if disc["root_count"] > 0 and disc["orphan_count"] > 0:
        alerts.append(
            _alert(
                "warning",
                "unreachable_active_orphans",
                "orphan_count",
                0,
                disc["orphan_count"],
                disc["orphan_sample_ids"],
            )
        )
    return alerts


def _baseline_alerts(
    disc: dict[str, Any],
    dens: dict[str, Any],
    bot: dict[str, Any],
    base: dict[str, Any],
    ids_map: dict[str, list[str]],
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    b_d3 = float(base.get("discoverability_3hop", 1.0))
    c_d3 = float(disc["discoverability_3hop"])
    if c_d3 < b_d3 - 0.05:
        sev = "error" if c_d3 <= b_d3 - 0.15 else "warning"
        alerts.append(
            _alert(
                sev,
                "discoverability_3hop_regression",
                "discoverability_3hop",
                b_d3,
                c_d3,
                disc["orphan_sample_ids"],
            )
        )

    b_orph = int(base.get("orphan_count", 0))
    c_orph = int(disc["orphan_count"])
    if c_orph > b_orph:
        sev = "error" if c_orph >= b_orph + 5 else "warning"
        alerts.append(
            _alert(
                sev,
                "orphan_count_increase",
                "orphan_count",
                b_orph,
                c_orph,
                disc["orphan_sample_ids"],
            )
        )

    monotonic_rules = (
        (
            "error",
            "structural_scc_regression",
            "structural_scc_count",
            bot["structural_scc_count"],
            "scc",
        ),
        (
            "error",
            "superseded_head_bypass_increase",
            "superseded_head_bypass_count",
            bot["superseded_head_bypass_count"],
            "bypass",
        ),
        (
            "warning",
            "qualification_desert_increase",
            "qualification_desert_count",
            dens["qualification_desert_count"],
            "desert",
        ),
        (
            "warning",
            "unresolved_contradicts_increase",
            "unresolved_contradicts_cluster_count",
            dens["contradicts"]["unresolved_contradicts_cluster_count"],
            "contradict",
        ),
    )
    for sev, rule, metric, current, key in monotonic_rules:
        prior = int(base.get(metric, 0))
        if int(current) > prior:
            alerts.append(_alert(sev, rule, metric, prior, int(current), ids_map[key]))

    b_ws = base.get("hop1_bytes_p95")
    c_ws = dens["working_set"]["hop1_bytes_p95"]
    if (
        isinstance(b_ws, (int, float))
        and isinstance(c_ws, (int, float))
        and c_ws > max(float(b_ws) * 1.25, float(b_ws) + 4096.0)
    ):
        alerts.append(
            _alert("warning", "working_set_bloat", "hop1_bytes_p95", b_ws, c_ws, ids_map["heavy"])
        )
    return alerts


def evaluate_graph_alerts(
    report: dict[str, Any],
    *,
    baseline: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate localized health alerts against an optional baseline or absolute defaults."""
    disc = report["discoverability"]
    dens = report["density_and_qualification"]
    bot = report["bottlenecks"]
    ids_map = {
        "scc": sorted({nid for row in bot["structural_sccs"] for nid in row["node_ids"]}),
        "bypass": sorted({row["from"] for row in bot["superseded_head_bypasses"]}),
        "desert": [row["id"] for row in dens["qualification_deserts"]],
        "contradict": sorted(
            {
                nid
                for c in dens["contradicts"]["unresolved_contradicts_clusters"]
                for nid in c["node_ids"]
            }
        ),
        "heavy": [row["id"] for row in dens["working_set"]["heaviest_working_sets"]],
    }
    if baseline is None:
        return _default_alerts(disc, dens, bot, ids_map)
    return _baseline_alerts(disc, dens, bot, baseline["metrics"], ids_map)
