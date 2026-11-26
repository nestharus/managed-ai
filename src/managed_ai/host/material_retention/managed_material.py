#!/usr/bin/env python3
"""Content-addressed material custody, exact ZIP recipes, and selected exports.

Python standard library only. This is a maintenance layer over existing byte
identities, not a replacement host/native runtime or semantic acceptance tool.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __name__ == "__main__" and not __package__:
    _pkg_dir = Path(__file__).resolve().parent
    if (
        _pkg_dir.parent.name == "host"
        and _pkg_dir.parents[1].name == "managed_ai"
        and (_pkg_dir.parents[1] / "__init__.py").is_file()
    ):
        _sys_root = str(_pkg_dir.parents[2])
        __package__ = "managed_ai.host.material_retention"
    else:
        _sys_root = str(_pkg_dir.parent)
        __package__ = _pkg_dir.name
    if _sys_root not in sys.path:
        sys.path.insert(0, _sys_root)
    __import__(__package__)

from . import (
    material_ops as _ops,
)
from . import (
    retention_closures as _closures,
)
from . import (
    retirement as _retirement,
)
from . import (
    snapshot_ops as _snapshot_ops,
)
from . import (
    store_core as _store_core,
)
from . import (
    store_io as _store_io,
)
from . import (
    transfer_ops as _transfer_ops,
)
from .config import (
    DEFAULT_LIMITS,
    FORMAT,
    IMMUTABLE_RECORD_DIRS,
    RAW_BODY_KINDS,
    MaterialRetentionService,
    RetentionLimits,
)
from .retention_closures import (
    current_selection,
    manifest_closure,
    materialization_state,
    observed_materialization_state,
    read_manifest,
    representation_closure,
    selected_for_manifest,
    selection_closure,
    unpublished_occurrences,
)
from .snapshot_ops import PORTABLE_LAUNCHER_SOURCE, load_portable_bundle
from .store_core import (
    MAX_EXPANDED_BYTES,
    MAX_MEMBER_BYTES,
    MAX_ZIP_DEPTH,
    MAX_ZIP_MEMBERS,
    Store,
)
from .store_io import (
    append_event_jsonl,
    check_unfollowed_ancestors,
    check_unfollowed_descendant,
    deflate,
    immutable,
    immutable_below,
    is_reparse_or_symlink,
    js,
    loads_json,
    path_state,
    replace_json,
    require,
    safe_relative,
    sha,
    sha256_file_checked,
    sha256_stream,
    store_lock,
    sync_dir,
    utc,
)


def _hooks() -> dict[str, Any]:
    """Build hooks lazily (no eager disk reads at import time) while honoring monkeypatches."""
    producer_bytes, sibling_bytes, launcher_bytes = load_portable_bundle()
    return {
        "immutable": immutable,
        "replace_json": replace_json,
        "producer_bytes": producer_bytes,
        "sibling_bytes": sibling_bytes,
        "launcher_bytes": launcher_bytes,
    }


def _producer_dependencies(store: Store) -> dict[str, str]:
    return _snapshot_ops.producer_dependencies(store, _hooks())


def snapshot(store: Store, spec_path: Path | str) -> dict[str, Any]:
    return _snapshot_ops.snapshot(store, spec_path, hooks=_hooks())


def verify(store: Store, occurrence: str | None = None) -> dict[str, Any]:
    return _snapshot_ops.verify(store, occurrence)


def export(store: Store, output: Path | str, occurrence: str | None = None) -> dict[str, Any]:
    return _transfer_ops.export(store, output, occurrence, hooks=_hooks())


def restore(
    store: Store,
    output: Path | str,
    occurrence: str | None = None,
    prefix: str | None = None,
) -> dict[str, Any]:
    return _transfer_ops.restore(store, output, occurrence, prefix, hooks=_hooks())


def retirement_plan(
    store: Store,
    spec_path: Path | str | dict[str, Any],
    occurrence: str | None = None,
) -> dict[str, Any]:
    return _retirement.retirement_plan(store, spec_path, occurrence)


def retire(
    store: Store,
    plan_path: Path | str,
    expected_plan: str,
    expected_selection: str,
) -> dict[str, Any]:
    return _retirement.retire(
        store,
        plan_path,
        expected_plan,
        expected_selection,
        hooks=_hooks(),
    )


def rebuild(
    source_path: Path | str,
    output: Path | str,
    expected_index: str,
    expected_selection: str,
    account: str,
) -> dict[str, Any]:
    return _transfer_ops.rebuild(
        source_path,
        output,
        expected_index,
        expected_selection,
        account,
        hooks=_hooks(),
    )


def retain_artifact(store: Store, path: Path | str, label: str, account: str) -> dict[str, Any]:
    return _snapshot_ops.retain_artifact(store, path, label, account, hooks=_hooks())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    commands = (
        "snapshot",
        "verify",
        "export",
        "restore",
        "get",
        "stats",
        "retirement-plan",
        "retire",
        "rebuild",
        "retain",
    )

    for name in commands:
        cmd = sub.add_parser(name, allow_abbrev=False)
        if name != "rebuild":
            cmd.add_argument("--store", required=True, type=Path)
        if name in ("verify", "export", "restore", "retirement-plan"):
            cmd.add_argument("--occurrence")
        if name in ("export", "restore", "get"):
            cmd.add_argument("--output", required=True, type=Path)
        if name == "restore":
            cmd.add_argument("--prefix")
        if name == "get":
            cmd.add_argument("--object", required=True)
        if name in ("snapshot", "retirement-plan"):
            cmd.add_argument("--spec", required=True, type=Path)
        if name == "retire":
            cmd.add_argument("--plan", required=True, type=Path)
            cmd.add_argument("--expected-plan", required=True)
            cmd.add_argument("--expected-selection", required=True)
        if name == "rebuild":
            cmd.add_argument("--source-store", required=True, type=Path)
            cmd.add_argument("--output", required=True, type=Path)
            cmd.add_argument("--expected-index", required=True)
            cmd.add_argument("--expected-selection", required=True)
            cmd.add_argument("--account", required=True)
        if name == "retain":
            cmd.add_argument("--path", required=True, type=Path)
            cmd.add_argument("--label", required=True)
            cmd.add_argument("--account", required=True)

    return parser


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "rebuild":
        return rebuild(
            args.source_store,
            args.output,
            args.expected_index,
            args.expected_selection,
            args.account,
        )

    if args.command in ("snapshot", "retire", "retain"):
        with store_lock(args.store):
            store = Store(args.store, create=args.command == "snapshot")
            if args.command == "snapshot":
                return snapshot(store, args.spec)
            if args.command == "retire":
                return retire(store, args.plan, args.expected_plan, args.expected_selection)
            return retain_artifact(store, args.path, args.label, args.account)

    with store_lock(args.store, shared=True):
        store = Store(args.store, create=False)
        if args.command == "verify":
            return verify(store, args.occurrence)
        if args.command == "export":
            return export(store, args.output, args.occurrence)
        if args.command == "restore":
            return restore(store, args.output, args.occurrence, args.prefix)
        if args.command == "get":
            output = check_unfollowed_ancestors(args.output, "Output file", include_target=True)
            require(
                not output.exists() and not is_reparse_or_symlink(output),
                "Preserve existing materialization",
            )
            raw = store.materialize(args.object)
            immutable(output, raw)
            return {
                "object": args.object,
                "output": str(output),
                "bytes": len(raw),
                "verified": True,
            }
        if args.command == "retirement-plan":
            return retirement_plan(store, args.spec, args.occurrence)
        return store.stats()


def main(argv: list[str] | None = None) -> None:
    """CLI entry point with structured error handling."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = _dispatch(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(
            json.dumps(
                {"error": str(exc), "error_type": type(exc).__name__, "command": args.command},
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    print(json.dumps(result, indent=2, allow_nan=False))


__all__ = [
    "DEFAULT_LIMITS",
    "FORMAT",
    "IMMUTABLE_RECORD_DIRS",
    "MAX_EXPANDED_BYTES",
    "MAX_MEMBER_BYTES",
    "MAX_ZIP_DEPTH",
    "MAX_ZIP_MEMBERS",
    "PORTABLE_LAUNCHER_SOURCE",
    "RAW_BODY_KINDS",
    "MaterialRetentionService",
    "RetentionLimits",
    "Store",
    "_closures",
    "_ops",
    "_retirement",
    "_snapshot_ops",
    "_store_core",
    "_store_io",
    "_transfer_ops",
    "append_event_jsonl",
    "check_unfollowed_ancestors",
    "check_unfollowed_descendant",
    "current_selection",
    "deflate",
    "export",
    "immutable",
    "immutable_below",
    "is_reparse_or_symlink",
    "js",
    "load_portable_bundle",
    "loads_json",
    "main",
    "manifest_closure",
    "materialization_state",
    "observed_materialization_state",
    "path_state",
    "read_manifest",
    "rebuild",
    "replace_json",
    "representation_closure",
    "require",
    "restore",
    "retain_artifact",
    "retire",
    "retirement_plan",
    "safe_relative",
    "selected_for_manifest",
    "selection_closure",
    "sha",
    "sha256_file_checked",
    "sha256_stream",
    "snapshot",
    "store_lock",
    "sync_dir",
    "unpublished_occurrences",
    "utc",
    "verify",
]


if __name__ == "__main__":
    main()
