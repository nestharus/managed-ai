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

from managed_ai_information.platform import check_ordinary_ancestors  # noqa: E402
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
    load_existing_records,
    slugify_id,
)

INGEST_REPO_FORMAT = "managed-ai-repo-ingest/1"

_PACKAGE_TO_COMPONENT_ID: dict[str, str] = {
    "platform": "pkg-platform",
    "deployment": "pkg-engine-deployment",
    "core": "pkg-engine-core",
    "store": "pkg-engine-store",
    "library": "pkg-engine-library",
    "host": "pkg-engine-host",
    "collective": "pkg-collective",
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


def _extract_public_exports(file_path: Path) -> list[str]:
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
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
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], dict[str, bytes]]:
    mod_name: str = mod["module"]
    rel_path: str = mod["path"]
    mod_id = mod_ids_by_name[mod_name]
    exports = _extract_public_exports(checked_repo / rel_path)
    is_facade = rel_path.endswith("/__init__.py") or mod_name.endswith(".domain_api")

    digest_entry = file_digest_entry(checked_repo, rel_path)
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

    records.append(
        {
            "id": mod_id,
            "kind": "code-module",
            "content": {
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
            },
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


def build_repo_records(
    repo_root: Path,
    system: System,
    *,
    package_rel: str = "src/managed_ai_information",
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], dict[str, bytes], dict[str, Any]]:
    """Purely compute desired repo records and in-memory digest pins (zero disk writes)."""
    checked_repo = check_ordinary_ancestors(
        repo_root, expect_dir=True, error_prefix="ordinary repository root required"
    ).resolve(strict=True)
    profile = profile_repository(checked_repo, package_rel=package_rel)
    commit = git_head_sha(checked_repo)
    existing_ids = set(load_existing_records(system))

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
    profile_rels: list[dict[str, str]] = []
    if "managed-ai-architecture" in existing_ids:
        profile_rels.append({"relation": "maps-to", "target": "managed-ai-architecture"})
    desired: list[dict[str, Any]] = [
        {
            "id": "profile-head",
            "kind": "profile-snapshot",
            "content": {
                "format": profile["format"],
                "package_dir": package_rel,
                "commit": commit,
                "summary": profile["summary"],
                "relationship_summary": relationships["summary"],
            },
            "relations": profile_rels,
            "basis": [],
            "active": True,
        }
    ]
    basis_shas: dict[str, dict[str, str]] = {"profile-head": {}}
    pending_digests: dict[str, bytes] = {}

    for mod in profile["modules"]:
        bundle_records, bundle_basis, bundle_digests = _build_one_module_bundle(
            checked_repo,
            mod,
            mod_ids_by_name,
            existing_ids,
            scc_members,
            cross_pkg_priv_callers,
            commit,
        )
        desired.extend(bundle_records)
        basis_shas.update(bundle_basis)
        pending_digests.update(bundle_digests)

    return desired, basis_shas, pending_digests, profile


def ingest_repository(
    repo_root: Path,
    store_root: Path,
    *,
    package_rel: str = "src/managed_ai_information",
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
    )
    receipt = apply_desired_records(
        system,
        desired,
        basis_shas,
        batch_prefix="ingest-repo",
        pending_digests=pending_digests,
        dry_run=dry_run,
    )
    return {
        "format": INGEST_REPO_FORMAT,
        "repo": str(repo_root),
        "store": str(checked_store),
        "package_dir": package_rel,
        "desired_record_count": len(desired),
        "summary": profile["summary"],
        **receipt,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic deep self-ingestor for managed-ai-information.",
        allow_abbrev=False,
    )
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--package-dir", default="src/managed_ai_information")
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
