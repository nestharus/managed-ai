#!/usr/bin/env python3
"""Prepare one source-bound G60 invocation. This program never starts the runtime."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import sys


PRODUCT = Path(__file__).resolve().parent
GUIDE_FILES = (
    "AGENTS.md", "README.md", "methods.md", "guide.md", "attention.md",
    "worked-discovery.md", "worked-examples.md",
    "software-patterns.md", "shared-systems.md",
)
RENDER_TOKENS = ("{run_dir}", "{repo_root}", "{method_root}", "{operations_root}")
ENTRY = (
    "import sys; sys.path.insert(0,sys.argv.pop(1)); "
    "from cognition.loop import main; sys.exit(main())"
)
KNOWLEDGE_ROLES = {
    "method": "agent/AGENTS.md",
    "overview": "prototype/knowledge-system/README.md",
    "contract": "prototype/knowledge-system/CONTRACT.md",
    "migration": "prototype/knowledge-system/MIGRATION.md",
}
MAX_CATALOGUE = 4 * 1024 * 1024
MAX_KNOWLEDGE_BODY = 2 * 1024 * 1024
MAX_SELECTED_KNOWLEDGE = 8 * 1024 * 1024
COMPATIBILITY_HEADING = "# Knowledge-system compatibility entrance"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_paths(path):
    """The inspected paths_values grammar; parse data without sourcing a shell."""
    values = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            key, value = line.split("=", 1)
            parts = shlex.split(value)
        except ValueError as error:
            raise ValueError(f"paths.conf line {number}: {error}") from error
        if len(parts) != 1 or not re.fullmatch(r"[A-Z_]+", key):
            raise ValueError(f"paths.conf line {number}: expected one literal KEY=value")
        if any(char in parts[0] for char in "\x00\r\n"):
            raise ValueError(f"paths.conf line {number}: multiline/NUL values are unsupported")
        values[key] = parts[0]
    for key in ("OPERATIONS_ROOT", "METHOD_ROOT", "INSTALL_ROOT"):
        if not values.get(key):
            raise ValueError(f"paths.conf needs a nonempty {key}")
    return values


def write_paths(values):
    # Both Python shlex.split and the inspected Bash reader recover these literals.
    return "# Prepared literal values; METHOD_ROOT and SEAT_WD select this G60 copy.\n" + "".join(
        f"{key}={shlex.quote(value)}\n" for key, value in values.items()
    )


def concrete_path(value, label):
    result = Path(value).expanduser().resolve()
    if any(char in str(result) for char in "\x00\r\n"):
        raise ValueError(f"{label} needs a single-line filesystem path")
    if any(token in str(result) for token in RENDER_TOKENS):
        raise ValueError(f"{label} contains a reserved runner template token")
    return result


def verify_runtime(runtime):
    contract = json.loads((PRODUCT / "source.json").read_text(encoding="utf-8"))
    actual = []
    for item in contract["delivery_files"]:
        path = runtime / item["path"]
        data = path.read_bytes()
        digest = sha256(data)
        if digest != item["sha256"]:
            raise ValueError(f"runtime delivery source differs from inspected body: {item['path']}")
        actual.append({"path": item["path"], "bytes": len(data), "sha256": digest})
    return contract, actual


def json_text(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def binding(path, data, copied_as=None):
    result = {"source": str(path), "bytes": len(data), "sha256": sha256(data)}
    if copied_as is not None:
        result["copied_as"] = copied_as
    return result


def read_knowledge_file(path, limit):
    """Read data only. No selected-library code is imported or executed."""
    path = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (*reversed(path.parents), path)):
        raise ValueError("knowledge inputs must not use symlink components")
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("knowledge input must be an ordinary file")
    if path.stat().st_size > limit:
        raise ValueError("knowledge input exceeds its byte bound")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("knowledge input grew beyond its byte bound")
    return data


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate knowledge JSON key: " + key)
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("nonfinite knowledge JSON value: " + value)
    return json.loads(data, object_pairs_hook=pairs, parse_constant=nonfinite)


def knowledge_identity(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,239}", value)
            or PurePosixPath(value).is_absolute() or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("invalid knowledge identity")
    return value


def knowledge_view(library, expected, extra):
    """Materialize complete named bodies as a local invocation view, not a library installation."""
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("--knowledge-library requires its exact --knowledge-sha256 catalogue pin")
    raw_path = Path(library).expanduser().absolute()
    raw = read_knowledge_file(raw_path / "catalogue.json", MAX_CATALOGUE)
    library = concrete_path(raw_path, "knowledge library")
    if sha256(raw) != expected:
        raise ValueError("knowledge catalogue differs from selected SHA-256; select the current edition explicitly")
    catalogue = strict_json(raw)
    if (not isinstance(catalogue, dict) or set(catalogue) != {"format", "edition", "bodies", "modules", "provenance"}
            or catalogue["format"] != "knowledge-library/1"):
        raise ValueError("unsupported knowledge catalogue; explicit source migration required")
    if not isinstance(catalogue["edition"], str) or not catalogue["edition"].strip():
        raise ValueError("knowledge edition label is absent")
    if not isinstance(catalogue["bodies"], list) or not 1 <= len(catalogue["bodies"]) <= 2048:
        raise ValueError("invalid knowledge body inventory")
    rows = {}
    for row in catalogue["bodies"]:
        if not isinstance(row, dict) or "id" not in row:
            raise ValueError("invalid knowledge body entry")
        ident = knowledge_identity(row["id"])
        if ident in rows:
            raise ValueError("duplicate knowledge identity: " + ident)
        rows[ident] = row
    selected = list(KNOWLEDGE_ROLES.values())
    for ident in extra:
        knowledge_identity(ident)
        if ident not in selected:
            selected.append(ident)
    files, supplied, texts, total = {"knowledge/source-catalogue.json": raw}, [], {}, 0
    for ident in selected:
        if ident not in rows:
            raise ValueError("required complete knowledge body is unavailable: " + ident)
        row = rows[ident]
        if set(row) != {"id", "title", "summary", "object", "sha256", "bytes", "source", "link_base", "references"}:
            raise ValueError("invalid selected body fields: " + ident)
        h = row["sha256"]
        if not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h) or row["object"] != "objects/" + h + ".txt":
            raise ValueError("selected body object is not content-addressed: " + ident)
        if type(row["bytes"]) is not int or not 0 <= row["bytes"] <= MAX_KNOWLEDGE_BODY:
            raise ValueError("selected body exceeds its byte bound: " + ident)
        if not isinstance(row["source"], dict) or set(row["source"]) != {"path", "sha256"} or row["source"]["sha256"] != h:
            raise ValueError("complete source identity differs from selected body: " + ident)
        knowledge_identity(row["source"]["path"]); knowledge_identity(row["link_base"])
        if not isinstance(row["references"], list) or len(row["references"]) > 2048:
            raise ValueError("invalid selected source routes")
        for ref in row["references"]:
            if (not isinstance(ref, dict) or set(ref) != {"target", "anchor", "kind"}
                    or not isinstance(ref["target"], str) or not isinstance(ref["anchor"], str)
                    or ref["kind"] not in ("source", "url")):
                raise ValueError("invalid selected reference route")
        if row["bytes"] > MAX_SELECTED_KNOWLEDGE - total:
            raise ValueError("selected complete knowledge exceeds the total byte bound")
        data = read_knowledge_file(library / row["object"], MAX_KNOWLEDGE_BODY)
        if len(data) != row["bytes"] or sha256(data) != h:
            raise ValueError("selected knowledge body differs from its source pin: " + ident)
        text = data.decode("utf-8"); total += len(data); texts[ident] = text
        copied = "knowledge/" + row["object"]
        files[copied] = data
        supplied.append({"pin": {"library_sha256": expected, "id": ident, "sha256": h}, "entry": row,
            "input": binding(library / row["object"], data, "method/" + copied)})
    method_text = texts[KNOWLEDGE_ROLES["method"]]
    if method_text.lstrip().startswith(COMPATIBILITY_HEADING):
        raise ValueError("selected method is a retired compatibility body, not the complete operating source")
    files["AGENTS.md"] = method_text.encode("utf-8")
    index = {"format": "g60-invocation-knowledge/1", "library_sha256": expected, "edition": catalogue["edition"],
        "required_roles": KNOWLEDGE_ROLES, "bodies": supplied, "body_bytes": total,
        "scope": "Purpose-specific complete-body view. The wider source catalogue is retained for identity and recovery; unselected objects and executable installation are not supplied."}
    files["knowledge/selection.json"] = json_text(index).encode("utf-8")
    readme = (
        "# Prepared knowledge view\n\n"
        "This invocation derives its complete operating method and current interpretation from a selected knowledge edition. "
        "It is not an independently maintained general agent manual. Read the current overview, contract and migration "
        "bodies below before interpreting historical distribution references. The complete method is also in AGENTS.md "
        "and the commissioned brief.\n\n"
        f"Exact source catalogue: `{expected}`. The [selection and source entries](knowledge/selection.json) bind the full copied bodies. "
        "The source-catalogue file is an identity witness; this view does not include every object in that wider catalogue.\n\n"
        "| Logical source | Complete copied body |\n|---|---|\n"
        + "".join(f"| `{item['pin']['id']}` | [Read complete text]({item['input']['copied_as'][7:]}) |\n" for item in supplied)
        + "\nReferences inside these exact source bodies retain their recorded link bases. Resolve a logical identity through "
        "the selection map; unselected source routes require further acquisition. A code body is text, not an installed operation. "
        "Use the actual task authority; imported knowledge supplies no permission or active deployment.\n"
    )
    files["README.md"] = readme.encode("utf-8")
    metadata = {"mode": "selected-knowledge-view", "catalogue": binding(library / "catalogue.json", raw,
        "method/knowledge/source-catalogue.json"), **index}
    return files, [item["input"] for item in supplied], metadata


def prepare(args):
    runtime = concrete_path(args.runtime_root, "runtime root")
    selected_paths = concrete_path(args.paths, "selected paths")
    guide_root = concrete_path(args.guide_root, "guide root") if args.guide_root else None
    task = concrete_path(args.task, "task")
    target = concrete_path(args.invocation, "invocation directory")
    invocation_cwd = concrete_path(args.invocation_cwd or Path.cwd(), "invocation cwd")
    work_root = concrete_path(args.work_root or task.parent, "task work root")
    outputs = [concrete_path(value, "output destination") for value in args.output]
    supports = [concrete_path(value, "support input") for value in args.support]
    if target.exists():
        raise ValueError("invocation directory already exists; select a new directory")
    if not target.parent.is_dir():
        raise ValueError("invocation directory's parent must already exist")
    if not invocation_cwd.is_dir() or not work_root.is_dir():
        raise ValueError("invocation cwd and task work root must be existing directories")
    if len(set(outputs)) != len(outputs):
        raise ValueError("output destinations must be distinct")
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", args.seat_class):
        raise ValueError("class must match the runner's lowercase class-name grammar")
    if not re.fullmatch(r"claude(?:[0-9]+)?", args.profile):
        raise ValueError("profile must explicitly name the supported Claude profile namespace")
    if args.profile in ("claude0", "claude1"):
        raise ValueError("the runtime redirects claude0/claude1; choose the effective profile explicitly")

    source_contract, runtime_files = verify_runtime(runtime)
    original_config_bytes = selected_paths.read_bytes()
    original_values = read_paths(selected_paths)
    knowledge = None
    if guide_root is not None:
        if args.knowledge_sha256 or args.knowledge_body:
            raise ValueError("knowledge selectors require --knowledge-library")
        guide_bytes = {name: (guide_root / name).read_bytes() for name in GUIDE_FILES}
        if any(data.decode("utf-8").lstrip().startswith(COMPATIBILITY_HEADING) for data in guide_bytes.values()):
            raise ValueError("guide root is a retired compatibility entrance; use --knowledge-library with its exact --knowledge-sha256 pin")
        guide_bindings = [binding(guide_root / name, data, "method/" + name) for name, data in guide_bytes.items()]
    else:
        guide_bytes, guide_bindings, knowledge = knowledge_view(args.knowledge_library, args.knowledge_sha256, args.knowledge_body)
    method_text = guide_bytes["AGENTS.md"].decode("utf-8")
    if any(token in method_text for token in RENDER_TOKENS):
        raise ValueError("AGENTS.md contains a runner template token; this inline-method route needs review")
    task_bytes = task.read_bytes()
    task_bytes.decode("utf-8")
    support_bytes = [(path, path.read_bytes()) for path in supports]

    method_root = target / "method"
    task_copy = target / "inputs" / "task.md"
    support_copies = []
    for number, (path, data) in enumerate(support_bytes, 1):
        name = re.sub(r"[^A-Za-z0-9._-]", "_", path.name) or "input"
        support_copies.append((path, data, target / "inputs" / f"support-{number:03d}-{name}"))
    values = dict(original_values)
    values.update(METHOD_ROOT=method_root.as_posix(), SEAT_WD=method_root.as_posix())
    normalizations = []
    router_config = values.get("MODEL_ROUTER_CONFIG")
    if router_config and not Path(router_config).is_absolute():
        # The Python chooser reads from the invocation cwd, while the shell reader
        # is reached from the runtime cwd. Name the same selected file at both.
        resolved = (invocation_cwd / router_config).resolve().as_posix()
        normalizations.append({"key": "MODEL_ROUTER_CONFIG", "before": router_config,
                               "after": resolved, "base": invocation_cwd.as_posix()})
        values["MODEL_ROUTER_CONFIG"] = resolved
    config_text = write_paths(values)

    brief = (
        "# G60 commissioned work\n\n"
        "Read the complete task input at the following exact copied location before doing the work:\n\n"
        f"{task_copy.as_posix()}\n\n"
        f"The task came from: {task.as_posix()}\n"
        f"Interpret task-relative project paths from this work root: {work_root.as_posix()}\n\n"
        "The task input is literal source text, not a runner template. Its braces and other text "
        "have been retained unchanged. Use its requested purpose, constraints, available support "
        "and authority with the G60 method below. The method working directory is a guide location; "
        "it is not the task's project root.\n\n"
        "## Required output destinations\n\n"
        + "".join(f"- {path.as_posix()}\n" for path in outputs)
        + "\n## Supplied supporting inputs\n\n"
    )
    if support_copies:
        brief += "".join(
            f"- Read {copy.as_posix()} (exact copy of {path.as_posix()}).\n"
            for path, _, copy in support_copies
        )
        brief += (
            "\nRelative references inside copied supporting material retain the original "
            "source location shown here as their base. The copied-input directory does "
            "not change that meaning.\n"
        )
    else:
        brief += "No additional supporting input was supplied.\n"
    if knowledge is not None:
        brief += (
            "\n## Current knowledge interpretation\n\n"
            "This invocation uses a purpose-specific view of complete pinned knowledge bodies. Read the generated "
            f"{method_root.as_posix()}/README.md and its complete current overview, contract and migration bodies "
            "before interpreting any older distribution references in the selected source text. The old general agent/research "
            "entrances have been retired; their complete operating bodies remain knowledge. No copied body installs code, "
            "changes the selected deployment or grants effects. The selection and original catalogue identity are retained at "
            f"{method_root.as_posix()}/knowledge/selection.json.\n"
        )
    brief += (
        "\n## Complete selected G60 operating method\n\n"
        + method_text
        + ("" if method_text.endswith("\n") else "\n")
        + "\n## Optional guide material\n\n"
        f"The selected {'knowledge view' if knowledge is not None else 'historical complete guide'} is retained at {method_root.as_posix()}. "
        f"Its reading map is {method_root.as_posix()}/README.md. "
        "Select supporting files when the task needs them.\n"
    )
    inputs = [task_copy.as_posix(), *(copy.as_posix() for _, _, copy in support_copies)]
    if knowledge is not None:
        inputs.extend((target / item["input"]["copied_as"]).as_posix() for item in knowledge["bodies"])
    manifest = {
        "schema": 1,
        "description": "One commissioned G60 task through the supported configurable runner.",
        "goal_view": False,
        "method_files": list(guide_bytes),
        "steps": [{
            "id": "work", "role": "maker", "class": args.seat_class,
            "profile": args.profile, "needs": [], "brief": "commission.md",
            "inputs": inputs, "artifacts": [path.as_posix() for path in outputs],
        }],
    }
    python = args.python or sys.executable
    argv = [python, "-B", "-c", ENTRY, runtime.as_posix(), "run",
            "--manifest", (target / "manifest.json").as_posix(),
            "--run-dir", (target / "run").as_posix(),
            "--paths", (target / "paths.conf").as_posix(), "--bash", args.bash]
    env = {
        "COGNITION_PATHS": (target / "paths.conf").as_posix(),
        "COGNITION_RUNTIME": runtime.as_posix(),
        "SEAT_WD": method_root.as_posix(),
    }
    invocation = {
        "schema": 1, "kind": "prepared-g60-invocation", "executed": False,
        "runtime_root": runtime.as_posix(), "source_commit": source_contract["commit"],
        "cwd": invocation_cwd.as_posix(), "argv": argv, "environment_overrides": env,
        "method_root": method_root.as_posix(), "task_work_root": work_root.as_posix(),
        "applicability": {
            "backend": "claude", "class": args.seat_class, "profile": args.profile,
            "class_backend_relation": "Caller supplies an existing class mapped to Claude in the selected router. Preparation neither contacts nor validates that router.",
            "runtime": "A working runtime at the inspected delivery-source identities; retained source slices alone are not a complete runtime installation.",
        },
        "original_paths": binding(selected_paths, original_config_bytes),
        "paths_values_before": original_values, "paths_values_after": values,
        "selector_changes": ["METHOD_ROOT", "SEAT_WD"],
        "configuration_normalizations": normalizations,
        "inputs": [binding(task, task_bytes, "inputs/task.md"), *[
            binding(path, data, copy.relative_to(target).as_posix()) for path, data, copy in support_copies
        ]],
        "guide": guide_bindings,
        "knowledge": knowledge,
        "runtime_delivery_sources": runtime_files,
    }
    command = (
        "#!/usr/bin/env bash\nset -eu\n"
        "# Prepared command. Running this file invokes the selected installed runtime/provider.\n"
        f"cd -- {shlex.quote(invocation_cwd.as_posix())}\n"
        "exec env " + " ".join(shlex.quote(f"{key}={value}") for key, value in env.items())
        + " " + shlex.join(argv) + "\n"
    )
    target.mkdir()
    (target / "method").mkdir()
    (target / "inputs").mkdir()
    for name, data in guide_bytes.items():
        destination = method_root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    task_copy.write_bytes(task_bytes)
    for _, data, copy in support_copies:
        copy.write_bytes(data)
    for name, text in (("paths.conf", config_text), ("manifest.json", json_text(manifest)),
                       ("commission.md", brief), ("invocation.json", json_text(invocation)),
                       ("invoke.sh", command)):
        (target / name).write_text(text, encoding="utf-8", newline="\n")
    files = []
    for path in sorted(target.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            files.append({"path": path.relative_to(target).as_posix(), "bytes": len(data), "sha256": sha256(data)})
    (target / "prepared-files.json").write_text(json_text({"files": files}), encoding="utf-8")
    return {"status": "prepared", "directory": target.as_posix(), "provider_started": False,
            "manifest": (target / "manifest.json").as_posix(), "files": len(files) + 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--paths", required=True, help="Existing explicitly selected paths.conf")
    acquisition = parser.add_mutually_exclusive_group(required=True)
    acquisition.add_argument("--guide-root", help="Historical complete G60 guide; current compatibility stubs are refused")
    acquisition.add_argument("--knowledge-library", help="Selected complete knowledge-library/1 data directory")
    parser.add_argument("--knowledge-sha256", help="Required exact catalogue SHA-256 for --knowledge-library")
    parser.add_argument("--knowledge-body", action="append", default=[], help="Additional complete logical body identity; repeatable")
    parser.add_argument("--task", required=True, help="UTF-8 task source; copied exactly as an input")
    parser.add_argument("--support", action="append", default=[])
    parser.add_argument("--output", action="append", required=True, help="Required output destination; repeatable")
    parser.add_argument("--class", dest="seat_class", required=True, help="Existing class selected by caller for the Claude backend")
    parser.add_argument("--profile", required=True, help="Explicit Claude namespace profile, e.g. claude2")
    parser.add_argument("--invocation", required=True, help="New invocation directory under an existing parent")
    parser.add_argument("--work-root", help="Base for task-relative project paths; defaults to original task parent")
    parser.add_argument("--invocation-cwd", help="Retained caller working directory for relative runtime configuration; defaults to current directory")
    parser.add_argument("--python", help="Interpreter for the prepared invocation; defaults to this interpreter")
    parser.add_argument("--bash", default="bash", help="Runner's existing Bash operand")
    args = parser.parse_args()
    try:
        print(json_text(prepare(args)), end="")
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(2, f"prepare: {error}\n")


if __name__ == "__main__":
    main()
