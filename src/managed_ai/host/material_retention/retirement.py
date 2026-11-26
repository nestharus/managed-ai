"""Explicit retirement planning and verified local materialization retirement."""

import os
import shutil
import stat
from pathlib import Path
from typing import Any

from .retention_closures import read_manifest
from .snapshot_ops import verify
from .store_io import (
    check_unfollowed_ancestors,
    check_unfollowed_descendant,
    immutable,
    is_reparse_or_symlink,
    js,
    loads_json,
    replace_json,
    require,
    safe_relative,
    sha,
    sha256_file_checked,
    store_lock,
    utc,
)


def _guard_subtree_no_junctions(source: Path) -> None:
    """Refuse retirement if a target directory or any descendant is an NTFS reparse point /
    junction.
    """
    require(
        not is_reparse_or_symlink(source),
        "Cannot remove symlink or reparse point as directory: " + str(source),
    )
    for parent, dirs, files in os.walk(source, followlinks=False):
        for name in dirs:
            candidate = Path(parent) / name
            if is_reparse_or_symlink(candidate) and not candidate.is_symlink():
                raise ValueError(
                    "Cannot remove NTFS reparse point / junction during retirement: "
                    + str(candidate),
                )
        for name in files:
            candidate = Path(parent) / name
            if is_reparse_or_symlink(candidate) and not candidate.is_symlink():
                raise ValueError(
                    "Cannot remove NTFS reparse point during retirement: " + str(candidate),
                )


def _prefixes_overlap(first: str, second: str) -> bool:
    return (
        first == second
        or first.startswith(second.rstrip("/") + "/")
        or second.startswith(first.rstrip("/") + "/")
    )


