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
    compute_digest_basis,
    dump_json,
    file_digest_entry,
    git_head_sha,
    slugify_id,
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
    crw_root: Path,
    record_id: str,
    rel_paths: list[str],
) -> tuple[list[str], dict[str, str], bytes]:
    files = [file_digest_entry(crw_root, rel) for rel in sorted(set(rel_paths))]
    basis_rel, encoded, basis_sha = compute_digest_basis(
        record_id,
        source_repo=crw_root.name,
        files=files,
    )
    return [basis_rel], {basis_rel: basis_sha}, encoded


def _build_family_and_constituent_records(
    crw_root: Path,
    commit: str,
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, str]],
    dict[str, bytes],
    list[str],
    dict[str, str],
    dict[str, str],
]:
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
    pending_digests: dict[str, bytes] = {}
    family_ids: list[str] = []
    fam_id_by_slug: dict[str, str] = {}
    dim_id_by_rel: dict[str, str] = {}
    dim_id_by_slug: dict[str, str] = {}

    for item in constituents:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("invalid constituent entry in observer-dimensions.json")
        rel = f"risk-axis-reviewers/{item['id']}"
        dim_slug = Path(item["id"]).stem.removesuffix(".prompt")
        dim_id = slugify_id("crw-dim", dim_slug)
        dim_id_by_rel[rel] = dim_id
        dim_id_by_slug[dim_slug] = dim_id

    for fam_path in family_files:
        rel = fam_path.relative_to(crw_root).as_posix()
        fam_slug = fam_path.stem.removesuffix(".prompt")
        fam_id = slugify_id("crw-fam", fam_slug)
        family_ids.append(fam_id)
        fam_id_by_slug[fam_slug] = fam_id
        text = _read_text(crw_root, rel)
        b_list, b_map, b_bytes = _pin_basis(crw_root, fam_id, [rel])
        basis_shas[fam_id] = b_map
        pending_digests[fam_id] = b_bytes
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

        b_list, b_map, b_bytes = _pin_basis(
            crw_root,
            dim_id,
            [rel, "risk-axis-reviewers/observer-dimensions.json"],
        )
        basis_shas[dim_id] = b_map
        pending_digests[dim_id] = b_bytes
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

    return desired, basis_shas, pending_digests, family_ids, fam_id_by_slug, dim_id_by_slug


CRW_COVERAGE_MANIFEST_FORMAT = "crw-control-coverage/1"


def _validate_slug_list(raw: Any, label: str) -> list[str]:
    if not isinstance(raw, list) or any(not isinstance(v, str) or not v.strip() for v in raw):
        raise ValueError(f"{label} must be a list of non-empty strings")
    return [v.strip() for v in raw]


