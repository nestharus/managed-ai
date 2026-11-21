#!/usr/bin/env python3
"""Package the one current source tree, without inherited release directories."""
import argparse
import hashlib
from itertools import zip_longest
import json
import os
from pathlib import Path
import stat
import tempfile
import zipfile
import runpy
import sys


ROOT = Path(__file__).resolve().parents[2]
SELECTION = Path("release/current_sources.json")
CATALOGUE = Path("prototype/agent-system/catalogue.json")
MARKER = Path("CURRENT-RELEASE.json")
# refresh_guidance stages each projection in an adjacent TemporaryDirectory.
STAGED_PROJECTIONS = (SELECTION, CATALOGUE, Path("SOURCES.md"), MARKER)


def identity(path):
    raw = path.read_bytes()
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "mode": stat.S_IMODE(path.stat().st_mode)}


def optional_entry(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def ordinary(path, info, kind):
    if not kind(info.st_mode) or getattr(info, "st_reparse_tag", 0):
        raise ValueError("Current source package requires ordinary files and directories: " + str(path))


def observed_path(root, relative, kind, *, scope_probe=False):
    """Find an ordinary member or determine that it is absent."""
    path = root
    for parent in relative.parts[:-1]:
        path /= parent
        info = optional_entry(path)
        if info is None:
            return None
        if scope_probe and stat.S_ISREG(info.st_mode) and not getattr(info, "st_reparse_tag", 0):
            return None
        ordinary(path, info, stat.S_ISDIR)
    path /= relative.name
    info = optional_entry(path)
    if info is not None:
        ordinary(path, info, kind)
        return path
    return None


def inventory_engine(root):
    if observed_path(root, Path("engine") / SELECTION, stat.S_ISREG, scope_probe=True) is not None:
        return (root / "engine").resolve(strict=True)
    if (observed_path(root, SELECTION, stat.S_ISREG, scope_probe=True) is not None
            and observed_path(root, MARKER, stat.S_ISREG) is not None):
        return root
    return None


def reserved_stage(path, info, prefixes):
    for prefix in prefixes:
        name = path.name
        if len(name) <= len(prefix) or not name.startswith(".") or name[len(prefix) - 1] != ".":
            continue
        if name.startswith(prefix):
            return True
        # Ask this directory's filesystem, including on Windows volumes that
        # permit case-distinct directory names.
        equivalent = optional_entry(path.with_name(prefix + name[len(prefix):]))
        if equivalent is not None and os.path.samestat(info, equivalent):
            return True
    return False


def source_paths(root, engine):
    ordinary(root, root.lstat(), stat.S_ISDIR)
    staging = {}
    if engine is not None:
        for target in STAGED_PROJECTIONS:
            parent = engine
            if target.parent.parts:
                parent = observed_path(engine, target.parent, stat.S_ISDIR)
            if parent is not None:
                key = parent.resolve(strict=True).as_posix()
                staging.setdefault(key, []).append(f".{target.name}.")
    pending = [root]
    while pending:
        parent = pending.pop()
        for path in parent.iterdir():
            if engine is not None and path.name.casefold() == ".git":
                raise ValueError(f"Git checkout metadata at {path}. Export the selected tracked files "
                                 "into a clean package directory as described in RELEASING.md.")
            if "__pycache__" in path.parts:
                continue
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode) and not getattr(info, "st_reparse_tag", 0):
                if not reserved_stage(path, info, staging.get(parent.as_posix(), ())):
                    pending.append(path)
            elif path.suffix != ".pyc":
                ordinary(path, info, stat.S_ISREG)
                yield path


def members(root, engine=...):
    if engine is ...:
        engine = inventory_engine(root)
    result = {}
    for path in sorted(source_paths(root, engine), key=lambda path: (path, path.as_posix())):
        relative = path.relative_to(root).as_posix()
        if "\\" in relative or ".." in Path(relative).parts:
            raise ValueError("Unsafe archive member path: " + relative)
        if path.suffix.lower() in {".zip", ".tar", ".gz"}:
            raise ValueError("Select extracted useful source instead of nested transport: " + str(path))
        result[relative] = identity(path)
    return result


