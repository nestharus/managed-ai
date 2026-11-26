"""Verification helpers for release packaging: identity, engine isolation, and boundaries."""
from __future__ import annotations

import codecs
from contextlib import contextmanager
import gzip
import hashlib
import importlib
import importlib.util
from itertools import zip_longest
import json
from pathlib import Path
import re
import runpy
import stat
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai.platform import (
    atomic_bytes,
    check_ordinary_ancestors,
    is_reparse_or_symlink_stat,
    sha256_file,
    strict_json_loads,
)
from scripts.release.refresh_guidance import source_identity_line

SELECTION = Path("release/current_sources.json")
CATALOGUE = Path("store/catalogue.json")
MARKER = Path("CURRENT-RELEASE.json")
MAX_COMPRESSED_TEXT_BYTES = 64 * 1024 * 1024
LEGACY_COMPRESSED_BASIS_PREFIXES = ("evidence/research-originals/",)
STAGED_PROJECTIONS = (SELECTION, CATALOGUE, Path("SOURCES.md"), MARKER)
ORDINARY_ERROR = "Current source package requires ordinary files and directories"


def identity(path: Path) -> dict:
    """Compute streaming SHA-256, exact byte length, and POSIX mode of an ordinary file."""
    info = path.lstat()
    ordinary(path, info, stat.S_ISREG)
    return {
        "sha256": sha256_file(path, follow_symlinks=False),
        "bytes": info.st_size,
        "mode": stat.S_IMODE(info.st_mode),
    }


