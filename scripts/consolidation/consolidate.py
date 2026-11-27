#!/usr/bin/env python3
"""Bidirectional Continuous Consolidation ETL (Code <-> Store) CLI."""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
for _entry in (str(SRC_ROOT), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai_information.core.ownership import packet_id, relative_path  # noqa: E402
from managed_ai_information.core.storage import exclusive  # noqa: E402
from managed_ai_information.platform import (  # noqa: E402
    check_ordinary_ancestors,
    is_ordinary_file_stat,
    strict_json_loads,
)
from managed_ai_information.store.records.record_batch import put_batch  # noqa: E402
from managed_ai_information.store.system import System  # noqa: E402
from scripts.ci.semantic_profile import (  # noqa: E402
    DEFAULT_RELATIONSHIP_BUDGETS,
    build_baseline,
    check_budget,
    load_baseline,
    profile_repository,
)
from scripts.consolidation._common import (  # noqa: E402
    DIGEST_REL_DIR,
    apply_desired_records,
    canonical_bytes,
    compute_digest_basis,
    dump_json,
    file_digest_entry,
    git_head_sha,
    load_existing_records,
    record_matches_desired,
    slugify_id,
    write_digest_basis,
    write_digest_basis_bytes,
)
from scripts.consolidation.ingest_crw import build_crw_records  # noqa: E402
from scripts.consolidation.ingest_repo import build_repo_records  # noqa: E402

CONSOLIDATION_PLAN_FORMAT = "managed-ai-consolidation-plan/1"
CONSOLIDATION_STATUS_FORMAT = "managed-ai-consolidation-status/1"
OBLIGATION_RECONCILE_FORMAT = "managed-ai-obligation-reconcile/1"

_SCOPE_RE = re.compile(r"^semantic-budget:([^:]+):([a-z_]+)<=(\d+)$")
_MANAGED_KINDS_BY_MODE: dict[str, frozenset[str]] = {
    "repo": frozenset({"code-module", "code-facade", "semantic-hotspot", "profile-snapshot"}),
    "crw": frozenset(
        {
            "crw-family",
            "crw-constituent",
            "crw-control",
            "crw-contract",
            "crw-escape-rule",
            "crw-calibration",
            "crw-tool-contract",
            "architecture-composition",
        }
    ),
}


def _open_store(store_root: Path) -> System:
    checked = check_ordinary_ancestors(
        store_root, expect_dir=True, error_prefix="ordinary store root required"
    ).resolve(strict=True)
    return System(checked)


def sync_digests(store_root: Path, repo_root: Path) -> dict[str, Any]:
    """Re-hash tracked source files referenced by work/sources/repo-digests/*.json under lock."""
    system = _open_store(store_root)
    checked_repo = check_ordinary_ancestors(
        repo_root, expect_dir=True, error_prefix="ordinary repository root required"
    ).resolve(strict=True)
    digest_dir = system.work / DIGEST_REL_DIR
    if not digest_dir.exists() and not digest_dir.is_symlink():
        return {"checked": 0, "updated": [], "missing": [], "unchanged": 0}
    checked_digest_dir = check_ordinary_ancestors(
        digest_dir, expect_dir=True, error_prefix="ordinary digest directory required"
    )

    checked = 0
    updated: list[str] = []
    missing: list[str] = []
    unchanged = 0

    with exclusive(system.ex.root / "owner.lock"):
        for pin_file in sorted(checked_digest_dir.glob("*.json")):
            info = pin_file.lstat()
            if not is_ordinary_file_stat(info):
                raise ValueError(f"ordinary digest pin file required: {pin_file}")
            pin = strict_json_loads(pin_file.read_text(encoding="utf-8"), label=pin_file.name)
            if not isinstance(pin, dict) or pin.get("format") != "managed-ai-source-digest-pin/1":
                continue
            checked += 1
            rec_id = packet_id(str(pin.get("record_id", "")))
            if pin_file.name != f"{rec_id}.json":
                raise ValueError(f"digest pin filename mismatch: {pin_file.name} vs {rec_id}.json")
            source_repo = str(pin.get("source_repo", checked_repo.name))
            refreshed: list[dict[str, Any]] = []
            for item in pin.get("files", []):
                if not isinstance(item, dict):
                    raise ValueError(f"invalid digest file entry in {pin_file.name}")
                rel = relative_path(str(item.get("path", "")))
                candidate = checked_repo / rel
                if not candidate.exists() and not candidate.is_symlink():
                    missing.append(f"{rec_id}:{rel}")
                    refreshed.append({"path": rel, "bytes": 0, "sha256": None, "missing": True})
                    continue
                refreshed.append(file_digest_entry(checked_repo, rel))
            _, changed, _ = write_digest_basis(
                system,
                rec_id,
                source_repo=source_repo,
                files=refreshed,
            )
            if changed:
                updated.append(rec_id)
            else:
                unchanged += 1

    return {
        "checked": checked,
        "updated": updated,
        "missing": missing,
        "unchanged": unchanged,
    }


def _stale_record_ids(system: System, records: dict[str, dict[str, Any]]) -> list[str]:
    stale: list[str] = []
    for rec_id, rec in sorted(records.items()):
        if system.record_store.basis_changes(rec):
            stale.append(rec_id)
    return stale


_TEST_MANAGED_KINDS: frozenset[str] = frozenset({"test-suite", "verification-unknown"})


def _managed_kinds(mode: str, *, include_tests: bool) -> frozenset[str]:
    base = _MANAGED_KINDS_BY_MODE[mode]
    return base | _TEST_MANAGED_KINDS if (mode == "repo" and include_tests) else base


def _build_mode_records(
    mode: str,
    repo_root: Path,
    system: System,
    *,
    package_rel: str,
    include_tests: bool = False,
    tests_rel: str = "tests",
    existing_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], dict[str, bytes]]:
    if mode == "repo":
        desired, basis_shas, pending_digests, _ = build_repo_records(
            repo_root,
            system,
            package_rel=package_rel,
            include_tests=include_tests,
            tests_rel=tests_rel,
            existing_ids=existing_ids,
        )
        return desired, basis_shas, pending_digests
    if mode == "crw":
        if include_tests:
            raise ValueError("--include-tests is only supported with --mode repo")
        desired, basis_shas, pending_digests, _ = build_crw_records(repo_root)
        return desired, basis_shas, pending_digests
    raise ValueError(f"unsupported consolidation mode: {mode}")


