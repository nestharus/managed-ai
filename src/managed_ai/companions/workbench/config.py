"""Shared workbench configuration normalization, command pinning, and controller loading."""
from __future__ import annotations

import importlib.util
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sys
from types import ModuleType
from typing import Any

from .io_utils import canonical_bytes, loads_strict, pin_file, sha256_bytes

WORKBENCH_ROOT = Path(__file__).resolve().parent
CORE_DIR = WORKBENCH_ROOT
DEFAULT_CONTROLLER_DIR = WORKBENCH_ROOT

CORE_MODULES = (
    "io_utils.py",
    "config.py",
    "model_bridges.py",
    "source_tools.py",
    "durable_agent.py",
    "workbench_agent.py",
    "resumable_workbench.py",
)

BUNDLED_SEARCH_ENTRY = Path("library/lexical.py")
BUNDLED_SEARCH_DEPS = (
    Path("library/config.py"),
)

RESOLVE_FIELDS = frozenset(
    {
        "operation_id",
        "value",
        "checkpoint",
        "evidence",
        "expected_contract_sha256",
    },
)


def default_search_paths(repo_root: Path | str | None = None) -> tuple[Path, list[Path]]:
    """Return (entry_path, dependency_paths) for the bundled lexical search library."""
    if repo_root is None:
        engine = WORKBENCH_ROOT.parents[1]
        candidates = (
            (engine, BUNDLED_SEARCH_ENTRY, BUNDLED_SEARCH_DEPS),
            (engine / "engine", BUNDLED_SEARCH_ENTRY, BUNDLED_SEARCH_DEPS),
        )
    else:
        base = Path(repo_root)
        candidates = (
            (base / "src/managed_ai", BUNDLED_SEARCH_ENTRY, BUNDLED_SEARCH_DEPS),
            (base, BUNDLED_SEARCH_ENTRY, BUNDLED_SEARCH_DEPS),
            (base / "engine", BUNDLED_SEARCH_ENTRY, BUNDLED_SEARCH_DEPS),
        )

    for root, entry_rel, dep_rels in candidates:
        candidate = root / entry_rel
        if candidate.exists() or candidate.is_symlink():
            return candidate, [root / rel for rel in dep_rels]

    default_root, default_entry, default_deps = candidates[0]
    return default_root / default_entry, [default_root / rel for rel in default_deps]


def is_clean_relative_posix(value: Any) -> bool:
    """Return True when value is a clean relative POSIX path without drives or traversal."""
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
        or PureWindowsPath(value).drive
        or (len(value) >= 2 and value[1] == ":")
        or PurePosixPath(value).is_absolute()
    ):
        return False

    parts = value.split("/")
    return all(
        part not in ("", ".", "..")
        and not PureWindowsPath(part).drive
        and not (len(part) >= 2 and part[1] == ":")
        for part in parts
    )


