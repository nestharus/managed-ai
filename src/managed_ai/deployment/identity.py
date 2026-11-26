"""Engine code identity, descriptor reading, Git provenance, and store binding verification."""
from __future__ import annotations

import ast
from importlib.machinery import EXTENSION_SUFFIXES
import os
from pathlib import Path
import stat
import subprocess
from typing import Any

from managed_ai.core.formats import (
    DEPLOYMENT_FORMAT as FORMAT,
    ENGINE_API,
    STORE_FORMAT,
    STORE_SCHEMA,
)
from managed_ai.core.ownership import packet_id
from managed_ai.core.storage import (
    check_ordinary_ancestors,
    encode,
    is_reparse_or_symlink,
    safe_relpath,
    sha,
    strict_json_loads,
)

HERE = Path(__file__).resolve().parent
ENGINE_ROOT = HERE.parent
SRC_ROOT = ENGINE_ROOT.parent

VERSION = "0.3.0"
NATIVE_ENTRY = Path("store/system.py")
NATIVE_CONTRACT = Path("store/selection/domain_selection.py")
NON_SOURCE_SUFFIXES = (".pyc", ".pyo", ".pyd", ".so", *EXTENSION_SUFFIXES)
CODE_IDENTITY_EXCLUDED_DIRS = frozenset({"tests", "examples", "patterns", "companions", "optional"})


def read_descriptor_bytes(path: Path | str) -> bytes:
    ordinary = check_ordinary_ancestors(Path(path), expect_dir=False, error_prefix="ordinary descriptor required")
    return ordinary.read_bytes()


def read_descriptor(path: Path | str, raw: bytes | None = None) -> dict[str, Any]:
    data = read_descriptor_bytes(path) if raw is None else raw
    return strict_json_loads(data, path, require_object=True)


_descriptor_bytes = read_descriptor_bytes


def _descriptor_object(path: Path | str, raw: bytes) -> dict[str, Any]:
    return read_descriptor(path, raw)


def read(path: Path | str) -> dict[str, Any]:
    return read_descriptor(path)


def _ordinary_mode(path: Path, info: os.stat_result) -> int:
    if is_reparse_or_symlink(info):
        raise ValueError(f"shared engine may not use symbolic links or reparse points: {path}")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError(f"ordinary shared engine file or directory required: {path}")
    return info.st_mode


def _engine_directory(path: Path | str) -> Path:
    target = Path(path)
    if not target.is_absolute():
        if target.anchor:
            raise ValueError(f"fully qualified or relative shared engine path required: {target}")
        target = Path.cwd() / target

    current = Path(target.anchor)
    _ordinary_mode(current, current.lstat())
    for part in target.parts[1:]:
        if part == "..":
            current = current.parent
            continue
        current /= part
        if not stat.S_ISDIR(_ordinary_mode(current, current.lstat())):
            raise ValueError(f"ordinary shared engine directory required: {current}")

    return current.resolve(strict=True)


def _ordinary_dir(
    path: Path | str,
    error_prefix: str = "ordinary deployment, engine and store directories required",
) -> Path:
    return check_ordinary_ancestors(Path(path), expect_dir=True, error_prefix=error_prefix).resolve(strict=True)


