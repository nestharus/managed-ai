"""Snapshot acquisition, closure verification, and historical artifact retention operations."""
from __future__ import annotations

import fnmatch
from functools import lru_cache
import os
from pathlib import Path
import platform as py_platform
import stat
import time
from typing import Any
import zlib

from ._platform_bridge import platform
from .config import PACKAGE_MODULES, PLATFORM_MODULES
from .retention_closures import (
    current_selection,
    manifest_closure,
    materialization_state,
    observed_materialization_state,
    read_manifest,
    selected_for_manifest,
    selection_closure,
)
from .store_io import (
    check_unfollowed_ancestors,
    immutable,
    is_reparse_or_symlink,
    js,
    loads_json,
    path_state,
    replace_json,
    require,
    safe_relative,
    sha,
    sha256_file_checked,
    store_lock,
    utc,
)

PORTABLE_LAUNCHER_SOURCE = """#!/usr/bin/env python3
\"\"\"Standalone recovery launcher for an exported managed-material archive.\"\"\"
from __future__ import annotations

from pathlib import Path
import sys

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from material_retention.managed_material import *  # noqa: F401,F403
from material_retention.managed_material import main

if __name__ == "__main__":
    main()
"""


@lru_cache(maxsize=1)
def load_portable_bundle() -> tuple[bytes, dict[str, bytes], bytes]:
    """Lazily read package and vendored stdlib platform modules for manifest pins and recovery ZIPs."""
    here = Path(__file__).resolve().parent
    platform_dir = Path(platform.__file__).resolve().parent
    producer_bytes = (here / "managed_material.py").read_bytes()

    sibling_bytes: dict[str, bytes] = {}
    for name in PACKAGE_MODULES:
        sibling_bytes["material_retention/" + name] = (here / name).read_bytes()
    for name in PLATFORM_MODULES:
        sibling_bytes["material_retention/_platform/" + name] = (platform_dir / name).read_bytes()

    launcher_bytes = PORTABLE_LAUNCHER_SOURCE.encode("utf-8")
    return producer_bytes, sibling_bytes, launcher_bytes


def default_hooks() -> dict[str, Any]:
    """Build default persistence and lazily-loaded producer bundle hooks."""
    producer_bytes, sibling_bytes, launcher_bytes = load_portable_bundle()
    return {
        "immutable": immutable,
        "replace_json": replace_json,
        "producer_bytes": producer_bytes,
        "sibling_bytes": sibling_bytes,
        "launcher_bytes": launcher_bytes,
    }


def resolve_hooks(hooks: dict[str, Any] | None) -> dict[str, Any]:
    """Return explicit caller hooks or the lazily-built defaults."""
    return default_hooks() if hooks is None else hooks


def producer_dependencies(store: Any, hooks: dict[str, Any]) -> dict[str, str]:
    """Pin all bundled producer and platform module bytes into the store."""
    return {name: store.blob(raw) for name, raw in sorted(hooks["sibling_bytes"].items())}


def _validate_snapshot_spec(spec: Any) -> dict[str, Any]:
    """Validate structural types of a selection spec before acquiring any roots."""
    require(isinstance(spec, dict), "Selection spec must be a JSON object")
    require(spec.get("format") == "managed-material-selection/1", "Unknown selection spec")
    require(isinstance(spec.get("id"), str) and bool(spec["id"]), "Explicit occurrence identity is required")
    safe_relative(spec["id"])

    roots = spec.get("roots")
    require(isinstance(roots, list), "Selection spec roots must be a list")
    for root in roots:
        require(
            isinstance(root, dict)
            and isinstance(root.get("label"), str)
            and isinstance(root.get("path"), str),
            "Selection spec root must be an object with string label and path",
        )

    for list_field in ("current", "exclude", "materialization_groups", "lineage"):
        if list_field in spec:
            require(isinstance(spec[list_field], list), f"Selection spec {list_field} must be a list")

    return spec


