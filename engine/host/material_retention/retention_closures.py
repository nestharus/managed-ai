"""Manifest, selection, representation, materialization-state closures and orphan audit for managed_material."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys


def _load_store_io():
    if __package__:
        from . import store_io as module
        return module
    path = Path(__file__).resolve().with_name("store_io.py")
    key = "_managed_material_store_io_" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    module = sys.modules.get(key)
    if module is None:
        spec = importlib.util.spec_from_file_location(key, path)
        if spec is None or spec.loader is None:
            raise ImportError("Unable to load store_io module: " + str(path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(key, None)
            raise
    return module


_io = _load_store_io()
IMMUTABLE_RECORD_DIRS = _io.IMMUTABLE_RECORD_DIRS
js = _io.js
loads_json = _io.loads_json
require = _io.require
safe_relative = _io.safe_relative
sha = _io.sha
store_lock = _io.store_lock


def read_manifest(store, occurrence=None):
    if occurrence:
        safe_relative(occurrence)
        p = loads_json((store.root / "occurrences" / (occurrence + ".json")).read_bytes())
        key = p["manifest"]
    else:
        c = loads_json((store.root / "CURRENT.json").read_bytes())
        sraw = (store.root / c["path"]).read_bytes()
        require(sha(sraw) == c["selection"], "Current selection differs")
        key = loads_json(sraw)["manifest"]
    raw = (store.root / "manifests" / (key + ".json")).read_bytes()
    require(sha(raw) == key, "Occurrence manifest differs")
    return key, loads_json(raw), raw


def manifest_closure(store, key):
    todo, found, material = [key], {}, set()
    while todo:
        ident = todo.pop()
        if ident in found:
            continue
        require(len(ident) == 64 and all(c in "0123456789abcdef" for c in ident), "Invalid manifest identity")
        raw = (store.root / "manifests" / (ident + ".json")).read_bytes()
        require(sha(raw) == ident, "Historical manifest differs")
        manifest = loads_json(raw)
        require(manifest["format"] == "managed-material-occurrence/1", "Unknown historical manifest format")
        found[ident] = (manifest, raw)
        material.update(r["object"] for r in manifest["files"] if "object" in r)
        producer = manifest.get("producer", {})
        if producer.get("object"):
            material.add(producer["object"])
        deps = producer.get("dependencies")
        if isinstance(deps, dict):
            material.update(deps.values())
        todo.extend(manifest.get("history", []))
    return found, store.closure(material)


def current_selection(store):
    pointer = loads_json((store.root / "CURRENT.json").read_bytes())
    key = pointer["selection"]
    require(pointer["path"] == "selections/" + key + ".json", "Current selection path differs")
    raw = (store.root / pointer["path"]).read_bytes()
    require(sha(raw) == key, "Current selection differs")
    selected = loads_json(raw)
    require(selected["format"] == "managed-material-current-selection/1", "Unknown selection format")
    return key, selected


def representation_closure(store, selections):
    """A transition's declared metadata files are exact recovery dependencies.

    Include them as historical records; do not make their selections current.
    Earlier declared transition records carry their own dependencies onward.
    """
    records, roots, files = {}, set(), {}
    pending = [key for selected, _ in selections.values()
               for key in selected.get("representation_transitions", [])]
    while pending:
        key = pending.pop()
        if key in records:
            continue
        require(len(key) == 64 and all(c in "0123456789abcdef" for c in key),
                "Invalid representation transition identity")
        raw = (store.root / "transitions" / (key + ".json")).read_bytes()
        require(sha(raw) == key, "Representation transition differs")
        record = loads_json(raw)
        require(record["format"] == "managed-material-representation-transition/1", "Unknown representation transition")
        records[key] = (record, raw)
        roots.update(record.get("evidence_objects", []))
        for declared in record.get("immutable_records", []):
            relative = safe_relative(declared["path"])
            require(len(relative.parts) >= 2 and relative.parts[0] in IMMUTABLE_RECORD_DIRS
                    and relative.suffix == ".json", "Invalid transition metadata dependency")
            path = store.root
            for part in relative.parts:
                path = path / part
                require(not path.is_symlink(), "Transition metadata dependency must not traverse a symlink")
            dependency = path.read_bytes()
            require(sha(dependency) == declared["sha256"] and len(dependency) == declared["bytes"],
                    "Transition metadata dependency differs: " + relative.as_posix())
            if relative.parts[0] != "occurrences":
                require(relative.stem == declared["sha256"], "Transition metadata name differs")
            files[relative.as_posix()] = dependency
            if relative.parts[0] == "transitions":
                pending.append(relative.stem)
    return records, store.closure(roots), files


def selection_closure(store, key, root=None):
    """Retained selected state includes its causal receipts and their manifests."""
    pending, selections, receipts, manifests, objects = [key], {}, {}, {}, set()
    while pending:
        ident = pending.pop()
        if ident in selections:
            continue
        require(len(ident) == 64 and all(c in "0123456789abcdef" for c in ident), "Invalid selection identity")
        raw = js(root) if ident == key and root is not None else (store.root / "selections" / (ident + ".json")).read_bytes()
        require(sha(raw) == ident, "Retained selection differs")
        selected = loads_json(raw)
        require(selected["format"] == "managed-material-current-selection/1", "Unknown retained selection format")
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
        receipt_ids.update(r["receipt"] for r in selected.get("materializations", []) if r.get("receipt"))
        for receipt_id in receipt_ids:
            if receipt_id in receipts:
                continue
            require(len(receipt_id) == 64 and all(c in "0123456789abcdef" for c in receipt_id), "Invalid lifecycle identity")
            receipt_raw = (store.root / "lifecycle" / (receipt_id + ".json")).read_bytes()
            require(sha(receipt_raw) == receipt_id, "Lifecycle receipt differs")
            receipt = loads_json(receipt_raw)
            require(receipt["format"] == "managed-material-retirement/1", "Unknown lifecycle receipt format")
            receipts[receipt_id] = (receipt, receipt_raw)
            receipt_manifests, receipt_objects = manifest_closure(store, receipt["source_manifest"])
            manifests.update(receipt_manifests)
            objects.update(receipt_objects)
            if receipt.get("previous_selection"):
                pending.append(receipt["previous_selection"])
    _, transition_objects, _ = representation_closure(store, selections)
    objects.update(transition_objects)
    return selections, receipts, manifests, objects


def observed_materialization_state(previous, manifest, manifest_key):
    states = {(r["prefix"], r["source_path"]): dict(r) for r in previous}
    paths = {r["path"]: r for r in manifest["files"]}
    for group in manifest.get("materialization_groups", []):
        row = paths.get(group["prefix"])
        # An absent old group is not evidence that its retired copy reappeared.
        if row is None:
            continue
        states[(group["prefix"], row["source_path"])] = {**group, "source_path": row["source_path"],
                                                          "manifest": manifest_key, "basis": "captured-group-selection"}
    return sorted(states.values(), key=lambda r: (r["prefix"], r["source_path"]))


def materialization_state(store, selections):
    """Derive current disposition without converting retained history to active work."""
    states, seen_manifests, ordered, visited, visiting = [], set(), [], set(), set()

    def visit(key):
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
            raw = (store.root / "manifests" / (manifest_key + ".json")).read_bytes()
            require(sha(raw) == manifest_key, "Materialization basis differs")
            states = observed_materialization_state(states, loads_json(raw), manifest_key)
            seen_manifests.add(manifest_key)
        by_identity = {(r["prefix"], r["source_path"]): r for r in states}
        for materialization in selected.get("materializations", []):
            row = dict(materialization)
            if row.get("receipt"):
                receipt_raw = (store.root / "lifecycle" / (row["receipt"] + ".json")).read_bytes()
                require(sha(receipt_raw) == row["receipt"], "Materialization receipt differs")
                receipt = loads_json(receipt_raw)
                matches = [r for r in receipt["retired"] if r["prefix"] == row["prefix"]]
                require(len(matches) == 1, "Materialization lacks an unambiguous retirement basis")
                row["source_path"] = matches[0]["source_path"]
                row["manifest"] = receipt["source_manifest"]
                row["basis"] = "retirement-receipt"
            require(row.get("source_path"), "Materialization lacks a source identity")
            by_identity[(row["prefix"], row["source_path"])] = row
        states = list(by_identity.values())
    return sorted(states, key=lambda r: (r["prefix"], r["source_path"]))


def selected_for_manifest(store, manifest_key, manifest):
    current_key, selected = current_selection(store)
    selections = selection_closure(store, current_key)[0]
    for key, (candidate, _) in selections.items():
        if candidate["manifest"] == manifest_key:
            return key, candidate
    # A deliberately omitted ancestry may still be requested as body history.
    selected = {"format": "managed-material-current-selection/1", "occurrence": manifest["id"],
                "manifest": manifest_key, "current": manifest["current"],
                "materializations": observed_materialization_state([], manifest, manifest_key),
                "account": "Explicit historical occurrence; no retained selected-state ancestry was available in the current lineage."}
    return sha(js(selected)), selected


def unpublished_occurrences(store):
    """Report retained occurrences that were never published to CURRENT.json."""
    with store_lock(store.root, shared=True):
        if (store.root / "index.json").exists():
            store.reload(create=False)
        published_ids = set()
        published_manifests = set()
        if (store.root / "CURRENT.json").exists():
            current_key, _ = current_selection(store)
            selections, _, manifests, _ = selection_closure(store, current_key)
            published_manifests.update(manifests)
            published_ids.update(m["id"] for m, _ in manifests.values())
            published_ids.update(s["occurrence"] for s, _ in selections.values() if "occurrence" in s)
        failed = {}
        events_path = store.root / "events.jsonl"
        if events_path.is_file():
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
                    if row.get("selection_published"):
                        published_ids.add(row["id"])
                        failed.pop(row["id"], None)
                        if row.get("manifest"):
                            published_manifests.add(row["manifest"])
                    elif row.get("occurrence_retained"):
                        failed[row["id"]] = {
                            "id": row["id"],
                            "manifest": row.get("manifest"),
                            "selection": row.get("selection"),
                            "error": row.get("error"),
                            "reason": "snapshot-failed-before-current-publish",
                        }
                elif kind == "historical-artifact-retention-failed" and row.get("occurrence"):
                    if row.get("selection_published"):
                        published_ids.add(row["occurrence"])
                        failed.pop(row["occurrence"], None)
                        if row.get("manifest"):
                            published_manifests.add(row["manifest"])
                    elif row.get("occurrence_retained"):
                        failed[row["occurrence"]] = {
                            "id": row["occurrence"],
                            "manifest": row.get("manifest"),
                            "selection": row.get("selection"),
                            "error": row.get("error"),
                            "reason": "historical-artifact-retention-failed-before-current-publish",
                        }
        selection_by_manifest = {}
        selections_dir = store.root / "selections"
        if selections_dir.is_dir():
            for spath in sorted(selections_dir.rglob("*.json")):
                if spath.is_symlink():
                    continue
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
        results = []
        occurrences_dir = store.root / "occurrences"
        if occurrences_dir.is_dir():
            for opath in sorted(occurrences_dir.rglob("*.json")):
                require(not opath.is_symlink(), "Immutable record must not be a symlink")
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
        return sorted(results, key=lambda r: r["id"])
