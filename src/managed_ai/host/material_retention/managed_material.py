#!/usr/bin/env python3
"""Content-addressed material custody, exact ZIP recipes and selected exports.

Python standard library only. This is a maintenance layer over existing byte
identities, not a replacement host/native runtime or semantic acceptance tool.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


def _load_sibling(stem: str):
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
    return module, path.read_bytes()


_store_io, LOADED_STORE_IO_BYTES = _load_sibling("store_io")
_closures, LOADED_CLOSURES_BYTES = _load_sibling("retention_closures")
_store_core, LOADED_STORE_CORE_BYTES = _load_sibling("store_core")
_ops, LOADED_MATERIAL_OPS_BYTES = _load_sibling("material_ops")
_retirement, LOADED_RETIREMENT_BYTES = _load_sibling("retirement")

LOADED_PRODUCER_BYTES = Path(__file__).read_bytes()
SIBLING_MODULE_BYTES = {
    "store_io.py": LOADED_STORE_IO_BYTES,
    "retention_closures.py": LOADED_CLOSURES_BYTES,
    "store_core.py": LOADED_STORE_CORE_BYTES,
    "material_ops.py": LOADED_MATERIAL_OPS_BYTES,
    "retirement.py": LOADED_RETIREMENT_BYTES,
}

# Re-export constants, I/O, locking, strict JSON, closures, and Store for compatibility.
FORMAT = _store_io.FORMAT
RAW_BODY_KINDS = _store_io.RAW_BODY_KINDS
IMMUTABLE_RECORD_DIRS = _store_io.IMMUTABLE_RECORD_DIRS
MAX_ZIP_DEPTH = _store_core.MAX_ZIP_DEPTH
MAX_ZIP_MEMBERS = _store_core.MAX_ZIP_MEMBERS
MAX_MEMBER_BYTES = _store_core.MAX_MEMBER_BYTES
MAX_EXPANDED_BYTES = _store_core.MAX_EXPANDED_BYTES

sha = _store_io.sha
loads_json = _store_io.loads_json
js = _store_io.js
utc = _store_io.utc
require = _store_io.require
is_reparse_or_symlink = _store_io.is_reparse_or_symlink
check_unfollowed_ancestors = _store_io.check_unfollowed_ancestors
sync_dir = _store_io.sync_dir
store_lock = _store_io.store_lock
append_event_jsonl = _store_io.append_event_jsonl
immutable = _store_io.immutable
replace_json = _store_io.replace_json
deflate = _store_io.deflate
safe_relative = _store_io.safe_relative
path_state = _store_io.path_state

read_manifest = _closures.read_manifest
manifest_closure = _closures.manifest_closure
current_selection = _closures.current_selection
selection_closure = _closures.selection_closure
representation_closure = _closures.representation_closure
observed_materialization_state = _closures.observed_materialization_state
materialization_state = _closures.materialization_state
selected_for_manifest = _closures.selected_for_manifest
unpublished_occurrences = _closures.unpublished_occurrences

Store = _store_core.Store


def _hooks():
    return {
        "immutable": immutable,
        "replace_json": replace_json,
        "producer_bytes": LOADED_PRODUCER_BYTES,
        "sibling_bytes": SIBLING_MODULE_BYTES,
    }


def _producer_dependencies(store):
    return _ops._producer_dependencies(store, _hooks())


def snapshot(store, spec_path):
    return _ops.snapshot(store, spec_path, hooks=_hooks())


def verify(store, occurrence=None):
    return _ops.verify(store, occurrence)


def export(store, output, occurrence=None):
    return _ops.export(store, output, occurrence, hooks=_hooks())


def restore(store, output, occurrence=None, prefix=None):
    return _ops.restore(store, output, occurrence, prefix, hooks=_hooks())


def retirement_plan(store, spec_path, occurrence=None):
    return _retirement.retirement_plan(store, spec_path, occurrence)


def retire(store, plan_path, expected_plan, expected_selection):
    return _retirement.retire(store, plan_path, expected_plan, expected_selection, hooks=_hooks())


def rebuild(source_path, output, expected_index, expected_selection, account):
    return _ops.rebuild(source_path, output, expected_index, expected_selection, account, hooks=_hooks())


def retain_artifact(store, path, label, account):
    return _ops.retain_artifact(store, path, label, account, hooks=_hooks())


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("snapshot", "verify", "export", "restore", "get", "stats", "retirement-plan", "retire", "rebuild", "retain"):
        p = sub.add_parser(name, allow_abbrev=False)
        if name != "rebuild":
            p.add_argument("--store", required=True, type=Path)
        if name in ("verify", "export", "restore", "retirement-plan"):
            p.add_argument("--occurrence")
        if name in ("export", "restore", "get"):
            p.add_argument("--output", required=True, type=Path)
        if name == "restore":
            p.add_argument("--prefix")
        if name == "get":
            p.add_argument("--object", required=True)
        if name in ("snapshot", "retirement-plan"):
            p.add_argument("--spec", required=True, type=Path)
        if name == "retire":
            p.add_argument("--plan", required=True, type=Path)
            p.add_argument("--expected-plan", required=True)
            p.add_argument("--expected-selection", required=True)
        if name == "rebuild":
            p.add_argument("--source-store", required=True, type=Path)
            p.add_argument("--output", required=True, type=Path)
            p.add_argument("--expected-index", required=True)
            p.add_argument("--expected-selection", required=True)
            p.add_argument("--account", required=True)
        if name == "retain":
            p.add_argument("--path", required=True, type=Path)
            p.add_argument("--label", required=True)
            p.add_argument("--account", required=True)
    args = parser.parse_args()
    if args.command == "rebuild":
        result = rebuild(args.source_store, args.output, args.expected_index, args.expected_selection, args.account)
        print(json.dumps(result, indent=2, allow_nan=False))
        return
    if args.command in ("snapshot", "retire", "retain"):
        with store_lock(args.store):
            store = Store(args.store, create=args.command == "snapshot")
            if args.command == "snapshot":
                result = snapshot(store, args.spec)
            elif args.command == "retire":
                result = retire(store, args.plan, args.expected_plan, args.expected_selection)
            else:
                result = retain_artifact(store, args.path, args.label, args.account)
    else:
        with store_lock(args.store, shared=True):
            store = Store(args.store, create=False)
            if args.command == "verify":
                result = verify(store, args.occurrence)
            elif args.command == "export":
                result = export(store, args.output, args.occurrence)
            elif args.command == "restore":
                result = restore(store, args.output, args.occurrence, args.prefix)
            elif args.command == "get":
                output = check_unfollowed_ancestors(args.output, "Output file", include_target=True)
                require(not output.exists() and not is_reparse_or_symlink(output), "Preserve existing materialization")
                raw = store.materialize(args.object)
                immutable(output, raw)
                result = {"object": args.object, "output": str(output), "bytes": len(raw), "verified": True}
            elif args.command == "retirement-plan":
                result = retirement_plan(store, args.spec, args.occurrence)
            else:
                result = store.stats()
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
