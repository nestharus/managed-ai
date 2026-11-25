#!/usr/bin/env python3
"""Package the one current source tree, without inherited release directories."""
import argparse
import codecs
import gzip
import re
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
CATALOGUE = Path("store/catalogue.json")
MARKER = Path("CURRENT-RELEASE.json")
MAX_COMPRESSED_TEXT_BYTES = 64 * 1024 * 1024
LEGACY_COMPRESSED_BASIS_PREFIXES = ("evidence/research-originals/",)
REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
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


def _is_reparse(info):
    return bool(
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_reparse_tag", 0)
        or (getattr(info, "st_file_attributes", 0) & REPARSE_FLAG)
    )


def ordinary(path, info, kind):
    if not kind(info.st_mode) or _is_reparse(info):
        raise ValueError("Current source package requires ordinary files and directories: " + str(path))


def observed_path(root, relative, kind, *, scope_probe=False):
    """Find an ordinary member or determine that it is absent."""
    path = root
    for parent in relative.parts[:-1]:
        path /= parent
        info = optional_entry(path)
        if info is None:
            return None
        if scope_probe and stat.S_ISREG(info.st_mode) and not _is_reparse(info):
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


def catalogue_path(core):
    return CATALOGUE


def source_paths(root, engine):
    ordinary(root, root.lstat(), stat.S_ISDIR)
    staging = {}
    if engine is not None:
        for target in STAGED_PROJECTIONS:
            if target == CATALOGUE:
                target = catalogue_path(engine)
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
            if stat.S_ISDIR(info.st_mode) and not _is_reparse(info):
                if not reserved_stage(path, info, staging.get(parent.as_posix(), ())):
                    pending.append(path)
            elif path.suffix != ".pyc":
                ordinary(path, info, stat.S_ISREG)
                yield path


def _run_with_engine(engine, relative):
    engine_str = str(Path(engine).resolve(strict=True))
    added = engine_str not in sys.path
    if added:
        sys.path.insert(0, engine_str)
    try:
        return runpy.run_path(str(Path(engine) / relative))
    finally:
        if added and engine_str in sys.path:
            sys.path.remove(engine_str)


def received_compressed_text(system):
    """Select complete, source-bound compressed data from native import receipts."""
    if hasattr(system, "completed_import_records"):
        acknowledgements = system.completed_import_records()
    else:
        # Earlier native instances expose the same exchange and instance
        # binding. Read their retained receipts through the current owner.
        engine = Path(__file__).resolve().parents[1]
        reader = _run_with_engine(engine, "store/records/record_import.py")["completed_receipt_records"]
        acknowledgements = reader(system)
    accepted = {(item["id"], item["revision"], item["sha256"]) for item in acknowledgements}
    evidence = set()
    for head in system.records():
        if (head["id"], head["revision"], head["sha256"]) not in accepted:
            continue
        basis = {(item["path"], item["sha256"], item["bytes"]) for item in head["basis"]}
        for link in head["relations"]:
            if (link["relation"] != "grounded in original edition"
                    or not link.get("origin", "").lower().endswith((".json.gz", ".jsonl.gz", ".tsv.gz"))
                    or "ordered_materials" in link):
                continue
            pin = (link["target"], link["sha256"], link["bytes"])
            if pin in basis:
                evidence.add(pin)
    return evidence


