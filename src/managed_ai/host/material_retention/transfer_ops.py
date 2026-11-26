"""Portable recovery archive export, verified restore, and store representation rebuild."""

import hashlib
import json
import os
import stat
import sys
import zipfile
from pathlib import Path
from typing import Any

from .config import DEFAULT_LIMITS, FORMAT, IMMUTABLE_RECORD_DIRS
from .retention_closures import (
    current_selection,
    read_manifest,
    representation_closure,
    selected_for_manifest,
    selection_closure,
)
from .snapshot_ops import producer_dependencies, resolve_hooks, verify, verify_loaded_closure
from .store_core import Store
from .store_io import (
    check_unfollowed_ancestors,
    check_unfollowed_descendant,
    immutable,
    immutable_below,
    is_reparse_or_symlink,
    js,
    loads_json,
    require,
    safe_relative,
    sha,
    sha256_file_checked,
    sha256_stream,
    store_lock,
    utc,
)


def _collect_export_entries(
    store: Any,
    key: str,
    manifest: dict[str, Any],
    manifest_raw: bytes,
    selection_key: str,
    selections: dict[str, tuple[dict[str, Any], bytes]],
    receipts: dict[str, tuple[dict[str, Any], bytes]],
    manifests: dict[str, tuple[dict[str, Any], bytes]],
    keys: set[str],
    checked: dict[str, Any],
    hooks: dict[str, Any],
) -> dict[str, bytes | Path]:
    """Assemble the verified portable archive entry map including miniature recovery package."""
    files: dict[str, bytes | Path] = {
        "index.json": js(
            {"format": FORMAT, "objects": {k: store.objects[k] for k in sorted(keys)}}
        ),
        "manifests/" + key + ".json": manifest_raw,
        "occurrences/" + manifest["id"] + ".json": js(
            {
                "format": "managed-material-occurrence-pointer/1",
                "id": manifest["id"],
                "manifest": key,
                "path": "manifests/" + key + ".json",
            },
        ),
        "managed_material.py": hooks["launcher_bytes"],
        **hooks["sibling_bytes"],
        "RECOVERY.json": js(
            {
                "format": "managed-material-recovery/1",
                "manifest": key,
                "verify": "python managed_material.py verify --store . --occurrence "
                + manifest["id"],
                "restore": (
                    "python managed_material.py restore --store . --occurrence "
                    + manifest["id"]
                    + " --output ../restored"
                ),
                "codec_boundary": (
                    "Stored bodies decode with DEFLATE. Historical deflate-recipe parts require "
                    "the "
                    "recorded encoder or another encoder producing the same pinned compressed "
                    "hash; "
                    "mismatch refuses exact replay."
                ),
                "verified": checked,
            },
        ),
        "CURRENT.json": js(
            {"selection": selection_key, "path": "selections/" + selection_key + ".json"},
        ),
    }

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
        files["occurrences/" + old_manifest["id"] + ".json"] = js(
            {
                "format": "managed-material-occurrence-pointer/1",
                "id": old_manifest["id"],
                "manifest": old_key,
                "path": "manifests/" + old_key + ".json",
            },
        )

    for obj in sorted(keys):
        row = store.objects[obj]
        if "path" in row:
            files[row["path"]] = store.root / row["path"]
        if row["kind"] == "record-view":
            files[row["record_path"]] = store.root / row["record_path"]

    return files


