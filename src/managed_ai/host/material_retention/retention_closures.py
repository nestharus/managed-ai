"""Manifest, selection, representation, materialization-state closures and orphan audit."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .config import IMMUTABLE_RECORD_DIRS
from .store_io import (
    check_unfollowed_descendant,
    is_reparse_or_symlink,
    js,
    loads_json,
    require,
    safe_relative,
    sha,
    store_lock,
)

_HEX_CHARS = frozenset("0123456789abcdef")


def _is_sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in _HEX_CHARS for ch in value)


def _read_store_bytes(store_root: Path, rel_path: str, label: str = "Store record") -> bytes:
    """Read an immutable record below store_root while guarding relative segments."""
    rel = safe_relative(rel_path)
    target = check_unfollowed_descendant(store_root, rel, role=label)
    return target.read_bytes()


def read_manifest(store: Any, occurrence: str | None = None) -> tuple[str, dict[str, Any], bytes]:
    """Read and verify either an explicit occurrence manifest or the current selection manifest."""
    if occurrence:
        safe_relative(occurrence)
        pointer = loads_json(
            _read_store_bytes(store.root, "occurrences/" + occurrence + ".json", "Occurrence pointer"),
        )
        key = pointer["manifest"]
    else:
        current = loads_json(_read_store_bytes(store.root, "CURRENT.json", "Current pointer"))
        sel_raw = _read_store_bytes(store.root, current["path"], "Current selection")
        require(sha(sel_raw) == current["selection"], "Current selection differs")
        key = loads_json(sel_raw)["manifest"]

    require(_is_sha256_hex(key), "Invalid manifest identity")
    raw = _read_store_bytes(store.root, "manifests/" + key + ".json", "Occurrence manifest")
    require(sha(raw) == key, "Occurrence manifest differs")
    return key, loads_json(raw), raw


def manifest_closure(store: Any, key: str) -> tuple[dict[str, tuple[dict[str, Any], bytes]], set[str]]:
    """Compute all historical manifests and content object hashes reachable from manifest key."""
    todo = [key]
    found: dict[str, tuple[dict[str, Any], bytes]] = {}
    material: set[str] = set()

    while todo:
        ident = todo.pop()
        if ident in found:
            continue

        require(_is_sha256_hex(ident), "Invalid manifest identity")
        raw = _read_store_bytes(store.root, "manifests/" + ident + ".json", "Historical manifest")
        require(sha(raw) == ident, "Historical manifest differs")
        manifest = loads_json(raw)
        require(
            manifest["format"] == "managed-material-occurrence/1",
            "Unknown historical manifest format",
        )
        found[ident] = (manifest, raw)
        material.update(row["object"] for row in manifest["files"] if "object" in row)

        producer = manifest.get("producer", {})
        if producer.get("object"):
            material.add(producer["object"])
        deps = producer.get("dependencies")
        if isinstance(deps, dict):
            material.update(deps.values())

        todo.extend(manifest.get("history", []))

    return found, store.closure(material)


def current_selection(store: Any) -> tuple[str, dict[str, Any]]:
    """Read and verify CURRENT.json and the selection document it points to."""
    pointer = loads_json(_read_store_bytes(store.root, "CURRENT.json", "Current pointer"))
    key = pointer["selection"]
    require(pointer["path"] == "selections/" + key + ".json", "Current selection path differs")
    raw = _read_store_bytes(store.root, pointer["path"], "Current selection")
    require(sha(raw) == key, "Current selection differs")
    selected = loads_json(raw)
    require(
        selected["format"] == "managed-material-current-selection/1",
        "Unknown selection format",
    )
    return key, selected


def representation_closure(
    store: Any,
    selections: dict[str, tuple[dict[str, Any], bytes]],
) -> tuple[dict[str, tuple[dict[str, Any], bytes]], set[str], dict[str, bytes]]:
    """Collect verified representation transition records and their immutable metadata dependencies."""
    records: dict[str, tuple[dict[str, Any], bytes]] = {}
    roots: set[str] = set()
    files: dict[str, bytes] = {}
    pending = [
        key
        for selected, _ in selections.values()
        for key in selected.get("representation_transitions", [])
    ]

    while pending:
        key = pending.pop()
        if key in records:
            continue

        require(_is_sha256_hex(key), "Invalid representation transition identity")
        raw = _read_store_bytes(store.root, "transitions/" + key + ".json", "Representation transition")
        require(sha(raw) == key, "Representation transition differs")
        record = loads_json(raw)
        require(
            record["format"] == "managed-material-representation-transition/1",
            "Unknown representation transition",
        )
        records[key] = (record, raw)
        roots.update(record.get("evidence_objects", []))

        for declared in record.get("immutable_records", []):
            relative = safe_relative(declared["path"])
            require(
                len(relative.parts) >= 2
                and relative.parts[0] in IMMUTABLE_RECORD_DIRS
                and relative.suffix == ".json",
                "Invalid transition metadata dependency",
            )
            path = check_unfollowed_descendant(
                store.root,
                relative,
                role="Transition metadata dependency",
            )
            dependency = path.read_bytes()
            require(
                sha(dependency) == declared["sha256"] and len(dependency) == declared["bytes"],
                "Transition metadata dependency differs: " + relative.as_posix(),
            )
            if relative.parts[0] != "occurrences":
                require(relative.stem == declared["sha256"], "Transition metadata name differs")
            files[relative.as_posix()] = dependency
            if relative.parts[0] == "transitions":
                pending.append(relative.stem)

    return records, store.closure(roots), files


def selection_closure(
    store: Any,
    key: str,
    root: dict[str, Any] | None = None,
) -> tuple[
    dict[str, tuple[dict[str, Any], bytes]],
    dict[str, tuple[dict[str, Any], bytes]],
    dict[str, tuple[dict[str, Any], bytes]],
    set[str],
]:
    """Compute retained selections, lifecycle receipts, manifests, and objects for selection key."""
    pending = [key]
    selections: dict[str, tuple[dict[str, Any], bytes]] = {}
    receipts: dict[str, tuple[dict[str, Any], bytes]] = {}
    manifests: dict[str, tuple[dict[str, Any], bytes]] = {}
    objects: set[str] = set()

    while pending:
        ident = pending.pop()
        if ident in selections:
            continue

        require(_is_sha256_hex(ident), "Invalid selection identity")
        raw = (
            js(root)
            if ident == key and root is not None
            else _read_store_bytes(store.root, "selections/" + ident + ".json", "Retained selection")
        )
        require(sha(raw) == ident, "Retained selection differs")
        selected = loads_json(raw)
        require(
            selected["format"] == "managed-material-current-selection/1",
            "Unknown retained selection format",
        )
        selections[ident] = (selected, raw)

        selected_manifests, selected_objects = manifest_closure(store, selected["manifest"])
        manifests.update(selected_manifests)
        objects.update(selected_objects)

        for additional in selected.get("additional_manifests", []):
            more_manifests, more_objects = manifest_closure(store, additional)
            manifests.update(more_manifests)
            objects.update(more_objects)

        if selected.get("previous_selection"):
            pending.append(selected["previous_selection"])

        receipt_ids = set(selected.get("lifecycle", []))
        receipt_ids.update(
            row["receipt"] for row in selected.get("materializations", []) if row.get("receipt")
        )
        for receipt_id in receipt_ids:
            if receipt_id in receipts:
                continue

            require(_is_sha256_hex(receipt_id), "Invalid lifecycle identity")
            receipt_raw = _read_store_bytes(
                store.root,
                "lifecycle/" + receipt_id + ".json",
                "Lifecycle receipt",
            )
            require(sha(receipt_raw) == receipt_id, "Lifecycle receipt differs")
            receipt = loads_json(receipt_raw)
            require(
                receipt["format"] == "managed-material-retirement/1",
                "Unknown lifecycle receipt format",
            )
            receipts[receipt_id] = (receipt, receipt_raw)
            receipt_manifests, receipt_objects = manifest_closure(store, receipt["source_manifest"])
            manifests.update(receipt_manifests)
            objects.update(receipt_objects)
            if receipt.get("previous_selection"):
                pending.append(receipt["previous_selection"])

    _, transition_objects, _ = representation_closure(store, selections)
    objects.update(transition_objects)
    return selections, receipts, manifests, objects


def observed_materialization_state(
    previous: list[dict[str, Any]],
    manifest: dict[str, Any],
    manifest_key: str,
) -> list[dict[str, Any]]:
    """Project observed materialization group states from a captured manifest."""
    states = {(row["prefix"], row["source_path"]): dict(row) for row in previous}
    paths = {row["path"]: row for row in manifest["files"]}

    for group in manifest.get("materialization_groups", []):
        row = paths.get(group["prefix"])
        if row is None:
            continue
        states[(group["prefix"], row["source_path"])] = {
            **group,
            "source_path": row["source_path"],
            "manifest": manifest_key,
            "basis": "captured-group-selection",
        }

    return sorted(states.values(), key=lambda row: (row["prefix"], row["source_path"]))


def materialization_state(
    store: Any,
    selections: dict[str, tuple[dict[str, Any], bytes]],
) -> list[dict[str, Any]]:
    """Derive current disposition across selection lineage without converting history to active work."""
    states: list[dict[str, Any]] = []
    seen_manifests: set[str] = set()
    ordered: list[dict[str, Any]] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(key: str) -> None:
        if key in visited:
            return
        require(key not in visiting, "Cyclic selected-state ancestry")
        visiting.add(key)
        selected = selections[key][0]
        previous = selected.get("previous_selection")
        if previous:
            require(previous in selections, "Missing selected-state ancestor")
            visit(previous)
        visiting.remove(key)
        visited.add(key)
        ordered.append(selected)

    for selected_key in reversed(list(selections)):
        visit(selected_key)

    for selected in ordered:
        manifest_key = selected["manifest"]
        if manifest_key not in seen_manifests:
            raw = _read_store_bytes(
                store.root,
                "manifests/" + manifest_key + ".json",
                "Materialization basis",
            )
            require(sha(raw) == manifest_key, "Materialization basis differs")
            states = observed_materialization_state(states, loads_json(raw), manifest_key)
            seen_manifests.add(manifest_key)

        by_identity = {(row["prefix"], row["source_path"]): row for row in states}
        for materialization in selected.get("materializations", []):
            row = dict(materialization)
            if row.get("receipt"):
                receipt_raw = _read_store_bytes(
                    store.root,
                    "lifecycle/" + row["receipt"] + ".json",
                    "Materialization receipt",
                )
                require(sha(receipt_raw) == row["receipt"], "Materialization receipt differs")
                receipt = loads_json(receipt_raw)
                matches = [item for item in receipt["retired"] if item["prefix"] == row["prefix"]]
                require(len(matches) == 1, "Materialization lacks an unambiguous retirement basis")
                row["source_path"] = matches[0]["source_path"]
                row["manifest"] = receipt["source_manifest"]
                row["basis"] = "retirement-receipt"
            require(row.get("source_path"), "Materialization lacks a source identity")
            by_identity[(row["prefix"], row["source_path"])] = row
        states = list(by_identity.values())

    return sorted(states, key=lambda row: (row["prefix"], row["source_path"]))


def selected_for_manifest(
    store: Any,
    manifest_key: str,
    manifest: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Find the retained selection matching manifest_key or synthesize a historical selection view."""
    current_key, _ = current_selection(store)
    selections = selection_closure(store, current_key)[0]
    for key, (candidate, _) in selections.items():
        if candidate["manifest"] == manifest_key:
            return key, candidate

    selected = {
        "format": "managed-material-current-selection/1",
        "occurrence": manifest["id"],
        "manifest": manifest_key,
        "current": manifest["current"],
        "materializations": observed_materialization_state([], manifest, manifest_key),
        "account": (
            "Explicit historical occurrence; no retained selected-state ancestry "
            "was available in the current lineage."
        ),
    }
    return sha(js(selected)), selected