def optional_entry(path: Path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def ordinary(path: Path, info, kind) -> None:
    if not kind(info.st_mode) or is_reparse_or_symlink_stat(info):
        raise ValueError(f"{ORDINARY_ERROR}: {path}")


def check_ordinary_path(path: Path, kind) -> Path:
    validated = check_ordinary_ancestors(
        path,
        expect_dir=(kind is stat.S_ISDIR),
        error_prefix=ORDINARY_ERROR,
    )
    return validated.resolve(strict=True)


def observed_path(root: Path, relative: Path, kind, *, scope_probe: bool = False) -> Path | None:
    """Find an ordinary member or determine that it is absent."""
    path = root
    for parent in relative.parts[:-1]:
        path = path / parent
        info = optional_entry(path)
        if info is None:
            return None
        if scope_probe and stat.S_ISREG(info.st_mode) and not is_reparse_or_symlink_stat(info):
            return None
        ordinary(path, info, stat.S_ISDIR)

    path = path / relative.name
    info = optional_entry(path)
    if info is not None:
        ordinary(path, info, kind)
        return path

    return None


def exact_path(root: Path, relative: Path, kind) -> Path | None:
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
                    expected = Path(*relative.parts[: index + 1]).as_posix()
                    raise ValueError(
                        f"Exact member spelling required: expected {expected}, observed {observed}."
                    )
            return None

        path = path / part
        ordinary(path, path.lstat(), expected_kind)

    return path


def is_package_engine(core: Path) -> bool:
    return (Path(core) / "__init__.py").is_file()


def inventory_engine(root: Path) -> Path | None:
    if observed_path(root, Path("src/managed_ai") / SELECTION, stat.S_ISREG, scope_probe=True) is not None:
        return (root / "src/managed_ai").resolve(strict=True)

    if observed_path(root, Path("engine") / SELECTION, stat.S_ISREG, scope_probe=True) is not None:
        return (root / "engine").resolve(strict=True)

    if (
        observed_path(root, SELECTION, stat.S_ISREG, scope_probe=True) is not None
        and observed_path(root, MARKER, stat.S_ISREG) is not None
    ):
        return root

    return None


def engine_prefix(root: Path, core: Path) -> str:
    return core.resolve(strict=True).relative_to(root.resolve(strict=True)).as_posix() + "/"


def catalogue_path(_core: Path) -> Path:
    return CATALOGUE


def write_bytes_atomic(path: Path, raw: bytes) -> None:
    existing = optional_entry(path)
    if existing is not None:
        ordinary(path, existing, stat.S_ISREG)

    atomic_bytes(path, raw)


def write_json(path: Path, value: object) -> None:
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_bytes_atomic(path, raw)


def read_json(path: Path) -> dict:
    ordinary(path, path.lstat(), stat.S_ISREG)
    return strict_json_loads(path.read_bytes(), str(path), require_object=True)


def read_catalogue(path: Path) -> dict:
    try:
        return read_json(path)
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(
            f"Invalid current source catalogue {path.name}: {error}. "
            "Repair the JSON or restore a valid catalogue, then run release guidance refresh."
        ) from None


@contextmanager
def with_engine(engine: Path, relative: str):
    """Mount an external or co-located engine cleanly across the full caller operation."""
    engine_path = Path(engine).resolve(strict=True)
    is_pkg = is_package_engine(engine_path)
    saved_modules = None
    saved_path = None
    try:
        if is_pkg:
            mod_name = "managed_ai." + relative[:-3].replace("/", ".")
            expected = (engine_path / relative).resolve(strict=True)
            existing = sys.modules.get("managed_ai")
            existing_file = getattr(existing, "__file__", None) if existing is not None else None
            existing_paths = list(getattr(existing, "__path__", ())) if existing is not None else []
            already_same = existing is not None and (
                (existing_file is not None and Path(existing_file).resolve() == (engine_path / "__init__.py").resolve())
                or any(Path(path_item).resolve() == engine_path for path_item in existing_paths)
            )
            if not already_same:
                saved_modules = {
                    key: sys.modules.pop(key)
                    for key in list(sys.modules)
                    if key == "managed_ai" or key.startswith("managed_ai.")
                }
                spec = importlib.util.spec_from_file_location(
                    "managed_ai",
                    str(engine_path / "__init__.py"),
                    submodule_search_locations=[str(engine_path)],
                )
                if spec is None or spec.loader is None:
                    raise ImportError("Unable to load managed_ai package from " + str(engine_path))
                pkg_mod = importlib.util.module_from_spec(spec)
                sys.modules["managed_ai"] = pkg_mod
                spec.loader.exec_module(pkg_mod)
                importlib.invalidate_caches()

            mod = importlib.import_module(mod_name)
            if Path(mod.__file__).resolve(strict=True) != expected:
                raise ValueError("Loaded engine module differs from selected engine: " + mod_name)
            yield vars(mod)
        else:
            saved_path = sys.path[:]
            sys.path.insert(0, str(engine_path))
            importlib.invalidate_caches()
            yield runpy.run_path(str(engine_path / relative))
    finally:
        if saved_modules is not None:
            for key in list(sys.modules):
                if key == "managed_ai" or key.startswith("managed_ai."):
                    sys.modules.pop(key, None)
            sys.modules.update(saved_modules)
            importlib.invalidate_caches()
        if saved_path is not None:
            sys.path[:] = saved_path
            importlib.invalidate_caches()


def received_compressed_text(system, *, engine: Path | None = None) -> set[tuple[str, str, int]]:
    """Select complete, source-bound compressed data from native import receipts."""
    if hasattr(system, "completed_import_records"):
        acknowledgements = system.completed_import_records()
    else:
        target_engine = engine if engine is not None else (REPO_ROOT / "src/managed_ai")
        with with_engine(target_engine, "store/records/record_import.py") as api:
            acknowledgements = list(api["completed_receipt_records"](system))

    accepted = {(item["id"], item["revision"], item["sha256"]) for item in acknowledgements}
    evidence = set()
    for head in system.records():
        if (head["id"], head["revision"], head["sha256"]) not in accepted:
            continue
        basis = {(item["path"], item["sha256"], item["bytes"]) for item in head["basis"]}
        for link in head["relations"]:
            if (
                link["relation"] != "grounded in original edition"
                or not link.get("origin", "").lower().endswith((".json.gz", ".jsonl.gz", ".tsv.gz"))
                or "ordered_materials" in link
            ):
                continue
            pin = (link["target"], link["sha256"], link["bytes"])
            if pin in basis:
                evidence.add(pin)

    return evidence


def check_compressed_text(path: Path, *, max_bytes: int = MAX_COMPRESSED_TEXT_BYTES) -> None:
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


def _normalize_prefixes(items) -> tuple[str, ...]:
    prefixes = []
    for prefix in items:
        if isinstance(prefix, str) and prefix:
            normalized = prefix if prefix.endswith("/") else prefix + "/"
            if normalized not in prefixes:
                prefixes.append(normalized)
    return tuple(prefixes)


def store_compressed_prefixes(store) -> tuple[str, ...]:
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
        descriptor = read_json(descriptor_path)
        for binding in descriptor.get("domains", []):
            if not isinstance(binding, dict):
                raise ValueError("Domain binding must be a JSON object")
            rel = binding.get("path")
            expected_sha = binding.get("sha256")
            if not isinstance(rel, str) or not rel or Path(rel).anchor or "\\" in rel:
                raise ValueError("Domain binding requires a relative POSIX path")
            if not isinstance(expected_sha, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
                raise ValueError("Domain binding requires an exact SHA-256 pin: " + rel)
            domain_path = check_ordinary_path(store_root / rel, stat.S_ISREG)
            domain_bytes = domain_path.read_bytes()
            if hashlib.sha256(domain_bytes).hexdigest() != expected_sha:
                raise ValueError("Domain descriptor pin mismatch: " + rel)
            domain = strict_json_loads(domain_bytes, str(domain_path), require_object=True)
            raw.extend(domain.get("content_addressed_basis_prefixes", []))

    normalized = _normalize_prefixes(raw)
    return normalized if normalized else LEGACY_COMPRESSED_BASIS_PREFIXES


def verify_core_selection(
    core: Path,
    native: dict,
    *,
    prefix: str = "",
    bundle_root: Path | None = None,
    tree: dict | None = None,
) -> list[dict]:
    catalogue_member = catalogue_path(core).as_posix()
    if catalogue_member not in native:
        raise ValueError(
            f"Package inventory requires the exact catalogue member {prefix}{catalogue_member}. "
            "Correct the on-disk spelling before running release guidance refresh."
        )

    bundle_mode = bundle_root is not None
    member_root = bundle_root.resolve(strict=True) if bundle_mode else core.resolve(strict=True)
    member_map = tree if (bundle_mode and tree is not None) else native
    if bundle_mode:
        expected_root_rel = "../../.." if (core.name == "managed_ai" and core.parent.name == "src") else "../.."
    else:
        expected_root_rel = ".."

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
            raise ValueError(
                f"Duplicate core registration ID {ident!r} in {prefix}{catalogue_member}. "
                "Correct the source catalogue before packaging."
            )
        entries[ident] = entry

    for row in selection["bodies"]:
        spelling = row["source"]
        if not isinstance(spelling, str) or not spelling or "\\" in spelling or Path(spelling).anchor:
            raise ValueError("Current source reference must be relative to the engine: " + row["id"])

        relative = Path(spelling)
        source = (core / relative).resolve(strict=True)
        allowed_root = member_root if (bundle_mode and ".." in relative.parts) else core
        if not source.is_relative_to(allowed_root):
            raise ValueError("Current source selection is not a package member: " + row["id"])

        member = source.relative_to(member_root).as_posix()
        rel_from_member_root = Path(member)
        if member not in member_map or any(
            (member_root / parent).resolve(strict=True).as_posix() not in directories
            for parent in rel_from_member_root.parents
        ):
            raise ValueError("Current source selection is not a package member: " + row["id"])

        actual = identity(source)
        if (actual["sha256"], actual["bytes"]) != (row["sha256"], row["bytes"]):
            raise ValueError("Current source selection changed: " + row["id"])

        entry = entries.get(row["id"], {})
        body_path = entry.get("body_path")
        if (
            not isinstance(body_path, str)
            or "\\" in body_path
            or Path(body_path).as_posix() != member
            or entry.get("source_root_relative") != expected_root_rel
            or entry.get("body_role") != "complete-current-source-reference"
            or entry.get("availability") == "sealed"
            or (entry.get("sha256"), entry.get("bytes")) != (row["sha256"], row["bytes"])
        ):
            raise ValueError(
                f"Current source catalogue reference differs for {row['id']}. "
                f"Run release guidance refresh for the exact body member {member}."
            )

    return selection["bodies"]


def displayed_source_rows(page: str, selected_ids: set[str]):
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


def verify_source_list(path: Path, bodies: list[dict]) -> None:
    expected = (f"{row['id']}: {source_identity_line(row)}" for row in bodies)
    displayed = displayed_source_rows(path.read_text(encoding="utf-8"), {row["id"] for row in bodies})
    for wanted, found in zip_longest(expected, displayed, fillvalue="<no selected row>"):
        if wanted != found:
            raise ValueError(
                f"Stale {path.parent.name}/SOURCES.md\nExpected {wanted}\nObserved {found}\n"
                "Run release guidance refresh."
            )


def descriptors(root: Path, directory: str, filename: str):
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


def _locate_peer_manifest(root: Path) -> tuple[Path | None, Path | None]:
    pkg_dir = exact_path(root, Path("src/managed_ai"), stat.S_ISDIR)
    companions_root = None
    manifest_label = "companions/peer/COMPONENTS.json"
    if pkg_dir is not None:
        companions_root = exact_path(pkg_dir, Path("companions"), stat.S_ISDIR)
        if companions_root is not None:
            manifest_label = "src/managed_ai/companions/peer/COMPONENTS.json"

    if companions_root is None:
        companions_root = exact_path(root, Path("companions"), stat.S_ISDIR)
    if companions_root is None:
        return None, None

    peer_root = exact_path(companions_root, Path("peer"), stat.S_ISDIR)
    if peer_root is None:
        return None, None

    manifest_path = exact_path(peer_root, Path("COMPONENTS.json"), stat.S_ISREG)
    if manifest_path is None:
        raise ValueError("Missing shared peer component manifest: " + manifest_label)

    return companions_root, manifest_path


def verify_workbench_components(root: Path, *, stamp: bool = False) -> Path | None:
    companions_root, manifest_path = _locate_peer_manifest(root)
    if companions_root is None or manifest_path is None:
        return None

    try:
        components = read_json(manifest_path)
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"Invalid peer component manifest {manifest_path.name}: {error}") from None

    files = components.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Invalid peer component manifest COMPONENTS.json: non-empty 'files' object required")

    resolved_companions = companions_root.resolve(strict=True)
    stamped_files = {}
    needs_stamp = False

    for name, row in files.items():
        if not isinstance(name, str) or not name or "\\" in name:
            raise ValueError("Invalid shared component path: " + repr(name))
        if not isinstance(row, dict):
            raise ValueError("Invalid shared component entry: " + name)

        relative = Path(name)
        if relative.anchor or ".." in relative.parts or not relative.parts or relative.as_posix() != name:
            raise ValueError("Unsafe shared component path: " + name)

        target = exact_path(companions_root, relative, stat.S_ISREG)
        if target is None:
            raise ValueError("Missing shared component: " + name)
        if not target.resolve(strict=True).is_relative_to(resolved_companions):
            raise ValueError("Unsafe shared component path: " + name)

        actual = identity(target)
        actual_pin = {"bytes": actual["bytes"], "sha256": actual["sha256"]}
        if "identity" in row:
            expected = row["identity"]
            if (
                not isinstance(expected, dict)
                or set(expected) != {"sha256", "bytes"}
                or not isinstance(expected["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", expected["sha256"]) is None
                or type(expected["bytes"]) is not int
                or expected["bytes"] < 0
            ):
                raise ValueError("Invalid shared component identity entry: " + name)
            if actual_pin != expected:
                raise ValueError("Shared component identity changed: " + name)
            stamped_files[name] = row
        else:
            needs_stamp = True
            updated = dict(row)
            updated["identity"] = actual_pin
            stamped_files[name] = updated

    if stamp and needs_stamp:
        stamped_doc = dict(components)
        stamped_doc["files"] = stamped_files
        write_json(manifest_path, stamped_doc)
        return manifest_path

    return None


def verify_store_schemas(root: Path, core: Path) -> None:
    stores = list(descriptors(root, "stores", "system.json"))
    if not stores:
        return

    with with_engine(core, "store/system.py") as api:
        for descriptor in stores:
            system = api["System"](descriptor.parent)
            for record in system.records():
                system.domain_selection.validate_record(record)


def verify_deployment_bindings(root: Path, core: Path) -> None:
    deployments = list(descriptors(root, "deployments", "deployment.json"))
    if not deployments:
        return

    with with_engine(core, "deployment/controller.py") as deployment_api:
        for descriptor in deployments:
            deployment_api["Deployment"](descriptor.parent).verify()
