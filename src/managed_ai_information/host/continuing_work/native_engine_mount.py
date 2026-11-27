"""Native engine source qualification, deployment pin verification, and isolated mounting."""

import ast
import contextlib
import hashlib
import importlib
import importlib.util
import json
import os
import re
import stat
import sys
from collections.abc import Generator
from contextlib import contextmanager
from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path
from typing import Any

from managed_ai_information.host.continuing_work.occurrence_files import (
    check_unfollowed_ancestors,
    is_reparse_or_symlink,
    loads_strict,
)

HERE = Path(__file__).resolve().parent
COLOCATED_ENGINE_ROOT = HERE.parents[1]
COLOCATED_ENGINE_ENTRY = COLOCATED_ENGINE_ROOT / "store/manage_store.py"
EXPECTED_ENGINE_API = "managed-ai-runtime/1"
EXPECTED_DEPLOYMENT_API = "agent-system-deployment/2"
EXPECTED_STORE_SCHEMA = "managed-ai-store/1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_ID_EXCLUDED_DIRS = frozenset({"tests", "examples", "patterns", "companions", "collective"})
_NON_SOURCE_SUFFIXES = (".pyc", ".pyo", ".pyd", ".so", *EXTENSION_SUFFIXES)

# Frozen historical pre-Wave-4 engine/runtime/ profiles kept only for archival replay.
LEGACY_NATIVE_PROFILES = frozenset(
    {
        "f65119d9a2751f7927ecfb502df1f26c07ee1c958c9b219aeb13f6efc1c6565e",
        "65d21e3482ae5ca9a4a9a3060e8d2c60e98bc95e224f6969dbed198d317910aa",
        "7b6176300e8802b9c2714bf9cd9ebdfa8d2741b41d21c680c39f5bf5a32784e3",
        "fb535b60e0d22b6fc482f84dcd4482ecce2dda52fa6e06ee5915268a4cc3f621",
        "e92245c09bc33a22e7aa086be2ae141093dd19bf2302f69d53c771e5e8e23cc9",
        "5f0830029a210dd73fd3299a3a6f5c170a2802dadf85b3076b5640831774d836",
        "e031f4e246801e7ea5d41306c7356891872af6444f7fd9b998bacd48ea67945a",
        "9b879473fc1a7a46b530027d5f7045101b05183b9742027861af15d3f145e6ea",
        "6bb020ad0a1feacaab040b7b8bfe448e9f472525902bad1203344d2ec2f436f4",
        "27c5c10a3c1a0016fb5fb9e86454025103aa1a22e9889044ad65bd9f8932316d",
        "db6b9c8a443483e99932b8970b5abbc506622d1a2f337a1fe9b452a98cec706a",
        "cc7fb2cdb1c88b5ecff826bac7088e93d11f13a0317fd673f5291778736f1c44",
        "e772c4c3a51d8db2937a5cbcb991dc0a5c2433f70082a7b02d32cda5aabdd5e4",
        "2eb453170ca260aa32575b628480ec529d71f78d80dcbe052ebe5f807569945f",
        "a61df8f59eadde2f1631f7002e062770ae68c577de973717e9f4997748fee1d4",
        "dbfaee1c623984cbd60833413de6c4aab1033214d4ddf6300c415b04bfbeefce",
        "23c5c06540959fe6cbdd0e9e58e3bb69c328616f879caa6fd4604b22d790cf82",
        "82f1d0421b9cb9c134e5d0fb97c14e530407c409ac3716730a7f758e16ddd609",
    },
)

PROFILE_ERROR = (
    "Selected native engine does not match the reviewed capture adapter; "
    "requalify this adapter before capture"
)
EXTERNAL_PROFILE_ERROR = (
    "External native engine requires explicit --expected-profile or "
    "verified --deployment pin before capture"
)
CONTRACT_ERROR = "Selected native engine does not satisfy the required capture adapter contract"
RETURN_ERROR = "Native return already exists; use its retained body or a new current occurrence"


