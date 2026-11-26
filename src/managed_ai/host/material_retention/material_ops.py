"""Snapshot, verify, export, restore, rebuild, and historical artifact retention operations."""
from __future__ import annotations

import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import stat
import sys
import time
import zipfile
import zlib


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
_core = _load_sibling("store_core")

FORMAT = _io.FORMAT
IMMUTABLE_RECORD_DIRS = _io.IMMUTABLE_RECORD_DIRS
check_unfollowed_ancestors = _io.check_unfollowed_ancestors
immutable = _io.immutable
is_reparse_or_symlink = _io.is_reparse_or_symlink
js = _io.js
loads_json = _io.loads_json
path_state = _io.path_state
replace_json = _io.replace_json
require = _io.require
safe_relative = _io.safe_relative
sha = _io.sha
store_lock = _io.store_lock
utc = _io.utc

current_selection = _closures.current_selection
manifest_closure = _closures.manifest_closure
materialization_state = _closures.materialization_state
observed_materialization_state = _closures.observed_materialization_state
read_manifest = _closures.read_manifest
representation_closure = _closures.representation_closure
selected_for_manifest = _closures.selected_for_manifest
selection_closure = _closures.selection_closure

Store = _core.Store

_SIBLING_FILES = ("store_io.py", "retention_closures.py", "store_core.py", "material_ops.py", "retirement.py")


def _default_hooks():
    here = Path(__file__).resolve().parent
    return {
        "immutable": immutable,
        "replace_json": replace_json,
        "producer_bytes": (here / "managed_material.py").read_bytes(),
        "sibling_bytes": {name: (here / name).read_bytes() for name in _SIBLING_FILES},
    }


def _resolve_hooks(hooks):
    if hooks is None:
        return _default_hooks()
    return hooks


def _producer_dependencies(store, hooks):
    return {name: store.blob(raw) for name, raw in sorted(hooks["sibling_bytes"].items())}


def _snapshot_history(store, spec):
    prior_key, prior_selection = current_selection(store) if (store.root / "CURRENT.json").exists() else (None, None)
    # A new current-use selection does not inherit every earlier delivery.
    # An explicitly supplied legacy history list remains an intentional
    # selection. Append-only ancestry is available only by an explicit policy.
    policy = spec.get("retention_policy")
    require(policy in (None, "current-use", "append-history"),
            "retention_policy must be current-use or append-history")
    history = spec.get("history", [])
    require(isinstance(history, list), "History must name exact prior manifest hashes")
    require(not (policy == "current-use" and history),
            "current-use conflicts with nonempty history; omit the policy to select exact history")
    history = list(history)
    if policy == "append-history" and prior_selection is not None:
        if prior_selection["manifest"] not in history:
            history.insert(0, prior_selection["manifest"])
    effective_policy = policy or ("selected-history" if history else "current-use")
    history_account = spec.get("history_account")
    if effective_policy == "current-use" and not history_account:
        history_account = ("Current-use selection retains the declared roots without implicit "
                           "ancestry. Earlier managed objects are not deleted.")
    retained_manifests = set()
    for old in history:
        retained_manifests.update(manifest_closure(store, old)[0])
    retain_prior_selection = prior_selection is not None and prior_selection["manifest"] in retained_manifests
    if prior_selection is not None and not retain_prior_selection:
        require(history_account, "Omitting the prior selected-state ancestry needs an explicit history account")
    prior_states = materialization_state(store, selection_closure(store, prior_key)[0]) if retain_prior_selection else []
    return history, effective_policy, history_account, prior_key if retain_prior_selection else None, prior_states