def write_json(path, value):
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(raw)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_catalogue(path):
    # Match native_runtime.inputs.read_input without importing the runtime.
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("nonfinite JSON: " + value)

    try:
        catalogue = json.loads(path.read_bytes().decode("utf-8"), object_pairs_hook=unique_keys,
                               parse_constant=reject_constant)
        if not isinstance(catalogue, dict):
            raise ValueError("JSON object required")
    except ValueError as error:
        raise ValueError(f"Invalid current source catalogue {path.name}: {error}. "
                         "Repair the JSON or restore a valid catalogue, then run release guidance refresh.") from None
    return catalogue


def verify_core_selection(core, native, *, prefix=""):
    catalogue_member = CATALOGUE.as_posix()
    if catalogue_member not in native:
        raise ValueError(f"Package inventory requires the exact catalogue member {prefix}{catalogue_member}. "
                         "Correct the on-disk spelling before running release guidance refresh.")
    directories = {(core / parent).as_posix() for name in native for parent in Path(name).parents}
    selection_path = core / SELECTION
    selection = read_json(selection_path)
    current = read_json(core / MARKER)
    if current["selection_sha256"] != identity(selection_path)["sha256"]:
        raise ValueError("Current release names a different source selection")
    catalogue = read_catalogue(core / CATALOGUE)
    entries = {}
    for entry in catalogue["entries"]:
        ident = entry["id"]
        if ident in entries:
            raise ValueError(f"Duplicate core registration ID {ident!r} in {prefix}{catalogue_member}. "
                             "Correct the source catalogue before packaging.")
        entries[ident] = entry
    for row in selection["bodies"]:
        spelling = row["source"]
        if not isinstance(spelling, str) or not spelling or Path(spelling).anchor:
            raise ValueError("Current source reference must be relative to the engine: " + row["id"])
        relative = Path(spelling)
        reference = core / relative
        source = reference.resolve(strict=True)
        if not source.is_relative_to(core):
            raise ValueError("Current source selection is not a package member: " + row["id"])
        member = source.relative_to(core).as_posix()
        if (member not in native
                or any((core / parent).resolve(strict=True).as_posix() not in directories
                       for parent in relative.parents)):
            raise ValueError("Current source selection is not a package member: " + row["id"])
        actual = identity(source)
        if (actual["sha256"], actual["bytes"]) != (row["sha256"], row["bytes"]):
            raise ValueError("Current source selection changed: " + row["id"])
        entry = entries.get(row["id"], {})
        body_path = entry.get("body_path")
        if (not isinstance(body_path, str)
                or "\\" in body_path
                or Path(body_path).as_posix() != member
                or entry.get("source_root_relative") != "../.."
                or entry.get("body_role") != "complete-current-source-reference"
                or entry.get("availability") == "sealed"
                or (entry.get("sha256"), entry.get("bytes")) != (row["sha256"], row["bytes"])):
            raise ValueError(f"Current source catalogue reference differs for {row['id']}. "
                             f"Run release guidance refresh for the exact body member {member}.")
    return selection["bodies"]


def displayed_source_rows(page, selected_ids):
    """Read the selected identity under each heading, ignoring navigation prose."""
    for section in ("\n" + page).split("\n## ")[1:]:
        ident, _, body = section.partition("\n")
        lines = iter(body.lstrip("\n").splitlines())
        first = next(lines, "<missing identity>")
        if ident in selected_ids:
            yield f"{ident}: {first}"
            for line in lines:
                if line.startswith("[") and " bytes; SHA-256 `" in line:
                    yield f"{ident}: {line}"
        elif first.startswith("[") and " bytes; SHA-256 `" in first:
            yield f"{ident}: {first}"


