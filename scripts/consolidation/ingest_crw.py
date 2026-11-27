#!/usr/bin/env python3
"""Deterministic ingestor for code-review-workflows (12 families, 40 constituents, O01-O48)."""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
for _entry in (str(SRC_ROOT), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai_information.platform import (  # noqa: E402
    check_ordinary_ancestors,
    is_ordinary_file_stat,
    strict_json_loads,
)
from managed_ai_information.store.system import System  # noqa: E402
from scripts.consolidation._common import (  # noqa: E402
    apply_desired_records,
    dump_json,
    file_digest_entry,
    git_head_sha,
    slugify_id,
    write_digest_basis,
)

INGEST_CRW_FORMAT = "managed-ai-crw-ingest/1"
_PROMPT_LINK_RE = re.compile(r"\]\(([^)#]+\.prompt\.md)(?:#[^)]*)?\)")
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _read_text(root: Path, rel_path: str) -> str:
    target = check_ordinary_ancestors(
        root / rel_path,
        expect_dir=False,
        error_prefix=f"ordinary CRW file required ({rel_path})",
    )
    info = target.lstat()
    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary CRW file required ({rel_path}): {target}")
    return target.read_text(encoding="utf-8")


def _first_heading(text: str, fallback: str) -> str:
    match = _HEADING_RE.search(text)
    return match.group(1).strip() if match else fallback


def _extract_prompt_cross_links(
    crw_root: Path,
    prompt_rel: str,
    text: str,
    id_by_rel: dict[str, str],
) -> list[str]:
    base_dir = (crw_root / prompt_rel).parent
    targets: set[str] = set()
    for match in _PROMPT_LINK_RE.finditer(text):
        raw_link = match.group(1).strip()
        if raw_link.startswith(("http://", "https://")):
            continue
        resolved = (base_dir / raw_link).resolve()
        if not resolved.is_relative_to(crw_root):
            continue
        rel = resolved.relative_to(crw_root).as_posix()
        if rel in id_by_rel:
            targets.add(id_by_rel[rel])
    return sorted(targets)


def _pin_basis(
    system: System,
    crw_root: Path,
    record_id: str,
    rel_paths: list[str],
    *,
    sync_digests: bool,
) -> tuple[list[str], dict[str, str]]:
    files = [file_digest_entry(crw_root, rel) for rel in sorted(set(rel_paths))]
    if sync_digests:
        basis_rel, _, basis_sha = write_digest_basis(
            system,
            record_id,
            source_repo=crw_root.name,
            files=files,
        )
        return [basis_rel], {basis_rel: basis_sha}
    basis_rel = f"sources/repo-digests/{record_id}.json"
    return ([basis_rel] if (system.work / basis_rel).is_file() else []), {}


def _build_family_and_constituent_records(
    crw_root: Path,
    system: System,
    commit: str,
    *,
    sync_digests: bool,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], list[str], dict[str, str]]:
    obs_raw = _read_text(crw_root, "risk-axis-reviewers/observer-dimensions.json")
    constituents = strict_json_loads(
        obs_raw, label="observer-dimensions.json", require_object=False
    )
    if not isinstance(constituents, list) or not constituents:
        raise ValueError("observer-dimensions.json must be a non-empty JSON array")

    families_dir = check_ordinary_ancestors(
        crw_root / "risk-axis-reviewers/families",
        expect_dir=True,
        error_prefix="ordinary CRW families directory required",
    )
    family_files = sorted(families_dir.glob("*.prompt.md"))
    if not family_files:
        raise ValueError("no CRW family prompt files found")

    desired: list[dict[str, Any]] = []
    basis_shas: dict[str, dict[str, str]] = {}
    family_ids: list[str] = []
    fam_id_by_slug: dict[str, str] = {}
    dim_id_by_rel: dict[str, str] = {}

    for item in constituents:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("invalid constituent entry in observer-dimensions.json")
        rel = f"risk-axis-reviewers/{item['id']}"
        dim_slug = Path(item["id"]).stem.removesuffix(".prompt")
        dim_id_by_rel[rel] = slugify_id("crw-dim", dim_slug)

    for fam_path in family_files:
        rel = fam_path.relative_to(crw_root).as_posix()
        fam_slug = fam_path.stem.removesuffix(".prompt")
        fam_id = slugify_id("crw-fam", fam_slug)
        family_ids.append(fam_id)
        fam_id_by_slug[fam_slug] = fam_id
        text = _read_text(crw_root, rel)
        b_list, b_map = _pin_basis(system, crw_root, fam_id, [rel], sync_digests=sync_digests)
        basis_shas[fam_id] = b_map
        desired.append(
            {
                "id": fam_id,
                "kind": "crw-family",
                "content": {
                    "slug": fam_slug,
                    "title": _first_heading(text, fam_slug),
                    "path": rel,
                    "lines": len(text.splitlines()),
                    "commit": commit,
                },
                "relations": [
                    {"relation": "maps-to", "target": "crw-catalog"},
                    {"relation": "qualifies", "target": "crw-contract-shared"},
                ],
                "basis": b_list,
                "active": True,
            }
        )

    for item in sorted(constituents, key=lambda x: str(x["id"])):
        rel = f"risk-axis-reviewers/{item['id']}"
        dim_id = dim_id_by_rel[rel]
        family_slug = str(item["family"])
        parent_fam_id = fam_id_by_slug.get(family_slug)
        text = _read_text(crw_root, rel)
        cross_targets = _extract_prompt_cross_links(crw_root, rel, text, dim_id_by_rel)

        rels: list[dict[str, str]] = []
        if parent_fam_id is not None:
            rels.append({"relation": "refines", "target": parent_fam_id})
        else:
            rels.append({"relation": "maps-to", "target": "crw-catalog"})
        for target_id in cross_targets:
            if target_id != dim_id:
                rels.append({"relation": "bears-on", "target": target_id})

        b_list, b_map = _pin_basis(
            system,
            crw_root,
            dim_id,
            [rel, "risk-axis-reviewers/observer-dimensions.json"],
            sync_digests=sync_digests,
        )
        basis_shas[dim_id] = b_map
        desired.append(
            {
                "id": dim_id,
                "kind": "crw-constituent",
                "content": {
                    "dimension_id": item["id"],
                    "family": family_slug,
                    "concern": str(item.get("concern", "")),
                    "cue": str(item.get("cue", "")),
                    "title": _first_heading(text, str(item.get("concern", dim_id))),
                    "path": rel,
                    "lines": len(text.splitlines()),
                    "commit": commit,
                },
                "relations": rels,
                "basis": b_list,
                "active": True,
            }
        )

    return desired, basis_shas, family_ids, fam_id_by_slug