def plan_consolidation(
    store_root: Path,
    repo_root: Path,
    *,
    mode: str = "repo",
    package_rel: str = "src/managed_ai_information",
    include_tests: bool = False,
    tests_rel: str = "tests",
) -> dict[str, Any]:
    """Read-only Code -> Store reconciliation plan using native basis_changes() + desired state."""
    system = _open_store(store_root)
    existing = load_existing_records(system)
    stale_basis = _stale_record_ids(system, existing)
    desired, basis_shas, _ = _build_mode_records(
        mode,
        repo_root,
        system,
        package_rel=package_rel,
        include_tests=include_tests,
        tests_rel=tests_rel,
        existing_ids=set(existing),
    )

    desired_by_id = {rec["id"]: rec for rec in desired}
    added: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    for rec_id, rec in sorted(desired_by_id.items()):
        prev = existing.get(rec_id)
        if prev is None:
            added.append(rec_id)
        elif rec_id in stale_basis or not record_matches_desired(
            prev, rec, basis_shas.get(rec_id, {})
        ):
            changed.append(rec_id)
        else:
            unchanged.append(rec_id)

    managed_kinds = _managed_kinds(mode, include_tests=include_tests)
    retired = sorted(
        rec_id
        for rec_id, rec in existing.items()
        if rec.get("kind") in managed_kinds
        and bool(rec.get("active", True))
        and rec_id not in desired_by_id
    )

    return {
        "format": CONSOLIDATION_PLAN_FORMAT,
        "mode": mode,
        "include_tests": include_tests,
        "store": str(system.root),
        "repo": str(repo_root),
        "added": added,
        "changed": changed,
        "stale_basis": stale_basis,
        "retired": retired,
        "unchanged_count": len(unchanged),
    }