def _load_coverage_manifest(
    crw_root: Path,
    ctrl_rel: str,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    manifest_rel = f"{ctrl_rel}/coverage-manifest.json"
    candidate = crw_root / manifest_rel
    if not candidate.exists() and not candidate.is_symlink():
        return {}, None
    raw = _read_text(crw_root, manifest_rel)
    doc = strict_json_loads(raw, label="coverage-manifest.json")
    if not isinstance(doc, dict) or doc.get("format") != CRW_COVERAGE_MANIFEST_FORMAT:
        raise ValueError(
            f"coverage-manifest.json must be a JSON object with format "
            f"{CRW_COVERAGE_MANIFEST_FORMAT!r}"
        )
    controls_raw = doc.get("controls")
    if not isinstance(controls_raw, dict):
        raise ValueError("coverage-manifest.json 'controls' must be a JSON object")

    parsed: dict[str, dict[str, Any]] = {}
    for ctrl_id, entry in controls_raw.items():
        if not isinstance(ctrl_id, str) or not ctrl_id.strip():
            raise ValueError("coverage-manifest.json control IDs must be non-empty strings")
        if not isinstance(entry, dict):
            raise ValueError(f"coverage-manifest.json entry for {ctrl_id!r} must be an object")
        unknown_keys = set(entry) - {"families", "constituents", "escape_class"}
        if unknown_keys:
            raise ValueError(
                f"coverage-manifest.json entry {ctrl_id!r} has unknown keys: {sorted(unknown_keys)}"
            )
        fams = _validate_slug_list(entry.get("families", []), f"controls[{ctrl_id!r}].families")
        dims = _validate_slug_list(
            entry.get("constituents", []), f"controls[{ctrl_id!r}].constituents"
        )
        esc = entry.get("escape_class")
        if esc is not None and (not isinstance(esc, str) or not esc.strip()):
            raise ValueError(
                f"controls[{ctrl_id!r}].escape_class must be a non-empty string when present"
            )
        parsed[ctrl_id] = {
            "families": fams,
            "constituents": dims,
            "escape_class": esc.strip() if isinstance(esc, str) else None,
        }
    return parsed, manifest_rel


def _build_control_records(
    crw_root: Path,
    commit: str,
    fam_id_by_slug: dict[str, str],
    dim_id_by_slug: dict[str, str],
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, str]],
    dict[str, bytes],
    dict[str, Any],
]:
    ctrl_rel = "risk-axis-reviewers/evals/organization-boundary-controls-v1"
    cases_rel = f"{ctrl_rel}/cases.json"
    cases = strict_json_loads(
        _read_text(crw_root, cases_rel), label="cases.json", require_object=False
    )
    if not isinstance(cases, list) or not cases:
        raise ValueError("organization-boundary-controls-v1/cases.json must be a non-empty array")

    manifest_controls, manifest_rel = _load_coverage_manifest(crw_root, ctrl_rel)
    desired: list[dict[str, Any]] = []
    basis_shas: dict[str, dict[str, str]] = {}
    pending_digests: dict[str, bytes] = {}
    covered_dim_ids: set[str] = set()
    mapped_controls = 0

    for entry in sorted(cases, key=lambda x: str(x.get("id", ""))):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise ValueError("invalid O-control case in cases.json")
        raw_id = entry["id"]
        rec_id = slugify_id("crw-ctrl", raw_id)
        pin_files = [cases_rel]
        if manifest_rel is not None and raw_id in manifest_controls:
            pin_files.append(manifest_rel)
        for leaf in ("README.md", "assignment.md"):
            cand = f"{ctrl_rel}/{raw_id}/{leaf}"
            if (crw_root / cand).is_file():
                pin_files.append(cand)

        rels: list[dict[str, str]] = [
            {"relation": "supports", "target": "crw-catalog"},
            {"relation": "evidenced-by", "target": "crw-esc-protocol"},
        ]
        content: dict[str, Any] = {
            "control_id": raw_id,
            "purpose": str(entry.get("purpose", "")),
            "contract": str(entry.get("contract", "")),
            "expected": entry.get("expected"),
            "files": pin_files,
            "commit": commit,
        }
        cov = manifest_controls.get(raw_id)
        if cov is not None:
            mapped_controls += 1
            for fam_slug in cov["families"]:
                fam_id = fam_id_by_slug.get(fam_slug)
                if fam_id is None:
                    raise ValueError(
                        f"coverage-manifest.json control {raw_id!r} references unknown family "
                        f"{fam_slug!r}"
                    )
                rels.append({"relation": "supports", "target": fam_id})
            for dim_slug in cov["constituents"]:
                dim_id = dim_id_by_slug.get(dim_slug)
                if dim_id is None:
                    raise ValueError(
                        f"coverage-manifest.json control {raw_id!r} references unknown constituent "
                        f"{dim_slug!r}"
                    )
                rels.append({"relation": "supports", "target": dim_id})
                covered_dim_ids.add(dim_id)
            content["families"] = list(cov["families"])
            content["constituents"] = list(cov["constituents"])
            if cov["escape_class"] is not None:
                content["escape_class"] = cov["escape_class"]

        b_list, b_map, b_bytes = _pin_basis(crw_root, rec_id, pin_files)
        basis_shas[rec_id] = b_map
        pending_digests[rec_id] = b_bytes
        desired.append(
            {
                "id": rec_id,
                "kind": "crw-control",
                "content": content,
                "relations": rels,
                "basis": b_list,
                "active": True,
            }
        )

    zero_ctrl_dims = (
        sorted(set(dim_id_by_slug.values()) - covered_dim_ids) if manifest_rel is not None else []
    )
    coverage_info: dict[str, Any] = {
        "manifest_present": manifest_rel is not None,
        "manifest_path": manifest_rel,
        "mapped_controls": mapped_controls,
        "covered_constituents": len(covered_dim_ids),
        "zero_control_constituents": zero_ctrl_dims,
    }
    return desired, basis_shas, pending_digests, coverage_info