def verify_source_list(path, bodies):
    guidance = runpy.run_path(str(Path(__file__).with_name("refresh_guidance.py")))
    format_identity = guidance["source_identity_line"]
    expected = (f"{row['id']}: {format_identity(row)}" for row in bodies)
    displayed = displayed_source_rows(path.read_text(encoding="utf-8"), {row["id"] for row in bodies})
    for wanted, found in zip_longest(expected, displayed, fillvalue="<no selected row>"):
        if wanted != found:
            raise ValueError(f"Stale engine/SOURCES.md\nExpected {wanted}\nObserved {found}\n"
                             "Run release guidance refresh.")


def descriptors(root, directory, filename):
    parent = observed_path(root, Path(directory), stat.S_ISDIR)
    if parent is None:
        return
    for child in sorted(parent.iterdir()):
        info = child.lstat()
        if stat.S_ISDIR(info.st_mode):
            ordinary(child, info, stat.S_ISDIR)
            descriptor = observed_path(child, Path(filename), stat.S_ISREG)
            if descriptor is not None:
                yield descriptor


def verify_sources(root, core, native):
    bodies = verify_core_selection(core, native, prefix="engine/")
    if "SOURCES.md" in native:
        verify_source_list(core / "SOURCES.md", bodies)
    component_root = root / "companions/workbench"
    components = read_json(component_root / "COMPONENTS.json")
    for name, row in components["files"].items():
        actual = identity(component_root / name)
        if {k: actual[k] for k in ("sha256", "bytes")} != row["identity"]:
            raise ValueError("Shared component identity changed: " + name)
    api = runpy.run_path(str(core / 'prototype/agent-system/system.py'))
    for descriptor in descriptors(root, 'stores', 'system.json'):
        system = api['System'](descriptor.parent)
        for record in system.records():
            system.domain_selection.validate_record(record)
    deployment_api = runpy.run_path(str(core / 'prototype/agent-system/deployment.py'))
    for descriptor in descriptors(root, 'deployments', 'deployment.json'):
        deployment_api['Deployment'](descriptor.parent).verify()


def manifest_name(root):
    name = Path("MANIFEST.json")
    path = observed_path(root, name, stat.S_ISREG)
    return name.as_posix() if path is None else path.resolve(strict=True).relative_to(root).as_posix()


def refresh_manifests(root, core):
    tree = members(root, core)
    tree.pop(manifest_name(root), None)
    if core == root:
        verify_core_selection(core, tree)
    elif core is not None:
        # The receiving bundle locates its engine at this exact member prefix.
        prefix = "engine/"
        native = {name[len(prefix):]: row for name, row in tree.items() if name.startswith(prefix)}
        inner = manifest_name(core)
        native.pop(inner, None)
        verify_sources(root, core, native)
        write_json(core / inner, {"format": "managed-ai-engine-manifest/1", "members": native})
        tree[prefix + inner] = identity(core / inner)
    write_json(root / "MANIFEST.json", {"format": "managed-ai-current-package/1", "members": tree,
        "retention": "One shared engine, selected domains and independent current-use stores; no predecessor tree or nested transport."})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="One current source tree to package")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--replace", action="store_true", help="Atomically replace the named current transport")
    parser.add_argument("--refresh-only", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    ordinary(root, root.lstat(), stat.S_ISDIR)
    if not args.refresh_only and args.output is None:
        parser.error("--output is required unless --refresh-only is selected")
    output = args.output.resolve() if args.output else None
    if output and (output == root or root in output.parents):
        raise ValueError("Put transport output outside the source tree")
    if output and optional_entry(output) is not None and not args.replace:
        raise ValueError("Output already exists; use --replace to update that current transport")
    core = inventory_engine(root)
    refresh_manifests(root, core)
    source = members(root, core)
    if args.refresh_only:
        print(json.dumps({"status": "verified", "files": len(source)}))
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".g60-package-", suffix=".partial", dir=output.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
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
                if len(raw) != expected["bytes"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]:
                    raise ValueError("Archive content differs: " + relative)
        if members(root, core) != source:
            raise ValueError("Source membership or content changed during packaging")
        os.replace(temporary, output)
        print(json.dumps({"status": "packaged", "output": str(output), "files": len(source),
                          "bytes": output.stat().st_size, "sha256": identity(output)["sha256"]}))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