def apply_consolidation(
    store_root: Path,
    repo_root: Path,
    *,
    mode: str = "repo",
    package_rel: str = "src/managed_ai_information",
    include_tests: bool = False,
    tests_rel: str = "tests",
) -> dict[str, Any]:
    """Sync digests, apply changed/added records, soft-retire deleted ones, reindex + lint."""
    system = _open_store(store_root)
    existing = load_existing_records(system)
    desired, basis_shas, pending_digests = _build_mode_records(
        mode,
        repo_root,
        system,
        package_rel=package_rel,
        include_tests=include_tests,
        tests_rel=tests_rel,
        existing_ids=set(existing),
    )
    desired_ids = {rec["id"] for rec in desired}
    managed_kinds = _managed_kinds(mode, include_tests=include_tests)

    retired_ids: list[str] = []
    for rec_id, rec in sorted(existing.items()):
        if (
            rec.get("kind") in managed_kinds
            and bool(rec.get("active", True))
            and rec_id not in desired_ids
        ):
            retired_ids.append(rec_id)
            desired.append(
                {
                    "id": rec_id,
                    "kind": str(rec["kind"]),
                    "content": rec["content"],
                    "relations": rec.get("relations", []),
                    "basis": [str(item["path"]) for item in rec.get("basis", [])],
                    "active": False,
                }
            )
            basis_shas[rec_id] = {
                str(item["path"]): str(item.get("sha256") or "") for item in rec.get("basis", [])
            }

    receipt = apply_desired_records(
        system,
        desired,
        basis_shas,
        batch_prefix=f"consolidate-{mode}",
        pending_digests=pending_digests,
        dry_run=False,
    )
    return {
        "format": CONSOLIDATION_PLAN_FORMAT,
        "mode": mode,
        "retired": retired_ids,
        **receipt,
    }


def plan_obligations(
    store_root: Path,
    repo_root: Path,
    *,
    baseline_path: Path | None = None,
    package_rel: str = "src/managed_ai_information",
    apply: bool = False,
) -> dict[str, Any]:
    """Emit schema-valid open native obligation records for semantic budget breaches."""
    system = _open_store(store_root)
    checked_repo = check_ordinary_ancestors(
        repo_root, expect_dir=True, error_prefix="ordinary repository root required"
    ).resolve(strict=True)
    if baseline_path is not None:
        baseline = load_baseline(baseline_path)
        profile = profile_repository(checked_repo, package_rel=baseline["package_dir"])
    else:
        profile = profile_repository(checked_repo, package_rel=package_rel)
        baseline = build_baseline(profile)
        baseline["allowances"] = {}
        baseline["relationship_budgets"] = dict(DEFAULT_RELATIONSHIP_BUDGETS)

    check = check_budget(profile, baseline)
    existing = load_existing_records(system)
    desired: list[dict[str, Any]] = []
    basis_shas: dict[str, dict[str, str]] = {}
    pending_digests: dict[str, bytes] = {}

    for viol in check["violations"]:
        v_path = str(viol["path"])
        metric = str(viol["metric"])
        ceiling = int(viol["ceiling"])
        actual = int(viol["actual"])
        obl_id = slugify_id("obl-sem", f"{v_path}-{metric}")
        scope = f"semantic-budget:{v_path}:{metric}<={ceiling}"

        rels: list[dict[str, str]] = []
        if "goal" in existing:
            rels.append({"relation": "supports", "target": "goal"})
        b_list: list[str] = []
        b_map: dict[str, str] = {}
        if v_path != "<relationships>" and (checked_repo / v_path).is_file():
            digest_entry = file_digest_entry(checked_repo, v_path)
            b_rel, b_bytes, b_sha = compute_digest_basis(
                obl_id, source_repo=checked_repo.name, files=[digest_entry]
            )
            b_list = [b_rel]
            b_map = {b_rel: b_sha}
            pending_digests[obl_id] = b_bytes

        desired.append(
            {
                "id": obl_id,
                "kind": "obligation",
                "content": {
                    "text": (
                        f"Reduce {v_path} {metric} from {actual} to <= {ceiling} "
                        "(verified via semantic_profile.py --check-budget)."
                    ),
                    "disposition": "open",
                    "source_scope": scope,
                },
                "relations": rels,
                "basis": b_list,
                "active": True,
            }
        )
        basis_shas[obl_id] = b_map

    receipt = apply_desired_records(
        system,
        desired,
        basis_shas,
        batch_prefix="plan-obligations",
        pending_digests=pending_digests,
        dry_run=not apply,
    )
    return {
        "format": OBLIGATION_RECONCILE_FORMAT,
        "action": "plan-obligations",
        "violation_count": check["violation_count"],
        "planned_obligations": [r["id"] for r in desired],
        **receipt,
    }


def _measure_scope_metric(
    profile: dict[str, Any],
    target_path: str,
    metric: str,
) -> int | None:
    if target_path == "<relationships>":
        rel_summary = profile.get("relationships", {}).get("summary", {})
        return int(rel_summary[metric]) if metric in rel_summary else None
    for mod in profile["modules"]:
        if mod["path"] == target_path:
            return int(mod[metric]) if metric in mod else None
    return None