def _build_control_records(
    crw_root: Path,
    system: System,
    commit: str,
    *,
    sync_digests: bool,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    ctrl_rel = "risk-axis-reviewers/evals/organization-boundary-controls-v1"
    cases_rel = f"{ctrl_rel}/cases.json"
    cases = strict_json_loads(
        _read_text(crw_root, cases_rel), label="cases.json", require_object=False
    )
    if not isinstance(cases, list) or not cases:
        raise ValueError("organization-boundary-controls-v1/cases.json must be a non-empty array")

    desired: list[dict[str, Any]] = []
    basis_shas: dict[str, dict[str, str]] = {}
    for entry in sorted(cases, key=lambda x: str(x.get("id", ""))):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ValueError("invalid O-control case in cases.json")
        raw_id = entry["id"]
        rec_id = slugify_id("crw-ctrl", raw_id)
        pin_files = [cases_rel]
        for leaf in ("README.md", "assignment.md"):
            cand = f"{ctrl_rel}/{raw_id}/{leaf}"
            if (crw_root / cand).is_file():
                pin_files.append(cand)
        b_list, b_map = _pin_basis(system, crw_root, rec_id, pin_files, sync_digests=sync_digests)
        basis_shas[rec_id] = b_map
        desired.append(
            {
                "id": rec_id,
                "kind": "crw-control",
                "content": {
                    "control_id": raw_id,
                    "purpose": str(entry.get("purpose", "")),
                    "contract": str(entry.get("contract", "")),
                    "expected": entry.get("expected"),
                    "files": pin_files,
                    "commit": commit,
                },
                "relations": [
                    {"relation": "supports", "target": "crw-catalog"},
                    {"relation": "evidenced-by", "target": "crw-esc-protocol"},
                ],
                "basis": b_list,
                "active": True,
            }
        )
    return desired, basis_shas


def _build_catalog_record(
    checked_crw: Path,
    system: System,
    commit: str,
    family_ids: list[str],
    constituent_count: int,
    control_count: int,
    *,
    sync_digests: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    catalog_rels = [{"relation": "maps-to", "target": fid} for fid in sorted(family_ids)]
    catalog_rels.extend(
        [
            {"relation": "maps-to", "target": "crw-contract-shared"},
            {"relation": "maps-to", "target": "crw-esc-protocol"},
            {"relation": "maps-to", "target": "crw-cal-routing"},
            {"relation": "maps-to", "target": "crw-tool-semantic-profiler"},
        ]
    )
    cat_basis, cat_map = _pin_basis(
        system,
        checked_crw,
        "crw-catalog",
        [
            "risk-axis-reviewers/review-dimensions.md",
            "risk-axis-reviewers/observer-dimensions.json",
        ],
        sync_digests=sync_digests,
    )
    record = {
        "id": "crw-catalog",
        "kind": "architecture-composition",
        "content": {
            "format": "g60-composition/1",
            "purpose": (
                "Canonical Code Review Workflows (CRW) taxonomy: families, constituents, "
                "O-controls, escape protocol, model calibration, and semantic profiler."
            ),
            "elements": [
                {"id": fid, "account": f"CRW review family {fid}."} for fid in sorted(family_ids)
            ],
            "relations": [],
            "premises": [],
            "opportunities": [],
            "coverage": {
                "commit": commit,
                "families": len(family_ids),
                "constituents": constituent_count,
                "controls": control_count,
            },
            "unknowns": [],
        },
        "relations": catalog_rels,
        "basis": cat_basis,
        "active": True,
    }
    return record, cat_map


def build_crw_records(
    crw_root: Path,
    system: System,
    *,
    sync_digests: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], dict[str, int]]:
    """Build deterministic CRW records covering families, constituents, O-controls, rules, tools."""
    checked_crw = check_ordinary_ancestors(
        crw_root, expect_dir=True, error_prefix="ordinary CRW repository root required"
    ).resolve(strict=True)
    commit = git_head_sha(checked_crw)

    fam_dim_records, basis_shas, family_ids, fam_by_slug = _build_family_and_constituent_records(
        checked_crw, system, commit, sync_digests=sync_digests
    )
    ctrl_records, ctrl_basis = _build_control_records(
        checked_crw, system, commit, sync_digests=sync_digests
    )
    basis_shas.update(ctrl_basis)

    desired: list[dict[str, Any]] = []
    static_docs: list[tuple[str, str, str, list[dict[str, str]]]] = [
        (
            "crw-contract-shared",
            "crw-contract",
            "risk-axis-reviewers/semantic-review-contract.md",
            [{"relation": "maps-to", "target": "crw-catalog"}],
        ),
        (
            "crw-esc-protocol",
            "crw-escape-rule",
            "docs/review-escapes.md",
            [{"relation": "bears-on", "target": "crw-catalog"}],
        ),
        (
            "crw-cal-routing",
            "crw-calibration",
            "docs/model-calibration.md",
            [{"relation": "scope-restricts", "target": "crw-contract-shared"}],
        ),
        (
            "crw-tool-semantic-profiler",
            "crw-tool-contract",
            "tools/semantic-profiler/README.md",
            [
                {"relation": "supports", "target": target}
                for slug in (
                    "organization-and-communication",
                    "continuing-processes-and-longitudinal-controls",
                )
                if (target := fam_by_slug.get(slug)) is not None
            ]
            or [{"relation": "supports", "target": "crw-catalog"}],
        ),
    ]

    for rec_id, kind, rel, rels in static_docs:
        text = _read_text(checked_crw, rel)
        b_list, b_map = _pin_basis(system, checked_crw, rec_id, [rel], sync_digests=sync_digests)
        basis_shas[rec_id] = b_map
        desired.append(
            {
                "id": rec_id,
                "kind": kind,
                "content": {
                    "title": _first_heading(text, rec_id),
                    "path": rel,
                    "lines": len(text.splitlines()),
                    "commit": commit,
                },
                "relations": rels,
                "basis": b_list,
                "active": True,
            }
        )

    constituent_count = sum(1 for r in fam_dim_records if r["kind"] == "crw-constituent")
    cat_rec, cat_map = _build_catalog_record(
        checked_crw,
        system,
        commit,
        family_ids,
        constituent_count,
        len(ctrl_records),
        sync_digests=sync_digests,
    )
    basis_shas["crw-catalog"] = cat_map
    desired.append(cat_rec)

    desired.extend(fam_dim_records)
    desired.extend(ctrl_records)
    counts = {
        "families": len(family_ids),
        "constituents": constituent_count,
        "controls": len(ctrl_records),
        "total_records": len(desired),
    }
    return desired, basis_shas, counts


def ingest_crw(crw_root: Path, store_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Ingest code-review-workflows into store_root idempotently."""
    checked_store = check_ordinary_ancestors(
        store_root, expect_dir=True, error_prefix="ordinary store root required"
    ).resolve(strict=True)
    system = System(checked_store)
    desired, basis_shas, counts = build_crw_records(
        crw_root,
        system,
        sync_digests=not dry_run,
    )
    receipt = apply_desired_records(
        system,
        desired,
        basis_shas,
        batch_prefix="ingest-crw",
        dry_run=dry_run,
    )
    return {
        "format": INGEST_CRW_FORMAT,
        "crw_root": str(crw_root),
        "store": str(checked_store),
        "counts": counts,
        **receipt,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic CRW ingestor (families, constituents, O-controls, rules).",
        allow_abbrev=False,
    )
    parser.add_argument("--crw-root", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.apply and args.dry_run:
            raise ValueError("--apply and --dry-run cannot be combined")
        report = ingest_crw(args.crw_root, args.store, dry_run=not args.apply)
        print(dump_json(report))
        return 0
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"format": INGEST_CRW_FORMAT, "status": "error", "error": str(exc)},
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
