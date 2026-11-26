"""Tree scanning, manifest stamping, and bundle verification for release packaging."""

import os
import re
import stat
import sys
from collections.abc import Iterator, Sequence
from contextlib import ExitStack
from pathlib import Path
from types import EllipsisType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai.platform import is_reparse_or_symlink_stat  # noqa: E402
from scripts.release._verify import (  # noqa: E402
    CATALOGUE,
    SCRIPTS_SELECTION,
    STAGED_PROJECTIONS,
    catalogue_path,
    check_compressed_text,
    engine_prefix,
    identity,
    inventory_engine,
    observed_path,
    optional_entry,
    ordinary,
    read_json,
    received_compressed_text,
    resolve_navigation_path,
    resolve_selection_path,
    store_compressed_prefixes,
    verify_core_selection,
    verify_deployment_bindings,
    verify_source_list,
    verify_store_schemas,
    verify_workbench_components,
    with_engine,
    write_bytes_atomic,
    write_json,
)
from scripts.release.refresh_guidance import render_sources_markdown  # noqa: E402


def reserved_stage(path: Path, info: os.stat_result, prefixes: Sequence[str]) -> bool:
    for prefix in prefixes:
        name = path.name
        if len(name) <= len(prefix) or not name.startswith(".") or name[len(prefix) - 1] != ".":
            continue
        if name.startswith(prefix):
            return True

        equivalent = optional_entry(path.with_name(prefix + name[len(prefix) :]))
        if equivalent is not None and os.path.samestat(info, equivalent):
            return True

    return False


def source_paths(root: Path, engine: Path | None) -> Iterator[Path]:
    ordinary(root, root.lstat(), stat.S_ISDIR)
    staging: dict[str, list[str]] = {}
    if engine is not None:
        for target in STAGED_PROJECTIONS:
            resolved_target = catalogue_path(engine) if target == CATALOGUE else target
            parent: Path | None = engine
            if resolved_target.parent.parts:
                parent = observed_path(engine, resolved_target.parent, stat.S_ISDIR)
            if parent is not None:
                key = parent.resolve(strict=True).as_posix()
                staging.setdefault(key, []).append(f".{resolved_target.name}.")

        scripts_release = observed_path(
            root, SCRIPTS_SELECTION.parent, stat.S_ISDIR, scope_probe=True
        )
        if scripts_release is not None:
            key = scripts_release.resolve(strict=True).as_posix()
            staging.setdefault(key, []).append(f".{SCRIPTS_SELECTION.name}.")

    pending = [root]
    while pending:
        parent = pending.pop()
        for path in parent.iterdir():
            if engine is not None and path.name.casefold() == ".git":
                raise ValueError(
                    f"Git checkout metadata at {path}. Export the selected tracked files "
                    "into a clean package directory as described in RELEASING.md."
                )
            if "__pycache__" in path.parts:
                continue

            info = path.lstat()
            if stat.S_ISDIR(info.st_mode) and not is_reparse_or_symlink_stat(info):
                if not reserved_stage(path, info, staging.get(parent.as_posix(), ())):
                    pending.append(path)
            elif path.suffix != ".pyc":
                ordinary(path, info, stat.S_ISREG)
                yield path