def _evaluate_obligations(
    existing: dict[str, dict[str, Any]],
    profile: dict[str, Any],
    checked_repo: Path,
    commit: str,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, bytes],
]:
    fulfilled: list[dict[str, Any]] = []
    still_open: list[dict[str, Any]] = []
    regressed: list[dict[str, Any]] = []
    missing_targets: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    pending_digests: dict[str, bytes] = {}

    for rec_id, rec in sorted(existing.items()):
        if rec.get("kind") != "obligation" or not rec_id.startswith("obl-sem-"):
            continue
        content = rec.get("content")
        if not isinstance(content, dict):
            continue
        match = _SCOPE_RE.match(str(content.get("source_scope", "")))
        if not match:
            continue
        target_path, metric, ceiling_str = match.groups()
        ceiling = int(ceiling_str)
        disposition = str(content.get("disposition", "open"))
        actual = _measure_scope_metric(profile, target_path, metric)
        if actual is None:
            missing_targets.append(
                {
                    "id": rec_id,
                    "path": target_path,
                    "metric": metric,
                    "disposition": disposition,
                    "reason": "target module missing from profile",
                }
            )
            continue

        if disposition == "open":
            if actual <= ceiling:
                account = (
                    f"Machine-verified at commit {commit}: {target_path} {metric}={actual} "
                    f"<= ceiling {ceiling}."
                )
                fulfilled.append(
                    {"id": rec_id, "path": target_path, "metric": metric, "actual": actual}
                )
                new_content = dict(content)
                new_content["disposition"] = "fulfilled"
                new_content["account"] = account
                basis_paths = [str(item["path"]) for item in rec.get("basis", [])]
                if target_path != "<relationships>" and (checked_repo / target_path).is_file():
                    b_rel, b_bytes, _ = compute_digest_basis(
                        rec_id,
                        source_repo=checked_repo.name,
                        files=[file_digest_entry(checked_repo, target_path)],
                    )
                    basis_paths = [b_rel]
                    pending_digests[rec_id] = b_bytes
                operations.append(
                    {
                        "id": rec_id,
                        "expected_revision": int(rec["revision"]),
                        "expected_sha256": str(rec["sha256"]),
                        "mode": "replace",
                        "record": {
                            "kind": "obligation",
                            "content": new_content,
                            "relations": rec.get("relations", []),
                            "basis": basis_paths,
                            "active": True,
                        },
                    }
                )
            else:
                still_open.append(
                    {
                        "id": rec_id,
                        "path": target_path,
                        "metric": metric,
                        "actual": actual,
                        "ceiling": ceiling,
                    }
                )
        elif disposition == "fulfilled" and actual > ceiling:
            regressed.append(
                {
                    "id": rec_id,
                    "path": target_path,
                    "metric": metric,
                    "actual": actual,
                    "ceiling": ceiling,
                }
            )

    return fulfilled, still_open, regressed, missing_targets, operations, pending_digests


def reconcile_obligations(
    store_root: Path,
    repo_root: Path,
    *,
    package_rel: str = "src/managed_ai_information",
    apply: bool = False,
) -> dict[str, Any]:
    """Machine-verify open semantic obligations against current AST metrics under owner.lock."""
    system = _open_store(store_root)
    checked_repo = check_ordinary_ancestors(
        repo_root, expect_dir=True, error_prefix="ordinary repository root required"
    ).resolve(strict=True)
    profile = profile_repository(checked_repo, package_rel=package_rel)
    commit = git_head_sha(checked_repo)

    if not apply:
        existing = load_existing_records(system)
        fulfilled, still_open, regressed, missing_targets, _, _ = _evaluate_obligations(
            existing, profile, checked_repo, commit
        )
        return {
            "format": OBLIGATION_RECONCILE_FORMAT,
            "action": "reconcile-obligations",
            "applied": False,
            "fulfilled": fulfilled,
            "still_open": still_open,
            "regressed": regressed,
            "missing_targets": missing_targets,
        }

    with exclusive(system.ex.root / "owner.lock"):
        existing = load_existing_records(system)
        fulfilled, still_open, regressed, missing_targets, operations, pending_digests = (
            _evaluate_obligations(existing, profile, checked_repo, commit)
        )
        applied = False
        if operations:
            for digest_id, encoded in sorted(pending_digests.items()):
                write_digest_basis_bytes(system, digest_id, encoded)
            batch_hash = hashlib.sha256(canonical_bytes(operations)).hexdigest()[:12]
            spec = {
                "format": "managed-ai-batch-update/1",
                "id": slugify_id("reconcile-obl", batch_hash),
                "operations": operations,
            }
            put_batch(system, spec)
            system.reindex()
            applied = True

    return {
        "format": OBLIGATION_RECONCILE_FORMAT,
        "action": "reconcile-obligations",
        "applied": applied,
        "fulfilled": fulfilled,
        "still_open": still_open,
        "regressed": regressed,
        "missing_targets": missing_targets,
    }