def _acquire_snapshot_roots(store, spec, rows):
    exclusions, source_checks, labels = [], [], set()
    for root in spec["roots"]:
        label = root["label"]
        safe_relative(label)
        require(label not in labels and "/" not in label, "Unique single-component root labels required")
        labels.add(label)
        base = check_unfollowed_ancestors(root["path"], "Selected root", include_target=False)
        require(base.exists() or base.is_symlink(), "Selected root absent: " + str(base))
        if is_reparse_or_symlink(base) and not base.is_symlink():
            raise ValueError("Special filesystem object or reparse point requires explicit handling: " + str(base))
        require(store.root.resolve() != base.resolve() and base.resolve() not in store.root.resolve().parents,
                "Store must not be inside a scanned source root")
        paths = [base]
        if base.is_dir() and not is_reparse_or_symlink(base):
            for parent, dirs, files in os.walk(base, followlinks=False):
                dirs.sort()
                files.sort()
                kept_dirs = []
                for name in dirs:
                    candidate = Path(parent) / name
                    if is_reparse_or_symlink(candidate) and not candidate.is_symlink():
                        paths.append(candidate)
                    else:
                        kept_dirs.append(name)
                        paths.append(candidate)
                dirs[:] = kept_dirs
                paths.extend(Path(parent) / x for x in files)
        for path in paths:
            rel = str(path.relative_to(base)).replace(os.sep, "/") if path != base else ""
            logical = label + ("/" + rel if rel else "")
            if any(fnmatch.fnmatchcase(logical, pattern) for pattern in spec.get("exclude", [])):
                exclusions.append({"path": logical, "reason": "explicit selection exclusion"})
                continue
            before = path_state(path)
            row = {"path": logical, "source_path": str(path), "mode": before["mode"],
                   "mtime_ns": before["mtime_ns"], "role": root.get("role", "observed-material"),
                   "materialization": root.get("materialization", "observed-working-copy")}
            if path.is_symlink():
                raw = os.fsencode(os.readlink(path))
                key = store.blob(raw)
                row.update({"type": "symlink", "object": key, "bytes": len(raw)})
            elif is_reparse_or_symlink(path):
                raise ValueError("Special filesystem object or reparse point requires explicit handling: " + str(path))
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


