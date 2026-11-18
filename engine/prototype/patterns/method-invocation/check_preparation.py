#!/usr/bin/env python3
"""Exercise this preparer's real source/input boundary without starting a provider."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
import prepare


def require(value, message):
    if not value:
        raise AssertionError(message)


def exact_function(path, name, namespace):
    text = path.read_text()
    node = next(item for item in ast.parse(text).body if isinstance(item, ast.FunctionDef) and item.name == name)
    source = ast.get_source_segment(text, node)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name], {"source": str(path), "function": name, "function_sha256": prepare.sha256(source.encode())}


def check_prepared(directory, original_task, render, paths_values):
    invocation = json.loads((directory / "invocation.json").read_text())
    manifest = json.loads((directory / "manifest.json").read_text())
    brief = (directory / "commission.md").read_text()
    method = (directory / "method/AGENTS.md").read_text()
    require(method in brief, "complete selected method not in commissioned brief")
    require((directory / "inputs/task.md").read_bytes() == original_task.read_bytes(), "task changed")
    require(render(brief, {key: "ACTUAL-RENDER-REPLACEMENT" for key in ("run_dir", "repo_root", "method_root", "operations_root")}) == brief,
            "actual inspected render function changed the commission")
    require(paths_values(directory / "paths.conf") == invocation["paths_values_after"], "inspected literal config parser differs")
    for item in json.loads((directory / "prepared-files.json").read_text())["files"]:
        body = (directory / item["path"]).read_bytes()
        require(len(body) == item["bytes"] and prepare.sha256(body) == item["sha256"], "prepared file identity changed")
    require(len(manifest["steps"]) == 1 and manifest["steps"][0]["needs"] == [] and manifest["goal_view"] is False,
            "one whole-purpose step changed")
    for name in manifest["method_files"]:
        require((directory / "method" / name).is_file(), "declared method input missing")
    require(invocation["executed"] is False, "preparation claims execution")
    lines = (directory / "invoke.sh").read_text().splitlines()
    command = next(line for line in lines if line.startswith("exec env "))
    words = shlex.split(command[len("exec env "):])
    expected = [key + "=" + value for key, value in invocation["environment_overrides"].items()] + invocation["argv"]
    require(words == expected, "prepared shell words differ from recorded command")
    if invocation["knowledge"] is not None:
        knowledge = invocation["knowledge"]
        raw = (directory / knowledge["catalogue"]["copied_as"]).read_bytes()
        require(prepare.sha256(raw) == knowledge["library_sha256"], "catalogue pin lost")
        for item in knowledge["bodies"]:
            data = (directory / item["input"]["copied_as"]).read_bytes()
            require(prepare.sha256(data) == item["pin"]["sha256"] == item["entry"]["source"]["sha256"], "selected source body changed")
            require((directory / item["input"]["copied_as"]).as_posix() in manifest["steps"][0]["inputs"], "selected full body absent from explicit input routes")
        require("Current knowledge interpretation" in brief, "current migration framing absent")
    return {"complete_method_bytes": len(method.encode()), "method_sha256": prepare.sha256(method.encode()),
            "knowledge_bodies": len(invocation["knowledge"]["bodies"]) if invocation["knowledge"] else 0,
            "source_commit": invocation["source_commit"], "runtime_sources": len(invocation["runtime_delivery_sources"]),
            "provider_executed": invocation["executed"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--knowledge-library", type=Path, required=True)
    parser.add_argument("--knowledge-sha256", required=True)
    parser.add_argument("--legacy-guide-root", type=Path, required=True)
    parser.add_argument("--retired-guide-root", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True, help="New verification directory under an existing parent")
    parser.add_argument("--prepared", type=Path, help="Additionally inspect an actual already prepared receiving packet")
    args = parser.parse_args()
    source, observed = prepare.verify_runtime(args.runtime_root.resolve())
    root = args.work_root.resolve(); root.mkdir()
    task = root / "literal-task.md"
    task.write_text("Task data: {run_dir} {repo_root} {method_root} {operations_root}\nLiteral $(echo NOT_EXECUTED) and `echo NOT_EXECUTED`.\n")
    config = root / "paths.conf"
    original_values = {"OPERATIONS_ROOT": str(root / "operations with spaces"), "METHOD_ROOT": "historical-placeholder",
        "INSTALL_ROOT": str(args.runtime_root.resolve()), "MODEL_ROUTER_CONFIG": "relative router.conf",
        "LITERAL_OPERAND": "$(echo NOT_EXECUTED) `echo STILL_LITERAL` 'quotes'"}
    config.write_text(prepare.write_paths(original_values))
    render, render_source = exact_function(args.runtime_root / "cognition/loop.py", "render", {})
    paths_values, paths_source = exact_function(args.runtime_root / "cognition/environment.py", "paths_values", {"Path": Path, "shlex": shlex, "re": re})
    cases = []

    def run(name, selectors, accepted, runtime=None):
        directory = root / ("invocation-" + name)
        argv = [sys.executable, "-B", str(Path(__file__).parent / "prepare.py"),
            "--runtime-root", str((runtime or args.runtime_root).resolve()), "--paths", str(config),
            *selectors, "--task", str(task), "--output", str(root / "unused 'quoted' $result.md"),
            "--class", "engineer", "--profile", "claude2", "--work-root", str(root),
            "--invocation-cwd", str(root), "--invocation", str(directory)]
        result = subprocess.run(argv, capture_output=True, text=True)
        record = {"case": name, "argv": argv, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
        cases.append(record)
        require((result.returncode == 0) == accepted, name + " unexpected acceptance/refusal")
        if accepted:
            record["checked"] = check_prepared(directory, task, render, paths_values)
            values = json.loads((directory / "invocation.json").read_text())["paths_values_after"]
            require(values["LITERAL_OPERAND"] == original_values["LITERAL_OPERAND"], "literal config was evaluated or changed")
            require(values["MODEL_ROUTER_CONFIG"] == str(root / "relative router.conf"), "relative router selection changed")
        else:
            require(not directory.exists(), name + " wrote an invocation despite refusal")

    current = ["--knowledge-library", str(args.knowledge_library.resolve()), "--knowledge-sha256", args.knowledge_sha256]
    run("current", current + ["--knowledge-body", "research/current/method.md"], True)
    run("historical", ["--guide-root", str(args.legacy_guide_root.resolve())], True)
    run("retired", ["--guide-root", str(args.retired_guide_root.resolve())], False)
    run("stale-catalogue", current[:-1] + ["0" * 64], False)
    run("missing-body", current + ["--knowledge-body", "missing/understanding.md"], False)

    def altered_library(name, change):
        destination = root / name; shutil.copytree(args.knowledge_library, destination)
        manifest = json.loads((destination / "catalogue.json").read_text())
        change(destination, manifest)
        raw = prepare.json_text(manifest).encode(); (destination / "catalogue.json").write_bytes(raw)
        return ["--knowledge-library", str(destination), "--knowledge-sha256", prepare.sha256(raw)]

    def tamper(directory, manifest):
        row = next(x for x in manifest["bodies"] if x["id"] == "agent/AGENTS.md")
        (directory / row["object"]).write_bytes(b"wrong complete body")
    run("changed-object", altered_library("tampered-library", tamper), False)

    def change_text(ident, suffix):
        def change(directory, manifest):
            row = next(x for x in manifest["bodies"] if x["id"] == ident)
            body = (directory / row["object"]).read_bytes() + suffix
            h = prepare.sha256(body); row.update(sha256=h, bytes=len(body), object="objects/" + h + ".txt")
            row["source"]["sha256"] = h; (directory / row["object"]).write_bytes(body)
        return change
    run("inline-template", altered_library("inline-library", change_text("agent/AGENTS.md", b"\n{run_dir}\n")), False)
    run("literal-selected-input", altered_library("literal-library", change_text("prototype/knowledge-system/README.md", b"\n{run_dir}\n")), True)

    changed_runtime = root / "changed-runtime"
    for item in source["delivery_files"]:
        destination = changed_runtime / item["path"]; destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.runtime_root / item["path"], destination)
    with (changed_runtime / source["delivery_files"][0]["path"]).open("ab") as stream:
        stream.write(b"\n# changed source\n")
    run("changed-runtime", current, False, changed_runtime)
    require(not (root / "unused 'quoted' $result.md").exists(), "preparation wrote a requested output")
    actual = None
    if args.prepared is not None:
        inv = json.loads((args.prepared / "invocation.json").read_text())
        actual = check_prepared(args.prepared.resolve(), Path(inv["inputs"][0]["source"]), render, paths_values)
    report = {"status": "pass", "cases": cases, "actual_prepared_packet": actual,
        "runtime_sources": observed, "executed_source_functions": [render_source, paths_source],
        "scope": "Actual preparation and refusals; exact inspected render/literal-parser functions only. No complete runtime import, provider invocation, router query or installed-use claim."}
    (root / "report.json").write_text(prepare.json_text(report))
    print(prepare.json_text({"status": "pass", "cases": len(cases), "report": str(root / "report.json"), "actual_prepared_packet": actual}), end="")


if __name__ == "__main__":
    main()