def paths_overlap(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def pin_files(paths: list[Path | str]) -> dict[str, dict[str, Any]]:
    return {str(Path(path).resolve()): pin_file(Path(path).resolve()) for path in paths}


def parse_command_targets(
    command: list[str],
    *,
    cwd: Path | str | None = None,
    allow_placeholders: bool = False,
) -> tuple[str, str | None]:
    """Identify the executable and directly named Python script target for a command argv."""
    if not isinstance(command, list) or not command or any(
        not isinstance(x, str) or "\x00" in x for x in command
    ):
        raise ValueError("argv must be a nonempty list of strings without NUL characters")

    first = command[0]
    if not first:
        raise ValueError("command executable cannot be empty")
    if "{workspace}" in first or "{receiving_dir}" in first:
        raise ValueError("command executable cannot contain runtime placeholders")

    base = Path(cwd or Path.cwd())
    if "/" in first or "\\" in first:
        executable = str((base / first).resolve())
        executable_name = Path(first).name
    else:
        located = shutil.which(first)
        if located is None:
            raise ValueError("Configured command executable is unavailable: " + first)
        executable = str(Path(located).resolve())
        executable_name = Path(located).name

    script = None
    if executable_name.lower().startswith("python"):
        for index, argument in enumerate(command[1:], 1):
            if argument in ("-c", "-m"):
                if index + 1 >= len(command) or not command[index + 1]:
                    raise ValueError(argument + " requires its code/module argument")
                break
            if argument.startswith("-"):
                continue
            if "{workspace}" in argument or "{receiving_dir}" in argument:
                break
            script = str((base / argument).resolve())
            break

    return executable, script


def command_code(command: list[str], cwd: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Bind the executable and a directly named Python script, not output paths."""
    if not command:
        return {}
    executable, script = parse_command_targets(command, cwd=cwd, allow_placeholders=True)
    paths: list[Path | str] = [executable]
    if script is not None:
        paths.append(script)
    return pin_files(paths)


def normalize_config(config: Any) -> dict[str, Any]:
    """Return a detached, path-normalized copy of a resumable workbench configuration."""
    result = loads_strict(canonical_bytes(config))
    if not isinstance(result, dict):
        raise ValueError("Workbench configuration must be a JSON object")

    for name in ("state_dir", "workspace", "corpus_root"):
        result[name] = str(Path(result[name]).resolve())

    result["controller_dir"] = str(
        Path(result.get("controller_dir", DEFAULT_CONTROLLER_DIR)).resolve(),
    )
    result["source_state_dir"] = str(
        Path(result.get("source_state_dir", Path(result["state_dir"]) / "source-service")).resolve(),
    )

    model = result.get("model")
    if not isinstance(model, dict):
        raise ValueError("A model transport configuration is required")
    if model.get("kind") in ("mailbox", "command"):
        model["directory"] = str(Path(model["directory"]).resolve())
    if model.get("cwd") is not None:
        model["cwd"] = str(Path(model["cwd"]).resolve())

    return result


def build_model_transport(model_config: Any) -> Any:
    """Construct the configured caller-mediated transport (mailbox or command)."""
    from .model_bridges import CommandModel, MailboxModel

    if not isinstance(model_config, dict):
        raise ValueError("model configuration must be an object")

    kind = model_config.get("kind")
    timeout = model_config.get("timeout_seconds", 900)
    if kind == "mailbox":
        return MailboxModel(model_config["directory"], timeout)
    if kind == "command":
        return CommandModel(
            model_config["argv"],
            model_config["directory"],
            cwd=model_config.get("cwd"),
            timeout_seconds=timeout,
        )
    raise ValueError("model.kind must be mailbox or command")


def validate_resolution_payload(payload: Any) -> dict[str, Any]:
    """Validate an explicit resolve-command JSON object without blind kwarg splatting."""
    if not isinstance(payload, dict):
        raise ValueError("Resolution payload must be a JSON object")

    keys = set(payload.keys())
    missing = sorted(RESOLVE_FIELDS - keys)
    extra = sorted(keys - RESOLVE_FIELDS)
    if missing or extra:
        raise ValueError(
            f"Resolution payload has invalid fields; missing={missing}, extra={extra}",
        )
    if not isinstance(payload["operation_id"], str) or not payload["operation_id"]:
        raise ValueError("Resolution operation_id must be a nonempty string")
    if not isinstance(payload["evidence"], str) or not payload["evidence"].strip():
        raise ValueError("Resolution evidence must be a nonempty string")
    if (
        not isinstance(payload["expected_contract_sha256"], str)
        or not payload["expected_contract_sha256"]
    ):
        raise ValueError("Resolution expected_contract_sha256 must be a nonempty string")

    return {
        "operation_id": payload["operation_id"],
        "value": payload["value"],
        "checkpoint": payload["checkpoint"],
        "evidence": payload["evidence"],
        "expected_contract_sha256": payload["expected_contract_sha256"],
    }


def load_controller(controller_dir: Path | str | None = None) -> tuple[Any, dict[str, str]]:
    """Load agent_runtime.run_agent cleanly in an isolated package namespace."""
    root = Path(controller_dir or DEFAULT_CONTROLLER_DIR).resolve()
    support_path = root / "evidence_support.py"
    controller_path = root / "agent_runtime.py"
    identities = {
        "agent_runtime.py": sha256_bytes(controller_path.read_bytes()),
        "evidence_support.py": sha256_bytes(support_path.read_bytes()),
    }

    pkg_name = "_g60_controller_" + sha256_bytes(str(root).encode("utf-8"))[:16]
    support_name = pkg_name + ".evidence_support"
    controller_name = pkg_name + ".agent_runtime"

    pkg = ModuleType(pkg_name)
    pkg.__path__ = [str(root)]
    pkg.__package__ = pkg_name

    support_spec = importlib.util.spec_from_file_location(support_name, support_path)
    support = importlib.util.module_from_spec(support_spec)
    controller_spec = importlib.util.spec_from_file_location(controller_name, controller_path)
    controller = importlib.util.module_from_spec(controller_spec)

    created = (pkg_name, support_name, controller_name)
    previous = {name: sys.modules.get(name) for name in created}
    sys.modules[pkg_name] = pkg
    sys.modules[support_name] = support
    sys.modules[controller_name] = controller
    try:
        support_spec.loader.exec_module(support)
        controller_spec.loader.exec_module(controller)
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old

    return controller.run_agent, identities
