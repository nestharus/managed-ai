"""Durable local continuation for the source-backed construction workbench.

The capable model is caller-supplied. Recorded callback returns are replayed by
durable_agent without repeating their effects; unobserved outcomes stay pending.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

from durable_agent import (inspect_durable_state, resolve_durable_operation,
                           run_durable_agent)
from model_bridges import (CommandModel, MailboxModel, atomic_json, encode,
                           write_mailbox_response)
from workbench_agent import Workbench, WorkbenchBasisMismatch, pin
import source_tools


VERSION = "g60-resumable-workbench/1"


def _config(config):
    result = json.loads(encode(config))
    for name in ("state_dir", "workspace", "corpus_root"):
        result[name] = str(Path(result[name]).resolve())
    result["controller_dir"] = str(Path(result.get(
        "controller_dir", Path(__file__).parent / "controller")).resolve())
    result["source_state_dir"] = str(Path(result.get(
        "source_state_dir", Path(result["state_dir"]) / "source-service")).resolve())
    model = result.get("model")
    if not isinstance(model, dict):
        raise ValueError("A model transport configuration is required")
    if model.get("kind") in ("mailbox", "command"):
        model["directory"] = str(Path(model["directory"]).resolve())
    if model.get("cwd") is not None:
        model["cwd"] = str(Path(model["cwd"]).resolve())
    return result


def _pins(paths):
    return {str(Path(path).resolve()): pin(Path(path).resolve()) for path in paths}


def _command_code(command, cwd=None):
    """Bind the executable and a directly named Python script, not output paths."""
    if not command:
        return {}
    base = Path(cwd or Path.cwd())
    first = command[0]
    executable = str((base / first).resolve()) if "/" in first else shutil.which(first)
    if executable is None:
        raise ValueError("Configured command executable is unavailable: " + first)
    paths = [executable]
    if Path(executable).name.lower().startswith("python"):
        for argument in command[1:]:
            if argument in ("-c", "-m"):
                break  # Module/dynamic dependencies belong in explicit inputs.
            if argument.startswith("-"):
                continue
            if "{workspace}" not in argument and "{receiving_dir}" not in argument:
                paths.append(base / argument)
            break
    return _pins(paths)


def _binding(config, injected_model):
    base = Path(__file__).parent
    code = _pins(base / name for name in (
        "resumable_workbench.py", "workbench_agent.py", "source_tools.py", "model_bridges.py"))
    source_code = _pins([source_tools.bundled_search_module_path()])
    model = config["model"]
    model_inputs = _pins(model.get("inputs", []))
    model_code = {}
    if injected_model is not None:
        if model.get("kind") != "injected" or not isinstance(model.get("identity"), str) or not model["identity"]:
            raise ValueError("Injected model use requires model.kind='injected' and a nonempty identity")
        target = injected_model if inspect.isfunction(injected_model) or inspect.ismethod(injected_model) else type(injected_model)
        path = inspect.getsourcefile(target)
        if path is not None and Path(path).is_file():
            model_code = _pins([path])
        if not model_inputs and not model_code:
            raise ValueError("Bind injected model code through model.inputs or an inspectable source file")
    elif model.get("kind") == "command":
        model_code = _command_code(model["argv"], model.get("cwd"))
    elif model.get("kind") != "mailbox":
        raise ValueError("Use a mailbox/command model, or supply an explicitly identified injected callable")
    receiver_inputs = {}
    receiver_code = {}
    for name, receiver in config.get("receivers", {}).items():
        receiver_inputs[name] = _pins(receiver.get("inputs", []))
        receiver_code[name] = _command_code(receiver["argv"], config["workspace"])
    return {"schema": VERSION, "configuration": config, "adapter_code": code,
            "source_search_code": source_code, "model_code": model_code,
            "model_inputs": model_inputs, "receiver_inputs": receiver_inputs,
            "receiver_code": receiver_code}


def inspect_workbench(state_dir):
    """Inspect retained material evidence without source or model execution."""
    state = Path(state_dir).resolve()
    result = inspect_durable_state(state / "durable")
    entry = state / "entry-basis.json"
    result["workbench"] = {
        "schema": VERSION, "state_dir": str(state),
        "entry_basis": json.loads(entry.read_bytes()) if entry.is_file() else None,
        "scope": "Retained observations and accepted history; inspection is not fresh source observation or receiving.",
    }
    return result


def _failure(status, message, state=None):
    result = {"status": status, "answer": None, "model_calls": 0, "tool_calls": 0,
              "error": {"code": status, "message": message}}
    if state is not None and (Path(state) / "durable/manifest.json").is_file():
        observation = inspect_durable_state(Path(state) / "durable")
        if "durable" in observation:
            result["durable"] = observation["durable"]
    return result


def _record_attempt(state, result):
    directory = Path(state) / "attempt-results"
    directory.mkdir(parents=True, exist_ok=True)
    name = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex + ".json"
    path = directory / name
    atomic_json(path, result, exclusive=True)
    atomic_json(Path(state) / "latest-result.json", {"path": str(path), **pin(path)})
    return str(path)


def run_workbench(config, *, resume=False, model=None, after_record=None):
    """Start/resume one occurrence; reuse known returns and stop on unknown ones."""
    state = None
    workbench = None
    try:
        configured = _config(config)
        state = Path(configured["state_dir"])
        if resume and not (state / "durable/manifest.json").is_file():
            return _failure("state_error", "Resume requires an existing durable occurrence", state)
        if not resume and (state / "entry-basis.json").exists():
            return _failure("state_error", "Workbench occurrence already exists; use resume", state)
        binding = _binding(configured, model)
        workbench = Workbench(configured, resume=resume)
        transport = model
        transport_kind = configured["model"]["kind"]
        if transport is None:
            specification = configured["model"]
            if transport_kind == "mailbox":
                transport = MailboxModel(specification["directory"], specification.get("timeout_seconds", 900))
            else:
                transport = CommandModel(specification["argv"], specification["directory"],
                                         cwd=specification.get("cwd"),
                                         timeout_seconds=specification.get("timeout_seconds", 900))
        transport_ordinal = 0

        def actual_model(request):
            nonlocal transport_ordinal
            transport_ordinal = request["step"]
            if transport_kind in ("mailbox", "command"):
                # Historical replay never enters this callback. A new transport
                # file retains its logical request ordinal across processes.
                transport.calls = request["step"] - 1
            return transport(request)

        def checkpoint():
            return {"schema": "g60-resumable-workbench-checkpoint/1",
                    "workbench": workbench.checkpoint(),
                    "model_transport_ordinal": transport_ordinal}

        def restore(value):
            nonlocal transport_ordinal
            if not isinstance(value, dict) or value.get("schema") != "g60-resumable-workbench-checkpoint/1":
                raise ValueError("Unknown workbench continuation checkpoint")
            ordinal = value.get("model_transport_ordinal")
            if type(ordinal) is not int or ordinal < 0:
                raise ValueError("Invalid model transport ordinal")
            workbench.restore(value["workbench"])
            transport_ordinal = ordinal
            if transport_kind in ("mailbox", "command"):
                transport.calls = ordinal

        sources = dict(workbench.initial_sources)
        if "workbench-tool-contract" in sources:
            raise ValueError("Reserved entry source ID: workbench-tool-contract")
        sources["workbench-tool-contract"] = json.dumps(workbench.tool_contract, ensure_ascii=False, indent=2)
        result = run_durable_agent(
            state_dir=state / "durable", binding=binding, task=workbench.task,
            sources=sources, model=actual_model, tools=workbench.tools(),
            max_steps=configured.get("max_steps", 12), resume=resume,
            checkpoint=checkpoint, restore=restore, after_record=after_record,
            controller_dir=configured["controller_dir"])
        if workbench.workspace_mismatch is not None:
            result["status"] = "binding_mismatch"
            result["error"] = {
                "code": "workspace_changed",
                "message": "Previously read, written or received working paths differ from the retained continuation.",
                "changed_paths": workbench.workspace_mismatch,
            }
        retained = inspect_durable_state(state / "durable")
        last = retained.get("checkpoint") or {}
        retained_workbench = last.get("workbench", {})
        result["workbench"] = {
            "schema": VERSION, "state_dir": str(state), "basis": workbench.basis,
            "accepted_artifacts": retained_workbench.get("accepted", []),
            "retained_checkpoint": last,
            "source_stats": workbench.source_service.stats(),
            "transport": transport_kind,
            "source_scope": "Recovered inventory metadata guards historical observed members; no cache/index or fresh currentness is restored.",
            "provider_inference_calls": None, "provider_tokens": None, "provider_prices": None,
        }
        result_path = _record_attempt(state, result)
        result["result_path"] = result_path
        return result
    except WorkbenchBasisMismatch as exc:
        return _failure("binding_mismatch", str(exc), state)
    except (OSError, ValueError, TypeError, KeyError, ImportError) as exc:
        return _failure("state_error", str(exc), state)
    finally:
        if workbench is not None:
            workbench.source_service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "resume"):
        item = commands.add_parser(command)
        item.add_argument("--config", required=True)
    item = commands.add_parser("inspect")
    item.add_argument("--state-dir", required=True)
    item = commands.add_parser("respond")
    item.add_argument("--mailbox", required=True)
    item.add_argument("--step", required=True, type=int)
    item.add_argument("--action", required=True)
    item.add_argument("--producer", default="host-supplied model participant")
    item = commands.add_parser("resolve")
    item.add_argument("--state-dir", required=True)
    item.add_argument("--resolution", required=True,
                      help="JSON with operation_id, value, checkpoint, evidence and expected_contract_sha256")
    args = parser.parse_args()
    if args.command == "inspect":
        result = inspect_workbench(args.state_dir)
    elif args.command == "respond":
        write_mailbox_response(args.mailbox, args.step,
                               json.loads(Path(args.action).read_bytes()), producer=args.producer)
        result = {"status": "delivered", "step": args.step}
    elif args.command == "resolve":
        resolution = json.loads(Path(args.resolution).read_bytes())
        result = resolve_durable_operation(state_dir=Path(args.state_dir) / "durable", **resolution)
    else:
        result = run_workbench(json.loads(Path(args.config).read_bytes()), resume=args.command == "resume")
        result = {key: result.get(key) for key in
                  ("status", "error", "model_calls", "tool_calls", "durable", "result_path")}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in ("complete", "ready", "delivered", "resolved") else 2


if __name__ == "__main__":
    raise SystemExit(main())