def _walk_ordinary_json_files(folder: Path, *, strict_entries: bool) -> list[Path]:
    """Return sorted ordinary JSON files under folder without traversing symlinks or reparse points."""
    if not folder.exists() and not is_reparse_or_symlink(folder):
        return []
    if is_reparse_or_symlink(folder):
        if strict_entries:
            raise ValueError(
                "Immutable record directory must not be a symlink or reparse point: " + str(folder),
            )
        return []
    if not folder.is_dir():
        return []

    found: list[Path] = []
    for parent, dirs, files in os.walk(folder, followlinks=False):
        dirs.sort()
        files.sort()
        kept_dirs: list[str] = []
        for dirname in dirs:
            candidate = Path(parent) / dirname
            if is_reparse_or_symlink(candidate):
                if strict_entries:
                    raise ValueError(
                        "Immutable record directory must not traverse a symlink or reparse point: "
                        + str(candidate),
                    )
                continue
            kept_dirs.append(dirname)
        dirs[:] = kept_dirs

        for name in files:
            if not name.endswith(".json"):
                continue
            fpath = Path(parent) / name
            if is_reparse_or_symlink(fpath):
                if strict_entries:
                    raise ValueError(
                        "Immutable record must not be a symlink or reparse point: " + str(fpath),
                    )
                continue
            found.append(fpath)

    return sorted(found)