def _build_catalog_record(
    checked_crw: Path,
    commit: str,
    family_ids: list[str],
    constituent_count: int,
    control_count: int,
    coverage_info: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str], bytes]:
    # Families and crw-contract-shared point one-directionally (`maps-to -> crw-catalog`)
    # so the taxonomy stays acyclic (`structural_scc_count == 0`).
    catalog_rels = [
        {"relation": "maps-to", "target": "crw-esc-protocol"},
        {"relation": "maps-to", "target": "crw-cal-routing"},
        {"relation": "maps-to", "target": "crw-tool-semantic-profiler"},
    ]
    cat_files = [
        "risk-axis-reviewers/review-dimensions.md",
        "risk-axis-reviewers/observer-dimensions.json",
    ]
    if coverage_info.get("manifest_path"):
        cat_files.append(str(coverage_info["manifest_path"]))
    cat_basis, cat_map, cat_bytes = _pin_basis(checked_crw, "crw-catalog", cat_files)
    coverage_block: dict[str, Any] = {
        "commit": commit,
        "families": len(family_ids),
        "constituents": constituent_count,
        "controls": control_count,
        "mapped_controls": int(coverage_info["mapped_controls"]),
        "covered_constituents": int(coverage_info["covered_constituents"]),
        "zero_control_constituent_count": len(coverage_info["zero_control_constituents"]),
    }
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
            "coverage": coverage_block,
            "unknowns": list(coverage_info["zero_control_constituents"]),
        },
        "relations": catalog_rels,
        "basis": cat_basis,
        "active": True,
    }
    return record, cat_map, cat_bytes


def build_crw_records(
    crw_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], dict[str, bytes], dict[str, Any]]:
    """Purely compute deterministic CRW records and in-memory digest pins (zero disk writes)."""
    checked_crw = check_ordinary_ancestors(
        crw_root, expect_dir=True, error_prefix="ordinary CRW repository root required"
    ).resolve(strict=True)
    commit = git_head_sha(checked_crw)

    fam_dim_records, basis_shas, pending_digests, family_ids, fam_by_slug, dim_by_slug = (
        _build_family_and_constituent_records(checked_crw, commit)
    )
    ctrl_records, ctrl_basis, ctrl_digests, coverage_info = _build_control_records(
        checked_crw, commit, fam_by_slug, dim_by_slug
    )
    basis_shas.update(ctrl_basis)
    pending_digests.update(ctrl_digests)

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
        b_list, b_map, b_bytes = _pin_basis(checked_crw, rec_id, [rel])
        basis_shas[rec_id] = b_map
        pending_digests[rec_id] = b_bytes
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
    cat_rec, cat_map, cat_bytes = _build_catalog_record(
        checked_crw,
        commit,
        family_ids,
        constituent_count,
        len(ctrl_records),
        coverage_info,
    )
    basis_shas["crw-catalog"] = cat_map
    pending_digests["crw-catalog"] = cat_bytes
    desired.append(cat_rec)

    desired.extend(fam_dim_records)
    desired.extend(ctrl_records)
    counts: dict[str, Any] = {
        "families": len(family_ids),
        "constituents": constituent_count,
        "controls": len(ctrl_records),
        "mapped_controls": int(coverage_info["mapped_controls"]),
        "covered_constituents": int(coverage_info["covered_constituents"]),
        "zero_control_constituents": list(coverage_info["zero_control_constituents"]),
        "total_records": len(desired),
    }
    return desired, basis_shas, pending_digests, counts


def ingest_crw(crw_root: Path, store_root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Ingest code-review-workflows into store_root idempotently."""
    checked_store = check_ordinary_ancestors(
        store_root, expect_dir=True, error_prefix="ordinary store root required"
    ).resolve(strict=True)
    system = System(checked_store)
    desired, basis_shas, pending_digests, counts = build_crw_records(crw_root)
    receipt = apply_desired_records(
        system,
        desired,
        basis_shas,
        batch_prefix="ingest-crw",
        pending_digests=pending_digests,
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
