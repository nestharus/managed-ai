#!/usr/bin/env python3
"""Read-only static preflight for the restartable construction workbench.

Usage: python3 -B inspect_config.py --config /absolute/config.json

This standalone CLI requires explicit controller_dir and absolute configured
filesystem paths (bare executable names may be resolved on PATH). It accepts
mailbox and command transports. source_state_dir defaults to state_dir/source-service.
No workbench code is imported, no command runs, no corpus bodies are acquired,
and no configured directory or file is created. Named files are checked by
metadata only. Future workspace, state and transport directories are allowed.

The conservative placement policy keeps editable work disjoint from retained
state and both outside the corpus, controller, and an adjacent operating package
when its resumable_workbench.py marker exists. Declared receivers need nonempty
artifact lists; artifacts themselves may be future outputs. This is not proof of
provider access, source sufficiency, dependency identity, storage writability,
receiver truth or successful execution. It does not judge start/resume history.
"""

import argparse
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import sys


class Inspection:
    def __init__(self):
        self.issues = []
        self.fixed_files = []

    def issue(self, field, message):
        self.issues.append({"field": field, "message": message})

    def path(self, field, value):
        if not isinstance(value, str) or not value or "\x00" in value:
            self.issue(field, "Supply a nonempty absolute filesystem path without NUL characters.")
            return None
        try:
            value.encode("utf-8")
            path = Path(value)
            if not path.is_absolute():
                self.issue(field, "Use an absolute filesystem path: " + value)
                return None
            return path.resolve(strict=False)
        except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
            self.issue(field, "Cannot resolve the configured path: " + str(exc))
            return None

    def directory(self, field, value, future=False):
        path = self.path(field, value)
        if path is None:
            return None
        try:
            if path.exists():
                if not path.is_dir():
                    self.issue(field, "Expected a directory, but this path is not a directory: " + str(path))
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

    def file(self, field, value, executable=False, ordinary_link=False):
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

    def limit(self, config, name, default, minimum):
        value = config.get(name, default)
        if type(value) is not int or value < minimum:
            word = "nonnegative" if minimum == 0 else "positive"
            self.issue(name, "Supply a " + word + " integer; booleans are not limits.")

    def timeout(self, field, value):
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
        except (OverflowError, ValueError):
            valid = False
        if not valid:
            self.issue(field, "Supply a finite positive numeric timeout in seconds.")

    def inputs(self, field, value):
        if not isinstance(value, list):
            self.issue(field, "Use a list of absolute paths to fixed code/input files.")
            return
        for index, item in enumerate(value):
            self.file(field + "[" + str(index) + "]", item)

    def command(self, field, value, receiver=False):
        if not isinstance(value, list) or not value or any(not isinstance(x, str) or "\x00" in x for x in value):
            self.issue(field, "Supply a nonempty argv list of strings without NUL characters; no shell is added.")
            return
        first = value[0]
        if not first:
            self.issue(field + "[0]", "Name an executable; the executable cannot be empty.")
            return
        if "{workspace}" in first or "{receiving_dir}" in first:
            self.issue(field + "[0]", "Name a fixed executable; executable binding happens before receiver path substitution.")
            return
        if "/" in first:
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
        # Mirror the adapter's directly named Python-script binding. Dynamic or
        # module dependencies must be supplied explicitly in the inputs list.
        for index, argument in enumerate(value[1:], 1):
            if argument in ("-c", "-m"):
                if index + 1 >= len(value) or not value[index + 1]:
                    self.issue(field + "[" + str(index) + "]", argument + " requires its code/module argument.")
                break
            if argument.startswith("-"):
                continue
            if receiver and ("{workspace}" in argument or "{receiving_dir}" in argument):
                break  # Runtime substitution; the adapter does not pin this path.
            self.file(field + "[" + str(index) + "]", argument)
            break

    def artifacts(self, field, value, workspace):
        if not isinstance(value, list) or not value:
            self.issue(field, "Declare a nonempty list of relative artifact paths for receiving acceptance.")
            return
        seen = set()
        for index, item in enumerate(value):
            label = field + "[" + str(index) + "]"
            if (not isinstance(item, str) or not item or "\x00" in item or "\\" in item
                    or PurePosixPath(item).is_absolute()
                    or any(part in ("", ".", "..") for part in item.split("/"))):
                self.issue(label, "Use a clean relative POSIX file path without empty, dot or parent components.")
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
                        self.issue(label, "Workspace artifact paths must not pass through symlinks: " + str(current))
                        break
                    if current.exists() and position < len(parts) - 1 and not current.is_dir():
                        self.issue(label, "Artifact parent is not a directory: " + str(current))
                        break
                if current.exists() and not current.is_file():
                    self.issue(label, "Existing artifact must be a regular file: " + str(current))
            except OSError as exc:
                self.issue(label, "Cannot inspect artifact path metadata: " + str(exc))

    @staticmethod
    def overlaps(a, b):
        return a == b or a.is_relative_to(b) or b.is_relative_to(a)

    def inspect(self, config):
        if not isinstance(config, dict):
            self.issue("config", "The JSON top level must be an object.")
            return
        if not isinstance(config.get("task"), str):
            self.issue("task", "Supply the task as a string.")
        sources = config.get("sources", {})
        if not isinstance(sources, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in sources.items()):
            self.issue("sources", "Use an object mapping string IDs to complete string bodies, not filenames or arrays.")
        elif "workbench-tool-contract" in sources:
            self.issue("sources.workbench-tool-contract", "This source ID is reserved for the workbench's generated tool contract; rename the entry.")
        if "run_id" in config and not isinstance(config["run_id"], str):
            self.issue("run_id", "Supply a string run identifier.")
        if not isinstance(config.get("obligations", []), list):
            self.issue("obligations", "Use a list; keep independent outstanding duties in this retained list.")
        # requirements may be any finite JSON value, as in the implementation.
        self.limit(config, "max_steps", 12, 0)
        self.limit(config, "max_cache_bytes", 65536, 0)
        self.limit(config, "max_file_bytes", 2097152, 1)

        workspace = self.directory("workspace", config.get("workspace"), future=True)
        state = self.directory("state_dir", config.get("state_dir"), future=True)
        corpus = self.directory("corpus_root", config.get("corpus_root"))
        if "controller_dir" not in config:
            self.issue("controller_dir", "This standalone inspector requires an explicit absolute controller_dir; select the product's controller directory.")
            controller = None
        else:
            controller = self.directory("controller_dir", config["controller_dir"])
        if controller is not None:
            for name in ("agent_runtime.py", "evidence_support.py"):
                self.file("controller_dir/" + name, str(controller / name))
        if corpus is not None:
            engine = Path(__file__).resolve().parents[2] / "engine"
            runtime = engine / "runtime"
            if not (runtime.exists() or runtime.is_symlink()):
                runtime = engine / "prototype"
            search = runtime / "knowledge-system/knowledge.py"
            self.file("bundled_search_implementation", str(search), ordinary_link=True)

        source_state = None
        if "source_state_dir" in config or state is not None:
            default = str(state / "source-service") if state is not None else None
            source_state = self.directory("source_state_dir", config.get("source_state_dir", default), future=True)
        model = config.get("model")
        transport = None
        if not isinstance(model, dict):
            self.issue("model", "Supply a model object with kind mailbox or command and an absolute directory.")
            model = {}
        else:
            kind = model.get("kind")
            if kind not in ("mailbox", "command"):
                self.issue("model.kind", "Use mailbox or command. An injected callable needs a Python host and is outside this standalone CLI preflight.")
            if kind in ("mailbox", "command"):
                transport = self.directory("model.directory", model.get("directory"), future=True)
            self.timeout("model.timeout_seconds", model.get("timeout_seconds", 900))
            self.inputs("model.inputs", model.get("inputs", []))
            if kind == "command":
                self.command("model.argv", model.get("argv"))
            if model.get("cwd") is not None:
                cwd = self.path("model.cwd", model["cwd"])
                if cwd is not None:
                    # These exact directories are created by launch before a
                    # model command runs. Other named working directories exist now.
                    made_at_launch = cwd in (workspace, state, source_state, transport)
                    self.directory("model.cwd", str(cwd), future=made_at_launch)

        receivers = config.get("receivers", {})
        if not isinstance(receivers, dict):
            self.issue("receivers", "Use an object mapping receiver names to command specifications, or an empty object.")
        else:
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

        retained = {"state_dir": state, "source_state_dir": source_state, "model.directory": transport}
        if workspace is not None:
            for field, path in retained.items():
                if path is not None and self.overlaps(workspace, path):
                    self.issue(field, "Keep retained storage and workspace disjoint (neither inside the other): " + str(path) + " versus " + str(workspace))
            for field, path in self.fixed_files:
                if path == workspace or path.is_relative_to(workspace):
                    self.issue(field, "Move fixed code/input files outside the model-editable workspace: " + str(path))

        protected = {"corpus_root": corpus, "controller_dir": controller}
        if controller is not None:
            product = controller.parent
            if (product / "resumable_workbench.py").is_file():
                protected["operating package beside controller_dir"] = product
                for name in ("resumable_workbench.py", "durable_agent.py", "workbench_agent.py", "source_tools.py", "model_bridges.py"):
                    self.file("operating package/" + name, str(product / name))
        for field, target in {"workspace": workspace, **retained}.items():
            if target is None:
                continue
            for protected_field, root in protected.items():
                if root is not None and self.overlaps(target, root):
                    self.issue(field, "Keep mutable storage disjoint from read-only " + protected_field + ": " + str(target) + " versus " + str(root))


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field: " + key)
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError("non-finite JSON number is not allowed: " + value)


def main(argv=None):
    check = Inspection()
    parser = JsonParser(description=__doc__)
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
                    config = json.loads(config_path.read_text(encoding="utf-8"),
                                        object_pairs_hook=unique_object, parse_constant=invalid_constant)
                    # The workbench's binding uses strict UTF-8 canonical JSON.
                    json.dumps(config, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    check.inspect(config)
        except Exception as exc:
            check.issue("config", "Cannot inspect configuration: " + type(exc).__name__ + ": " + str(exc))
    result = {"ok": not check.issues, "issues": check.issues}
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