def _retain_snapshot(store, spec, attempt, occurrence_path, rows, hooks):
    immutable_fn = hooks["immutable"]
    producer_object = store.blob(hooks["producer_bytes"])
    producer_deps = _producer_dependencies(store, hooks)
    history, effective_policy, history_account, retained_selection, prior_states = _snapshot_history(store, spec)
    exclusions, source_checks = _acquire_snapshot_roots(store, spec, rows)
    logicals = [r["path"] for r in rows]
    require(len(logicals) == len(set(logicals)), "Duplicate occurrence path")
    by_path = {r["path"]: r for r in rows}
    currents = []
    for current in spec.get("current", []):
        selected = by_path.get(current["path"])
        require(selected is not None and "object" in selected, "Current choice absent from observed occurrence: " + current["path"])
        if current.get("expected_sha256"):
            require(selected["object"] == current["expected_sha256"], "Current selection identity differs")
        currents.append({**current, "object": selected["object"], "bytes": selected["bytes"]})
    for check in source_checks:
        path = Path(check["path"])
        require(not is_reparse_or_symlink(path), "Source changed to symlink or reparse point before publication: " + str(path))
        require(path_state(path) == check["state"] and sha(path.read_bytes()) == check["sha256"],
                "Source changed before selection publication: " + str(path))
    manifest = {"format": "managed-material-occurrence/1", "id": spec["id"],
                "started_at": attempt["started_at"], "completed_at": utc(),
                "spec": spec, "spec_sha256": attempt["spec_sha256"], "files": rows, "excluded": exclusions,
                "current": currents, "lineage": spec.get("lineage", []),
                "history": history,
                "retention": {"policy": effective_policy, "history_account": history_account},
                "materialization_groups": spec.get("materialization_groups", []),
                "producer": {"sha256": producer_object, "object": producer_object,
                             "dependencies": producer_deps, "python": platform.python_version(),
                             "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION, "zlib_compile": zlib.ZLIB_VERSION},
                "observations": store.observations,
                "scope": "Per-file stable custody plus final identity recheck; not an atomic source transaction, semantic acceptance, runtime relocation, or deletion authority."}
    raw = js(manifest)
    manifest_hash = sha(raw)
    selection = {"format": "managed-material-current-selection/1", "occurrence": spec["id"],
                 "manifest": manifest_hash, "current": currents,
                 "account": spec.get("account", "Observed selection; no implied acceptance.")}
    if retained_selection is not None:
        selection["previous_selection"] = retained_selection
    selection["materializations"] = observed_materialization_state(prior_states, manifest, manifest_hash)
    selection_raw = js(selection)
    selection_hash = sha(selection_raw)
    store.commit_index()
    immutable_fn(store.root / "manifests" / (manifest_hash + ".json"), raw)
    immutable_fn(store.root / "selections" / (selection_hash + ".json"), selection_raw)
    pointer = {"format": "managed-material-occurrence-pointer/1", "id": spec["id"], "manifest": manifest_hash,
               "path": "manifests/" + manifest_hash + ".json"}
    immutable_fn(occurrence_path, js(pointer))
    return manifest_hash, selection_hash, effective_policy


def snapshot(store, spec_path, *, hooks=None):
    hooks = _resolve_hooks(hooks)
    replace_fn = hooks["replace_json"]
    spec_file = check_unfollowed_ancestors(spec_path, "Selection spec", include_target=True)
    spec_raw = spec_file.read_bytes()
    spec = loads_json(spec_raw)
    require(spec.get("format") == "managed-material-selection/1", "Unknown selection spec")
    require(spec.get("id"), "Explicit occurrence identity is required")
    safe_relative(spec["id"])
    with store_lock(store.root):
        store.reload(create=True)
        store.observations = []
        existing = store.root / "occurrences" / (spec["id"] + ".json")
        require(not existing.exists() and not is_reparse_or_symlink(existing),
                "Occurrence identity already exists; retain it and select a new identity")
        attempt = {"id": spec["id"], "spec_sha256": sha(spec_raw), "started_at": utc()}
        store.event("snapshot-started", attempt)
        rows = []
        started = time.monotonic()
        manifest_hash = None
        selection_hash = None
        try:
            manifest_hash, selection_hash, policy = _retain_snapshot(store, spec, attempt, existing, rows, hooks)
            replace_fn(store.root / "CURRENT.json", {"selection": selection_hash,
                                                     "path": "selections/" + selection_hash + ".json"})
        except Exception as exc:
            store.commit_index()
            retained = existing.is_file() and not is_reparse_or_symlink(existing)
            failure = {**attempt, "error_type": type(exc).__name__, "error": str(exc),
                       "completed_entries": len(rows), "selection_published": False,
                       "occurrence_retained": retained}
            if manifest_hash is not None:
                failure["manifest"] = manifest_hash
            if selection_hash is not None:
                failure["selection"] = selection_hash
            if retained:
                failure["recovery"] = ("Occurrence and selection were retained before CURRENT.json replacement failed; "
                                       "inspect the retained occurrence or select a new occurrence identity.")
            store.event("snapshot-failed", failure)
            raise

        try:
            closure = store.closure(r["object"] for r in rows if "object" in r)
            result = {"id": spec["id"], "manifest": manifest_hash, "selection": selection_hash,
                      "retention_policy": policy,
                      "occurrence_entries": len(rows), "file_occurrences": sum(r["type"] == "file" for r in rows),
                      "source_bytes": sum(r.get("bytes", 0) for r in rows), "seconds": round(time.monotonic() - started, 3),
                      **store.stats(closure)}
            store.event("snapshot-completed", result)
            return result
        except Exception as exc:
            store.event("snapshot-failed", {**attempt, "error_type": type(exc).__name__, "error": str(exc),
                                            "completed_entries": len(rows), "selection_published": True,
                                            "occurrence_retained": True,
                                            "manifest": manifest_hash, "selection": selection_hash})
            raise


def verify(store, occurrence=None):
    with store_lock(store.root, shared=True):
        store.reload(create=False)
        key, manifest, _ = read_manifest(store, occurrence)
        selection_key, selected = selected_for_manifest(store, key, manifest)
        selections, receipts, manifests, keys = selection_closure(store, selection_key, selected)
        started = time.monotonic()
        store.cache.clear(); store.cache_bytes = 0
        for obj in sorted(keys):
            store.materialize(obj)
        return {"manifest": key, "objects_verified": len(keys), "historical_manifests_verified": len(manifests),
                "selections_verified": len(selections), "lifecycle_receipts_verified": len(receipts),
                "current_materializations": materialization_state(store, selections),
                "file_occurrences_covered": sum(r["type"] == "file" for m, _ in manifests.values() for r in m["files"]),
                "seconds": round(time.monotonic() - started, 3),
                "exact_recovery": "Every reachable body's original SHA-256 and byte count, including complete ZIP transports, matched.",
                "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION, **store.stats(keys)}


def export(store, output, occurrence=None, *, hooks=None):
    hooks = _resolve_hooks(hooks)
    immutable_fn = hooks["immutable"]
    output = check_unfollowed_ancestors(output, "Export output", include_target=True)
    with store_lock(store.root, shared=True):
        store.reload(create=False)
        key, manifest, manifest_raw = read_manifest(store, occurrence)
        require(not output.exists() and not is_reparse_or_symlink(output),
                "Preserve prior export; select a new output identity")
        selection_key, selection = selected_for_manifest(store, key, manifest)
        selections, receipts, manifests, keys = selection_closure(store, selection_key, selection)
        # Export is constructed from a verified reference closure, never a folder walk.
        checked = verify(store, occurrence)
        files = {"index.json": js({"format": FORMAT, "objects": {k: store.objects[k] for k in sorted(keys)}}),
                 "manifests/" + key + ".json": manifest_raw,
                 "occurrences/" + manifest["id"] + ".json": js({"format": "managed-material-occurrence-pointer/1",
                     "id": manifest["id"], "manifest": key, "path": "manifests/" + key + ".json"}),
                 "managed_material.py": hooks["producer_bytes"],
                 **hooks["sibling_bytes"],
                 "RECOVERY.json": js({"format": "managed-material-recovery/1", "manifest": key,
                     "verify": "python managed_material.py verify --store . --occurrence " + manifest["id"],
                     "restore": "python managed_material.py restore --store . --occurrence " + manifest["id"] + " --output ../restored",
                     "codec_boundary": "Stored bodies decode with DEFLATE. Historical deflate-recipe parts require the recorded encoder or another encoder producing the same pinned compressed hash; mismatch refuses exact replay.",
                     "verified": checked})}
        files["CURRENT.json"] = js({"selection": selection_key, "path": "selections/" + selection_key + ".json"})
        for selected_key, (_, selected_raw) in selections.items():
            files["selections/" + selected_key + ".json"] = selected_raw
        for receipt_key, (_, receipt_raw) in receipts.items():
            files["lifecycle/" + receipt_key + ".json"] = receipt_raw
        transitions, _, transition_files = representation_closure(store, selections)
        files.update(transition_files)
        for transition_key, (_, transition_raw) in transitions.items():
            files["transitions/" + transition_key + ".json"] = transition_raw
        for old_key, (old_manifest, old_raw) in manifests.items():
            files["manifests/" + old_key + ".json"] = old_raw
            files["occurrences/" + old_manifest["id"] + ".json"] = js({"format": "managed-material-occurrence-pointer/1",
                "id": old_manifest["id"], "manifest": old_key, "path": "manifests/" + old_key + ".json"})
        for obj in sorted(keys):
            row = store.objects[obj]
            if "path" in row:
                files[row["path"]] = store.root / row["path"]
            if row["kind"] == "record-view":
                files[row["record_path"]] = store.root / row["record_path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        manifest_entries = []
        with zipfile.ZipFile(output, "x") as z:
            for name, source in sorted(files.items()):
                if isinstance(source, Path):
                    require(not is_reparse_or_symlink(source), "Export source must not be a symlink or reparse point: " + str(source))
                    raw = source.read_bytes()
                else:
                    raw = source
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.external_attr = (0o644 | stat.S_IFREG) << 16
                info.compress_type = zipfile.ZIP_STORED if name.endswith(".deflate") else zipfile.ZIP_DEFLATED
                z.writestr(info, raw)
                manifest_entries.append({"path": name, "sha256": sha(raw), "bytes": len(raw)})
            info = zipfile.ZipInfo("EXPORT-MANIFEST.json", (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, js({"format": "managed-material-export/1", "source_manifest": key,
                                "entries": manifest_entries, "nested_archive_payloads": 0}))
        with zipfile.ZipFile(output) as z:
            for row in manifest_entries:
                require(sha(z.read(row["path"])) == row["sha256"], "Export member readback differs")
        result = {"output": str(output), "sha256": sha(output.read_bytes()), "bytes": output.stat().st_size,
                  "source_manifest": key, "archive_entries": len(files) + 1, "nested_zip_files": 0,
                  "historical_manifests": len(manifests),
                  "retained_selections": len(selections), "retained_lifecycle_receipts": len(receipts),
                  "source_occurrence_bytes": sum(r.get("bytes", 0) for r in manifest["files"]),
                  "recovery_verified": True, **store.stats(keys)}
    immutable_fn(output.with_suffix(output.suffix + ".receipt.json"), js(result))
    store.event("export-completed", result)
    return result


def restore(store, output, occurrence=None, prefix=None, *, hooks=None):
    hooks = _resolve_hooks(hooks)
    immutable_fn = hooks["immutable"]
    output = check_unfollowed_ancestors(output, "Restore target", include_target=True)
    with store_lock(store.root, shared=True):
        store.reload(create=False)
        key, manifest, _ = read_manifest(store, occurrence)
        require(not output.exists() and not is_reparse_or_symlink(output), "Restore target must not exist")
        output.mkdir(parents=True)
        output_resolved = output.resolve()
        count, byte_count, directories = 0, 0, []
        for row in manifest["files"]:
            if prefix and not (row["path"] == prefix or row["path"].startswith(prefix.rstrip("/") + "/")):
                continue
            rel = safe_relative(row["path"])
            target = output.joinpath(*rel.parts)
            # A captured symlink or reparse point must never become an ancestor used by another row.
            for parent in target.parents:
                if parent == output:
                    break
                require(not is_reparse_or_symlink(parent), "Symlink or reparse point ancestor during restore")
            require(not is_reparse_or_symlink(target), "Symlink or reparse point target during restore")
            if row["type"] == "directory":
                target.mkdir(parents=True, exist_ok=True)
                require(target.resolve().is_relative_to(output_resolved), "Restore target escapes output directory")
                directories.append((target, row))
            elif row["type"] == "symlink":
                target.parent.mkdir(parents=True, exist_ok=True)
                require(target.parent.resolve().is_relative_to(output_resolved), "Restore target escapes output directory")
                os.symlink(os.fsdecode(store.materialize(row["object"])), target)
                count += 1
            elif row["type"] == "file":
                target.parent.mkdir(parents=True, exist_ok=True)
                require(target.parent.resolve().is_relative_to(output_resolved), "Restore target escapes output directory")
                raw = store.materialize(row["object"])
                immutable_fn(target, raw)
                require(target.resolve().is_relative_to(output_resolved), "Restore target escapes output directory")
                os.chmod(target, row["mode"])
                os.utime(target, ns=(row["mtime_ns"], row["mtime_ns"]))
                require(sha(target.read_bytes()) == row["object"], "Restored file readback differs")
                count += 1; byte_count += len(raw)
            else:
                raise ValueError("Unknown occurrence type")
        for target, row in reversed(directories):
            require(not is_reparse_or_symlink(target), "Symlink or reparse point directory during restore")
            os.chmod(target, row["mode"])
            os.utime(target, ns=(row["mtime_ns"], row["mtime_ns"]))
        return {"manifest": key, "output": str(output), "entries_restored": count, "bytes": byte_count,
                "prefix": prefix, "readback": "All restored file bytes matched original identities; no relocation of captured paths."}


def rebuild(source_path, output, expected_index, expected_selection, account, *, hooks=None):
    """Regenerate existing identities into a new physical store; never edit source."""
    hooks = _resolve_hooks(hooks)
    immutable_fn = hooks["immutable"]
    replace_fn = hooks["replace_json"]
    source_path = check_unfollowed_ancestors(source_path, "Source store", include_target=True).resolve()
    output = check_unfollowed_ancestors(output, "Representation target", include_target=True)
    require(not output.exists() and not is_reparse_or_symlink(output), "Representation target must not exist")
    output_resolved = output.resolve()
    require(source_path not in output_resolved.parents and output_resolved not in source_path.parents
            and source_path != output_resolved,
            "Representation target and source must be separate roots")
    require(account, "Representation transition needs an owner account")
    with store_lock(source_path, shared=True):
        index_path = source_path / "index.json"
        current_path = source_path / "CURRENT.json"
        require(not is_reparse_or_symlink(index_path), "Source index must not be a symlink or reparse point")
        require(not is_reparse_or_symlink(current_path), "Source current pointer must not be a symlink or reparse point")
        index_raw = index_path.read_bytes()
        current_raw = current_path.read_bytes()
        require(sha(index_raw) == expected_index, "Source index differs from received identity")
        require(loads_json(current_raw)["selection"] == expected_selection, "Source selection differs from received identity")
        source = Store(source_path)
        source_selected_key, source_selected = current_selection(source)
        selection_closure(source, source_selected_key)
        events_path = source_path / "events.jsonl"
        if events_path.exists() or is_reparse_or_symlink(events_path):
            require(not is_reparse_or_symlink(events_path), "Source event log must not be a symlink or reparse point")
            events_raw = events_path.read_bytes()
        else:
            events_raw = b""
        started = utc(); output.mkdir(parents=True)
        copied_records = []
        for directory in IMMUTABLE_RECORD_DIRS:
            folder = source_path / directory
            if not folder.exists() and not is_reparse_or_symlink(folder):
                continue
            require(not is_reparse_or_symlink(folder), "Cannot migrate a linked immutable record directory")
            if not folder.is_dir():
                continue
            for parent, dirs, files in os.walk(folder, followlinks=False):
                dirs.sort()
                files.sort()
                for d in dirs:
                    subdir = Path(parent) / d
                    require(not is_reparse_or_symlink(subdir), "Cannot migrate a linked immutable record directory")
                for name in files:
                    if not name.endswith(".json"):
                        continue
                    path = Path(parent) / name
                    require(not is_reparse_or_symlink(path), "Cannot migrate a linked immutable record")
                    raw = path.read_bytes(); relative = path.relative_to(source_path)
                    immutable_fn(output / relative, raw)
                    copied_records.append({"path": relative.as_posix(), "sha256": sha(raw), "bytes": len(raw)})
        target = Store(output, create=True)
        target.event("representation-rebuild-started", {"source": str(source_path), "source_index": expected_index,
                                                        "source_selection": expected_selection, "account": account})
        try:
            # Materialize underlying archive frames before managed exports which
            # contain their encoded views. This preserves the same raw identities
            # without reinstating one physical file for each legacy header.
            archive_order = []
            for key, row in source.objects.items():
                if row["kind"] == "zip-recipe":
                    recipe = source.recipe(key)
                    names = {m["name"] for m in recipe["members"]}
                    managed = "index.json" in names and "EXPORT-MANIFEST.json" in names
                    archive_order.append((managed, row["bytes"], key))
            for number, (_, _, key) in enumerate(sorted(archive_order), 1):
                raw = source.materialize(key)
                require(target.ingest(raw, "retained.zip") == key, "Regenerated transport identity differs")
                print(json.dumps({"phase": "rebuild-transports", "completed": number, "total": len(archive_order)}, allow_nan=False), file=sys.stderr, flush=True)
            for number, key in enumerate(sorted(source.objects), 1):
                if key not in target.objects:
                    raw = source.materialize(key)
                    require(target.ingest(raw) == key, "Regenerated retained body differs")
                if number % 20000 == 0:
                    print(json.dumps({"phase": "rebuild-content", "completed": number, "total": len(source.objects)}, allow_nan=False), file=sys.stderr, flush=True)
            # Full raw-identity readback covers old internal frame/encoding identities
            # too; they remain addressable even when represented as verified views.
            target.cache.clear(); target.cache_bytes = 0
            for number, key in enumerate(sorted(source.objects), 1):
                raw = target.materialize(key)
                require(len(raw) == source.objects[key]["bytes"], "Rebuilt original byte count differs")
                if number % 20000 == 0:
                    print(json.dumps({"phase": "rebuild-readback", "completed": number, "total": len(source.objects)}, allow_nan=False), file=sys.stderr, flush=True)
            require(index_path.read_bytes() == index_raw and current_path.read_bytes() == current_raw,
                    "Source store changed during representation regeneration")
            if events_path.exists():
                require(events_path.read_bytes() == events_raw, "Source operation history changed")
            producer = target.blob(hooks["producer_bytes"])
            producer_deps = _producer_dependencies(target, hooks)
            source_events = target.blob(events_raw)
            record = {"format": "managed-material-representation-transition/1", "started_at": started,
                      "completed_at": utc(), "source_store": str(source_path), "source_index_sha256": expected_index,
                      "source_index_bytes": len(index_raw), "source_selection": expected_selection,
                      "source_current_sha256": sha(current_raw), "source_object_count": len(source.objects),
                      "source_object_id_set_sha256": sha(js(sorted(source.objects))),
                      "source_objects_exactly_read_back": len(source.objects), "immutable_records": copied_records,
                      "evidence_objects": [producer, *producer_deps.values(), source_events],
                      "producer_sha256": producer, "producer_dependencies": producer_deps,
                      "before_payload": source.stats(), "after_payload": target.stats(), "account": account,
                      "physical_change": "Raw content and transport identities retained; ZIP frame bytes packed with exact slice views; recipe JSON physically compressed; immutable metadata bodies reused by verified views.",
                      "source_untouched": True, "source_representation_retired": False,
                      "source_index_evidence": "Exact old index is pinned here, not treated as an immutable view of a mutable index. Retaining its full historical transport is a separate explicit input and measured cost.",
                      "limits": "New store is a received candidate until owner acceptance. No source-store cleanup, native migration, semantic reacceptance or concurrent-editor transaction."}
            record_raw = js(record); transition_key = sha(record_raw)
            immutable_fn(output / "transitions" / (transition_key + ".json"), record_raw)
            successor = {**source_selected, "previous_selection": expected_selection,
                         "representation_transitions": [*source_selected.get("representation_transitions", []), transition_key]}
            selected_raw = js(successor); selected_key = sha(selected_raw)
            immutable_fn(output / "selections" / (selected_key + ".json"), selected_raw)
            target.commit_index()
            replace_fn(output / "CURRENT.json", {"selection": selected_key, "path": "selections/" + selected_key + ".json"})
            checked = verify(target)
            result = {"format": "managed-material-rebuild-result/1", "output": str(output),
                      "transition": transition_key, "selection": selected_key, "source_index_sha256": expected_index,
                      "all_original_identities_preserved": len(source.objects), "source_untouched": True,
                      "source_physical_retirement": "pending-owner-receiving", "verified": checked,
                      "before_store_payload": source.stats(), "after_store_payload": target.stats()}
            target.event("representation-rebuild-completed", result)
            return result
        except Exception as exc:
            target.commit_index()
            target.event("representation-rebuild-failed", {"error_type": type(exc).__name__, "error": str(exc),
                                                           "source_untouched": True})
            raise


def retain_artifact(store, path, label, account, *, hooks=None):
    """Add exact historical evidence without changing the main current artifact."""
    hooks = _resolve_hooks(hooks)
    immutable_fn = hooks["immutable"]
    replace_fn = hooks["replace_json"]
    abs_path = check_unfollowed_ancestors(path, "Retained artifact", include_target=True)
    require(abs_path.exists() and stat.S_ISREG(abs_path.lstat().st_mode),
            "Retained artifact must be an existing ordinary file: " + str(abs_path))
    resolved = abs_path.resolve(strict=True)
    safe_relative(label)
    require("/" not in label and account, "Retained artifact needs one label and an account")
    with store_lock(store.root):
        store.reload(create=False)
        before = path_state(resolved); raw = resolved.read_bytes()
        require(path_state(resolved) == before, "Historical input changed while reading")
        previous_key, previous = current_selection(store)
        object_key = store.ingest(raw, resolved.name)
        require(sha(resolved.read_bytes()) == object_key, "Historical input changed before retention")
        occurrence = "retained-" + label + "-" + object_key[:16]
        occurrence_path = store.root / "occurrences" / (occurrence + ".json")
        require(not occurrence_path.exists() and not is_reparse_or_symlink(occurrence_path),
                "Retained artifact occurrence already exists")
        producer = store.blob(hooks["producer_bytes"])
        producer_deps = _producer_dependencies(store, hooks)
        manifest = {"format": "managed-material-occurrence/1", "id": occurrence, "started_at": utc(),
                    "completed_at": utc(), "spec": {"account": account}, "files": [{"path": label,
                    "source_path": str(resolved), "type": "file", "object": object_key, "bytes": len(raw),
                    "mode": before["mode"], "mtime_ns": before["mtime_ns"], "role": "historical-evidence",
                    "materialization": "observed-transport-copy"}], "history": [], "current": [],
                    "materialization_groups": [],
                    "producer": {"object": producer, "sha256": producer, "dependencies": producer_deps},
                    "scope": "Exact explicitly selected historical artifact; main current roles unchanged."}
        manifest_raw = js(manifest); manifest_key = sha(manifest_raw)
        selected = {**previous, "previous_selection": previous_key,
                    "additional_manifests": [*previous.get("additional_manifests", []), manifest_key]}
        selected_raw = js(selected); selected_key = sha(selected_raw)
        store.commit_index()
        immutable_fn(store.root / "manifests" / (manifest_key + ".json"), manifest_raw)
        immutable_fn(store.root / "selections" / (selected_key + ".json"), selected_raw)
        immutable_fn(occurrence_path, js({"format": "managed-material-occurrence-pointer/1",
            "id": occurrence, "manifest": manifest_key, "path": "manifests/" + manifest_key + ".json"}))
        try:
            replace_fn(store.root / "CURRENT.json", {"selection": selected_key, "path": "selections/" + selected_key + ".json"})
        except Exception as exc:
            store.event("historical-artifact-retention-failed", {
                "occurrence": occurrence, "manifest": manifest_key, "selection": selected_key,
                "occurrence_retained": occurrence_path.is_file() and not is_reparse_or_symlink(occurrence_path),
                "selection_published": False,
                "error_type": type(exc).__name__, "error": str(exc)})
            raise
        result = {"object": object_key, "bytes": len(raw), "occurrence": occurrence,
                  "manifest": manifest_key, "selection": selected_key, "current_roles_unchanged": True}
        store.event("historical-artifact-retained", result)
        return result
