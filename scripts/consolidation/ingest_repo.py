#!/usr/bin/env python3
"""Deterministic deep self-ingestor for managed-ai-information into a native store."""

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
for _entry in (str(SRC_ROOT), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai_information.core.ownership import relative_path  # noqa: E402
from managed_ai_information.core.storage import MAX_BYTES, read_bytes, sha  # noqa: E402
from managed_ai_information.platform import (  # noqa: E402
    check_ordinary_ancestors,
    is_ordinary_file_stat,
    strict_json_loads,
)
from managed_ai_information.store.system import System  # noqa: E402
from scripts.ci.semantic_profile import (  # noqa: E402
    DEFAULT_BUDGETS,
    DEFAULT_RELATIONSHIP_BUDGETS,
    profile_repository,
)
from scripts.consolidation._common import (  # noqa: E402
    apply_desired_records,
    compute_digest_basis,
    dump_json,
    file_digest_entry,
    git_head_sha,
    slugify_id,
)
from scripts.consolidation.test_value_analyzer import (  # noqa: E402
    analyze_test_value,
    extract_facade_reexports,
    has_executable_statements,
)

INGEST_REPO_FORMAT = "managed-ai-repo-ingest/1"
_EXTERNAL_STRUCTURAL_EDGE_KINDS: frozenset[str] = frozenset({"imports", "declares_mod"})
_EXTERNAL_SIGNAL_EDGE_KINDS: frozenset[str] = frozenset(
    {"bypasses", "bypass", "in_scc", "shares_write_target", "phantom_cochanges"}
)
_ALL_EXTERNAL_EDGE_KINDS: frozenset[str] = (
    _EXTERNAL_STRUCTURAL_EDGE_KINDS | _EXTERNAL_SIGNAL_EDGE_KINDS
)


def load_external_graph_edges(
    edges_path: Path,
    mod_ids_by_path: dict[str, str],
) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Load nestharus/semantic-profiler --emit-graph-edges JSON cleanly into record relations."""
    checked = check_ordinary_ancestors(
        edges_path, expect_dir=False, error_prefix="ordinary external graph edges file required"
    ).resolve(strict=True)
    if not is_ordinary_file_stat(checked.lstat()):
        raise ValueError(f"ordinary external graph edges file required: {checked}")
    doc = strict_json_loads(read_bytes(checked, MAX_BYTES), label=checked.name)
    schema_ver = doc.get("schema_version") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or type(schema_ver) is not int or schema_ver != 1:
        raise ValueError("external graph edges must be a schema_version=1 JSON object")
    edges = doc.get("edges")
    if not isinstance(edges, list):
        raise ValueError("external graph edges 'edges' field must be a list")

    extra_rels: dict[str, list[dict[str, str]]] = {}
    seen_rels: dict[str, set[tuple[str, str]]] = {}
    signals_by_mod: dict[str, list[dict[str, Any]]] = {}
    seen_signals: dict[str, set[tuple[str, str, int, Any]]] = {}
    by_kind: dict[str, int] = {}
    mapped_count = 0
    unmapped_count = 0

    for row in edges:
        if not isinstance(row, dict):
            raise ValueError("external graph edge entry must be an object")
        kind = str(row.get("kind", "")).strip()
        if kind not in _ALL_EXTERNAL_EDGE_KINDS:
            raise ValueError(f"unsupported external graph edge kind: {kind!r}")
        src_path = str(row.get("from", "")).strip().replace("\\", "/")
        dst_path = str(row.get("to", "")).strip().replace("\\", "/")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        src_id = mod_ids_by_path.get(src_path)
        dst_id = mod_ids_by_path.get(dst_path)
        if src_id is None or dst_id is None or src_id == dst_id:
            unmapped_count += 1
            continue
        mapped_count += 1
        relation = "depends-on" if kind in _EXTERNAL_STRUCTURAL_EDGE_KINDS else "bears-on"
        key = (relation, dst_id)
        bucket_seen = seen_rels.setdefault(src_id, set())
        if key not in bucket_seen:
            bucket_seen.add(key)
            extra_rels.setdefault(src_id, []).append({"relation": relation, "target": dst_id})
        if kind in _EXTERNAL_SIGNAL_EDGE_KINDS:
            weight = row.get("weight", 1)
            norm_kind = "bypasses" if kind == "bypass" else kind
            norm_weight = weight if type(weight) is int else 1
            label = row.get("label")
            sig_key = (norm_kind, dst_id, norm_weight, label)
            sig_seen = seen_signals.setdefault(src_id, set())
            if sig_key not in sig_seen:
                sig_seen.add(sig_key)
                signals_by_mod.setdefault(src_id, []).append(
                    {
                        "kind": norm_kind,
                        "target": dst_id,
                        "target_path": dst_path,
                        "weight": norm_weight,
                        "label": label,
                    }
                )

    for rels in extra_rels.values():
        rels.sort(key=lambda r: (r["relation"], r["target"]))
    for sigs in signals_by_mod.values():
        sigs.sort(key=lambda s: (str(s["kind"]), str(s["target"]), str(s.get("label") or "")))

    summary = {
        "schema_version": 1,
        "repo_label": str(doc.get("repo_label", "")),
        "commit": doc.get("commit"),
        "total_edges": len(edges),
        "mapped_edges": mapped_count,
        "unmapped_edges": unmapped_count,
        "by_kind": dict(sorted(by_kind.items())),
    }
    return extra_rels, signals_by_mod, summary


_PACKAGE_TO_COMPONENT_ID: dict[str, str] = {
    "platform": "pkg-platform",
    "deployment": "pkg-engine-deployment",
    "core": "pkg-engine-core",
    "store": "pkg-engine-store",
    "library": "pkg-engine-library",
    "host": "pkg-engine-host",
    "collective": "pkg-collective",
    "companions": "pkg-companions",
    "companions.peer": "pkg-companions-peer",
    "companions.publication_review": "pkg-companions-publication-review",
    "companions.representation": "pkg-companions-representation",
    "companions.workbench": "pkg-companions-workbench",
}


def _module_record_id(module_name: str) -> str:
    parts = module_name.split(".")
    trimmed = ".".join(parts[1:]) if len(parts) > 1 else parts[0]
    return slugify_id("mod", trimmed)


def _facade_record_id(module_name: str) -> str:
    parts = module_name.split(".")
    trimmed = ".".join(parts[1:]) if len(parts) > 1 else parts[0]
    return slugify_id("facade", trimmed)


def _hotspot_record_id(module_name: str) -> str:
    parts = module_name.split(".")
    trimmed = ".".join(parts[1:]) if len(parts) > 1 else parts[0]
    return slugify_id("hotspot", trimmed)


def _extract_public_exports_from_tree(tree: ast.Module) -> list[str]:
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    try:
                        val = ast.literal_eval(stmt.value)
                    except TypeError, ValueError:
                        break
                    if isinstance(val, (list, tuple)) and all(isinstance(x, str) for x in val):
                        return sorted(val)
    exports: list[str] = []
    for stmt in tree.body:
        if isinstance(
            stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) and not stmt.name.startswith("_"):
            exports.append(stmt.name)
    return sorted(exports)[:40]


def _scan_profile_module_source(
    checked_repo: Path,
    mod: dict[str, Any],
    known_names: set[str],
    verified_dirs: set[Path],
    *,
    collect_test_meta: bool,
) -> tuple[list[str], dict[str, Any], bool, dict[tuple[str, str], str]]:
    """Read and parse one profiled module once for exports, digest pin, and facade reexports."""
    mod_name: str = mod["module"]
    safe_rel = relative_path(str(mod["path"]))
    target = checked_repo / safe_rel
    parent = target.parent
    if parent not in verified_dirs:
        checked_parent = check_ordinary_ancestors(
            parent, expect_dir=True, error_prefix=f"ordinary source file required ({safe_rel})"
        ).resolve(strict=True)
        if not checked_parent.is_relative_to(checked_repo):
            raise ValueError(f"source path escapes repository root: {safe_rel}")
        verified_dirs.add(parent)
        cur = parent
        while cur != checked_repo and cur.parent != cur:
            cur = cur.parent
            verified_dirs.add(cur)
    info = target.lstat()
    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary source file required ({safe_rel}): {target}")
    raw = read_bytes(target, MAX_BYTES)
    digest_entry = {"path": safe_rel, "bytes": len(raw), "sha256": sha(raw)}
    tree = ast.parse(raw.decode("utf-8"), filename=safe_rel)
    exports = _extract_public_exports_from_tree(tree)
    has_exec = has_executable_statements(tree) if collect_test_meta else True
    is_init = safe_rel.endswith("/__init__.py") or safe_rel == "__init__.py"
    is_facade = is_init or mod_name.endswith(".domain_api")
    reexports = (
        extract_facade_reexports(mod_name, is_init, tree, known_names)
        if (collect_test_meta and is_facade)
        else {}
    )
    del tree
    return exports, digest_entry, has_exec, reexports


def _parent_component_id(module_name: str, existing_ids: set[str]) -> str | None:
    parts = module_name.split(".")
    if len(parts) < 2:
        return None
    sub2 = ".".join(parts[1:3]) if len(parts) >= 3 else parts[1]
    for key in (sub2, parts[1]):
        comp_id = _PACKAGE_TO_COMPONENT_ID.get(key)
        if comp_id is not None and comp_id in existing_ids:
            return comp_id
    return None


def _hotspot_reasons(
    mod: dict[str, Any],
    scc_members: set[str],
    cross_pkg_priv_callers: set[str],
) -> list[str]:
    reasons: list[str] = []
    if mod["lines"] > DEFAULT_BUDGETS["max_file_lines"]:
        reasons.append(f"lines:{mod['lines']}>{DEFAULT_BUDGETS['max_file_lines']}")
    if mod["max_cc"] > DEFAULT_BUDGETS["max_cc"]:
        reasons.append(f"max_cc:{mod['max_cc']}>{DEFAULT_BUDGETS['max_cc']}")
    if mod["max_depth"] > DEFAULT_BUDGETS["max_depth"]:
        reasons.append(f"max_depth:{mod['max_depth']}>{DEFAULT_BUDGETS['max_depth']}")
    if mod["working_set_tokens"] > DEFAULT_BUDGETS["max_working_set_tokens"]:
        reasons.append(
            f"working_set_tokens:{mod['working_set_tokens']}>"
            f"{DEFAULT_BUDGETS['max_working_set_tokens']}"
        )
    if mod["module"] in scc_members:
        reasons.append("module_scc_member")
    if mod["module"] in cross_pkg_priv_callers:
        reasons.append("cross_package_private_import")
    if (
        not mod["is_primitive"]
        and mod["nexus_score"] > DEFAULT_RELATIONSHIP_BUDGETS["max_non_primitive_nexus"]
    ):
        reasons.append(
            f"nexus_score:{mod['nexus_score']}>"
            f"{DEFAULT_RELATIONSHIP_BUDGETS['max_non_primitive_nexus']}"
        )
    return reasons


def _build_hotspot_record(
    mod: dict[str, Any],
    mod_id: str,
    reasons: list[str],
    basis_list: list[str],
    commit: str,
) -> dict[str, Any]:
    return {
        "id": _hotspot_record_id(mod["module"]),
        "kind": "semantic-hotspot",
        "content": {
            "module": mod["module"],
            "module_id": mod_id,
            "path": mod["path"],
            "reasons": reasons,
            "metrics": {
                "lines": mod["lines"],
                "max_cc": mod["max_cc"],
                "max_depth": mod["max_depth"],
                "working_set_tokens": mod["working_set_tokens"],
                "nexus_score": mod["nexus_score"],
                "blast_radius": mod["blast_radius"],
            },
            "commit": commit,
        },
        "relations": [
            {"relation": "bears-on", "target": mod_id},
            {"relation": "evidenced-by", "target": "profile-head"},
        ],
        "basis": basis_list,
        "active": True,
    }


def _build_one_module_bundle(
    checked_repo: Path,
    mod: dict[str, Any],
    mod_ids_by_name: dict[str, str],
    existing_ids: set[str],
    scc_members: set[str],
    cross_pkg_priv_callers: set[str],
    commit: str,
    exports: list[str],
    digest_entry: dict[str, Any],
    *,
    extra_rels: list[dict[str, str]] | None = None,
    external_signals: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], dict[str, bytes]]:
    mod_name: str = mod["module"]
    rel_path: str = mod["path"]
    mod_id = mod_ids_by_name[mod_name]
    is_facade = rel_path.endswith("/__init__.py") or mod_name.endswith(".domain_api")

    basis_rel, basis_bytes, basis_sha = compute_digest_basis(
        mod_id,
        source_repo=checked_repo.name,
        files=[digest_entry],
    )
    basis_list = [basis_rel]
    mod_basis_map = {basis_rel: basis_sha}
    pending_digests = {mod_id: basis_bytes}

    records: list[dict[str, Any]] = []
    basis_shas: dict[str, dict[str, str]] = {mod_id: mod_basis_map}
    mod_rels: list[dict[str, str]] = [
        {"relation": "depends-on", "target": mod_ids_by_name[target]}
        for target in mod["internal_imports"]
        if target in mod_ids_by_name and mod_ids_by_name[target] != mod_id
    ]
    comp_id = _parent_component_id(mod_name, existing_ids)
    if comp_id is not None:
        mod_rels.append({"relation": "maps-to", "target": comp_id})
    if extra_rels:
        seen_pairs = {(r["relation"], r["target"]) for r in mod_rels}
        for rel in extra_rels:
            pair = (rel["relation"], rel["target"])
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                mod_rels.append(rel)

    if is_facade:
        facade_id = _facade_record_id(mod_name)
        mod_rels.append({"relation": "instantiates", "target": facade_id})
        facade_rels = [{"relation": "qualifies", "target": mod_id}]
        if comp_id is not None:
            facade_rels.append({"relation": "maps-to", "target": comp_id})
        records.append(
            {
                "id": facade_id,
                "kind": "code-facade",
                "content": {
                    "module": mod_name,
                    "path": rel_path,
                    "public_symbols": exports,
                    "is_primitive": mod["is_primitive"],
                    "commit": commit,
                },
                "relations": facade_rels,
                "basis": basis_list,
                "active": True,
            }
        )
        basis_shas[facade_id] = dict(mod_basis_map)

    mod_content: dict[str, Any] = {
        "module": mod_name,
        "path": rel_path,
        "lines": mod["lines"],
        "tokens": mod["tokens"],
        "functions": mod["functions"],
        "max_cc": mod["max_cc"],
        "max_cc_function": mod["max_cc_function"],
        "max_depth": mod["max_depth"],
        "max_depth_function": mod["max_depth_function"],
        "fan_in": mod["fan_in"],
        "fan_out": mod["fan_out"],
        "nexus_score": mod["nexus_score"],
        "blast_radius": mod["blast_radius"],
        "working_set_tokens": mod["working_set_tokens"],
        "bypass_out": mod["bypass_out"],
        "bypass_in": mod["bypass_in"],
        "is_facade": is_facade,
        "is_primitive": mod["is_primitive"],
        "public_exports": exports,
        "source_sha256": digest_entry["sha256"],
        "commit": commit,
    }
    if external_signals:
        mod_content["external_graph_signals"] = external_signals

    records.append(
        {
            "id": mod_id,
            "kind": "code-module",
            "content": mod_content,
            "relations": mod_rels,
            "basis": basis_list,
            "active": True,
        }
    )

    reasons = _hotspot_reasons(mod, scc_members, cross_pkg_priv_callers)
    if reasons:
        hs_rec = _build_hotspot_record(mod, mod_id, reasons, basis_list, commit)
        records.append(hs_rec)
        basis_shas[hs_rec["id"]] = dict(mod_basis_map)

    return records, basis_shas, pending_digests


def _build_test_value_records(
    checked_repo: Path,
    package_rel: str,
    tests_rel: str,
    mod_ids_by_name: dict[str, str],
    mod_basis_by_id: dict[str, dict[str, str]],
    effective_existing: set[str],
    commit: str,
    *,
    precomputed_prod: tuple[dict[str, dict[str, Any]], dict[tuple[str, str], str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], dict[str, bytes], dict[str, Any]]:
    test_digests: dict[str, dict[str, Any]] = {}
    report = analyze_test_value(
        checked_repo,
        package_rel=package_rel,
        tests_rel=tests_rel,
        precomputed_prod=precomputed_prod,
        digest_out=test_digests,
        commit=commit,
    )
    records: list[dict[str, Any]] = []
    basis_shas: dict[str, dict[str, str]] = {}
    pending_digests: dict[str, bytes] = {}
    cluster_members = {c["cluster_id"]: c["members"] for c in report["overlap_clusters"]}
    verified_dirs: set[Path] = {checked_repo}

    for suite in report["suites"]:
        suite_id = str(suite["suite_id"])
        rel_path = str(suite["path"])
        digest_entry = test_digests.get(rel_path) or file_digest_entry(
            checked_repo, rel_path, checked_root=checked_repo, verified_dirs=verified_dirs
        )
        basis_rel, basis_bytes, basis_sha = compute_digest_basis(
            suite_id, source_repo=checked_repo.name, files=[digest_entry]
        )
        basis_shas[suite_id] = {basis_rel: basis_sha}
        pending_digests[suite_id] = basis_bytes

        rels: list[dict[str, str]] = [{"relation": "evidenced-by", "target": "profile-head"}]
        comp_targets: set[str] = set()
        for mod_name in suite["exercises_modules"]:
            mod_id = mod_ids_by_name.get(mod_name)
            if mod_id is not None:
                rels.append({"relation": "qualifies", "target": mod_id})
            comp_id = _parent_component_id(mod_name, effective_existing)
            if comp_id is not None:
                comp_targets.add(comp_id)
        for comp_id in sorted(comp_targets):
            rels.append({"relation": "maps-to", "target": comp_id})

        cid = suite["overlap_cluster_id"]
        overlap_peers = [m for m in cluster_members.get(cid, ()) if m != suite_id] if cid else []
        records.append(
            {
                "id": suite_id,
                "kind": "test-suite",
                "content": {
                    "path": rel_path,
                    "test_method_count": suite["test_method_count"],
                    "assertion_count": suite["assertion_count"],
                    "assertion_density": suite["assertion_density"],
                    "boundary_class": suite["boundary_class"],
                    "exercises_modules": suite["exercises_modules"],
                    "asserts_contract": suite["asserts_contract"],
                    "mocks_boundary": suite["mocks_boundary"],
                    "mock_site_count": suite["mock_site_count"],
                    "overlap_cluster_id": cid,
                    "overlap_peers": overlap_peers,
                    "source_sha256": digest_entry["sha256"],
                    "commit": commit,
                },
                "relations": rels,
                "basis": [basis_rel],
                "active": True,
            }
        )

    for unk in report["verification_unknowns"]:
        unk_id = str(unk["unknown_id"])
        mod_name = str(unk["module"])
        mod_id = str(unk["module_id"])
        mod_basis = dict(mod_basis_by_id.get(mod_id, {}))
        rels = [
            {"relation": "bears-on", "target": mod_id},
            {"relation": "evidenced-by", "target": "profile-head"},
        ]
        comp_id = _parent_component_id(mod_name, effective_existing)
        if comp_id is not None:
            rels.append({"relation": "maps-to", "target": comp_id})
        records.append(
            {
                "id": unk_id,
                "kind": "verification-unknown",
                "content": {
                    "module": mod_name,
                    "module_id": mod_id,
                    "path": unk["path"],
                    "lines": unk["lines"],
                    "is_facade": unk["is_facade"],
                    "reason": unk["reason"],
                    "transitive_only": unk["transitive_only"],
                    "exercising_suites": unk["exercising_suites"],
                    "mock_heavy_suites": unk["mock_heavy_suites"],
                    "commit": commit,
                },
                "relations": rels,
                "basis": sorted(mod_basis),
                "active": True,
            }
        )
        basis_shas[unk_id] = mod_basis

    return records, basis_shas, pending_digests, report["summary"]


def build_repo_records(
    repo_root: Path,
    system: System,
    *,
    package_rel: str = "src/managed_ai_information",
    include_tests: bool = False,
    tests_rel: str = "tests",
    existing_ids: set[str] | None = None,
    external_graph_edges: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], dict[str, bytes], dict[str, Any]]:
    """Purely compute desired repo records and in-memory digest pins (zero disk writes)."""
    checked_repo = check_ordinary_ancestors(
        repo_root, expect_dir=True, error_prefix="ordinary repository root required"
    ).resolve(strict=True)
    profile = profile_repository(checked_repo, package_rel=package_rel)
    commit = git_head_sha(checked_repo)
    resolved_existing = (
        set(system.record_store.ids()) if existing_ids is None else set(existing_ids)
    )

    relationships = profile["relationships"]
    scc_members: set[str] = {
        member for comp in relationships["sccs"]["module"] for member in comp["members"]
    }
    cross_pkg_priv_callers: set[str] = {
        row["caller"] for row in relationships["private_imports"] if row["scope"] == "cross_package"
    }

    mod_ids_by_name = {
        mod["module"]: _module_record_id(mod["module"]) for mod in profile["modules"]
    }
    mod_ids_by_path = {mod["path"]: mod_ids_by_name[mod["module"]] for mod in profile["modules"]}
    ext_rels: dict[str, list[dict[str, str]]] = {}
    ext_signals: dict[str, list[dict[str, Any]]] = {}
    ext_summary: dict[str, Any] | None = None
    if external_graph_edges is not None:
        ext_rels, ext_signals, ext_summary = load_external_graph_edges(
            external_graph_edges, mod_ids_by_path
        )
        profile["external_graph_edges_summary"] = ext_summary

    profile_rels: list[dict[str, str]] = []
    if "managed-ai-architecture" in resolved_existing:
        profile_rels.append({"relation": "maps-to", "target": "managed-ai-architecture"})
    profile_content: dict[str, Any] = {
        "format": profile["format"],
        "package_dir": package_rel,
        "commit": commit,
        "summary": profile["summary"],
        "relationship_summary": relationships["summary"],
    }
    if ext_summary is not None:
        profile_content["external_graph_edges"] = ext_summary
    desired: list[dict[str, Any]] = [
        {
            "id": "profile-head",
            "kind": "profile-snapshot",
            "content": profile_content,
            "relations": profile_rels,
            "basis": [],
            "active": True,
        }
    ]
    basis_shas: dict[str, dict[str, str]] = {"profile-head": {}}
    pending_digests: dict[str, bytes] = {}
    effective_existing = set(resolved_existing)

    has_companions = any(
        len(mod["module"].split(".")) >= 2 and mod["module"].split(".")[1] == "companions"
        for mod in profile["modules"]
    )
    if has_companions and "pkg-companions" not in effective_existing:
        comp_rels = (
            [{"relation": "maps-to", "target": "managed-ai-architecture"}]
            if "managed-ai-architecture" in effective_existing
            else []
        )
        desired.append(
            {
                "id": "pkg-companions",
                "kind": "software-component",
                "content": {
                    "name": "companions",
                    "description": "Top-level companion packages and shared companion contracts.",
                    "commit": commit,
                },
                "relations": comp_rels,
                "basis": [],
                "active": True,
            }
        )
        basis_shas["pkg-companions"] = {}
        effective_existing.add("pkg-companions")

    known_names = set(mod_ids_by_name)
    verified_dirs: set[Path] = {checked_repo}
    precomputed_mods: dict[str, dict[str, Any]] = {}
    precomputed_reexports: dict[tuple[str, str], str] = {}

    for mod in profile["modules"]:
        exports, digest_entry, has_exec, mod_reexports = _scan_profile_module_source(
            checked_repo,
            mod,
            known_names,
            verified_dirs,
            collect_test_meta=include_tests,
        )
        if include_tests:
            mod_name = str(mod["module"])
            rel_path = str(mod["path"])
            is_facade = rel_path.endswith("/__init__.py") or mod_name.endswith(".domain_api")
            precomputed_mods[mod_name] = {
                "module": mod_name,
                "module_id": mod_ids_by_name[mod_name],
                "path": rel_path,
                "lines": int(mod["lines"]),
                "is_facade": is_facade,
                "has_executable_code": has_exec,
                "internal_imports": list(mod["internal_imports"]),
            }
            if mod_reexports:
                precomputed_reexports.update(mod_reexports)
        mod_id_cur = mod_ids_by_name[mod["module"]]
        bundle_records, bundle_basis, bundle_digests = _build_one_module_bundle(
            checked_repo,
            mod,
            mod_ids_by_name,
            effective_existing,
            scc_members,
            cross_pkg_priv_callers,
            commit,
            exports,
            digest_entry,
            extra_rels=ext_rels.get(mod_id_cur),
            external_signals=ext_signals.get(mod_id_cur),
        )
        desired.extend(bundle_records)
        basis_shas.update(bundle_basis)
        pending_digests.update(bundle_digests)

    if include_tests:
        test_recs, test_basis, test_digests, test_summary = _build_test_value_records(
            checked_repo,
            package_rel,
            tests_rel,
            mod_ids_by_name,
            basis_shas,
            effective_existing,
            commit,
            precomputed_prod=(precomputed_mods, precomputed_reexports),
        )
        desired.extend(test_recs)
        basis_shas.update(test_basis)
        pending_digests.update(test_digests)
        profile["test_value_summary"] = test_summary

    return desired, basis_shas, pending_digests, profile


def ingest_repository(
    repo_root: Path,
    store_root: Path,
    *,
    package_rel: str = "src/managed_ai_information",
    include_tests: bool = False,
    tests_rel: str = "tests",
    external_graph_edges: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Ingest a Python repository into store_root idempotently."""
    checked_store = check_ordinary_ancestors(
        store_root, expect_dir=True, error_prefix="ordinary store root required"
    ).resolve(strict=True)
    system = System(checked_store)
    desired, basis_shas, pending_digests, profile = build_repo_records(
        repo_root,
        system,
        package_rel=package_rel,
        include_tests=include_tests,
        tests_rel=tests_rel,
        external_graph_edges=external_graph_edges,
    )
    receipt = apply_desired_records(
        system,
        desired,
        basis_shas,
        batch_prefix="ingest-repo",
        pending_digests=pending_digests,
        dry_run=dry_run,
    )
    report: dict[str, Any] = {
        "format": INGEST_REPO_FORMAT,
        "repo": str(repo_root),
        "store": str(checked_store),
        "package_dir": package_rel,
        "include_tests": include_tests,
        "desired_record_count": len(desired),
        "summary": profile["summary"],
        **receipt,
    }
    if include_tests:
        report["test_value_summary"] = profile.get("test_value_summary")
    if external_graph_edges is not None:
        report["external_graph_edges"] = profile.get("external_graph_edges_summary")
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic deep self-ingestor for managed-ai-information.",
        allow_abbrev=False,
    )
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--package-dir", default="src/managed_ai_information")
    parser.add_argument("--include-tests", action="store_true")
    parser.add_argument("--tests-dir", default="tests")
    parser.add_argument("--external-graph-edges", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.apply and args.dry_run:
            raise ValueError("--apply and --dry-run cannot be combined")
        dry_run = not args.apply
        report = ingest_repository(
            args.repo,
            args.store,
            package_rel=args.package_dir,
            include_tests=args.include_tests,
            tests_rel=args.tests_dir,
            external_graph_edges=args.external_graph_edges,
            dry_run=dry_run,
        )
        print(dump_json(report))
        return 0
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"format": INGEST_REPO_FORMAT, "status": "error", "error": str(exc)},
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