def _is_test_module(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(".py") and (lower.startswith("test_") or lower.endswith("_test.py"))


def _profile_digest(programs: dict[str, str]) -> str:
    payload = json.dumps(programs, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scan_ordinary_programs(
    base: Path, roots: tuple[Path, ...], *, exclude_tests: bool
) -> dict[str, str]:
    programs: dict[str, str] = {}

    for folder in roots:
        if is_reparse_or_symlink(folder):
            raise ValueError(
                f"Native engine tree must not contain symlinks or reparse points: {folder}"
            )
        if not folder.is_dir():
            continue

        for parent, dirs, files in os.walk(folder, followlinks=False):
            dirs.sort()
            files.sort()
            kept_dirs: list[str] = []
            for dname in dirs:
                dpath = Path(parent) / dname
                if is_reparse_or_symlink(dpath):
                    raise ValueError(
                        f"Native engine tree must not contain symlinks or reparse points: {dpath}",
                    )
                if dname == "__pycache__" or (exclude_tests and dname == "tests"):
                    continue
                kept_dirs.append(dname)
            dirs[:] = kept_dirs

            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = Path(parent) / fname
                if is_reparse_or_symlink(fpath):
                    raise ValueError(
                        f"Native engine tree must not contain symlinks or reparse points: {fpath}",
                    )
                if not stat.S_ISREG(fpath.lstat().st_mode):
                    raise ValueError(f"Native engine source must be an ordinary file: {fpath}")
                if exclude_tests and _is_test_module(fname):
                    continue
                programs[fpath.relative_to(base).as_posix()] = hashlib.sha256(
                    fpath.read_bytes()
                ).hexdigest()

    return programs


def _native_sources_with_layout(entry: Path) -> tuple[Path, dict[str, str], str]:
    checked = check_unfollowed_ancestors(entry, "Native engine entry", include_target=True)
    if not checked.exists() or not stat.S_ISREG(checked.lstat().st_mode):
        raise ValueError(PROFILE_ERROR)

    resolved = checked.resolve(strict=True)
    engine = resolved.parents[1]

    if resolved == engine / "store/manage_store.py" and (engine / "store/system.py").is_file():
        native = engine / "store/system.py"
        if is_reparse_or_symlink(native):
            raise ValueError(
                f"Native engine entry must not be a symlink or reparse point: {native}"
            )
        roots = tuple(
            engine / name
            for name in ("deployment", "core", "store", "platform")
            if (engine / name).exists()
        )
        programs = _scan_ordinary_programs(engine, roots, exclude_tests=True)
        return native, programs, "canonical"

    if (
        resolved == engine / "runtime/manage_store.py"
        and (engine / "runtime/native/system.py").is_file()
    ):
        native = engine / "runtime/native/system.py"
        if is_reparse_or_symlink(native):
            raise ValueError(
                f"Native engine entry must not be a symlink or reparse point: {native}"
            )
        programs = _scan_ordinary_programs(engine, (engine / "runtime",), exclude_tests=False)
        return native, programs, "legacy"

    raise ValueError(PROFILE_ERROR)


def native_sources(entry: Path) -> tuple[Path, dict[str, str]]:
    """Collect the exact executable native engine source map for profile qualification."""
    native, programs, _ = _native_sources_with_layout(entry)
    return native, programs


def _collect_engine_entry(
    entry: os.DirEntry[str],
    directory: Path,
    in_cache: bool,
    pending: list[tuple[Path, bool]],
    paths: list[Path],
) -> None:
    """Validate one scanned engine directory entry and enqueue subdirs or Python sources."""
    path = directory / entry.name
    info = entry.stat(follow_symlinks=False)
    if is_reparse_or_symlink(path):
        raise ValueError(f"shared engine may not use symbolic links or reparse points: {path}")

    mode = info.st_mode
    if stat.S_ISDIR(mode):
        if entry.name.casefold() not in _CODE_ID_EXCLUDED_DIRS:
            pending.append((path, in_cache or entry.name.casefold() == "__pycache__"))
        return

    if not stat.S_ISREG(mode):
        raise ValueError(f"ordinary shared engine file or directory required: {path}")

    if in_cache:
        if path.suffix.lower() != ".pyc":
            raise ValueError(f"ordinary bytecode cache files required below __pycache__: {path}")
        return

    if path.name.lower().endswith(_NON_SOURCE_SUFFIXES):
        raise ValueError(f"shared engine requires Python source for executable modules: {path}")

    if path.match("*.py") and not _is_test_module(entry.name):
        paths.append(path)


def _declared_constants(source: bytes, path: Path) -> dict[str, Any]:
    """Parse top-level literal assignments from an engine declaration module without exec."""
    result: dict[str, Any] = {}
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise ValueError(f"invalid engine API declaration in {path}: {exc.msg}") from exc
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            with contextlib.suppress(ValueError, TypeError):
                result[node.targets[0].id] = ast.literal_eval(node.value)
    return result


def _engine_code_identity(engine_root: Path) -> str:
    """Compute deployment/controller.py:code_identity() without importing external engine code."""
    engine = check_unfollowed_ancestors(
        engine_root,
        "Deployment pinned engine",
        include_target=True,
    ).resolve(strict=True)
    if not engine.is_dir():
        raise ValueError(f"ordinary shared engine directory required: {engine}")

    paths: list[Path] = []
    pending: list[tuple[Path, bool]] = [(engine, False)]
    while pending:
        directory, in_cache = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                _collect_engine_entry(entry, directory, in_cache, pending, paths)

    native_entry = Path("store/system.py")
    native_contract = Path("store/selection/domain_selection.py")
    declaration_paths = {native_entry, native_contract}
    sorted_paths = sorted(paths)

    def _hash_chunk(chunk: list[Path]) -> list[tuple[Path, dict[str, str], bytes | None]]:
        out: list[tuple[Path, dict[str, str], bytes | None]] = []
        for path in chunk:
            relative = path.relative_to(engine)
            raw = path.read_bytes()
            out.append(
                (
                    relative,
                    {"path": relative.as_posix(), "sha256": hashlib.sha256(raw).hexdigest()},
                    raw if relative in declaration_paths else None,
                )
            )
        return out

    if len(sorted_paths) >= 16:
        from concurrent.futures import ThreadPoolExecutor

        workers = min(8, (len(sorted_paths) + 7) // 8)
        step = (len(sorted_paths) + workers - 1) // workers
        chunks = [sorted_paths[i : i + step] for i in range(0, len(sorted_paths), step)]
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            hashed = [item for sub in pool.map(_hash_chunk, chunks) for item in sub]
    else:
        hashed = _hash_chunk(sorted_paths)

    rows: list[dict[str, str]] = []
    declarations: dict[Path, bytes] = {}
    for relative, row, raw in hashed:
        rows.append(row)
        if raw is not None:
            declarations[relative] = raw

    if declaration_paths != declarations.keys():
        raise ValueError(
            "select the current engine root containing its native entry and API declarations"
        )

    contract = _declared_constants(declarations[native_contract], engine / native_contract)
    entry_decl = _declared_constants(declarations[native_entry], engine / native_entry)
    if (
        contract.get("ENGINE_API") != EXPECTED_ENGINE_API
        or contract.get("STORE_SCHEMA") != EXPECTED_STORE_SCHEMA
        or entry_decl.get("DEPLOYMENT_API") != EXPECTED_DEPLOYMENT_API
    ):
        raise ValueError("selected engine declares incompatible storage/deployment APIs")

    encoded_rows = (
        json.dumps(rows, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(encoded_rows).hexdigest()


def _verify_deployment_pin(deployment_dir: Path, expected_engine_root: Path) -> None:
    dep_root = check_unfollowed_ancestors(
        deployment_dir,
        "Deployment directory",
        include_target=True,
    ).resolve(strict=True)
    if not dep_root.is_dir():
        raise ValueError(f"Deployment directory does not exist: {dep_root}")

    descriptor_path = check_unfollowed_ancestors(
        dep_root / "deployment.json",
        "Deployment descriptor",
        include_target=True,
    )
    if not descriptor_path.is_file():
        raise ValueError(f"Deployment descriptor is missing: {descriptor_path}")

    doc = loads_strict(descriptor_path.read_bytes())
    if not isinstance(doc, dict) or doc.get("format") != EXPECTED_DEPLOYMENT_API:
        raise ValueError("Deployment descriptor format is incompatible")

    engine_spec = doc.get("engine")
    if (
        not isinstance(engine_spec, dict)
        or not isinstance(engine_spec.get("path"), str)
        or not isinstance(engine_spec.get("code_sha256"), str)
        or engine_spec.get("engine_api") != EXPECTED_ENGINE_API
    ):
        raise ValueError("Deployment descriptor lacks a valid engine pin")

    pinned_engine = check_unfollowed_ancestors(
        dep_root / engine_spec["path"],
        "Deployment pinned engine",
        include_target=True,
    )
    if pinned_engine.resolve(strict=True) != expected_engine_root.resolve(strict=True):
        raise ValueError("Selected native engine path differs from verified deployment pin")
    if _engine_code_identity(pinned_engine) != engine_spec["code_sha256"]:
        raise ValueError("Selected native engine code differs from verified deployment pin")


def _resolve_git_ref(base: Path, ref_name: str) -> str | None:
    """Resolve one Git ref from loose ref file or packed-refs under base."""
    ref_file = base / ref_name
    if ref_file.is_file():
        commit = ref_file.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{40}", commit):
            return commit

    packed = base / "packed-refs"
    if not packed.is_file():
        return None
    for line in packed.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", "^")):
            continue
        parts = line.split(" ", 1)
        if (
            len(parts) == 2
            and parts[1].strip() == ref_name
            and re.fullmatch(r"[0-9a-f]{40}", parts[0])
        ):
            return parts[0]
    return None


def _git_head_commit(engine_root: Path) -> str | None:
    """Best-effort read of Git HEAD commit SHA without spawning subprocesses."""
    cur = engine_root.resolve()
    git_entry = None
    for candidate in (cur, *cur.parents[:4]):
        probe = candidate / ".git"
        if probe.exists():
            git_entry = probe
            break

    if git_entry is None:
        return None

    try:
        if git_entry.is_file():
            first = git_entry.read_text(encoding="utf-8").strip()
            if not first.startswith("gitdir:"):
                return None
            raw_dir = first.split(":", 1)[1].strip()
            git_dir = Path(raw_dir)
            if not git_dir.is_absolute():
                git_dir = (git_entry.parent / git_dir).resolve()
        elif git_entry.is_dir():
            git_dir = git_entry
        else:
            return None

        head_text = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{40}", head_text):
            return head_text
        if not head_text.startswith("ref:"):
            return None

        ref_name = head_text.split(":", 1)[1].strip()
        search_dirs = [git_dir]
        commondir_file = git_dir / "commondir"
        if commondir_file.is_file():
            common_raw = commondir_file.read_text(encoding="utf-8").strip()
            common_dir = Path(common_raw)
            if not common_dir.is_absolute():
                common_dir = (git_dir / common_dir).resolve()
            search_dirs.append(common_dir)

        for base in search_dirs:
            resolved = _resolve_git_ref(base, ref_name)
            if resolved is not None:
                return resolved
    except OSError, UnicodeDecodeError:
        return None

    return None


def _verify_canonical_contract(module: Any) -> None:
    ep = getattr(module, "ENTRYPOINT", None)
    if (
        ep is None
        or getattr(ep, "engine_api", None) != EXPECTED_ENGINE_API
        or getattr(ep, "deployment_api", None) != EXPECTED_DEPLOYMENT_API
    ):
        raise ValueError(CONTRACT_ERROR)

    system_cls = getattr(module, "System", None)
    attention_cls = getattr(module, "Attention", None)
    exchange_cls = getattr(module, "Exchange", None)
    if (
        system_cls is None
        or attention_cls is None
        or exchange_cls is None
        or not callable(getattr(system_cls, "assess", None))
        or not callable(getattr(attention_cls, "inspect_receipt", None))
        or not callable(getattr(exchange_cls, "packet_summary", None))
    ):
        raise ValueError(CONTRACT_ERROR)


def _is_colocated_engine(resolved_entry: Path) -> bool:
    if not COLOCATED_ENGINE_ENTRY.is_file():
        return False
    try:
        checked = check_unfollowed_ancestors(
            COLOCATED_ENGINE_ENTRY,
            "Co-located engine",
            include_target=True,
        )
    except ValueError:
        return False
    return resolved_entry == checked.resolve(strict=True)


@contextmanager
def _mount_external_managed_ai_package(
    path: Path, resolved_engine: Path, init_file: Path
) -> Generator[Any]:
    """Temporarily mount an external package copy in an isolated sys.modules snapshot."""
    colocated_src = COLOCATED_ENGINE_ROOT.resolve().parent
    saved_path = list(sys.path)
    saved_modules = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == "managed_ai_information" or name.startswith("managed_ai_information.")
    }

    def _is_colocated_src(entry: str) -> bool:
        try:
            return Path(entry or ".").resolve() == colocated_src
        except OSError:
            return False

    try:
        sys.path[:] = [entry for entry in saved_path if not _is_colocated_src(entry)]
        importlib.invalidate_caches()
        pkg_spec = importlib.util.spec_from_file_location(
            "managed_ai_information",
            init_file,
            submodule_search_locations=[str(resolved_engine)],
        )
        if pkg_spec is None or pkg_spec.loader is None:
            raise ValueError(PROFILE_ERROR)

        pkg_mod = importlib.util.module_from_spec(pkg_spec)
        sys.modules["managed_ai_information"] = pkg_mod
        pkg_spec.loader.exec_module(pkg_mod)
        module = importlib.import_module("managed_ai_information.store.system")
        loaded_file = getattr(module, "__file__", None)
        if not loaded_file or Path(loaded_file).resolve() != path.resolve():
            raise ValueError(PROFILE_ERROR)
        yield module
    finally:
        sys.path[:] = saved_path
        for name in list(sys.modules):
            if name == "managed_ai_information" or name.startswith("managed_ai_information."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        importlib.invalidate_caches()


@contextmanager
def _mount_legacy_engine_layout(path: Path, resolved_engine: Path) -> Generator[Any]:
    """Temporarily mount a non-package legacy engine/ layout with core/store/deployment on
    sys.path.
    """
    saved_path = list(sys.path)
    saved_legacy = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name in ("core", "store", "deployment")
        or name.startswith(("core.", "store.", "deployment."))
    }
    engine_root_str = str(resolved_engine)
    spec = importlib.util.spec_from_file_location("reviewed_capture_engine", path)
    if spec is None or spec.loader is None:
        sys.modules.update(saved_legacy)
        raise ValueError(PROFILE_ERROR)

    previous = sys.modules.get(spec.name)
    try:
        if engine_root_str in sys.path:
            sys.path.remove(engine_root_str)
        sys.path.insert(0, engine_root_str)
        importlib.invalidate_caches()
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path[:] = saved_path
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
        for name in list(sys.modules):
            if name in ("core", "store", "deployment") or name.startswith(
                ("core.", "store.", "deployment."),
            ):
                sys.modules.pop(name, None)
        sys.modules.update(saved_legacy)
        importlib.invalidate_caches()


@contextmanager
def _mount_canonical_module(path: Path, engine_root: Path) -> Generator[Any]:
    """Mount store/system.py from a canonical package or legacy engine/ layout."""
    resolved_engine = engine_root.resolve()
    init_file = resolved_engine / "__init__.py"

    if init_file.is_file():
        colocated_root = COLOCATED_ENGINE_ROOT.resolve()
        if resolved_engine == colocated_root:
            src_root_str = str(resolved_engine.parent)
            if src_root_str not in sys.path:
                sys.path.insert(0, src_root_str)
            yield importlib.import_module("managed_ai_information.store.system")
            return

        with _mount_external_managed_ai_package(path, resolved_engine, init_file) as module:
            yield module
        return

    with _mount_legacy_engine_layout(path, resolved_engine) as module:
        yield module


def _load_canonical_module(path: Path, engine_root: Path) -> Any:
    """Load store/system.py (prefer qualified_engine() context manager for external engines)."""
    with _mount_canonical_module(path, engine_root) as module:
        return module
