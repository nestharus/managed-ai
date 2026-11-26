"""Explicit retirement planning and verified local materialization retirement."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import stat
import sys


def _load_sibling(stem: str):
    if __package__:
        module = importlib.import_module("." + stem, __package__)
        return module
    path = Path(__file__).resolve().with_name(stem + ".py")
    key = f"_managed_material_{stem}_" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    module = sys.modules.get(key)
    if module is None:
        spec = importlib.util.spec_from_file_location(key, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Unable to load managed_material sibling module: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(key, None)
            raise
    return module


_io = _load_sibling("store_io")
_closures = _load_sibling("retention_closures")
_ops = _load_sibling("material_ops")

check_unfollowed_ancestors = _io.check_unfollowed_ancestors
immutable = _io.immutable
is_reparse_or_symlink = _io.is_reparse_or_symlink
js = _io.js
loads_json = _io.loads_json
replace_json = _io.replace_json
require = _io.require
safe_relative = _io.safe_relative
sha = _io.sha
store_lock = _io.store_lock
utc = _io.utc

read_manifest = _closures.read_manifest
verify = _ops.verify


def _guard_subtree_no_junctions(source: Path) -> None:
    """Refuse retirement if a target directory or any descendant is an NTFS reparse point / junction."""
    require(
        not is_reparse_or_symlink(source),
        "Cannot remove symlink or reparse point as directory: " + str(source),
    )
    for parent, dirs, files in os.walk(source, followlinks=False):
        for name in dirs:
            candidate = Path(parent) / name
            if is_reparse_or_symlink(candidate) and not candidate.is_symlink():
                raise ValueError("Cannot remove NTFS reparse point / junction during retirement: " + str(candidate))
        for name in files:
            candidate = Path(parent) / name
            if is_reparse_or_symlink(candidate) and not candidate.is_symlink():
                raise ValueError("Cannot remove NTFS reparse point during retirement: " + str(candidate))


def retirement_plan(store, spec_path, occurrence=None):
    """Verify selected copies without removing them or deciding their applicability."""
    with store_lock(store.root, shared=True):
        store.reload(create=False)
        key, manifest, _ = read_manifest(store, occurrence)
        if isinstance(spec_path, dict):
            proposal = spec_path
        else:
            proposal_file = check_unfollowed_ancestors(spec_path, "Retirement proposal", include_target=True)
            proposal = loads_json(proposal_file.read_bytes())
        require(proposal.get("format") == "managed-material-retirement-proposal/1", "Unknown retirement proposal")
        require(proposal.get("account"), "Retirement proposal needs an applicability account")
        protected = proposal.get("protected_prefixes", [])
        require(protected, "Explicitly declare protected active paths")
        groups = manifest.get("materialization_groups", [])
        protected = protected + [g["prefix"] for g in groups if g["state"] == "active"]
        nominated = {g["prefix"] for g in groups if g["state"] == "retirement-candidate"}
        require(all(p in nominated for p in proposal["prefixes"]), "Retire only groups declared candidates in the captured selection")
        for i, prefix in enumerate(proposal["prefixes"]):
            require(not any(prefix == other or prefix.startswith(other.rstrip("/") + "/") or
                            other.startswith(prefix.rstrip("/") + "/") for other in proposal["prefixes"][i+1:]),
                    "Overlapping retirement prefixes")
        rows_by_path = {r["path"]: r for r in manifest["files"]}
        recoverable = verify(store, occurrence)
        results = []
        for prefix in proposal["prefixes"]:
            safe_relative(prefix)
            require(not any(prefix == p or prefix.startswith(p.rstrip("/") + "/") or
                            p.startswith(prefix.rstrip("/") + "/") for p in protected),
                    "Proposal overlaps a protected active materialization: " + prefix)
            require(prefix in rows_by_path, "Proposed root was not captured: " + prefix)
            require(not any(c["path"] == prefix or c["path"].startswith(prefix.rstrip("/") + "/") for c in manifest["current"]),
                    "Proposed path supplies an explicitly current selected body")
            root_row = rows_by_path[prefix]
            captured = [r for r in manifest["files"] if r["path"] == prefix or r["path"].startswith(prefix.rstrip("/") + "/")]
            source = check_unfollowed_ancestors(root_row["source_path"], "Proposed materialization", include_target=False)
            require(source.exists() or source.is_symlink(), "Proposed materialization is absent")
            if is_reparse_or_symlink(source) and not source.is_symlink():
                raise ValueError("Proposed materialization must not be an NTFS reparse point / junction: " + str(source))
            observed = {source}
            if source.is_dir() and not is_reparse_or_symlink(source):
                for parent, dirs, files in os.walk(source, followlinks=False):
                    kept_dirs = []
                    for name in dirs:
                        candidate = Path(parent) / name
                        observed.add(candidate)
                        if not is_reparse_or_symlink(candidate):
                            kept_dirs.append(name)
                    dirs[:] = kept_dirs
                    observed.update(Path(parent) / n for n in files)
            require(observed == {Path(r["source_path"]) for r in captured},
                    "Materialization inventory changed or was excluded: " + prefix)
            for row in captured:
                path = Path(row["source_path"])
                require(stat.S_IMODE(path.lstat().st_mode) == row["mode"], "Mode changed after capture: " + str(path))
                if row["type"] == "file":
                    require(path.is_file() and not is_reparse_or_symlink(path) and sha(path.read_bytes()) == row["object"],
                            "Live file changed after capture: " + str(path))
                elif row["type"] == "symlink":
                    require(path.is_symlink() and sha(os.fsencode(os.readlink(path))) == row["object"],
                            "Live symlink changed after capture: " + str(path))
                else:
                    require(path.is_dir() and not is_reparse_or_symlink(path), "Live directory kind changed")
            results.append({"prefix": prefix, "source_path": str(source), "entries": len(captured),
                            "bytes": sum(r.get("bytes", 0) for r in captured),
                            "restore": {"manifest": key, "occurrence": manifest["id"], "prefix": prefix},
                            "observed_at": utc(), "state": "byte-recoverable-and-currently-matching"})
        return {"format": "managed-material-retirement-plan/1", "source_manifest": key,
                "proposal": proposal, "recovery_verification": recoverable, "materializations": results,
                "state": "ready-for-owner-receiving", "removed_paths": 0,
                "boundary": "Physical recoverability and live equality only. Owner must establish no active dependency, select the semantic retirement and recheck immediately before any removal. This producer performs no deletion."}


def _remove_planned_materializations(store, materializations, expected, expected_plan, expected_selection):
    store.event("retirement-started", {"plan": expected_plan, "selection": expected_selection,
                                        "materializations": expected})
    removed = []
    try:
        for row in materializations:
            source = check_unfollowed_ancestors(row["source_path"], "Retired materialization", include_target=False)
            require(source.resolve() != store.root.resolve() and source.resolve() not in store.root.resolve().parents,
                    "Cannot remove the managed store")
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
        store.event("retirement-partial", {"plan": expected_plan, "removed": removed,
                                           "error_type": type(exc).__name__, "error": str(exc)})
        raise
    return removed


def _record_retirement(store, current, plan, removed, expected_plan, expected_selection, *, hooks=None):
    immutable_fn = hooks["immutable"] if hooks else immutable
    replace_fn = hooks["replace_json"] if hooks else replace_json
    receipt = {"format": "managed-material-retirement/1", "at": utc(), "plan": plan,
               "plan_sha256": expected_plan, "previous_selection": expected_selection,
               "source_manifest": current["manifest"], "retired": removed,
               "recovery": "Bodies, occurrences and original transports remain in the managed reference closure.",
               "boundary": "Cooperative local maintenance; store mutations serialize via cooperative store lock, with no cross-filesystem atomicity or lock against external editors."}
    raw = js(receipt); key = sha(raw)
    immutable_fn(store.root / "lifecycle" / (key + ".json"), raw)
    successor = {**current, "previous_selection": expected_selection,
                 "lifecycle": [*current.get("lifecycle", []), key],
                 "materializations": [*current.get("materializations", []),
                      *({"prefix": r["prefix"], "state": "retired", "receipt": key} for r in removed)]}
    successor_raw = js(successor); successor_key = sha(successor_raw)
    immutable_fn(store.root / "selections" / (successor_key + ".json"), successor_raw)
    try:
        replace_fn(store.root / "CURRENT.json", {"selection": successor_key, "path": "selections/" + successor_key + ".json"})
    except Exception as exc:
        store.event("retirement-selection-failed", {"receipt": key, "selection": successor_key,
                                                    "selection_published": False,
                                                    "error_type": type(exc).__name__, "error": str(exc)})
        raise
    return {"receipt": key, "selection": successor_key, "retired_materializations": len(removed),
            "retired_occurrence_bytes": sum(r["bytes"] for r in removed), "canonical_objects_removed": 0}


def retire(store, plan_path, expected_plan, expected_selection, *, hooks=None):
    """Explicit operator command; never invoked by snapshot, verify or export."""
    plan_file = check_unfollowed_ancestors(plan_path, "Retirement plan", include_target=True)
    plan_raw = plan_file.read_bytes()
    require(sha(plan_raw) == expected_plan, "Retirement plan differs from the received plan")
    plan = loads_json(plan_raw)
    with store_lock(store.root):
        store.reload(create=False)
        current_ptr = store.root / "CURRENT.json"
        require(not is_reparse_or_symlink(current_ptr), "Current pointer must not be a symlink or reparse point")
        pointer = loads_json(current_ptr.read_bytes())
        require(pointer["selection"] == expected_selection, "Current selection changed since receiving")
        sel_rel = safe_relative(pointer["path"])
        sel_path = store.root.joinpath(*sel_rel.parts)
        require(not is_reparse_or_symlink(sel_path), "Current selection must not be a symlink or reparse point")
        current_raw = sel_path.read_bytes()
        require(sha(current_raw) == expected_selection, "Current selection bytes differ")
        current = loads_json(current_raw)
        require(current["manifest"] == plan["source_manifest"], "Retirement is against a different captured occurrence")
        require(plan["state"] == "ready-for-owner-receiving", "Plan was not physically validated")
        fresh = retirement_plan(store, plan["proposal"])
        expected = [{k: r[k] for k in ("prefix", "source_path", "entries", "bytes", "restore")} for r in plan["materializations"]]
        observed = [{k: r[k] for k in ("prefix", "source_path", "entries", "bytes", "restore")} for r in fresh["materializations"]]
        require(expected == observed, "Live materializations differ from received plan")
        removed = _remove_planned_materializations(store, fresh["materializations"], expected,
                                                  expected_plan, expected_selection)
        return _record_retirement(store, current, plan, removed, expected_plan, expected_selection, hooks=hooks)