def _load_and_validate_proposal(
    spec_path: Path | str | dict[str, Any],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Load and validate a retirement proposal against declared active and candidate groups."""
    if isinstance(spec_path, dict):
        proposal = spec_path
    else:
        proposal_file = check_unfollowed_ancestors(
            spec_path,
            "Retirement proposal",
            include_target=True,
        )
        proposal = loads_json(proposal_file.read_bytes())

    require(isinstance(proposal, dict), "Retirement proposal must be a JSON object")
    require(
        proposal.get("format") == "managed-material-retirement-proposal/1",
        "Unknown retirement proposal",
    )
    require(bool(proposal.get("account")), "Retirement proposal needs an applicability account")

    protected_raw = proposal.get("protected_prefixes", [])
    require(
        isinstance(protected_raw, list) and bool(protected_raw),
        "Explicitly declare protected active paths",
    )
    prefixes = proposal.get("prefixes")
    require(isinstance(prefixes, list), "Retirement proposal prefixes must be a list")
    if not isinstance(prefixes, list):
        raise ValueError("Retirement proposal prefixes must be a list")

    groups = manifest.get("materialization_groups", [])
    protected = [*protected_raw, *(g["prefix"] for g in groups if g["state"] == "active")]
    nominated = {g["prefix"] for g in groups if g["state"] == "retirement-candidate"}
    require(
        all(p in nominated for p in prefixes),
        "Retire only groups declared candidates in the captured selection",
    )

    for idx, prefix in enumerate(prefixes):
        require(
            not any(_prefixes_overlap(prefix, other) for other in prefixes[idx + 1 :]),
            "Overlapping retirement prefixes",
        )

    return proposal, protected


def _verify_live_prefix_inventory(
    prefix: str,
    root_row: dict[str, Any],
    captured: list[dict[str, Any]],
) -> Path:
    """Confirm the live directory/file inventory under prefix matches its captured manifest rows."""
    source = check_unfollowed_ancestors(
        root_row["source_path"],
        "Proposed materialization",
        include_target=False,
    )
    require(source.exists() or source.is_symlink(), "Proposed materialization is absent")
    if is_reparse_or_symlink(source) and not source.is_symlink():
        raise ValueError(
            "Proposed materialization must not be an NTFS reparse point / junction: " + str(source),
        )

    observed = {source}
    if source.is_dir() and not is_reparse_or_symlink(source):
        for parent, dirs, files in os.walk(source, followlinks=False):
            kept_dirs: list[str] = []
            for name in dirs:
                candidate = Path(parent) / name
                observed.add(candidate)
                if not is_reparse_or_symlink(candidate):
                    kept_dirs.append(name)
            dirs[:] = kept_dirs
            observed.update(Path(parent) / name for name in files)

    require(
        observed == {Path(row["source_path"]) for row in captured},
        "Materialization inventory changed or was excluded: " + prefix,
    )
    return source


def _verify_live_entries_unchanged(captured: list[dict[str, Any]]) -> None:
    """Verify every captured file, symlink, and directory still matches live bytes and mode."""
    for row in captured:
        path = Path(row["source_path"])
        require(
            stat.S_IMODE(path.lstat().st_mode) == row["mode"],
            "Mode changed after capture: " + str(path),
        )
        if row["type"] == "file":
            require(
                path.is_file()
                and not is_reparse_or_symlink(path)
                and sha256_file_checked(path, label="Live file") == row["object"],
                "Live file changed after capture: " + str(path),
            )
        elif row["type"] == "symlink":
            require(
                path.is_symlink() and sha(os.fsencode(os.readlink(path))) == row["object"],
                "Live symlink changed after capture: " + str(path),
            )
        else:
            require(
                path.is_dir() and not is_reparse_or_symlink(path),
                "Live directory kind changed",
            )


def retirement_plan(
    store: Any,
    spec_path: Path | str | dict[str, Any],
    occurrence: str | None = None,
) -> dict[str, Any]:
    """Verify selected copies without removing them or deciding their applicability."""
    with store_lock(store.root, shared=True):
        store.reload(create=False)
        key, manifest, _ = read_manifest(store, occurrence)
        proposal, protected = _load_and_validate_proposal(spec_path, manifest)
        rows_by_path = {row["path"]: row for row in manifest["files"]}
        recoverable = verify(store, occurrence)

        results: list[dict[str, Any]] = []
        for prefix in proposal["prefixes"]:
            safe_relative(prefix)
            require(
                not any(_prefixes_overlap(prefix, prot) for prot in protected),
                "Proposal overlaps a protected active materialization: " + prefix,
            )
            require(prefix in rows_by_path, "Proposed root was not captured: " + prefix)
            prefix_slash = prefix.rstrip("/") + "/"
            require(
                not any(
                    cur["path"] == prefix or cur["path"].startswith(prefix_slash)
                    for cur in manifest["current"]
                ),
                "Proposed path supplies an explicitly current selected body",
            )

            captured = [
                row
                for row in manifest["files"]
                if row["path"] == prefix or row["path"].startswith(prefix_slash)
            ]
            source = _verify_live_prefix_inventory(prefix, rows_by_path[prefix], captured)
            _verify_live_entries_unchanged(captured)

            results.append(
                {
                    "prefix": prefix,
                    "source_path": str(source),
                    "entries": len(captured),
                    "bytes": sum(row.get("bytes", 0) for row in captured),
                    "restore": {"manifest": key, "occurrence": manifest["id"], "prefix": prefix},
                    "observed_at": utc(),
                    "state": "byte-recoverable-and-currently-matching",
                },
            )

        return {
            "format": "managed-material-retirement-plan/1",
            "source_manifest": key,
            "proposal": proposal,
            "recovery_verification": recoverable,
            "materializations": results,
            "state": "ready-for-owner-receiving",
            "removed_paths": 0,
            "boundary": (
                "Physical recoverability and live equality only. Owner must establish no active "
                "dependency, select the semantic retirement and recheck immediately before any "
                "removal. This producer performs no deletion."
            ),
        }


def _remove_planned_materializations(
    store: Any,
    materializations: list[dict[str, Any]],
    expected: list[dict[str, Any]],
    expected_plan: str,
    expected_selection: str,
) -> list[dict[str, Any]]:
    """Remove verified live materializations while refusing store paths or NTFS junctions."""
    store.event(
        "retirement-started",
        {
            "plan": expected_plan,
            "selection": expected_selection,
            "materializations": expected,
        },
    )
    removed: list[dict[str, Any]] = []
    store_resolved = store.root.resolve()

    try:
        for row in materializations:
            source = check_unfollowed_ancestors(
                row["source_path"],
                "Retired materialization",
                include_target=False,
            )
            source_resolved = source.resolve()
            require(
                source_resolved != store_resolved and source_resolved not in store_resolved.parents,
                "Cannot remove the managed store",
            )
            if is_reparse_or_symlink(source) and not source.is_symlink():
                raise ValueError("Cannot retire NTFS reparse point / junction: " + str(source))
            if source.is_dir() and not is_reparse_or_symlink(source):
                _guard_subtree_no_junctions(source)
                shutil.rmtree(source)
            else:
                source.unlink()
            removed.append(row)
            store.event("materialization-retired", {"plan": expected_plan, "materialization": row})
    except Exception as exc:
        store.event(
            "retirement-partial",
            {
                "plan": expected_plan,
                "removed": removed,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        raise

    return removed


def _record_retirement(
    store: Any,
    current: dict[str, Any],
    plan: dict[str, Any],
    removed: list[dict[str, Any]],
    expected_plan: str,
    expected_selection: str,
    *,
    hooks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist the immutable retirement receipt, successor selection, and CURRENT.json pointer."""
    immutable_fn = hooks["immutable"] if hooks else immutable
    replace_fn = hooks["replace_json"] if hooks else replace_json

    receipt = {
        "format": "managed-material-retirement/1",
        "at": utc(),
        "plan": plan,
        "plan_sha256": expected_plan,
        "previous_selection": expected_selection,
        "source_manifest": current["manifest"],
        "retired": removed,
        "recovery": "Bodies, occurrences and original transports "
        "remain in the managed reference closure.",
        "boundary": (
            "Cooperative local maintenance; store mutations serialize via cooperative store lock, "
            "with no cross-filesystem atomicity or lock against external editors."
        ),
    }
    raw = js(receipt)
    key = sha(raw)
    immutable_fn(store.root / "lifecycle" / (key + ".json"), raw)

    successor = {
        **current,
        "previous_selection": expected_selection,
        "lifecycle": [*current.get("lifecycle", []), key],
        "materializations": [
            *current.get("materializations", []),
            *({"prefix": row["prefix"], "state": "retired", "receipt": key} for row in removed),
        ],
    }
    successor_raw = js(successor)
    successor_key = sha(successor_raw)
    immutable_fn(store.root / "selections" / (successor_key + ".json"), successor_raw)

    try:
        replace_fn(
            store.root / "CURRENT.json",
            {"selection": successor_key, "path": "selections/" + successor_key + ".json"},
        )
    except Exception as exc:
        store.event(
            "retirement-selection-failed",
            {
                "receipt": key,
                "selection": successor_key,
                "selection_published": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        raise

    return {
        "receipt": key,
        "selection": successor_key,
        "retired_materializations": len(removed),
        "retired_occurrence_bytes": sum(row["bytes"] for row in removed),
        "canonical_objects_removed": 0,
    }


def retire(
    store: Any,
    plan_path: Path | str,
    expected_plan: str,
    expected_selection: str,
    *,
    hooks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Explicit operator command executing a received retirement plan under exclusive lock."""
    plan_file = check_unfollowed_ancestors(plan_path, "Retirement plan", include_target=True)
    plan_raw = plan_file.read_bytes()
    require(sha(plan_raw) == expected_plan, "Retirement plan differs from the received plan")
    plan = loads_json(plan_raw)
    require(isinstance(plan, dict), "Retirement plan must be a JSON object")

    with store_lock(store.root):
        store.reload(create=False)
        current_ptr = store.root / "CURRENT.json"
        require(
            not is_reparse_or_symlink(current_ptr),
            "Current pointer must not be a symlink or reparse point",
        )
        pointer = loads_json(current_ptr.read_bytes())
        require(
            pointer["selection"] == expected_selection,
            "Current selection changed since receiving",
        )

        sel_rel = safe_relative(pointer["path"])
        sel_path = check_unfollowed_descendant(
            store.root,
            sel_rel.as_posix(),
            role="Current selection",
        )
        current_raw = sel_path.read_bytes()
        require(sha(current_raw) == expected_selection, "Current selection bytes differ")
        current = loads_json(current_raw)

        require(
            current["manifest"] == plan["source_manifest"],
            "Retirement is against a different captured occurrence",
        )
        require(
            plan["state"] == "ready-for-owner-receiving",
            "Plan was not physically validated",
        )

        fresh = retirement_plan(store, plan["proposal"])
        expected = [
            {k: row[k] for k in ("prefix", "source_path", "entries", "bytes", "restore")}
            for row in plan["materializations"]
        ]
        observed = [
            {k: row[k] for k in ("prefix", "source_path", "entries", "bytes", "restore")}
            for row in fresh["materializations"]
        ]
        require(expected == observed, "Live materializations differ from received plan")

        removed = _remove_planned_materializations(
            store,
            fresh["materializations"],
            expected,
            expected_plan,
            expected_selection,
        )
        return _record_retirement(
            store,
            current,
            plan,
            removed,
            expected_plan,
            expected_selection,
            hooks=hooks,
        )