def check_compressed_text(path, *, max_bytes=MAX_COMPRESSED_TEXT_BYTES):
    """Read a bounded gzip stream and reject binary payloads or decompression bombs."""
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("Compressed text byte bound must be a nonnegative integer")
    decode = codecs.getincrementaldecoder("utf-8-sig")()
    total = 0
    with gzip.open(path, "rb") as source:
        while True:
            chunk = source.read(min(65536, max_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"Compressed text exceeds byte bound ({max_bytes} bytes): {path}")
            if re.search(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", chunk):
                raise ValueError("Binary content in compressed research data: " + str(path))
            decode.decode(chunk)
        decode.decode(b"", final=True)


def _normalize_prefixes(items):
    prefixes = []
    for prefix in items:
        if isinstance(prefix, str) and prefix:
            normalized = prefix if prefix.endswith("/") else prefix + "/"
            if normalized not in prefixes:
                prefixes.append(normalized)
    return tuple(prefixes)


def _check_ordinary_path(path, kind):
    normalized = Path(os.path.abspath(path))
    for ancestor in reversed(normalized.parents):
        ordinary(ancestor, ancestor.lstat(), stat.S_ISDIR)
    ordinary(normalized, normalized.lstat(), kind)
    return normalized.resolve(strict=True)


def _store_compressed_prefixes(store):
    """Return allowed content-addressed basis prefixes declared by the store's domains."""
    selection = getattr(store, "domain_selection", None)
    declared = getattr(selection, "content_addressed_basis_prefixes", None) if selection is not None else None
    if declared:
        normalized = _normalize_prefixes(declared)
        if normalized:
            return normalized
    raw = []
    store_root = Path(store.root)
    descriptor_path = store_root / "system.json"
    if optional_entry(descriptor_path) is not None:
        descriptor = _read_strict_object(descriptor_path)
        for binding in descriptor.get("domains", []):
            if not isinstance(binding, dict):
                raise ValueError("Domain binding must be a JSON object")
            rel = binding.get("path")
            expected_sha = binding.get("sha256")
            if not isinstance(rel, str) or not rel or Path(rel).anchor or "\\" in rel:
                raise ValueError("Domain binding requires a relative POSIX path")
            if not isinstance(expected_sha, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
                raise ValueError("Domain binding requires an exact SHA-256 pin: " + rel)
            domain_path = _check_ordinary_path(store_root / rel, stat.S_ISREG)
            domain_bytes = domain_path.read_bytes()
            if hashlib.sha256(domain_bytes).hexdigest() != expected_sha:
                raise ValueError("Domain descriptor pin mismatch: " + rel)
            domain = _parse_strict_object(domain_bytes)
            raw.extend(domain.get("content_addressed_basis_prefixes", []))
    normalized = _normalize_prefixes(raw)
    return normalized if normalized else LEGACY_COMPRESSED_BASIS_PREFIXES


def members(root, engine=...):
    if engine is ...:
        engine = inventory_engine(root)
    result = {}
    stores = {}
    native_api = None
    for path in sorted(source_paths(root, engine), key=lambda path: (path, path.as_posix())):
        relative = path.relative_to(root).as_posix()
        if "\\" in relative or ".." in Path(relative).parts:
            raise ValueError("Unsafe archive member path: " + relative)
        if path.suffix.lower() in {".zip", ".tar", ".gz"}:
            match = re.fullmatch(
                r"stores/([^/]+)/work/((?:.+/)?([a-f0-9]{2})/([a-f0-9]{64})\.gz)",
                relative,
            )
            if (not match or match[3] != match[4][:2] or engine is None or engine == root
                    or not (root / "stores" / match[1] / "system.json").is_file()):
                raise ValueError("Select extracted useful source instead of nested transport: " + str(path))
            store_name, work_rel, _, expected_sha = match.groups()
            if store_name not in stores:
                if native_api is None:
                    native_api = _run_with_engine(engine, "store/system.py")
                store = native_api["System"](root / "stores" / store_name)
                stores[store_name] = (
                    store,
                    received_compressed_text(store),
                    _store_compressed_prefixes(store),
                )
            store, allowed, allowed_prefixes = stores[store_name]
            if not any(work_rel.startswith(prefix) for prefix in allowed_prefixes):
                raise ValueError("Select extracted useful source instead of nested transport: " + str(path))
            current = identity(path)
            if current["sha256"] != expected_sha or (work_rel, expected_sha, current["bytes"]) not in allowed:
                raise ValueError("Missing accepted research evidence for compressed source: " + relative)
            store.ex.objects.read(expected_sha, expected_bytes=current["bytes"])
            check_compressed_text(path)
            result[relative] = current
        else:
            result[relative] = identity(path)
    return result


def write_json(path, value):
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(raw)


def read_json(path):
    return _read_strict_object(path)


def _parse_strict_object(raw):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("nonfinite JSON: " + value)

    data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys,
                      parse_constant=reject_constant)
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    return data


def _read_strict_object(path):
    # Match store.protocol.inputs without importing the runtime.
    ordinary(path, path.lstat(), stat.S_ISREG)
    return _parse_strict_object(path.read_bytes())


def read_catalogue(path):
    try:
        return _read_strict_object(path)
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"Invalid current source catalogue {path.name}: {error}. "
                         "Repair the JSON or restore a valid catalogue, then run release guidance refresh.") from None


def verify_core_selection(core, native, *, prefix="", bundle_root=None, tree=None):
    catalogue_member = catalogue_path(core).as_posix()
    if catalogue_member not in native:
        raise ValueError(f"Package inventory requires the exact catalogue member {prefix}{catalogue_member}. "
                         "Correct the on-disk spelling before running release guidance refresh.")
    bundle_mode = bundle_root is not None
    member_root = bundle_root.resolve(strict=True) if bundle_mode else core.resolve(strict=True)
    member_map = tree if (bundle_mode and tree is not None) else native
    expected_root_rel = "../.." if bundle_mode else ".."
    directories = {(member_root / parent).as_posix() for name in member_map for parent in Path(name).parents}
    selection_path = core / SELECTION
    selection = read_json(selection_path)
    current = read_json(core / MARKER)
    if current["selection_sha256"] != identity(selection_path)["sha256"]:
        raise ValueError("Current release names a different source selection")
    catalogue = read_catalogue(core / catalogue_member)
    entries = {}
    for entry in catalogue["entries"]:
        ident = entry["id"]
        if ident in entries:
            raise ValueError(f"Duplicate core registration ID {ident!r} in {prefix}{catalogue_member}. "
                             "Correct the source catalogue before packaging.")
        entries[ident] = entry
    for row in selection["bodies"]:
        spelling = row["source"]
        if not isinstance(spelling, str) or not spelling or "\\" in spelling or Path(spelling).anchor:
            raise ValueError("Current source reference must be relative to the engine: " + row["id"])
        relative = Path(spelling)
        reference = core / relative
        source = reference.resolve(strict=True)
        allowed_root = member_root if (bundle_mode and ".." in relative.parts) else core
        if not source.is_relative_to(allowed_root):
            raise ValueError("Current source selection is not a package member: " + row["id"])
        member = source.relative_to(member_root).as_posix()
        rel_from_member_root = Path(member)
        if (member not in member_map
                or any((member_root / parent).resolve(strict=True).as_posix() not in directories
                       for parent in rel_from_member_root.parents)):
            raise ValueError("Current source selection is not a package member: " + row["id"])
        actual = identity(source)
        if (actual["sha256"], actual["bytes"]) != (row["sha256"], row["bytes"]):
            raise ValueError("Current source selection changed: " + row["id"])
        entry = entries.get(row["id"], {})
        body_path = entry.get("body_path")
        if (not isinstance(body_path, str)
                or "\\" in body_path
                or Path(body_path).as_posix() != member
                or entry.get("source_root_relative") != expected_root_rel
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


def exact_path(root, relative, kind):
    """Find an ordinary member with exact on-disk spelling at every segment, or None if absent."""
    ordinary(root, root.lstat(), stat.S_ISDIR)
    path = root
    for index, part in enumerate(relative.parts):
        expected_kind = stat.S_ISDIR if index < len(relative.parts) - 1 else kind
        names = [entry.name for entry in path.iterdir()]
        if part not in names:
            for existing in names:
                if existing.casefold() == part.casefold():
                    observed = Path(*relative.parts[:index], existing).as_posix()
                    expected = Path(*relative.parts[:index + 1]).as_posix()
                    raise ValueError(
                        f"Exact member spelling required: expected {expected}, observed {observed}."
                    )
            return None
        path = path / part
        ordinary(path, path.lstat(), expected_kind)
    return path


def verify_workbench_components(root):
    companions_root = exact_path(root, Path("companions"), stat.S_ISDIR)
    if companions_root is None:
        return
    peer_root = exact_path(companions_root, Path("peer"), stat.S_ISDIR)
    if peer_root is None:
        return
    manifest_path = exact_path(peer_root, Path("COMPONENTS.json"), stat.S_ISREG)
    if manifest_path is None:
        raise ValueError("Missing shared peer component manifest: companions/peer/COMPONENTS.json")
    try:
        components = _read_strict_object(manifest_path)
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"Invalid peer component manifest {manifest_path.name}: {error}") from None
    files = components.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Invalid peer component manifest COMPONENTS.json: non-empty 'files' object required")
    resolved_companions = companions_root.resolve(strict=True)
    for name, row in files.items():
        if not isinstance(name, str) or not name or "\\" in name:
            raise ValueError("Invalid shared component path: " + repr(name))
        relative = Path(name)
        if relative.anchor or ".." in relative.parts or not relative.parts or relative.as_posix() != name:
            raise ValueError("Unsafe shared component path: " + name)
        target = exact_path(companions_root, relative, stat.S_ISREG)
        if target is None:
            raise ValueError("Missing shared component: " + name)
        if not target.resolve(strict=True).is_relative_to(resolved_companions):
            raise ValueError("Unsafe shared component path: " + name)
        expected = row.get("identity") if isinstance(row, dict) else None
        if (not isinstance(expected, dict) or set(expected) != {"sha256", "bytes"}
                or not isinstance(expected["sha256"], str) or type(expected["bytes"]) is not int):
            raise ValueError("Invalid shared component identity entry: " + name)
        actual = identity(target)
        if {k: actual[k] for k in ("sha256", "bytes")} != expected:
            raise ValueError("Shared component identity changed: " + name)


def verify_store_schemas(root, core):
    stores = list(descriptors(root, 'stores', 'system.json'))
    if not stores:
        return
    api = _run_with_engine(core, 'store/system.py')
    for descriptor in stores:
        system = api['System'](descriptor.parent)
        for record in system.records():
            system.domain_selection.validate_record(record)


def verify_deployment_bindings(root, core):
    deployments = list(descriptors(root, 'deployments', 'deployment.json'))
    if not deployments:
        return
    deployment_api = _run_with_engine(core, 'deployment/controller.py')
    for descriptor in deployments:
        deployment_api['Deployment'](descriptor.parent).verify()


def verify_bundle(root, core, native, tree=None):
    if tree is None:
        tree = members(root, core)
    bodies = verify_core_selection(core, native, prefix="engine/", bundle_root=root, tree=tree)
    if "SOURCES.md" in native:
        verify_source_list(core / "SOURCES.md", bodies)
    verify_workbench_components(root)
    verify_store_schemas(root, core)
    verify_deployment_bindings(root, core)


# Retain the original callable for release tooling.
verify_sources = verify_bundle


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
        verify_bundle(root, core, native, tree=tree)
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