def _write_export_zip(
    output: Path,
    key: str,
    files: dict[str, bytes | Path],
) -> list[dict[str, Any]]:
    """Stream archive entries into output ZIP without buffering large stored objects in RAM."""
    chunk_size = DEFAULT_LIMITS.stream_chunk_bytes
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_entries: list[dict[str, Any]] = []

    with zipfile.ZipFile(output, "x") as archive:
        for name, source in sorted(files.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.external_attr = (0o644 | stat.S_IFREG) << 16
            info.compress_type = (
                zipfile.ZIP_STORED if name.endswith(".deflate") else zipfile.ZIP_DEFLATED
            )

            if isinstance(source, Path):
                require(
                    not is_reparse_or_symlink(source),
                    "Export source must not be a symlink or reparse point: " + str(source),
                )
                hasher = hashlib.sha256()
                total_bytes = 0
                with source.open("rb") as src, archive.open(info, "w") as dst:
                    while True:
                        chunk = src.read(chunk_size)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        dst.write(chunk)
                        total_bytes += len(chunk)
                manifest_entries.append(
                    {"path": name, "sha256": hasher.hexdigest(), "bytes": total_bytes},
                )
            else:
                archive.writestr(info, source)
                manifest_entries.append(
                    {"path": name, "sha256": sha(source), "bytes": len(source)},
                )

        manifest_info = zipfile.ZipInfo("EXPORT-MANIFEST.json", (1980, 1, 1, 0, 0, 0))
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(
            manifest_info,
            js(
                {
                    "format": "managed-material-export/1",
                    "source_manifest": key,
                    "entries": manifest_entries,
                    "nested_archive_payloads": 0,
                },
            ),
        )

    return manifest_entries


def _verify_export_zip(output: Path, manifest_entries: list[dict[str, Any]]) -> None:
    """Stream-verify every exported ZIP member against its recorded SHA-256."""
    chunk_size = DEFAULT_LIMITS.stream_chunk_bytes
    with zipfile.ZipFile(output) as archive:
        for row in manifest_entries:
            with archive.open(row["path"], "r") as stream:
                require(
                    sha256_stream(stream, chunk_size=chunk_size) == row["sha256"],
                    "Export member readback differs",
                )


def export(
    store: Any,
    output: Path | str,
    occurrence: str | None = None,
    *,
    hooks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Export a verified selection closure as a self-contained portable recovery ZIP."""
    resolved_hooks = resolve_hooks(hooks)
    immutable_fn = resolved_hooks["immutable"]
    checked_output = check_unfollowed_ancestors(output, "Export output", include_target=True)

    with store_lock(store.root, shared=True):
        store.reload(create=False)
        key, manifest, manifest_raw = read_manifest(store, occurrence)
        require(
            not checked_output.exists() and not is_reparse_or_symlink(checked_output),
            "Preserve prior export; select a new output identity",
        )
        selection_key, selection = selected_for_manifest(store, key, manifest)
        selections, receipts, manifests, keys = selection_closure(store, selection_key, selection)
        checked = verify_loaded_closure(store, key, selections, receipts, manifests, keys)
        files = _collect_export_entries(
            store,
            key,
            manifest,
            manifest_raw,
            selection_key,
            selections,
            receipts,
            manifests,
            keys,
            checked,
            resolved_hooks,
        )
        manifest_entries = _write_export_zip(checked_output, key, files)
        _verify_export_zip(checked_output, manifest_entries)

        result = {
            "output": str(checked_output),
            "sha256": sha256_file_checked(checked_output, label="Export archive"),
            "bytes": checked_output.stat().st_size,
            "source_manifest": key,
            "archive_entries": len(files) + 1,
            "nested_zip_files": 0,
            "historical_manifests": len(manifests),
            "retained_selections": len(selections),
            "retained_lifecycle_receipts": len(receipts),
            "source_occurrence_bytes": sum(row.get("bytes", 0) for row in manifest["files"]),
            "recovery_verified": True,
            **store.stats(keys),
        }

    immutable_fn(checked_output.with_suffix(checked_output.suffix + ".receipt.json"), js(result))
    store.event("export-completed", result)
    return result


def restore(
    store: Any,
    output: Path | str,
    occurrence: str | None = None,
    prefix: str | None = None,
    *,
    hooks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize and readback-verify files from a retained occurrence into a fresh directory."""
    resolved_hooks = resolve_hooks(hooks)
    immutable_fn = resolved_hooks["immutable"]
    use_fast_immutable = immutable_fn is immutable
    checked_output = check_unfollowed_ancestors(output, "Restore target", include_target=True)

    with store_lock(store.root, shared=True):
        store.reload(create=False)
        key, manifest, _ = read_manifest(store, occurrence)
        require(
            not checked_output.exists() and not is_reparse_or_symlink(checked_output),
            "Restore target must not exist",
        )
        checked_output.mkdir(parents=True)
        output_resolved = checked_output.resolve()

        count = 0
        byte_count = 0
        directories: list[tuple[Path, dict[str, Any]]] = []
        prefix_slash = prefix.rstrip("/") + "/" if prefix else None

        for row in manifest["files"]:
            if prefix and not (row["path"] == prefix or row["path"].startswith(prefix_slash or "")):
                continue

            rel = safe_relative(row["path"])
            target = check_unfollowed_descendant(
                checked_output,
                rel.as_posix(),
                allow_missing_leaf=True,
                role="Restore target",
            )
            row_type = row["type"]

            if row_type == "directory":
                target.mkdir(parents=True, exist_ok=True)
                require(
                    target.resolve().is_relative_to(output_resolved),
                    "Restore target escapes output directory",
                )
                directories.append((target, row))
            elif row_type == "symlink":
                target.parent.mkdir(parents=True, exist_ok=True)
                require(
                    target.parent.resolve().is_relative_to(output_resolved),
                    "Restore target escapes output directory",
                )
                os.symlink(os.fsdecode(store.materialize(row["object"])), target)
                count += 1
            elif row_type == "file":
                target.parent.mkdir(parents=True, exist_ok=True)
                require(
                    target.parent.resolve().is_relative_to(output_resolved),
                    "Restore target escapes output directory",
                )
                raw = store.materialize(row["object"])
                if use_fast_immutable:
                    immutable_below(checked_output, rel, raw)
                else:
                    immutable_fn(target, raw)
                require(
                    target.resolve().is_relative_to(output_resolved),
                    "Restore target escapes output directory",
                )
                os.chmod(target, row["mode"])
                os.utime(target, ns=(row["mtime_ns"], row["mtime_ns"]))
                require(
                    sha256_file_checked(target, label="Restored file") == row["object"],
                    "Restored file readback differs",
                )
                count += 1
                byte_count += len(raw)
            else:
                raise ValueError("Unknown occurrence type")

        for target, row in reversed(directories):
            require(
                not is_reparse_or_symlink(target),
                "Symlink or reparse point directory during restore",
            )
            os.chmod(target, row["mode"])
            os.utime(target, ns=(row["mtime_ns"], row["mtime_ns"]))

        return {
            "manifest": key,
            "output": str(checked_output),
            "entries_restored": count,
            "bytes": byte_count,
            "prefix": prefix,
            "readback": (
                "All restored file bytes matched original identities; "
                "no relocation of captured paths."
            ),
        }


def _copy_immutable_records(
    source_path: Path,
    output: Path,
    immutable_fn: Any,
) -> list[dict[str, Any]]:
    """Copy all verified immutable metadata JSON files from source_path to output."""
    use_fast_immutable = immutable_fn is immutable
    copied_records: list[dict[str, Any]] = []

    for directory in IMMUTABLE_RECORD_DIRS:
        folder = source_path / directory
        if not folder.exists() and not is_reparse_or_symlink(folder):
            continue
        require(
            not is_reparse_or_symlink(folder),
            "Cannot migrate a linked immutable record directory",
        )
        if not folder.is_dir():
            continue

        for parent, dirs, files in os.walk(folder, followlinks=False):
            dirs.sort()
            files.sort()
            for dirname in dirs:
                subdir = Path(parent) / dirname
                require(
                    not is_reparse_or_symlink(subdir),
                    "Cannot migrate a linked immutable record directory",
                )
            for name in files:
                if not name.endswith(".json"):
                    continue
                path = Path(parent) / name
                require(not is_reparse_or_symlink(path), "Cannot migrate a linked immutable record")
                raw = path.read_bytes()
                relative = path.relative_to(source_path)
                if use_fast_immutable:
                    immutable_below(output, relative, raw)
                else:
                    immutable_fn(output / relative, raw)
                copied_records.append(
                    {"path": relative.as_posix(), "sha256": sha(raw), "bytes": len(raw)},
                )

    return copied_records


def _regenerate_objects(source: Store, target: Store) -> None:
    """Regenerate all transports and content bodies from source into target."""
    archive_order: list[tuple[bool, int, str]] = []
    for key, row in source.objects.items():
        if row["kind"] == "zip-recipe":
            recipe = source.recipe(key)
            names = {member["name"] for member in recipe["members"]}
            managed = "index.json" in names and "EXPORT-MANIFEST.json" in names
            archive_order.append((managed, row["bytes"], key))

    for number, (_, _, key) in enumerate(sorted(archive_order), 1):
        raw = source.materialize(key)
        require(target.ingest(raw, "retained.zip") == key, "Regenerated transport identity differs")
        print(
            json.dumps(
                {"phase": "rebuild-transports", "completed": number, "total": len(archive_order)},
                allow_nan=False,
            ),
            file=sys.stderr,
            flush=True,
        )

    for number, key in enumerate(sorted(source.objects), 1):
        if key not in target.objects:
            raw = source.materialize(key)
            require(target.ingest(raw) == key, "Regenerated retained body differs")
        if number % 20000 == 0:
            print(
                json.dumps(
                    {"phase": "rebuild-content", "completed": number, "total": len(source.objects)},
                    allow_nan=False,
                ),
                file=sys.stderr,
                flush=True,
            )


def _verify_rebuilt_objects(source: Store, target: Store) -> None:
    """Read back every regenerated identity from target without cached reuse."""
    target.clear_cache()
    try:
        for number, key in enumerate(sorted(source.objects), 1):
            raw = target.materialize(key)
            require(
                len(raw) == source.objects[key]["bytes"],
                "Rebuilt original byte count differs",
            )
            if number % 20000 == 0:
                print(
                    json.dumps(
                        {
                            "phase": "rebuild-readback",
                            "completed": number,
                            "total": len(source.objects),
                        },
                        allow_nan=False,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
    finally:
        target.clear_cache()


def _publish_rebuild_transition(
    source_path: Path,
    output: Path,
    source: Store,
    target: Store,
    *,
    started: str,
    expected_index: str,
    expected_selection: str,
    index_raw: bytes,
    current_raw: bytes,
    events_raw: bytes,
    copied_records: list[dict[str, Any]],
    source_selected: dict[str, Any],
    account: str,
    hooks: dict[str, Any],
) -> dict[str, Any]:
    """Commit representation transition record, successor selection, and CURRENT.json."""
    immutable_fn = hooks["immutable"]
    replace_fn = hooks["replace_json"]

    producer = target.blob(hooks["producer_bytes"])
    producer_deps = producer_dependencies(target, hooks)
    source_events = target.blob(events_raw)

    record = {
        "format": "managed-material-representation-transition/1",
        "started_at": started,
        "completed_at": utc(),
        "source_store": str(source_path),
        "source_index_sha256": expected_index,
        "source_index_bytes": len(index_raw),
        "source_selection": expected_selection,
        "source_current_sha256": sha(current_raw),
        "source_object_count": len(source.objects),
        "source_object_id_set_sha256": sha(js(sorted(source.objects))),
        "source_objects_exactly_read_back": len(source.objects),
        "immutable_records": copied_records,
        "evidence_objects": [producer, *producer_deps.values(), source_events],
        "producer_sha256": producer,
        "producer_dependencies": producer_deps,
        "before_payload": source.stats(),
        "after_payload": target.stats(),
        "account": account,
        "physical_change": (
            "Raw content and transport identities retained; ZIP frame bytes packed with exact "
            "slice views; recipe JSON physically compressed; immutable metadata bodies reused "
            "by verified views."
        ),
        "source_untouched": True,
        "source_representation_retired": False,
        "source_index_evidence": (
            "Exact old index is pinned here, not treated as an immutable view of a mutable index. "
            "Retaining its full historical transport is a separate explicit input and measured "
            "cost."
        ),
        "limits": (
            "New store is a received candidate until owner acceptance. No source-store cleanup, "
            "native migration, semantic reacceptance or concurrent-editor transaction."
        ),
    }
    record_raw = js(record)
    transition_key = sha(record_raw)
    immutable_fn(output / "transitions" / (transition_key + ".json"), record_raw)

    successor = {
        **source_selected,
        "previous_selection": expected_selection,
        "representation_transitions": [
            *source_selected.get("representation_transitions", []),
            transition_key,
        ],
    }
    selected_raw = js(successor)
    selected_key = sha(selected_raw)
    immutable_fn(output / "selections" / (selected_key + ".json"), selected_raw)

    target.commit_index()
    replace_fn(
        output / "CURRENT.json",
        {"selection": selected_key, "path": "selections/" + selected_key + ".json"},
    )
    checked = verify(target)
    return {
        "format": "managed-material-rebuild-result/1",
        "output": str(output),
        "transition": transition_key,
        "selection": selected_key,
        "source_index_sha256": expected_index,
        "all_original_identities_preserved": len(source.objects),
        "source_untouched": True,
        "source_physical_retirement": "pending-owner-receiving",
        "verified": checked,
        "before_store_payload": source.stats(),
        "after_store_payload": target.stats(),
    }


def rebuild(
    source_path: Path | str,
    output: Path | str,
    expected_index: str,
    expected_selection: str,
    account: str,
    *,
    hooks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Regenerate existing identities into a new physical store without modifying source."""
    resolved_hooks = resolve_hooks(hooks)
    immutable_fn = resolved_hooks["immutable"]
    checked_source = check_unfollowed_ancestors(
        source_path, "Source store", include_target=True
    ).resolve()
    checked_output = check_unfollowed_ancestors(
        output, "Representation target", include_target=True
    )

    require(
        not checked_output.exists() and not is_reparse_or_symlink(checked_output),
        "Representation target must not exist",
    )
    output_resolved = checked_output.resolve()
    require(
        checked_source not in output_resolved.parents
        and output_resolved not in checked_source.parents
        and checked_source != output_resolved,
        "Representation target and source must be separate roots",
    )
    require(bool(account), "Representation transition needs an owner account")

    with store_lock(checked_source, shared=True):
        index_path = checked_source / "index.json"
        current_path = checked_source / "CURRENT.json"
        require(
            not is_reparse_or_symlink(index_path),
            "Source index must not be a symlink or reparse point",
        )
        require(
            not is_reparse_or_symlink(current_path),
            "Source current pointer must not be a symlink or reparse point",
        )
        index_raw = index_path.read_bytes()
        current_raw = current_path.read_bytes()
        require(sha(index_raw) == expected_index, "Source index differs from received identity")
        require(
            loads_json(current_raw)["selection"] == expected_selection,
            "Source selection differs from received identity",
        )

        source = Store(checked_source)
        source_selected_key, source_selected = current_selection(source)
        selection_closure(source, source_selected_key)

        events_path = checked_source / "events.jsonl"
        if events_path.exists() or is_reparse_or_symlink(events_path):
            require(
                not is_reparse_or_symlink(events_path),
                "Source event log must not be a symlink or reparse point",
            )
            events_raw = events_path.read_bytes()
        else:
            events_raw = b""

        started = utc()
        checked_output.mkdir(parents=True)
        copied_records = _copy_immutable_records(checked_source, checked_output, immutable_fn)
        target = Store(checked_output, create=True)
        target.event(
            "representation-rebuild-started",
            {
                "source": str(checked_source),
                "source_index": expected_index,
                "source_selection": expected_selection,
                "account": account,
            },
        )
        try:
            _regenerate_objects(source, target)
            _verify_rebuilt_objects(source, target)
            require(
                index_path.read_bytes() == index_raw and current_path.read_bytes() == current_raw,
                "Source store changed during representation regeneration",
            )
            if events_path.exists():
                require(events_path.read_bytes() == events_raw, "Source operation history changed")

            result = _publish_rebuild_transition(
                checked_source,
                checked_output,
                source,
                target,
                started=started,
                expected_index=expected_index,
                expected_selection=expected_selection,
                index_raw=index_raw,
                current_raw=current_raw,
                events_raw=events_raw,
                copied_records=copied_records,
                source_selected=source_selected,
                account=account,
                hooks=resolved_hooks,
            )
            target.event("representation-rebuild-completed", result)
            return result
        except Exception as exc:
            target.commit_index()
            target.event(
                "representation-rebuild-failed",
                {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "source_untouched": True,
                },
            )
            raise