def _snapshot_history(
    store: Any,
    spec: dict[str, Any],
) -> tuple[list[str], str, str | None, str | None, list[dict[str, Any]]]:
    prior_key, prior_selection = (
        current_selection(store) if (store.root / "CURRENT.json").exists() else (None, None)
    )
    policy = spec.get("retention_policy")
    require(
        policy in (None, "current-use", "append-history"),
        "retention_policy must be current-use or append-history",
    )
    history = spec.get("history", [])
    require(isinstance(history, list), "History must name exact prior manifest hashes")
    require(
        not (policy == "current-use" and history),
        "current-use conflicts with nonempty history; omit the policy to select exact history",
    )

    history = list(history)
    if policy == "append-history" and prior_selection is not None:
        if prior_selection["manifest"] not in history:
            history.insert(0, prior_selection["manifest"])

    effective_policy = policy or ("selected-history" if history else "current-use")
    history_account = spec.get("history_account")
    if effective_policy == "current-use" and not history_account:
        history_account = (
            "Current-use selection retains the declared roots without implicit ancestry. "
            "Earlier managed objects are not deleted."
        )

    retained_manifests: set[str] = set()
    for old in history:
        retained_manifests.update(manifest_closure(store, old)[0])

    retain_prior_selection = (
        prior_selection is not None and prior_selection["manifest"] in retained_manifests
    )
    if prior_selection is not None and not retain_prior_selection:
        require(
            history_account,
            "Omitting the prior selected-state ancestry needs an explicit history account",
        )

    prior_states = (
        materialization_state(store, selection_closure(store, prior_key)[0])
        if retain_prior_selection and prior_key is not None
        else []
    )
    return (
        history,
        effective_policy,
        history_account,
        prior_key if retain_prior_selection else None,
        prior_states,
    )


def _walk_root_paths(base: Path) -> list[Path]:
    """Collect paths under base without following symlinks or NTFS reparse points."""
    paths = [base]
    if base.is_dir() and not is_reparse_or_symlink(base):
        for parent, dirs, files in os.walk(base, followlinks=False):
            dirs.sort()
            files.sort()
            kept_dirs: list[str] = []
            for name in dirs:
                candidate = Path(parent) / name
                paths.append(candidate)
                if not (is_reparse_or_symlink(candidate) and not candidate.is_symlink()):
                    kept_dirs.append(name)
            dirs[:] = kept_dirs
            paths.extend(Path(parent) / name for name in files)
    return paths


