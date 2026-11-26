"""Durable local continuation for the source-backed construction workbench.

The capable model is caller-supplied. Recorded callback returns are replayed by
durable_agent without repeating their effects; unobserved outcomes stay pending.
"""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
import time
import uuid

try:
    from .config import (
        CORE_MODULES,
        build_model_transport,
        command_code as _command_code,
        normalize_config as _config,
        pin_files as _pins,
        validate_resolution_payload,
    )
    from .durable_agent import (
        inspect_durable_state,
        resolve_durable_operation,
        run_durable_agent,
    )
    from .io_utils import atomic_json, loads_strict, pin_file as pin
    from .model_bridges import write_mailbox_response
    from .source_tools import SearchImplementation
    from .workbench_agent import Workbench, WorkbenchBasisMismatch
except ImportError:
    from config import (
        CORE_MODULES,
        build_model_transport,
        command_code as _command_code,
        normalize_config as _config,
        pin_files as _pins,
        validate_resolution_payload,
    )
    from durable_agent import (
        inspect_durable_state,
        resolve_durable_operation,
        run_durable_agent,
    )
    from io_utils import atomic_json, loads_strict, pin_file as pin
    from model_bridges import write_mailbox_response
    from source_tools import SearchImplementation
    from workbench_agent import Workbench, WorkbenchBasisMismatch


VERSION = "g60-resumable-workbench/1"


def _binding(config, injected_model, search):
    base = Path(__file__).parent
    adapter_files = [
        base / name for name in CORE_MODULES
        if name != "durable_agent.py"
    ]
    code = _pins(adapter_files)
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
    return {
        "schema": VERSION,
        "configuration": config,
        "adapter_code": code,
        "source_search_code": search.pins,
        "source_search_analyzer": search.analyzer,
        "model_code": model_code,
        "model_inputs": model_inputs,
        "receiver_inputs": receiver_inputs,
        "receiver_code": receiver_code,
    }


def inspect_workbench(state_dir):
    """Inspect retained material evidence without source or model execution."""
    state = Path(state_dir).resolve()
    result = inspect_durable_state(state / "durable")
    entry = state / "entry-basis.json"
    result["workbench"] = {
        "schema": VERSION,
        "state_dir": str(state),
        "entry_basis": loads_strict(entry.read_bytes()) if entry.is_file() else None,
        "scope": "Retained observations and accepted history; inspection is not fresh source observation or receiving.",
    }
    return result