def members(root: Path, engine: Path | EllipsisType | None = ...) -> dict[str, dict[str, Any]]:
    resolved_engine: Path | None = (
        inventory_engine(root) if isinstance(engine, EllipsisType) else engine
    )

    result: dict[str, dict[str, Any]] = {}
    stores: dict[str, tuple[Any, set[tuple[str, str, int]], tuple[str, ...]]] = {}
    native_api: dict[str, Any] | None = None

    with ExitStack() as stack:
        for path in sorted(
            source_paths(root, resolved_engine), key=lambda item: (item, item.as_posix())
        ):
            relative = path.relative_to(root).as_posix()
            if "\\" in relative or ".." in Path(relative).parts:
                raise ValueError("Unsafe archive member path: " + relative)

            if path.suffix.lower() in {".zip", ".tar", ".gz"}:
                match = re.fullmatch(
                    r"stores/([^/]+)/work/((?:.+/)?([a-f0-9]{2})/([a-f0-9]{64})\.gz)",
                    relative,
                )
                if (
                    not match
                    or match[3] != match[4][:2]
                    or resolved_engine is None
                    or resolved_engine == root
                    or not (root / "stores" / match[1] / "system.json").is_file()
                ):
                    raise ValueError(
                        "Select extracted useful source instead of nested transport: " + str(path)
                    )

                store_name, work_rel, _, expected_sha = match.groups()
                if store_name not in stores:
                    if native_api is None:
                        native_api = stack.enter_context(
                            with_engine(resolved_engine, "store/system.py")
                        )
                    store = native_api["System"](root / "stores" / store_name)
                    stores[store_name] = (
                        store,
                        received_compressed_text(store, engine=resolved_engine),
                        store_compressed_prefixes(store),
                    )

                store, allowed, allowed_prefixes = stores[store_name]
                if not any(work_rel.startswith(prefix) for prefix in allowed_prefixes):
                    raise ValueError(
                        "Select extracted useful source instead of nested transport: " + str(path)
                    )

                current = identity(path)
                if (
                    current["sha256"] != expected_sha
                    or (work_rel, expected_sha, current["bytes"]) not in allowed
                ):
                    raise ValueError(
                        "Missing accepted research evidence for compressed source: " + relative
                    )

                store.ex.objects.read(expected_sha, expected_bytes=current["bytes"])
                check_compressed_text(path)
                result[relative] = current
            else:
                result[relative] = identity(path)

    return result


def generate_sources_markdown(core: Path, *, bundle_root: Path | None = None) -> Path:
    selected = read_json(resolve_selection_path(core, bundle_root=bundle_root))
    nav_path = resolve_navigation_path(core, bundle_root=bundle_root)
    nav = read_json(nav_path) if nav_path is not None else {}
    raw = render_sources_markdown(selected, nav)
    target = core / "SOURCES.md"
    write_bytes_atomic(target, raw)
    return target


def verify_bundle(root: Path, core: Path, native: dict, tree: dict | None = None) -> None:
    if tree is None:
        tree = members(root, core)

    prefix = engine_prefix(root, core)
    bodies = verify_core_selection(core, native, prefix=prefix, bundle_root=root, tree=tree)
    if "SOURCES.md" in native:
        verify_source_list(core / "SOURCES.md", bodies)

    verify_workbench_components(root)
    verify_store_schemas(root, core)
    verify_deployment_bindings(root, core)


verify_sources = verify_bundle


def manifest_name(root: Path) -> str:
    name = Path("MANIFEST.json")
    path = observed_path(root, name, stat.S_ISREG)
    return (
        name.as_posix() if path is None else path.resolve(strict=True).relative_to(root).as_posix()
    )


def refresh_manifests(root: Path, core: Path | None) -> None:
    tree = members(root, core)
    tree.pop(manifest_name(root), None)
    if core == root:
        bodies = verify_core_selection(core, tree)
        if "SOURCES.md" in tree:
            verify_source_list(core / "SOURCES.md", bodies)
        else:
            generated = generate_sources_markdown(core)
            tree["SOURCES.md"] = identity(generated)
    elif core is not None:
        prefix = engine_prefix(root, core)
        native = {name[len(prefix) :]: row for name, row in tree.items() if name.startswith(prefix)}
        inner = manifest_name(core)
        native.pop(inner, None)
        verify_bundle(root, core, native, tree=tree)
        stamped_manifest = verify_workbench_components(root, stamp=True)
        if stamped_manifest is not None:
            stamped_rel = (
                stamped_manifest.resolve(strict=True)
                .relative_to(root.resolve(strict=True))
                .as_posix()
            )
            stamped_id = identity(stamped_manifest)
            tree[stamped_rel] = stamped_id
            if stamped_rel.startswith(prefix):
                native[stamped_rel[len(prefix) :]] = stamped_id
        if "SOURCES.md" not in native:
            generated = generate_sources_markdown(core, bundle_root=root)
            src_id = identity(generated)
            native["SOURCES.md"] = src_id
            tree[prefix + "SOURCES.md"] = src_id

        write_json(core / inner, {"format": "managed-ai-engine-manifest/1", "members": native})
        tree[prefix + inner] = identity(core / inner)

    write_json(
        root / "MANIFEST.json",
        {
            "format": "managed-ai-current-package/1",
            "members": tree,
            "retention": "One shared engine, selected domains and independent current-use stores; "
            "no predecessor tree or nested transport.",
        },
    )
