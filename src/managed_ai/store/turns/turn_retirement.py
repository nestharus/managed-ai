"""Guarded two-phase retirement of completed turn working copies under work/.system/turns/.

Implements the MAINTENANCE.md working-copy retirement contract without touching
exchange/packets/, exchange/objects/, or native records.
"""

import json
import os
import stat
from pathlib import Path
from typing import Any

from managed_ai.core.formats import TURN_RETIRE_PLAN_FORMAT as PLAN_FORMAT
from managed_ai.core.formats import TURN_RETIRE_RECEIPT_FORMAT as RECEIPT_FORMAT
from managed_ai.core.ownership import packet_id
from managed_ai.core.storage import (
    encode,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    is_reparse_or_symlink,
    now,
    open_directory,
    sha,
    sync_dir,
    write_json,
)
from managed_ai.store.protocol.identities import digest, exact_fields, nonempty, require
from managed_ai.store.turns.turn_retirement_checks import (
    TURNS_RELATIVE,
    _check_live_references,
    _check_packet_and_return,
    _check_publications,
    _check_working_copy_integrity,
    _lstat_or_none,
    _read_strict_file,
)
from managed_ai.store.turns.turn_retirement_scan import (
    _collect_attention_references,
    _collect_cross_packet_references,
    _collect_interpretation_references,
    _collect_record_references,
    _collect_uncommitted_turn_references,
    _scan_turn_directory,
)

RECEIPT_DIRECTORY = ".system/retirements"

_PLAN_REQUIRED = {
    "format",
    "instance_id",
    "selection_mode",
    "nominated_turns",
    "eligible",
    "ineligible",
    "reclaimable_bytes",
    "reclaimable_files",
    "scope",
    "plan_sha256",
}
_ELIGIBLE_FIELDS = {
    "turn",
    "working_path",
    "request_sha256",
    "context_sha256",
    "return_sha256",
    "declared_targets",
    "published_targets",
    "files",
    "bytes",
    "working_dir_sha256",
}
_RECEIPT_REQUIRED = {
    "format",
    "instance_id",
    "plan_sha256",
    "reason",
    "complete",
    "retired_at",
    "retired_turns",
    "turns",
    "retired_count",
    "files_removed",
    "bytes_reclaimed",
    "post_verification",
    "receipt_path",
    "scope",
}