def _failure(status, message, state=None):
    result = {
        "status": status, "answer": None, "model_calls": 0, "tool_calls": 0,
        "error": {"code": status, "message": message},
    }
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
        try:
            search = SearchImplementation()
        except (OSError, ValueError, TypeError, AttributeError, ImportError) as exc:
            if resume:
                return _failure("binding_mismatch", str(exc), state)
            return _failure("state_error", str(exc), state)
        binding = _binding(configured, model, search)
        workbench = Workbench(configured, resume=resume, _source_search=search)
        transport = model
        transport_kind = configured["model"]["kind"]
        if transport is None:
            transport = build_model_transport(configured["model"])
        transport_ordinal = 0

        def actual_model(request):
            nonlocal transport_ordinal
            transport_ordinal = request["step"]
            if transport_kind in ("mailbox", "command"):
                # Historical replay never enters this callback. A new transport
                # file retains its logical request ordinal across processes.
                transport.set_step(request["step"])
            return transport(request)

        def checkpoint():
            return {
                "schema": "g60-resumable-workbench-checkpoint/1",
                "workbench": workbench.checkpoint(),
                "model_transport_ordinal": transport_ordinal,
            }

        def restore(value):
            nonlocal transport_ordinal
            if not isinstance(value, dict) or value.get("schema") != "g60-resumable-workbench-checkpoint/1":
                raise ValueError("Unknown workbench continuation checkpoint")
            ordinal = value.get("model_transport_ordinal")
            if type(ordinal) is not int or ordinal < 0:
                raise ValueError("Invalid model transport ordinal")
            # Another invocation may have completed between construction and
            # the durable owner's lock. Restore its checkpoint against that cut.
            if resume:
                workbench.source_service.reopen_receipts()
            workbench.restore(value["workbench"])
            transport_ordinal = ordinal
            if transport_kind in ("mailbox", "command"):
                transport.resume_at(ordinal)

        sources = dict(workbench.initial_sources)
        if "workbench-tool-contract" in sources:
            raise ValueError("Reserved entry source ID: workbench-tool-contract")
        sources["workbench-tool-contract"] = json.dumps(
            workbench.tool_contract, ensure_ascii=False, indent=2, allow_nan=False,
        )
        result = run_durable_agent(
            state_dir=state / "durable", binding=binding, task=workbench.task,
            sources=sources, model=actual_model, tools=workbench.tools(),
            max_steps=configured.get("max_steps", 12), resume=resume,
            checkpoint=checkpoint, restore=restore, after_record=after_record,
            controller_dir=configured["controller_dir"],
        )
        retained = inspect_durable_state(state / "durable")
        last = retained.get("checkpoint") or {}
        retained_workbench = last.get("workbench", {})
        result["workbench"] = {
            "schema": VERSION,
            "state_dir": str(state),
            "basis": workbench.basis,
            "accepted_artifacts": retained_workbench.get("accepted", []),
            "retained_checkpoint": last,
            "source_stats": workbench.source_service.stats(),
            "transport": transport_kind,
            "source_scope": "Recovered inventory metadata guards historical observed members; no cache/index or fresh currentness is restored.",
            "provider_inference_calls": None,
            "provider_tokens": None,
            "provider_prices": None,
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


def resolve_workbench_operation(state_dir, resolution_payload):
    """Validate an owner resolution payload strictly before recording it."""
    validated = validate_resolution_payload(resolution_payload)
    return resolve_durable_operation(
        state_dir=Path(state_dir) / "durable",
        operation_id=validated["operation_id"],
        value=validated["value"],
        checkpoint=validated["checkpoint"],
        evidence=validated["evidence"],
        expected_contract_sha256=validated["expected_contract_sha256"],
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "resume"):
        item = commands.add_parser(command, allow_abbrev=False)
        item.add_argument("--config", required=True)
    item = commands.add_parser("inspect", allow_abbrev=False)
    item.add_argument("--state-dir", required=True)
    item = commands.add_parser("respond", allow_abbrev=False)
    item.add_argument("--mailbox", required=True)
    item.add_argument("--step", required=True, type=int)
    item.add_argument("--action", required=True)
    item.add_argument("--producer", default="host-supplied model participant")
    item = commands.add_parser("resolve", allow_abbrev=False)
    item.add_argument("--state-dir", required=True)
    item.add_argument("--resolution", required=True,
                      help="JSON with operation_id, value, checkpoint, evidence and expected_contract_sha256")
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_workbench(args.state_dir)
        elif args.command == "respond":
            write_mailbox_response(
                args.mailbox,
                args.step,
                loads_strict(Path(args.action).read_bytes()),
                producer=args.producer,
            )
            result = {"status": "delivered", "step": args.step}
        elif args.command == "resolve":
            resolution = loads_strict(Path(args.resolution).read_bytes())
            result = resolve_workbench_operation(args.state_dir, resolution)
        else:
            result = run_workbench(
                loads_strict(Path(args.config).read_bytes()),
                resume=args.command == "resume",
            )
            result = {
                key: result.get(key) for key in
                ("status", "error", "model_calls", "tool_calls", "durable", "result_path")
            }
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = _failure("state_error", str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result.get("status") in ("complete", "ready", "delivered", "resolved") else 2


if __name__ == "__main__":
    raise SystemExit(main())