def _is_test_module_name(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(".py") and (lower.startswith("test_") or lower.endswith("_test.py"))


def _git_provenance(engine: Path) -> dict[str, object] | None:
    """Return optional Git commit/clean provenance for a checkout, or None outside Git."""
    has_git = any((candidate / ".git").exists() for candidate in (engine, *engine.parents))
    if not has_git:
        return None

    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        head = subprocess.run(
            ["git", "-C", str(engine), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=3.0,
        )
        if head.returncode != 0:
            return None

        commit = head.stdout.strip()
        if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit.lower()):
            return None

        status = subprocess.run(
            ["git", "-C", str(engine), "status", "--porcelain", "--", "."],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=3.0,
        )
        if status.returncode != 0:
            return None

        return {"git_commit": commit.lower(), "git_clean": not bool(status.stdout.strip())}
    except (OSError, subprocess.SubprocessError):
        return None


def _engine_record(engine: Path, root: Path, code_sha: str) -> dict[str, object]:
    record: dict[str, object] = {
        "path": safe_relpath(engine, root),
        "code_sha256": code_sha,
        "engine_api": ENGINE_API,
    }
    prov = _git_provenance(engine)
    if prov is not None:
        record.update(prov)
    return record


def _status_payload(
    root: Path,
    engine: Path,
    runtime: Path,
    config: dict[str, Any],
    descriptor_sha256: str,
) -> dict[str, Any]:
    store = config["store"]
    eng = config["engine"]
    payload: dict[str, Any] = {
        "format": FORMAT,
        "deployment": str(root),
        "engine": str(engine),
        "engine_code_sha256": eng["code_sha256"],
        "store": str(runtime),
        "instance_id": store["instance_id"],
        "store_schema": store["store_schema"],
        "engine_api": store["engine_api"],
        "descriptor_sha256": descriptor_sha256,
        "scope": "One shared code tree; explicit store and data-domain selection. No copied runtime.",
    }
    if "git_commit" in eng:
        payload["engine_git_commit"] = eng["git_commit"]
    if "git_clean" in eng:
        payload["engine_git_clean"] = eng["git_clean"]
    return payload


def _collect_engine_modules(engine: Path) -> list[Path]:
    paths: list[Path] = []
    pending: list[tuple[Path, bool]] = [(engine, False)]

    while pending:
        directory, cache = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                path = directory / name
                mode = _ordinary_mode(path, entry.stat(follow_symlinks=False))
                if stat.S_ISDIR(mode):
                    lower = name.casefold()
                    if lower in CODE_IDENTITY_EXCLUDED_DIRS:
                        continue
                    pending.append((path, cache or lower == "__pycache__"))
                elif cache:
                    if not name.lower().endswith(".pyc"):
                        raise ValueError(f"ordinary bytecode cache files required below __pycache__: {path}")
                else:
                    lower = name.lower()
                    if lower.endswith(NON_SOURCE_SUFFIXES):
                        raise ValueError(f"shared engine requires Python source for executable modules: {path}")
                    if lower.endswith(".py") and not _is_test_module_name(name):
                        paths.append(path)

    return sorted(paths)


def _hash_engine_modules(
    engine: Path,
    sorted_paths: list[Path],
    declaration_paths: set[Path],
) -> tuple[list[dict[str, str]], dict[Path, bytes]]:
    def hash_chunk(chunk: list[Path]) -> list[tuple[Path, dict[str, str], bytes | None]]:
        out = []
        for path in chunk:
            relative = path.relative_to(engine)
            raw = path.read_bytes()
            out.append((
                relative,
                {"path": relative.as_posix(), "sha256": sha(raw)},
                raw if relative in declaration_paths else None,
            ))
        return out

    if len(sorted_paths) >= 32:
        from concurrent.futures import ThreadPoolExecutor

        workers = min(8, (len(sorted_paths) + 15) // 16)
        step = (len(sorted_paths) + workers - 1) // workers
        chunks = [sorted_paths[i : i + step] for i in range(0, len(sorted_paths), step)]
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            hashed = [item for sub in pool.map(hash_chunk, chunks) for item in sub]
    else:
        hashed = hash_chunk(sorted_paths)

    rows: list[dict[str, str]] = []
    declarations: dict[Path, bytes] = {}
    for relative, row, raw in hashed:
        rows.append(row)
        if raw is not None:
            declarations[relative] = raw

    return rows, declarations


def _verify_api_declarations(engine: Path, declarations: dict[Path, bytes]) -> None:
    interfaces = ((NATIVE_ENTRY, NATIVE_CONTRACT),)
    declaration_paths = {path for pair in interfaces for path in pair}
    if declaration_paths != declarations.keys():
        raise ValueError("select the current engine root containing its native entry and API declarations")

    def declared(path: Path) -> dict[str, Any]:
        result: dict[str, Any] = {}
        try:
            declaration = ast.parse(declarations[path], filename=str(engine / path))
        except SyntaxError as exc:
            raise ValueError(f"invalid engine API declaration in {engine / path}: {exc.msg}") from exc

        for node in declaration.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                try:
                    result[node.targets[0].id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass
        return result

    for entry, schema in interfaces:
        contract = declared(schema)
        if (
            contract.get("ENGINE_API") != ENGINE_API
            or contract.get("STORE_SCHEMA") != STORE_SCHEMA
            or declared(entry).get("DEPLOYMENT_API") != FORMAT
        ):
            raise ValueError("selected engine declares incompatible storage/deployment APIs")


def code_identity(engine: Path | str) -> str:
    """Hash executable Python source modules under the engine root."""
    engine_dir = _engine_directory(engine)
    sorted_paths = _collect_engine_modules(engine_dir)
    declaration_paths = {NATIVE_ENTRY, NATIVE_CONTRACT}
    rows, declarations = _hash_engine_modules(engine_dir, sorted_paths, declaration_paths)
    _verify_api_declarations(engine_dir, declarations)
    return sha(encode(rows))


def runtime_binding(root: Path) -> dict[str, str]:
    config = read(root / "system.json")
    binding = read(root / "exchange/workspace.json")

    if (
        config.get("format") != STORE_FORMAT
        or config.get("store_schema") != STORE_SCHEMA
        or config.get("engine_api") != ENGINE_API
    ):
        raise ValueError("store/engine schema incompatibility; explicit import or migration required")

    packet_id(config["instance_id"])
    if (
        type(binding.get("version")) is not int
        or binding["version"] != 2
        or binding.get("workspace_relative") != "../work"
        or binding.get("instance_id") != config["instance_id"]
    ):
        raise ValueError("portable store binding differs")

    return {
        "instance_id": config["instance_id"],
        "store_schema": STORE_SCHEMA,
        "engine_api": ENGINE_API,
    }