def _collect_published_state(store: Any) -> tuple[set[str], set[str]]:
    """Collect occurrence IDs and manifest hashes currently published in CURRENT.json lineage."""
    published_ids: set[str] = set()
    published_manifests: set[str] = set()
    if (store.root / "CURRENT.json").exists():
        current_key, _ = current_selection(store)
        selections, _, manifests, _ = selection_closure(store, current_key)
        published_manifests.update(manifests)
        published_ids.update(manifest["id"] for manifest, _ in manifests.values())
        published_ids.update(
            sel["occurrence"] for sel, _ in selections.values() if "occurrence" in sel
        )
    return published_ids, published_manifests


def _index_failure_events(
    store: Any,
    published_ids: set[str],
    published_manifests: set[str],
) -> dict[str, dict[str, Any]]:
    """Parse events.jsonl to reconcile completed publications and retained pre-publish failures."""
    failed: dict[str, dict[str, Any]] = {}
    events_path = store.root / "events.jsonl"
    if not events_path.exists() and not is_reparse_or_symlink(events_path):
        return failed

    require(
        not is_reparse_or_symlink(events_path),
        "Event log must not be a symlink or reparse point: " + str(events_path),
    )
    if not events_path.is_file():
        return failed

    for line in events_path.read_bytes().splitlines():
        if not line.strip():
            continue
        row = loads_json(line)
        kind = row.get("kind")

        if kind == "snapshot-completed":
            if row.get("id"):
                published_ids.add(row["id"])
                failed.pop(row["id"], None)
            if row.get("manifest"):
                published_manifests.add(row["manifest"])
        elif kind == "historical-artifact-retained":
            if row.get("occurrence"):
                published_ids.add(row["occurrence"])
                failed.pop(row["occurrence"], None)
            if row.get("manifest"):
                published_manifests.add(row["manifest"])
        elif kind == "snapshot-failed" and row.get("id"):
            _record_failed_event(
                row,
                ident=row["id"],
                reason="snapshot-failed-before-current-publish",
                published_ids=published_ids,
                published_manifests=published_manifests,
                failed=failed,
            )
        elif kind == "historical-artifact-retention-failed" and row.get("occurrence"):
            _record_failed_event(
                row,
                ident=row["occurrence"],
                reason="historical-artifact-retention-failed-before-current-publish",
                published_ids=published_ids,
                published_manifests=published_manifests,
                failed=failed,
            )

    return failed