def _evaluate_turn(
    system: Any,
    turn: str,
    turn_dir: Path,
    *,
    record_refs: dict[str, list[dict[str, Any]]],
    record_scan_errors: list[str],
    packet_refs: dict[str, list[dict[str, str]]],
    packet_scan_errors: list[str],
    attention_refs: dict[str, list[dict[str, str]]],
    attention_error: str | None,
    interp_refs: dict[str, list[dict[str, str]]],
    interp_errors: list[str],
    uncommitted_refs: dict[str, list[dict[str, str]]],
    uncommitted_errors: list[str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    reasons: list[str] = []
    details: dict[str, Any] = {}

    def refuse(code: str, detail: Any = None) -> None:
        if code not in reasons:
            reasons.append(code)
        if detail is not None:
            details[code] = detail

    file_count, total_bytes, dir_sha, dir_issues, file_hashes, dir_paths = _scan_turn_directory(
        turn_dir
    )
    if dir_issues:
        refuse("working_dir_unsafe_entries", dir_issues)

    packet_dir, binding, request, returned = _check_packet_and_return(system, turn, refuse)
    prep_state, declared_targets, complete_targets = _check_publications(
        turn,
        turn_dir,
        packet_dir,
        binding,
        request,
        returned,
        refuse,
    )
    _check_live_references(
        turn,
        refuse,
        record_refs=record_refs,
        record_scan_errors=record_scan_errors,
        packet_refs=packet_refs,
        packet_scan_errors=packet_scan_errors,
        attention_refs=attention_refs,
        attention_error=attention_error,
        interp_refs=interp_refs,
        interp_errors=interp_errors,
        uncommitted_refs=uncommitted_refs,
        uncommitted_errors=uncommitted_errors,
    )
    if binding is not None and request is not None and returned is not None:
        _check_working_copy_integrity(
            system,
            turn,
            turn_dir,
            binding,
            request,
            returned,
            prep_state,
            declared_targets,
            dir_issues,
            file_hashes,
            dir_paths,
            refuse,
        )

    if reasons or binding is None or returned is None:
        if not reasons:
            refuse("packet_invalid", "missing validated binding or return")
        ineligible_row: dict[str, Any] = {"turn": turn, "reasons": sorted(reasons)}
        if details:
            ineligible_row["details"] = details
        return None, ineligible_row

    return {
        "turn": turn,
        "working_path": f"{TURNS_RELATIVE}/{turn}",
        "request_sha256": binding["request_sha256"],
        "context_sha256": binding["context_sha256"],
        "return_sha256": returned["return_sha256"],
        "declared_targets": declared_targets,
        "published_targets": complete_targets,
        "files": file_count,
        "bytes": total_bytes,
        "working_dir_sha256": dir_sha,
    }, None


def _collect_turn_candidates(
    turns_root: Path,
    root_info: os.stat_result | None,
    turns: list[str] | tuple[str, ...] | None,
    all_eligible: bool,
) -> tuple[list[tuple[str, Path]], list[dict[str, Any]], list[str]]:
    candidates: list[tuple[str, Path]] = []
    ineligible: list[dict[str, Any]] = []
    nominated_turns: list[str] = []

    if all_eligible:
        if root_info is None:
            return candidates, ineligible, nominated_turns

        for entry in sorted(turns_root.iterdir(), key=lambda p: p.name):
            if entry.name.startswith("."):
                continue
            nominated_turns.append(entry.name)
            try:
                valid_name = packet_id(entry.name)
            except ValueError as exc:
                ineligible.append(
                    {
                        "turn": entry.name,
                        "reasons": ["invalid_turn_identity"],
                        "details": {"invalid_turn_identity": str(exc)},
                    }
                )
                continue

            if not is_ordinary_dir_stat(entry.lstat()):
                ineligible.append({"turn": valid_name, "reasons": ["symlink_or_non_directory"]})
                continue
            candidates.append((valid_name, entry))
        return candidates, ineligible, nominated_turns

    if turns is None:
        raise ValueError("nominate at least one turn (--turn TURN) or pass --all-eligible")

    seen: set[str] = set()
    for raw_turn in turns:
        require(isinstance(raw_turn, str), "turn identity must be a string")
        turn = packet_id(raw_turn)
        require(turn not in seen, f"duplicate nominated turn: {turn}")
        seen.add(turn)

        turn_dir = turns_root / turn
        try:
            info = turn_dir.lstat()
        except FileNotFoundError:
            raise ValueError(f"unknown turn working directory: {turn}") from None
        require(
            is_ordinary_dir_stat(info),
            f"ordinary turn working directory required (symlinks refused): {turn}",
        )
        nominated_turns.append(turn)
        candidates.append((turn, turn_dir))

    return candidates, ineligible, nominated_turns


def plan_turn_retirement(
    system: Any,
    *,
    turns: list[str] | tuple[str, ...] | None = None,
    all_eligible: bool = False,
) -> dict[str, Any]:
    """Audit candidate working copies under work/.system/turns/ and build a hashed plan."""
    require(type(all_eligible) is bool, "all_eligible must be Boolean")
    if all_eligible:
        require(
            turns is None or len(turns) == 0,
            "supply either --turn TURN or --all-eligible, not both",
        )
    else:
        require(
            isinstance(turns, (list, tuple)) and len(turns) > 0,
            "nominate at least one turn (--turn TURN) or pass --all-eligible",
        )

    turns_root = system.ex.path(TURNS_RELATIVE)
    root_info = _lstat_or_none(turns_root)
    if root_info is not None:
        require(is_ordinary_dir_stat(root_info), "ordinary .system/turns directory required")

    candidates, ineligible, nominated_turns = _collect_turn_candidates(
        turns_root, root_info, turns, all_eligible
    )
    candidate_set = {name for name, _ in candidates}

    record_refs, record_scan_errors = _collect_record_references(system, candidate_set)
    packet_refs, packet_scan_errors = _collect_cross_packet_references(system, candidate_set)
    attention_refs, attention_error = _collect_attention_references(system, candidate_set)
    interp_refs, interp_errors = _collect_interpretation_references(system, candidate_set)
    uncommitted_refs, uncommitted_errors = _collect_uncommitted_turn_references(
        system, turns_root, candidate_set
    )

    eligible: list[dict[str, Any]] = []
    for turn, turn_dir in sorted(candidates, key=lambda item: item[0]):
        ok_row, bad_row = _evaluate_turn(
            system,
            turn,
            turn_dir,
            record_refs=record_refs,
            record_scan_errors=record_scan_errors,
            packet_refs=packet_refs,
            packet_scan_errors=packet_scan_errors,
            attention_refs=attention_refs,
            attention_error=attention_error,
            interp_refs=interp_refs,
            interp_errors=interp_errors,
            uncommitted_refs=uncommitted_refs,
            uncommitted_errors=uncommitted_errors,
        )
        if ok_row is not None:
            eligible.append(ok_row)
        elif bad_row is not None:
            ineligible.append(bad_row)

    eligible.sort(key=lambda r: r["turn"])
    ineligible.sort(key=lambda r: r["turn"])
    plan_body = {
        "format": PLAN_FORMAT,
        "instance_id": system.instance_id,
        "selection_mode": "all-eligible" if all_eligible else "explicit",
        "nominated_turns": sorted(nominated_turns),
        "eligible": eligible,
        "ineligible": ineligible,
        "reclaimable_bytes": sum(row["bytes"] for row in eligible),
        "reclaimable_files": sum(row["files"] for row in eligible),
        "scope": (
            "Read-only working-copy retirement plan under MAINTENANCE.md guards; "
            "exchange packets, objects, and native records remain untouched."
        ),
    }
    return dict(plan_body, plan_sha256=sha(encode(plan_body)))


def _validate_plan(system: Any, plan: Any, expected_plan: Any) -> tuple[str, list[dict[str, Any]]]:
    require(isinstance(plan, dict), "turn retirement plan must be an object")
    json.dumps(plan, allow_nan=False)
    exact_fields(plan, _PLAN_REQUIRED, set(), "turn retirement plan")
    require(plan["format"] == PLAN_FORMAT, "unsupported turn retirement plan format")
    require(
        plan["instance_id"] == system.instance_id,
        "turn retirement plan targets a different instance",
    )
    require(
        plan["selection_mode"] in ("all-eligible", "explicit"),
        "unsupported turn retirement selection_mode",
    )
    require(
        isinstance(plan["nominated_turns"], list) and isinstance(plan["ineligible"], list),
        "turn retirement plan lists must be arrays",
    )

    expected_sha = digest(expected_plan, "expected_plan")
    declared_sha = digest(plan["plan_sha256"], "plan_sha256")
    computed_sha = sha(encode({k: v for k, v in plan.items() if k != "plan_sha256"}))
    require(
        declared_sha == computed_sha and computed_sha == expected_sha,
        "turn retirement plan digest mismatch",
    )

    eligible = plan["eligible"]
    require(
        isinstance(eligible, list) and len(eligible) > 0,
        "turn retirement plan has no eligible turns to retire",
    )

    seen: set[str] = set()
    for row in eligible:
        exact_fields(row, _ELIGIBLE_FIELDS, set(), "turn retirement plan eligible entry")
        turn = packet_id(row["turn"])
        require(turn not in seen, f"duplicate eligible turn in plan: {turn}")
        seen.add(turn)
        require(
            row["working_path"] == f"{TURNS_RELATIVE}/{turn}",
            f"invalid working_path for turn {turn}",
        )
        digest(row["request_sha256"], "request_sha256")
        digest(row["context_sha256"], "context_sha256")
        digest(row["return_sha256"], "return_sha256")
        digest(row["working_dir_sha256"], "working_dir_sha256")
        require(type(row["files"]) is int and row["files"] >= 0, "invalid file count in plan")
        require(type(row["bytes"]) is int and row["bytes"] >= 0, "invalid byte count in plan")

    require(
        type(plan["reclaimable_files"]) is int
        and type(plan["reclaimable_bytes"]) is int
        and plan["reclaimable_files"] == sum(r["files"] for r in eligible)
        and plan["reclaimable_bytes"] == sum(r["bytes"] for r in eligible),
        "turn retirement plan totals differ from eligible entries",
    )
    return computed_sha, eligible


def _remove_dir_fd(parent_fd: int, name: str, display_prefix: str) -> None:
    """Recursively remove a verified non-symlink directory subtree via dir_fd."""
    child_fd = open_directory(name, dir_fd=parent_fd)
    try:
        for entry in sorted(os.listdir(child_fd)):
            entry_info = os.lstat(entry, dir_fd=child_fd)
            mode = entry_info.st_mode
            if stat.S_ISLNK(mode):
                raise ValueError(f"refused symlink during turn removal: {display_prefix}/{entry}")
            if stat.S_ISDIR(mode):
                _remove_dir_fd(child_fd, entry, f"{display_prefix}/{entry}")
            elif stat.S_ISREG(mode):
                os.unlink(entry, dir_fd=child_fd)
            else:
                raise ValueError(
                    f"refused special file during turn removal: {display_prefix}/{entry}"
                )
    finally:
        os.close(child_fd)

    os.rmdir(name, dir_fd=parent_fd)


def _safe_remove_turn_dir(system: Any, turn: str) -> None:
    """Remove only work/.system/turns/<turn> without following any symlink."""
    turn = packet_id(turn)
    turns_root = system.ex.path(TURNS_RELATIVE)
    target = turns_root / turn
    require(
        target.parent == turns_root and target.is_relative_to(turns_root),
        f"turn path escapes {TURNS_RELATIVE}: {turn}",
    )
    require(
        is_ordinary_dir_stat(target.lstat()), f"ordinary turn working directory required: {turn}"
    )

    if hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
        with system.ex.open_workspace_dir(TURNS_RELATIVE) as turns_fd:
            _remove_dir_fd(turns_fd, turn, f"{TURNS_RELATIVE}/{turn}")
        sync_dir(turns_root)
        return

    def remove_fallback(current: Path) -> None:
        for child in sorted(current.iterdir(), key=lambda p: p.name):
            cinfo = child.lstat()
            if is_reparse_or_symlink(cinfo):
                raise ValueError(f"refused symlink during turn removal: {child}")
            if stat.S_ISDIR(cinfo.st_mode):
                remove_fallback(child)
            elif stat.S_ISREG(cinfo.st_mode):
                child.unlink()
            else:
                raise ValueError(f"refused special file during turn removal: {child}")
        current.rmdir()

    remove_fallback(target)


def _verify_live_retirement_preconditions(
    system: Any,
    planned_eligible: list[dict[str, Any]],
) -> dict[str, Any]:
    nominated = [row["turn"] for row in planned_eligible]
    fresh = plan_turn_retirement(system, turns=nominated, all_eligible=False)
    if fresh["ineligible"]:
        details = "; ".join(f"{r['turn']} ({', '.join(r['reasons'])})" for r in fresh["ineligible"])
        raise ValueError("planned turns became ineligible before execution: " + details)

    fresh_by_turn = {row["turn"]: row for row in fresh["eligible"]}
    for expected_row in planned_eligible:
        turn = expected_row["turn"]
        require(
            fresh_by_turn.get(turn) == expected_row,
            f"turn working copy or packet changed since planning: {turn}",
        )

    pre_status = system.status()
    pre_turns = {row["packet"]: row for row in pre_status["turns"]}
    for expected_row in planned_eligible:
        turn = expected_row["turn"]
        require(
            turn not in pre_status["incomplete_turns"],
            f"turn appears in incomplete_turns before retirement: {turn}",
        )
        require(
            pre_turns.get(turn, {}).get("transport") == "returned",
            f"turn transport is not returned in native status: {turn}",
        )
    return pre_status


def _verify_post_retirement_status(
    system: Any,
    pre_status: dict[str, Any],
    retired_rows: list[dict[str, Any]],
) -> None:
    post_status = system.status()
    post_turns = {row["packet"]: row for row in post_status["turns"]}

    require(
        post_status.get("current_goal", {}).get("sha256")
        == pre_status.get("current_goal", {}).get("sha256"),
        "current goal changed during turn retirement",
    )
    require(
        post_status["incomplete_turns"] == pre_status["incomplete_turns"],
        "incomplete_turns changed during turn retirement",
    )
    require(
        post_status["gaps"]["changed_record_basis"] == pre_status["gaps"]["changed_record_basis"],
        "changed_record_basis drifted during turn retirement",
    )
    require(
        post_status["gaps"]["pending_returns"] == pre_status["gaps"]["pending_returns"],
        "pending_returns drifted during turn retirement",
    )
    require(
        post_status["gaps"]["whole_goal_requires_interpretation"]
        == pre_status["gaps"]["whole_goal_requires_interpretation"],
        "whole_goal_requires_interpretation drifted during turn retirement",
    )
    require(
        post_status["current_use"]["evidence_changes"]
        == pre_status["current_use"]["evidence_changes"]
        and post_status["current_use"]["needs_interpretation"]
        == pre_status["current_use"]["needs_interpretation"]
        and post_status["current_use"]["reassessment_reasons"]
        == pre_status["current_use"]["reassessment_reasons"],
        "current_use interpretation state drifted during turn retirement",
    )
    for row in retired_rows:
        require(
            post_turns.get(row["turn"], {}).get("transport") == "returned",
            f"post-removal status lost returned transport for {row['turn']}",
        )


def execute_turn_retirement(
    system: Any,
    *,
    plan: dict[str, Any],
    expected_plan: str,
    reason: str,
) -> dict[str, Any]:
    """Verify plan and live guards under LOCK_EX, remove working copies, and write receipt."""
    reason = nonempty(reason, "turn retirement reason")
    plan_sha, planned_eligible = _validate_plan(system, plan, expected_plan)

    receipt_dir = system.ex.path(RECEIPT_DIRECTORY)
    receipt_path = system.ex.path(f"{RECEIPT_DIRECTORY}/{plan_sha}.json")
    receipt_info = _lstat_or_none(receipt_path)
    if receipt_info is not None:
        require(
            is_ordinary_file_stat(receipt_info),
            "ordinary turn retirement receipt required: " + str(receipt_path),
        )
        existing = _read_strict_file(receipt_path)
        if existing.get("complete"):
            raise ValueError("turn retirement plan already executed; inspect its receipt")
        raise ValueError(
            "interrupted turn retirement detected; inspect receipt and re-plan remaining turns"
        )

    pre_status = _verify_live_retirement_preconditions(system, planned_eligible)

    rdir_info = _lstat_or_none(receipt_dir)
    if rdir_info is not None:
        require(
            is_ordinary_dir_stat(rdir_info), "ordinary turn retirement receipt directory required"
        )
    receipt_dir.mkdir(parents=True, exist_ok=True)
    sync_dir(receipt_dir.parent)

    retired_rows: list[dict[str, Any]] = []
    receipt: dict[str, Any] = {
        "format": RECEIPT_FORMAT,
        "instance_id": system.instance_id,
        "plan_sha256": plan_sha,
        "reason": reason,
        "complete": False,
        "retired_at": now(),
        "retired_turns": [],
        "turns": [],
        "retired_count": 0,
        "files_removed": 0,
        "bytes_reclaimed": 0,
        "post_verification": {
            "all_views_verified": False,
            "all_recovers_verified": False,
            "all_explain_contexts_verified": False,
            "status_verified": False,
        },
        "receipt_path": f"{RECEIPT_DIRECTORY}/{plan_sha}.json",
        "scope": (
            "Completed working copies removed under work/.system/turns/ only; "
            "exchange packets, immutable objects, and native records remain intact and verified."
        ),
    }
    write_json(receipt_path, receipt)

    turns_root = system.ex.path(TURNS_RELATIVE)
    for expected_row in planned_eligible:
        turn = expected_row["turn"]
        _safe_remove_turn_dir(system, turn)
        require(
            _lstat_or_none(turns_root / turn) is None,
            f"turn working directory remained after removal: {turn}",
        )

        view_doc = system.view(turn)
        require(isinstance(view_doc, dict), f"post-removal view failed for {turn}")
        recovered = system.recover(turn)
        require(
            recovered.get("request_sha256") == expected_row["request_sha256"]
            and recovered.get("context_sha256") == expected_row["context_sha256"],
            f"post-removal recover binding failed for {turn}",
        )
        explained = system.explain_context(turn)
        require(
            explained.get("request_sha256") == expected_row["request_sha256"]
            and explained.get("context_sha256") == expected_row["context_sha256"],
            f"post-removal explain_context binding failed for {turn}",
        )

        retired_rows.append(
            {
                "turn": turn,
                "working_path": expected_row["working_path"],
                "files_removed": expected_row["files"],
                "bytes_reclaimed": expected_row["bytes"],
                "working_dir_sha256": expected_row["working_dir_sha256"],
                "request_sha256": expected_row["request_sha256"],
                "context_sha256": expected_row["context_sha256"],
                "return_sha256": expected_row["return_sha256"],
                "view_verified": True,
                "recover_verified": True,
                "explain_context_verified": True,
            }
        )
        receipt.update(
            retired_turns=[r["turn"] for r in retired_rows],
            turns=list(retired_rows),
            retired_count=len(retired_rows),
            files_removed=sum(r["files_removed"] for r in retired_rows),
            bytes_reclaimed=sum(r["bytes_reclaimed"] for r in retired_rows),
        )
        write_json(receipt_path, receipt)

    _verify_post_retirement_status(system, pre_status, retired_rows)

    receipt["post_verification"] = {
        "all_views_verified": True,
        "all_recovers_verified": True,
        "all_explain_contexts_verified": True,
        "status_verified": True,
    }
    receipt["complete"] = True
    receipt["retired_at"] = now()
    write_json(receipt_path, receipt)
    return receipt