def _acquire_snapshot_roots(
    store: Any,
    spec: dict[str, Any],
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    exclusions: list[dict[str, Any]] = []
    source_checks: list[dict[str, Any]] = []
    labels: set[str] = set()
    store_resolved = store.root.resolve()

    for root in spec["roots"]:
        label = root["label"]
        safe_relative(label)
        require(label not in labels and "/" not in label, "Unique single-component root labels required")
        labels.add(label)

        base = check_unfollowed_ancestors(root["path"], "Selected root", include_target=False)
        require(base.exists() or base.is_symlink(), "Selected root absent: " + str(base))
        if is_reparse_or_symlink(base) and not base.is_symlink():
            raise ValueError(
                "Special filesystem object or reparse point requires explicit handling: " + str(base),
            )

        base_resolved = base.resolve()
        require(
            store_resolved != base_resolved and base_resolved not in store_resolved.parents,
            "Store must not be inside a scanned source root",
        )

        for path in _walk_root_paths(base):
            rel = path.relative_to(base).as_posix() if path != base else ""
            logical = label + ("/" + rel if rel else "")
            if any(fnmatch.fnmatchcase(logical, pattern) for pattern in spec.get("exclude", [])):
                exclusions.append({"path": logical, "reason": "explicit selection exclusion"})
                continue

            before = path_state(path)
            row: dict[str, Any] = {
                "path": logical,
                "source_path": str(path),
                "mode": before["mode"],
                "mtime_ns": before["mtime_ns"],
                "role": root.get("role", "observed-material"),
                "materialization": root.get("materialization", "observed-working-copy"),
            }
            if path.is_symlink():
                raw = os.fsencode(os.readlink(path))
                key = store.blob(raw)
                row.update({"type": "symlink", "object": key, "bytes": len(raw)})
            elif is_reparse_or_symlink(path):
                raise ValueError(
                    "Special filesystem object or reparse point requires explicit handling: " + str(path),
                )
            elif path.is_file():
                raw = path.read_bytes()
                after = path_state(path)
                require(before == after, "Source changed during acquisition: " + str(path))
                key = store.ingest(raw, logical)
                row.update({"type": "file", "object": key, "bytes": len(raw)})
                source_checks.append({"path": str(path), "state": after, "sha256": key})
            elif path.is_dir():
                row.update({"type": "directory"})
            else:
                raise ValueError("Special filesystem object requires explicit handling: " + str(path))
            rows.append(row)

    return exclusions, source_checks


def _retain_snapshot(
    store: Any,
    spec: dict[str, Any],
    attempt: dict[str, Any],
    occurrence_path: Path,
    rows: list[dict[str, Any]],
    hooks: dict[str, Any],
) -> tuple[str, str, str]:
    immutable_fn = hooks["immutable"]
    producer_object = store.blob(hooks["producer_bytes"])
    producer_deps = producer_dependencies(store, hooks)
    history, effective_policy, history_account, retained_selection, prior_states = _snapshot_history(
        store,
        spec,
    )
    exclusions, source_checks = _acquire_snapshot_roots(store, spec, rows)

    logicals = [row["path"] for row in rows]
    require(len(logicals) == len(set(logicals)), "Duplicate occurrence path")
    by_path = {row["path"]: row for row in rows}

    currents: list[dict[str, Any]] = []
    for current in spec.get("current", []):
        selected = by_path.get(current["path"])
        require(
            selected is not None and "object" in selected,
            "Current choice absent from observed occurrence: " + current["path"],
        )
        if current.get("expected_sha256"):
            require(
                selected["object"] == current["expected_sha256"],
                "Current selection identity differs",
            )
        currents.append({**current, "object": selected["object"], "bytes": selected["bytes"]})

    for check in source_checks:
        path = Path(check["path"])
        require(
            not is_reparse_or_symlink(path),
            "Source changed to symlink or reparse point before publication: " + str(path),
        )
        require(
            path_state(path) == check["state"]
            and sha256_file_checked(path, label="Snapshot source") == check["sha256"],
            "Source changed before selection publication: " + str(path),
        )

    manifest = {
        "format": "managed-material-occurrence/1",
        "id": spec["id"],
        "started_at": attempt["started_at"],
        "completed_at": utc(),
        "spec": spec,
        "spec_sha256": attempt["spec_sha256"],
        "files": rows,
        "excluded": exclusions,
        "current": currents,
        "lineage": spec.get("lineage", []),
        "history": history,
        "retention": {"policy": effective_policy, "history_account": history_account},
        "materialization_groups": spec.get("materialization_groups", []),
        "producer": {
            "sha256": producer_object,
            "object": producer_object,
            "dependencies": producer_deps,
            "python": py_platform.python_version(),
            "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
            "zlib_compile": zlib.ZLIB_VERSION,
        },
        "observations": store.observations,
        "scope": (
            "Per-file stable custody plus final identity recheck; not an atomic source transaction, "
            "semantic acceptance, runtime relocation, or deletion authority."
        ),
    }
    raw = js(manifest)
    manifest_hash = sha(raw)

    selection: dict[str, Any] = {
        "format": "managed-material-current-selection/1",
        "occurrence": spec["id"],
        "manifest": manifest_hash,
        "current": currents,
        "account": spec.get("account", "Observed selection; no implied acceptance."),
    }
    if retained_selection is not None:
        selection["previous_selection"] = retained_selection
    selection["materializations"] = observed_materialization_state(prior_states, manifest, manifest_hash)
    selection_raw = js(selection)
    selection_hash = sha(selection_raw)

    store.commit_index()
    immutable_fn(store.root / "manifests" / (manifest_hash + ".json"), raw)
    immutable_fn(store.root / "selections" / (selection_hash + ".json"), selection_raw)
    pointer = {
        "format": "managed-material-occurrence-pointer/1",
        "id": spec["id"],
        "manifest": manifest_hash,
        "path": "manifests/" + manifest_hash + ".json",
    }
    immutable_fn(occurrence_path, js(pointer))
    return manifest_hash, selection_hash, effective_policy


def snapshot(store: Any, spec_path: Path | str, *, hooks: dict[str, Any] | None = None) -> dict[str, Any]:
    """Capture declared roots, commit immutable metadata, and publish CURRENT.json."""
    resolved_hooks = resolve_hooks(hooks)
    replace_fn = resolved_hooks["replace_json"]
    spec_file = check_unfollowed_ancestors(spec_path, "Selection spec", include_target=True)
    spec_raw = spec_file.read_bytes()
    spec = _validate_snapshot_spec(loads_json(spec_raw))

    with store_lock(store.root):
        store.reload(create=True)
        store.observations = []
        existing = store.root / "occurrences" / (spec["id"] + ".json")
        require(
            not existing.exists() and not is_reparse_or_symlink(existing),
            "Occurrence identity already exists; retain it and select a new identity",
        )
        attempt = {"id": spec["id"], "spec_sha256": sha(spec_raw), "started_at": utc()}
        store.event("snapshot-started", attempt)
        rows: list[dict[str, Any]] = []
        started = time.monotonic()
        manifest_hash: str | None = None
        selection_hash: str | None = None

        try:
            manifest_hash, selection_hash, policy = _retain_snapshot(
                store,
                spec,
                attempt,
                existing,
                rows,
                resolved_hooks,
            )
            replace_fn(
                store.root / "CURRENT.json",
                {"selection": selection_hash, "path": "selections/" + selection_hash + ".json"},
            )
        except Exception as exc:
            store.commit_index()
            retained = existing.is_file() and not is_reparse_or_symlink(existing)
            failure: dict[str, Any] = {
                **attempt,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "completed_entries": len(rows),
                "selection_published": False,
                "occurrence_retained": retained,
            }
            if manifest_hash is not None:
                failure["manifest"] = manifest_hash
            if selection_hash is not None:
                failure["selection"] = selection_hash
            if retained:
                failure["recovery"] = (
                    "Occurrence and selection were retained before CURRENT.json replacement failed; "
                    "inspect the retained occurrence or select a new occurrence identity."
                )
            store.event("snapshot-failed", failure)
            raise

        try:
            closure = store.closure(row["object"] for row in rows if "object" in row)
            result = {
                "id": spec["id"],
                "manifest": manifest_hash,
                "selection": selection_hash,
                "retention_policy": policy,
                "occurrence_entries": len(rows),
                "file_occurrences": sum(row["type"] == "file" for row in rows),
                "source_bytes": sum(row.get("bytes", 0) for row in rows),
                "seconds": round(time.monotonic() - started, 3),
                **store.stats(closure),
            }
            store.event("snapshot-completed", result)
            return result
        except Exception as exc:
            store.event(
                "snapshot-failed",
                {
                    **attempt,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "completed_entries": len(rows),
                    "selection_published": True,
                    "occurrence_retained": True,
                    "manifest": manifest_hash,
                    "selection": selection_hash,
                },
            )
            raise


def verify_loaded_closure(
    store: Any,
    key: str,
    selections: dict[str, tuple[dict[str, Any], bytes]],
    receipts: dict[str, tuple[dict[str, Any], bytes]],
    manifests: dict[str, tuple[dict[str, Any], bytes]],
    keys: set[str],
) -> dict[str, Any]:
    """Verify every object in an already-loaded closure and release the LRU cache afterward."""
    started = time.monotonic()
    store.clear_cache()
    try:
        for obj in sorted(keys):
            store.materialize(obj)
        summary = {
            "manifest": key,
            "objects_verified": len(keys),
            "historical_manifests_verified": len(manifests),
            "selections_verified": len(selections),
            "lifecycle_receipts_verified": len(receipts),
            "current_materializations": materialization_state(store, selections),
            "file_occurrences_covered": sum(
                row["type"] == "file"
                for manifest, _ in manifests.values()
                for row in manifest["files"]
            ),
            "seconds": round(time.monotonic() - started, 3),
            "exact_recovery": (
                "Every reachable body's original SHA-256 and byte count, including complete "
                "ZIP transports, matched."
            ),
            "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
            **store.stats(keys),
        }
    finally:
        store.clear_cache()
    return summary


def verify(store: Any, occurrence: str | None = None) -> dict[str, Any]:
    """Verify every reachable object and metadata record for an occurrence under shared lock."""
    with store_lock(store.root, shared=True):
        store.reload(create=False)
        key, manifest, _ = read_manifest(store, occurrence)
        selection_key, selected = selected_for_manifest(store, key, manifest)
        selections, receipts, manifests, keys = selection_closure(store, selection_key, selected)
        return verify_loaded_closure(store, key, selections, receipts, manifests, keys)


def retain_artifact(
    store: Any,
    path: Path | str,
    label: str,
    account: str,
    *,
    hooks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add exact historical evidence without changing the main current artifact roles."""
    resolved_hooks = resolve_hooks(hooks)
    immutable_fn = resolved_hooks["immutable"]
    replace_fn = resolved_hooks["replace_json"]

    abs_path = check_unfollowed_ancestors(path, "Retained artifact", include_target=True)
    require(
        abs_path.exists() and stat.S_ISREG(abs_path.lstat().st_mode),
        "Retained artifact must be an existing ordinary file: " + str(abs_path),
    )
    resolved = abs_path.resolve(strict=True)
    safe_relative(label)
    require("/" not in label and bool(account), "Retained artifact needs one label and an account")

    with store_lock(store.root):
        store.reload(create=False)
        before = path_state(resolved)
        raw = resolved.read_bytes()
        require(path_state(resolved) == before, "Historical input changed while reading")

        previous_key, previous = current_selection(store)
        object_key = store.ingest(raw, resolved.name)
        require(
            sha256_file_checked(resolved, label="Retained artifact") == object_key,
            "Historical input changed before retention",
        )

        occurrence = "retained-" + label + "-" + object_key[:16]
        occurrence_path = store.root / "occurrences" / (occurrence + ".json")
        require(
            not occurrence_path.exists() and not is_reparse_or_symlink(occurrence_path),
            "Retained artifact occurrence already exists",
        )

        producer = store.blob(resolved_hooks["producer_bytes"])
        producer_deps = producer_dependencies(store, resolved_hooks)
        manifest = {
            "format": "managed-material-occurrence/1",
            "id": occurrence,
            "started_at": utc(),
            "completed_at": utc(),
            "spec": {"account": account},
            "files": [
                {
                    "path": label,
                    "source_path": str(resolved),
                    "type": "file",
                    "object": object_key,
                    "bytes": len(raw),
                    "mode": before["mode"],
                    "mtime_ns": before["mtime_ns"],
                    "role": "historical-evidence",
                    "materialization": "observed-transport-copy",
                },
            ],
            "history": [],
            "current": [],
            "materialization_groups": [],
            "producer": {"object": producer, "sha256": producer, "dependencies": producer_deps},
            "scope": "Exact explicitly selected historical artifact; main current roles unchanged.",
        }
        manifest_raw = js(manifest)
        manifest_key = sha(manifest_raw)

        selected = {
            **previous,
            "previous_selection": previous_key,
            "additional_manifests": [*previous.get("additional_manifests", []), manifest_key],
        }
        selected_raw = js(selected)
        selected_key = sha(selected_raw)

        store.commit_index()
        immutable_fn(store.root / "manifests" / (manifest_key + ".json"), manifest_raw)
        immutable_fn(store.root / "selections" / (selected_key + ".json"), selected_raw)
        immutable_fn(
            occurrence_path,
            js(
                {
                    "format": "managed-material-occurrence-pointer/1",
                    "id": occurrence,
                    "manifest": manifest_key,
                    "path": "manifests/" + manifest_key + ".json",
                },
            ),
        )
        try:
            replace_fn(
                store.root / "CURRENT.json",
                {"selection": selected_key, "path": "selections/" + selected_key + ".json"},
            )
        except Exception as exc:
            store.event(
                "historical-artifact-retention-failed",
                {
                    "occurrence": occurrence,
                    "manifest": manifest_key,
                    "selection": selected_key,
                    "occurrence_retained": (
                        occurrence_path.is_file() and not is_reparse_or_symlink(occurrence_path)
                    ),
                    "selection_published": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise

        result = {
            "object": object_key,
            "bytes": len(raw),
            "occurrence": occurrence,
            "manifest": manifest_key,
            "selection": selected_key,
            "current_roles_unchanged": True,
        }
        store.event("historical-artifact-retained", result)
        return result
