#!/usr/bin/env python3
"""Read-only static preflight for the restartable construction workbench.

Usage: python3 -B inspect_config.py --config /absolute/config.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import sys
from typing import Any

if __package__ in (None, ""):
    _src_root = str(Path(__file__).resolve().parents[4])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai.companions.workbench.core.config import (
    CORE_MODULES,
    DEFAULT_CONTROLLER_DIR,
    default_search_paths,
    is_clean_relative_posix,
    paths_overlap,
)
from managed_ai.companions.workbench.core.io_utils import canonical_bytes, loads_strict


class Inspection:
    """Static metadata-only validator for workbench launch configurations."""

    def __init__(self) -> None:
        self.issues: list[dict[str, str]] = []
        self.fixed_files: list[tuple[str, Path]] = []

    def issue(self, field: str, message: str) -> None:
        self.issues.append({"field": field, "message": message})

    def path(self, field: str, value: Any) -> Path | None:
        if not isinstance(value, str) or not value or "\x00" in value:
            self.issue(field, "Supply a nonempty absolute filesystem path without NUL characters.")
            return None

        try:
            value.encode("utf-8")
            candidate = Path(value)
            if not candidate.is_absolute():
                self.issue(field, "Use an absolute filesystem path: " + value)
                return None
            return candidate.resolve(strict=False)
        except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
            self.issue(field, "Cannot resolve the configured path: " + str(exc))
            return None

    def directory(self, field: str, value: Any, future: bool = False) -> Path | None:
        path = self.path(field, value)
        if path is None:
            return None

        try:
            if path.exists():
                if not path.is_dir():
                    self.issue(
                        field,
                        "Expected a directory, but this path is not a directory: " + str(path),
                    )
            elif not future:
                self.issue(field, "Required directory is missing: " + str(path))
            else:
                ancestor = path.parent
                while not ancestor.exists() and ancestor != ancestor.parent:
                    ancestor = ancestor.parent
                if not ancestor.is_dir():
                    self.issue(field, "A parent is not a directory: " + str(ancestor))
        except OSError as exc:
            self.issue(field, "Cannot inspect directory metadata: " + str(exc))

        return path

    def file(
        self,
        field: str,
        value: Any,
        executable: bool = False,
        ordinary_link: bool = False,
    ) -> Path | None:
        path = self.path(field, value)
        if path is None:
            return None

        try:
            if ordinary_link and Path(value).is_symlink():
                self.issue(field, "Use an ordinary trusted code file, not a symlink: " + value)
            if not path.is_file():
                self.issue(field, "Required regular file is missing or is not a file: " + str(path))
            elif not os.access(path, os.R_OK):
                self.issue(field, "Required file is not readable: " + str(path))
            if executable and path.is_file() and not os.access(path, os.X_OK):
                self.issue(field, "Command executable needs execute permission: " + str(path))
        except OSError as exc:
            self.issue(field, "Cannot inspect file metadata: " + str(exc))

        self.fixed_files.append((field, path))
        return path

    def limit(self, config: dict[str, Any], name: str, default: int, minimum: int) -> None:
        value = config.get(name, default)
        if type(value) is not int or value < minimum:
            word = "nonnegative" if minimum == 0 else "positive"
            self.issue(name, "Supply a " + word + " integer; booleans are not limits.")

    def timeout(self, field: str, value: Any) -> None:
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
        except (OverflowError, ValueError):
            valid = False
        if not valid:
            self.issue(field, "Supply a finite positive numeric timeout in seconds.")

    def inputs(self, field: str, value: Any) -> None:
        if not isinstance(value, list):
            self.issue(field, "Use a list of absolute paths to fixed code/input files.")
            return
        for index, item in enumerate(value):
            self.file(f"{field}[{index}]", item)

    def command(self, field: str, value: Any, receiver: bool = False) -> None:
        if not isinstance(value, list) or not value or any(
            not isinstance(x, str) or "\x00" in x for x in value
        ):
            self.issue(
                field,
                "Supply a nonempty argv list of strings without NUL characters; no shell is added.",
            )
            return

        first = value[0]
        if not first:
            self.issue(field + "[0]", "Name an executable; the executable cannot be empty.")
            return
        if "{workspace}" in first or "{receiving_dir}" in first:
            self.issue(
                field + "[0]",
                "Name a fixed executable; executable binding happens before receiver path substitution.",
            )
            return

        if "/" in first or "\\" in first:
            executable = self.file(field + "[0]", first, executable=True)
            executable_name = Path(first).name
        else:
            located = shutil.which(first)
            if located is None:
                self.issue(field + "[0]", "Executable is unavailable on PATH: " + first)
                return
            executable = self.file(field + "[0]", str(Path(located).absolute()), executable=True)
            executable_name = Path(located).name

        if executable is None or not executable_name.lower().startswith("python"):
            return

        for index, argument in enumerate(value[1:], 1):
            if argument in ("-c", "-m"):
                if index + 1 >= len(value) or not value[index + 1]:
                    self.issue(f"{field}[{index}]", argument + " requires its code/module argument.")
                break
            if argument.startswith("-"):
                continue
            if receiver and ("{workspace}" in argument or "{receiving_dir}" in argument):
                break
            self.file(f"{field}[{index}]", argument)
            break

    def artifacts(self, field: str, value: Any, workspace: Path | None) -> None:
        if not isinstance(value, list) or not value:
            self.issue(
                field,
                "Declare a nonempty list of relative artifact paths for receiving acceptance.",
            )
            return

        seen: set[str] = set()
        for index, item in enumerate(value):
            label = f"{field}[{index}]"
            if not is_clean_relative_posix(item):
                self.issue(
                    label,
                    "Use a clean relative POSIX file path without empty, dot or parent components.",
                )
                continue
            if item in seen:
                self.issue(label, "Remove duplicate artifact path: " + item)
            seen.add(item)
            if workspace is None:
                continue

            current = workspace
            try:
                parts = item.split("/")
                for position, part in enumerate(parts):
                    current = current / part
                    if current.is_symlink():
                        self.issue(
                            label,
                            "Workspace artifact paths must not pass through symlinks: " + str(current),
                        )
                        break
                    if current.exists() and position < len(parts) - 1 and not current.is_dir():
                        self.issue(label, "Artifact parent is not a directory: " + str(current))
                        break
                if current.exists() and not current.is_file():
                    self.issue(label, "Existing artifact must be a regular file: " + str(current))
            except OSError as exc:
                self.issue(label, "Cannot inspect artifact path metadata: " + str(exc))

    @staticmethod
    def overlaps(a: Path, b: Path) -> bool:
        return paths_overlap(a, b)

    def _inspect_scalars(self, config: dict[str, Any]) -> None:
        if not isinstance(config.get("task"), str):
            self.issue("task", "Supply the task as a string.")

        sources = config.get("sources", {})
        if not isinstance(sources, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in sources.items()
        ):
            self.issue(
                "sources",
                "Use an object mapping string IDs to complete string bodies, not filenames or arrays.",
            )
        elif "workbench-tool-contract" in sources:
            self.issue(
                "sources.workbench-tool-contract",
                "This source ID is reserved for the workbench's generated tool contract; rename the entry.",
            )

        if "run_id" in config and not isinstance(config["run_id"], str):
            self.issue("run_id", "Supply a string run identifier.")
        if not isinstance(config.get("obligations", []), list):
            self.issue("obligations", "Use a list; keep independent outstanding duties in this retained list.")

        self.limit(config, "max_steps", 12, 0)
        self.limit(config, "max_cache_bytes", 65536, 0)
        self.limit(config, "max_file_bytes", 2097152, 1)

    def _inspect_model(
        self,
        config: dict[str, Any],
        *,
        workspace: Path | None,
        state: Path | None,
        source_state: Path | None,
    ) -> Path | None:
        model = config.get("model")
        transport = None
        if not isinstance(model, dict):
            self.issue("model", "Supply a model object with kind mailbox or command and an absolute directory.")
            return None

        kind = model.get("kind")
        if kind not in ("mailbox", "command"):
            self.issue(
                "model.kind",
                "Use mailbox or command. An injected callable needs a Python host and is outside this standalone CLI preflight.",
            )
        if kind in ("mailbox", "command"):
            transport = self.directory("model.directory", model.get("directory"), future=True)

        self.timeout("model.timeout_seconds", model.get("timeout_seconds", 900))
        self.inputs("model.inputs", model.get("inputs", []))
        if kind == "command":
            self.command("model.argv", model.get("argv"))
        if model.get("cwd") is not None:
            cwd = self.path("model.cwd", model["cwd"])
            if cwd is not None:
                made_at_launch = cwd in (workspace, state, source_state, transport)
                self.directory("model.cwd", str(cwd), future=made_at_launch)

        return transport

    def _inspect_receivers(self, config: dict[str, Any], workspace: Path | None) -> None:
        receivers = config.get("receivers", {})
        if not isinstance(receivers, dict):
            self.issue(
                "receivers",
                "Use an object mapping receiver names to command specifications, or an empty object.",
            )
            return

        for name, spec in receivers.items():
            field = "receivers." + name
            if not name:
                self.issue("receivers", "Give each receiver a nonempty name.")
            if not isinstance(spec, dict):
                self.issue(field, "Supply an object with argv, fixed inputs and relative artifact paths.")
                continue
            self.command(field + ".argv", spec.get("argv"), receiver=True)
            self.inputs(field + ".inputs", spec.get("inputs", []))
            self.artifacts(field + ".artifacts", spec.get("artifacts", []), workspace)
            self.timeout(field + ".timeout_seconds", spec.get("timeout_seconds", 60))

    def _inspect_isolation(
        self,
        *,
        workspace: Path | None,
        corpus: Path | None,
        controller: Path | None,
        retained: dict[str, Path | None],
    ) -> None:
        if workspace is not None:
            for field, path in retained.items():
                if path is not None and self.overlaps(workspace, path):
                    self.issue(
                        field,
                        "Keep retained storage and workspace disjoint (neither inside the other): "
                        + str(path)
                        + " versus "
                        + str(workspace),
                    )
            for field, path in self.fixed_files:
                if path == workspace or path.is_relative_to(workspace):
                    self.issue(
                        field,
                        "Move fixed code/input files outside the model-editable workspace: " + str(path),
                    )

        protected: dict[str, Path | None] = {"corpus_root": corpus, "controller_dir": controller}
        if controller is not None:
            product = controller.parent
            core_dir = product / "core"
            if (core_dir / "resumable_workbench.py").is_file():
                protected["operating package beside controller_dir"] = product
                for name in CORE_MODULES:
                    self.file("operating package/core/" + name, str(core_dir / name))

        for field, target in {"workspace": workspace, **retained}.items():
            if target is None:
                continue
            for protected_field, root in protected.items():
                if root is not None and self.overlaps(target, root):
                    self.issue(
                        field,
                        "Keep mutable storage disjoint from read-only "
                        + protected_field
                        + ": "
                        + str(target)
                        + " versus "
                        + str(root),
                    )

    def inspect(self, config: Any) -> None:
        if not isinstance(config, dict):
            self.issue("config", "The JSON top level must be an object.")
            return

        self._inspect_scalars(config)
        workspace = self.directory("workspace", config.get("workspace"), future=True)
        state = self.directory("state_dir", config.get("state_dir"), future=True)
        corpus = self.directory("corpus_root", config.get("corpus_root"))
        controller = self.directory("controller_dir", config.get("controller_dir", str(DEFAULT_CONTROLLER_DIR)))

        if controller is not None:
            for name in ("agent_runtime.py", "evidence_support.py"):
                self.file("controller_dir/" + name, str(controller / name))

        if corpus is not None:
            entry, deps = default_search_paths()
            self.file("bundled_search_implementation", str(entry), ordinary_link=True)
            for dependency in deps:
                if dependency.exists() or dependency.is_symlink():
                    self.file(
                        "bundled_search_implementation/" + dependency.name,
                        str(dependency),
                        ordinary_link=True,
                    )

        source_state = None
        if "source_state_dir" in config or state is not None:
            default = str(state / "source-service") if state is not None else None
            source_state = self.directory(
                "source_state_dir",
                config.get("source_state_dir", default),
                future=True,
            )

        transport = self._inspect_model(
            config,
            workspace=workspace,
            state=state,
            source_state=source_state,
        )
        self._inspect_receivers(config, workspace)
        self._inspect_isolation(
            workspace=workspace,
            corpus=corpus,
            controller=controller,
            retained={
                "state_dir": state,
                "source_state_dir": source_state,
                "model.directory": transport,
            },
        )


class JsonParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def main(argv: list[str] | None = None) -> int:
    check = Inspection()
    parser = JsonParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", required=True, help="Absolute path to launch configuration JSON")

    try:
        args = parser.parse_args(argv)
    except (ValueError, TypeError) as exc:
        check.issue("arguments", str(exc))
    else:
        try:
            config_path = check.path("config", args.config)
            if config_path is not None:
                if not config_path.is_file():
                    check.issue("config", "Supply an existing regular JSON file: " + str(config_path))
                else:
                    config = loads_strict(config_path.read_bytes())
                    canonical_bytes(config)
                    check.inspect(config)
        except Exception as exc:
            check.issue("config", f"Cannot inspect configuration: {type(exc).__name__}: {exc}")

    result = {"ok": not check.issues, "issues": check.issues}
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