def _record_failed_event(
    row: dict[str, Any],
    *,
    ident: str,
    reason: str,
    published_ids: set[str],
    published_manifests: set[str],
    failed: dict[str, dict[str, Any]],
) -> None:
    if row.get("selection_published"):
        published_ids.add(ident)
        failed.pop(ident, None)
        if row.get("manifest"):
            published_manifests.add(row["manifest"])
    elif row.get("occurrence_retained"):
        failed[ident] = {
            "id": ident,
            "manifest": row.get("manifest"),
            "selection": row.get("selection"),
            "error": row.get("error"),
            "reason": reason,
        }


def _index_selections_by_manifest(store: Any) -> dict[str, str]:
    """Map manifest hashes to retained selection hashes for orphaned occurrence reporting."""
    selection_by_manifest: dict[str, str] = {}
    for spath in _walk_ordinary_json_files(store.root / "selections", strict_entries=False):
        sraw = spath.read_bytes()
        skey = sha(sraw)
        if spath.stem != skey:
            continue
        sel = loads_json(sraw)
        if sel.get("format") != "managed-material-current-selection/1":
            continue
        if sel.get("manifest"):
            selection_by_manifest.setdefault(sel["manifest"], skey)
        for extra_manifest in sel.get("additional_manifests", []):
            selection_by_manifest.setdefault(extra_manifest, skey)
    return selection_by_manifest


def _build_unpublished_entries(
    store: Any,
    published_ids: set[str],
    published_manifests: set[str],
    failed: dict[str, dict[str, Any]],
    selection_by_manifest: dict[str, str],
) -> list[dict[str, Any]]:
    """Inspect occurrences/*.json and return entries that were never published to CURRENT.json."""
    results: list[dict[str, Any]] = []
    for opath in _walk_ordinary_json_files(store.root / "occurrences", strict_entries=True):
        pointer = loads_json(opath.read_bytes())
        ident = pointer.get("id", opath.stem)
        manifest_key = pointer.get("manifest")
        if ident in published_ids or (manifest_key and manifest_key in published_manifests):
            continue

        info = failed.get(ident)
        if info is not None:
            entry = dict(info)
            if not entry.get("manifest"):
                entry["manifest"] = manifest_key
            if not entry.get("selection") and manifest_key:
                entry["selection"] = selection_by_manifest.get(manifest_key)
        else:
            entry = {
                "id": ident,
                "manifest": manifest_key,
                "selection": selection_by_manifest.get(manifest_key) if manifest_key else None,
                "error": None,
                "reason": "unconfirmed-occurrence-not-published",
            }
        results.append(entry)

    return sorted(results, key=lambda row: row["id"])


def unpublished_occurrences(store: Any) -> list[dict[str, Any]]:
    """Report retained occurrences that were never published to CURRENT.json."""
    with store_lock(store.root, shared=True):
        if (store.root / "index.json").exists():
            store.reload(create=False)

        published_ids, published_manifests = _collect_published_state(store)
        failed = _index_failure_events(store, published_ids, published_manifests)
        selection_by_manifest = _index_selections_by_manifest(store)
        return _build_unpublished_entries(
            store,
            published_ids,
            published_manifests,
            failed,
            selection_by_manifest,
        )