def consolidation_status(store_root: Path) -> dict[str, Any]:
    """Summarize record counts, obligations, and basis staleness in store_root."""
    system = _open_store(store_root)
    existing = load_existing_records(system)
    by_kind = Counter(str(rec.get("kind", "")) for rec in existing.values())
    open_obls = sorted(
        rec_id
        for rec_id, rec in existing.items()
        if rec.get("kind") == "obligation"
        and isinstance(rec.get("content"), dict)
        and rec["content"].get("disposition") == "open"
    )
    fulfilled_obls = sorted(
        rec_id
        for rec_id, rec in existing.items()
        if rec.get("kind") == "obligation"
        and isinstance(rec.get("content"), dict)
        and rec["content"].get("disposition") == "fulfilled"
    )
    stale = _stale_record_ids(system, existing)
    return {
        "format": CONSOLIDATION_STATUS_FORMAT,
        "store": str(system.root),
        "total_records": len(existing),
        "by_kind": dict(sorted(by_kind.items())),
        "open_obligations": open_obls,
        "fulfilled_obligations": fulfilled_obls,
        "stale_basis_records": stale,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bidirectional Continuous Consolidation ETL (Code <-> Store).",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync-digests", allow_abbrev=False)
    p_sync.add_argument("--store", type=Path, required=True)
    p_sync.add_argument("--repo", type=Path, required=True)

    for name in ("plan", "apply"):
        p_cmd = sub.add_parser(name, allow_abbrev=False)
        p_cmd.add_argument("--store", type=Path, required=True)
        p_cmd.add_argument("--repo", type=Path, required=True)
        p_cmd.add_argument("--mode", choices=("repo", "crw"), default="repo")
        p_cmd.add_argument("--package-dir", default="src/managed_ai_information")
        p_cmd.add_argument("--include-tests", action="store_true")
        p_cmd.add_argument("--tests-dir", default="tests")

    p_po = sub.add_parser("plan-obligations", allow_abbrev=False)
    p_po.add_argument("--store", type=Path, required=True)
    p_po.add_argument("--repo", type=Path, required=True)
    p_po.add_argument("--baseline", type=Path, default=None)
    p_po.add_argument("--package-dir", default="src/managed_ai_information")
    p_po.add_argument("--apply", action="store_true")

    p_ro = sub.add_parser("reconcile-obligations", allow_abbrev=False)
    p_ro.add_argument("--store", type=Path, required=True)
    p_ro.add_argument("--repo", type=Path, required=True)
    p_ro.add_argument("--package-dir", default="src/managed_ai_information")
    p_ro.add_argument("--apply", action="store_true")

    p_st = sub.add_parser("status", allow_abbrev=False)
    p_st.add_argument("--store", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "sync-digests":
            report = sync_digests(args.store, args.repo)
        elif args.command == "plan":
            report = plan_consolidation(
                args.store,
                args.repo,
                mode=args.mode,
                package_rel=args.package_dir,
                include_tests=args.include_tests,
                tests_rel=args.tests_dir,
            )
        elif args.command == "apply":
            report = apply_consolidation(
                args.store,
                args.repo,
                mode=args.mode,
                package_rel=args.package_dir,
                include_tests=args.include_tests,
                tests_rel=args.tests_dir,
            )
        elif args.command == "plan-obligations":
            report = plan_obligations(
                args.store,
                args.repo,
                baseline_path=args.baseline,
                package_rel=args.package_dir,
                apply=args.apply,
            )
        elif args.command == "reconcile-obligations":
            report = reconcile_obligations(
                args.store, args.repo, package_rel=args.package_dir, apply=args.apply
            )
        else:
            report = consolidation_status(args.store)
        print(dump_json(report))
        return 0
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "error", "command": args.command, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
