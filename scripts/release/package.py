#!/usr/bin/env python3
"""Package the one current source tree, without inherited release directories."""

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai.platform import check_unfollowed_ancestors  # noqa: E402
from scripts.release._manifest import (  # noqa: E402
    manifest_name,
    members,
    refresh_manifests,
    reserved_stage,
    source_paths,
    verify_bundle,
    verify_sources,
)
from scripts.release._verify import (  # noqa: E402
    CATALOGUE,
    LEGACY_COMPRESSED_BASIS_PREFIXES,
    MARKER,
    MAX_COMPRESSED_TEXT_BYTES,
    ORDINARY_ERROR,
    SELECTION,
    STAGED_PROJECTIONS,
    check_compressed_text,
    check_ordinary_path,
    displayed_source_rows,
    exact_path,
    identity,
    inventory_engine,
    observed_path,
    optional_entry,
    ordinary,
    read_catalogue,
    read_json,
    received_compressed_text,
    verify_core_selection,
    verify_deployment_bindings,
    verify_source_list,
    verify_store_schemas,
    verify_workbench_components,
    write_json,
)
from scripts.release._verify import (  # noqa: E402
    store_compressed_prefixes as _store_compressed_prefixes,
)
from scripts.release._verify import (  # noqa: E402
    with_engine as _with_engine,
)

ROOT = REPO_ROOT


def _validate_output_path(root: Path, output_arg: Path, *, replace: bool) -> Path:
    output = check_unfollowed_ancestors(output_arg, ORDINARY_ERROR, include_target=True)
    existing = optional_entry(output)
    if existing is not None:
        ordinary(output, existing, stat.S_ISREG)
        if not replace:
            raise ValueError(
                "Output already exists; use --replace to update that current transport"
            )

    if output == root or root in output.parents:
        raise ValueError("Put transport output outside the source tree")

    for ancestor in (output.parent, *output.parent.parents):
        if optional_entry(ancestor) is not None:
            resolved_ancestor = ancestor.resolve(strict=True)
            if resolved_ancestor == root or root in resolved_ancestor.parents:
                raise ValueError("Put transport output outside the source tree")
            break

    return output


def _build_archive(root: Path, core: Path | None, source: dict[str, dict], output: Path) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".g60-package-", suffix=".partial", dir=output.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for relative, expected in source.items():
                path = root / relative
                if identity(path) != expected:
                    raise ValueError("Source changed during packaging: " + relative)
                info = zipfile.ZipInfo(root.name + "/" + relative, (1980, 1, 1, 0, 0, 0))
                info.external_attr = (stat.S_IFREG | expected["mode"]) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())

        with zipfile.ZipFile(temporary) as archive:
            if archive.namelist() != [root.name + "/" + relative for relative in source]:
                raise ValueError("Archive membership differs from the source inventory")
            if archive.testzip() is not None:
                raise ValueError("Archive CRC verification failed")
            for relative, expected in source.items():
                raw = archive.read(root.name + "/" + relative)
                if (
                    len(raw) != expected["bytes"]
                    or hashlib.sha256(raw).hexdigest() != expected["sha256"]
                ):
                    raise ValueError("Archive content differs: " + relative)

        if members(root, core) != source:
            raise ValueError("Source membership or content changed during packaging")

        existing = optional_entry(output)
        if existing is not None:
            ordinary(output, existing, stat.S_ISREG)

        os.replace(temporary, output)
        return {
            "status": "packaged",
            "output": str(output),
            "files": len(source),
            "bytes": output.lstat().st_size,
            "sha256": identity(output)["sha256"],
        }
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="One current source tree to package"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--replace", action="store_true", help="Atomically replace the named current transport"
    )
    parser.add_argument("--refresh-only", action="store_true")
    args = parser.parse_args()

    root = check_ordinary_path(args.root, stat.S_ISDIR)
    if not args.refresh_only and args.output is None:
        parser.error("--output is required unless --refresh-only is selected")

    output = (
        _validate_output_path(root, args.output, replace=args.replace)
        if args.output is not None
        else None
    )
    core = inventory_engine(root)
    refresh_manifests(root, core)
    source = members(root, core)
    if args.refresh_only:
        print(json.dumps({"status": "verified", "files": len(source)}))
        return 0

    if output is None:
        parser.error("--output is required unless --refresh-only is selected")

    summary = _build_archive(root, core, source, output)
    print(json.dumps(summary))
    return 0


__all__ = [
    "CATALOGUE",
    "LEGACY_COMPRESSED_BASIS_PREFIXES",
    "MARKER",
    "MAX_COMPRESSED_TEXT_BYTES",
    "ROOT",
    "SELECTION",
    "STAGED_PROJECTIONS",
    "_store_compressed_prefixes",
    "_with_engine",
    "check_compressed_text",
    "displayed_source_rows",
    "exact_path",
    "identity",
    "inventory_engine",
    "main",
    "manifest_name",
    "members",
    "observed_path",
    "optional_entry",
    "ordinary",
    "read_catalogue",
    "read_json",
    "received_compressed_text",
    "refresh_manifests",
    "reserved_stage",
    "source_paths",
    "verify_bundle",
    "verify_core_selection",
    "verify_deployment_bindings",
    "verify_source_list",
    "verify_sources",
    "verify_store_schemas",
    "verify_workbench_components",
    "write_json",
]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
