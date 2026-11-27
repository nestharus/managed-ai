#!/usr/bin/env python3
"""Source-backed model worker for the accepted finite peer harness.

See docs/PEER-CONTRACT.md. This module owns no scheduler or model inference.
"""

import argparse
import base64
import importlib.util
import json
import math
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

if __package__ in (None, ""):
    _src_root = str(Path(__file__).resolve().parents[3])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai_information.companions.peer.peer_protocol import (
    OWNER_STATUSES as OWNER_STATUSES,
)
from managed_ai_information.companions.peer.peer_protocol import (
    REQUEST_SCHEMA,
    canonical,
    detached,
    digest,
    fields,
    identifier,
    identity,
    integer,
    need,
    scoped_file,
    validate_peer_entry,
)
from managed_ai_information.companions.peer.peer_protocol import (
    RETURN_SCHEMA as RETURN_SCHEMA,
)
from managed_ai_information.companions.peer.peer_protocol import (
    WORKBENCH_CONFIG_SCHEMA as CONFIG_SCHEMA,
)
from managed_ai_information.companions.peer.peer_protocol import (
    WORKBENCH_STATE_SCHEMA as STATE_SCHEMA,
)
from managed_ai_information.companions.peer.peer_protocol import (
    canonical_absolute as absolute,
)
from managed_ai_information.companions.peer.peer_protocol import (
    loads_strict as decode_json,
)
from managed_ai_information.companions.peer.peer_tools import COUNT_KEYS, PeerTools, write_json

HERE = Path(__file__).resolve().parent
COMPANIONS_ROOT = HERE.parent
CONTRACT_FILES = ("docs/PEER-CONTRACT.md",)
_COMPONENT_ROLES = (
    "io_utils",
    "config",
    "model_bridges",
    "source_tools",
    "evidence_support",
    "agent_runtime",
    "workbench_agent",
)
_MODULES: tuple[tuple[Path, str], dict[str, ModuleType]] | None = None


def _contract_file(name: str) -> Path:
    local = HERE / name
    if local.is_file():
        return local

    repo_doc = HERE.parents[3] / "docs/companions/peer" / name
    if repo_doc.is_file():
        return repo_doc

    raise FileNotFoundError("Peer contract document is absent: " + str(repo_doc))


def _verify_component_manifest() -> tuple[dict[str, Path], dict[str, Any]]:
    manifest = decode_json((HERE / "COMPONENTS.json").read_bytes())
    need(
        isinstance(manifest, dict)
        and manifest.get("schema") == "peer-workbench-acquired-components/1"
        and isinstance(manifest.get("files"), dict),
        "Invalid peer component manifest",
    )

    role_names = set(_COMPONENT_ROLES)
    aliases: dict[str, Path] = {}
    measured_files: dict[str, dict[str, Any]] = {}

    for name, item in manifest["files"].items():
        need(isinstance(item, dict), "Component entry must be an object: " + str(name))
        fields(item, ("source",), ("identity",))
        need(
            item["source"] == name, "Component source path differs from manifest key: " + str(name)
        )

        path = scoped_file(COMPANIONS_ROOT, name)
        live_identity = identity(path.read_bytes())
        if "identity" in item:
            need(
                live_identity == item["identity"],
                "Acquired component differs from accepted body: " + name,
            )

        measured_files[name] = {**item, "identity": live_identity}
        role = path.stem
        if role in role_names:
            need(role not in aliases and path.suffix == ".py", "Ambiguous peer component: " + role)
            aliases[role] = path

    missing = [name for name in _COMPONENT_ROLES if name not in aliases]
    need(not missing, "Missing peer component: " + ", ".join(missing))
    return aliases, {**manifest, "files": measured_files}


def _load_component_namespace(aliases: dict[str, Path]) -> dict[str, ModuleType]:
    pkg_root = "_g60_peer_" + digest(str(COMPANIONS_ROOT).encode("utf-8"))[:16]
    created = [pkg_root, *(f"{pkg_root}.{name}" for name in _COMPONENT_ROLES)]
    prior = {key: sys.modules.get(key) for key in created}
    loaded: dict[str, ModuleType] = {}

    try:
        pkg = ModuleType(pkg_root)
        pkg.__path__ = [str(COMPANIONS_ROOT / "workbench")]
        pkg.__package__ = pkg_root
        sys.modules[pkg_root] = pkg

        for name in _COMPONENT_ROLES:
            mod_name = f"{pkg_root}.{name}"
            spec = importlib.util.spec_from_file_location(mod_name, aliases[name])
            if spec is None or spec.loader is None:
                raise ImportError(f"Unable to load peer component {name} from {aliases[name]}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
    except BaseException:
        for key, old in prior.items():
            if old is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = old
        raise

    return loaded


def components() -> tuple[dict[str, ModuleType], dict[str, Any]]:
    """Load declared workbench modules in an isolated package namespace and measure live
    identities.
    """
    global _MODULES
    aliases, effective_manifest = _verify_component_manifest()
    basis = (COMPANIONS_ROOT, digest(canonical(effective_manifest["files"])))
    if _MODULES is not None and _MODULES[0] == basis:
        return _MODULES[1], effective_manifest

    loaded = _load_component_namespace(aliases)
    _MODULES = (basis, loaded)
    return loaded, effective_manifest


def _validate_worker_config(config: dict[str, Any]) -> None:
    fields(
        config,
        ("schema", "task", "sources", "corpus_root", "evidence_root", "model"),
        ("max_steps", "max_file_bytes", "max_cache_bytes", "receivers", "obligations"),
    )
    need(
        config["schema"] == CONFIG_SCHEMA and isinstance(config["task"], str),
        "Invalid worker configuration schema/task",
    )
    need(
        isinstance(config["sources"], dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in config["sources"].items()),
        "sources must map strings to complete strings",
    )
    need(
        not {"peer-request", "peer-workbench-contract", "workbench-tool-contract"}
        & config["sources"].keys(),
        "Reserved entry source identity",
    )

    for name, default, minimum in (
        ("max_steps", 12, 0),
        ("max_file_bytes", 2097152, 1),
        ("max_cache_bytes", 65536, 0),
    ):
        need(integer(config.get(name, default), minimum), name + " has an invalid limit")

    need(isinstance(config.get("obligations", []), list), "obligations must be an array")
    need(isinstance(config.get("receivers", {}), dict), "receivers must be an object")

    model = config["model"]
    need(isinstance(model, dict), "model must be an object")
    if model.get("kind") == "mailbox":
        fields(model, ("kind",), ("timeout_seconds",))
    elif model.get("kind") == "command":
        fields(model, ("kind", "argv"), ("cwd", "timeout_seconds"))
        need(
            isinstance(model["argv"], list)
            and bool(model["argv"])
            and all(isinstance(s, str) and bool(s) for s in model["argv"]),
            "model.argv must be a nonempty string array",
        )
        if "cwd" in model:
            absolute(model["cwd"], "model.cwd", directory=True)
    else:
        raise ValueError("model.kind must be mailbox or command")

    timeout = model.get("timeout_seconds", 900)
    need(
        type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
        "Invalid model timeout",
    )


def _validate_peer_request(
    config: dict[str, Any],
    request: dict[str, Any],
) -> tuple[Path, Path, Path]:
    fields(
        request,
        (
            "schema",
            "run_id",
            "owner",
            "turn",
            "revision",
            "brief",
            "input",
            "state",
            "inbox",
            "peers",
            "workspace",
            "limits_remaining",
        ),
    )
    need(
        request["schema"] == REQUEST_SCHEMA
        and isinstance(request["run_id"], str)
        and bool(request["run_id"]),
        "Invalid peer request schema/run",
    )
    need(identifier(request["owner"]), "Invalid request owner")
    need(
        integer(request["turn"], 1) and integer(request["revision"], 1),
        "Invalid request turn/revision",
    )

    workspace = absolute(request["workspace"], "workspace", directory=True)
    corpus = absolute(config["corpus_root"], "corpus_root", directory=True)
    evidence = absolute(config["evidence_root"], "evidence_root")
    need(
        not evidence.is_relative_to(workspace),
        "Evidence must remain outside the editable workspace",
    )
    need(not evidence.is_relative_to(corpus), "Evidence must remain outside the source corpus")
    need(
        not workspace.is_relative_to(corpus) and not corpus.is_relative_to(workspace),
        "Workspace and corpus must be separate",
    )

    need(isinstance(request["inbox"], list), "Inbox must be an array")
    message_ids: set[str] = set()
    for message in request["inbox"]:
        fields(message, ("id", "from", "to", "payload"))
        need(
            isinstance(message["id"], str)
            and bool(message["id"])
            and message["id"] not in message_ids
            and message["to"] == request["owner"]
            and identifier(message["from"]),
            "Invalid delivered message identity",
        )
        message_ids.add(message["id"])

    need(isinstance(request["peers"], dict), "Peers must be an object")
    for owner, peer_entry in request["peers"].items():
        need(identifier(owner) and owner != request["owner"], "Invalid peer owner")
        validate_peer_entry(peer_entry)

    limits = request["limits_remaining"]
    fields(
        limits, ("wall_seconds", "turns", "turn_seconds", "max_output_bytes", "max_artifact_bytes")
    )
    need(
        integer(limits["turns"])
        and integer(limits["max_output_bytes"], 1)
        and integer(limits["max_artifact_bytes"], 1),
        "Invalid harness integer limits",
    )
    need(
        all(
            type(limits[k]) in (int, float) and math.isfinite(limits[k]) and limits[k] >= 0
            for k in ("wall_seconds", "turn_seconds")
        ),
        "Invalid harness time limits",
    )
    return workspace, corpus, evidence


def _validate_prior_state(
    config: dict[str, Any],
    request: dict[str, Any],
    config_sha: str,
) -> None:
    previous = request["state"]
    if request["revision"] == 1:
        need(previous is None, "First adapter turn requires null prior state")
        return

    fields(
        previous,
        (
            "schema",
            "owner",
            "revision",
            "worker_config_sha256",
            "model_state",
            "continuing_duties",
            "completed_turns",
            "accounting",
            "last_turn",
        ),
    )
    need(
        previous["schema"] == STATE_SCHEMA
        and previous["owner"] == request["owner"]
        and previous["revision"] == request["revision"] - 1,
        "Prior adapter state belongs to another owner/revision",
    )
    need(
        previous["worker_config_sha256"] == config_sha,
        "Worker configuration changed across owner turns",
    )
    need(
        canonical(previous["continuing_duties"]) == canonical(config.get("obligations", [])),
        "Continuing duty objects changed",
    )
    need(
        integer(previous["completed_turns"], 1)
        and previous["completed_turns"] == request["revision"] - 1,
        "Prior completed-turn account is invalid",
    )
    fields(previous["accounting"], COUNT_KEYS)
    need(
        all(integer(previous["accounting"][k]) for k in COUNT_KEYS),
        "Prior callback/read counts are invalid",
    )


def validate(config: dict[str, Any], request: dict[str, Any]) -> tuple[Path, Path, Path, str]:
    """Validate worker configuration, launch request, filesystem isolation, and prior state."""
    _validate_worker_config(config)
    workspace, corpus, evidence = _validate_peer_request(config, request)
    config_sha = digest(canonical(config))
    _validate_prior_state(config, request, config_sha)
    return workspace, corpus, evidence, config_sha


def _build_turn_report(
    config: dict[str, Any],
    request: dict[str, Any],
    *,
    result: dict[str, Any],
    response: dict[str, Any] | None,
    error: dict[str, str] | None,
    peer: PeerTools | None,
    workbench: Any,
    started: float,
) -> dict[str, Any]:
    reads = peer.reads if peer is not None else []
    return {
        "schema": "peer-workbench-result/1",
        "ok": response is not None,
        "owner": request["owner"],
        "revision": request["revision"],
        "model_status": result["status"],
        "model_calls": result["model_calls"],
        "tool_calls": result["tool_calls"],
        "peer_read_attempts": len(reads),
        "peer_read_calls": sum(entry["successful"] for entry in reads),
        "peer_read_bytes": sum(
            entry["selected_body_bytes"] for entry in reads if entry["successful"]
        ),
        "peer_read_io_bytes_all_attempts": sum(entry["io_bytes"] for entry in reads),
        "source_stats": workbench.source_service.stats() if workbench is not None else None,
        "accepted_artifacts": workbench.accepted if workbench is not None else [],
        "continuing_duties": config.get("obligations", []),
        "returned_file_bytes": (
            sum(len(base64.b64decode(x)) for x in response["files"].values()) if response else 0
        ),
        "response_identity": identity(canonical(response) + b"\n") if response else None,
        "per_turn_model_limit": config.get("max_steps", 12),
        "run_wide_model_limit_enforced": False,
        "elapsed_seconds": time.monotonic() - started,
        "error": error,
        "scope": (
            "local model/tool observations and prepared peer return; harness admission and "
            "semantic "
            "whole-use receiving are subsequent"
        ),
        "provider_inference_calls": None,
        "provider_tokens": None,
        "provider_prices": None,
    }


def _setup_peer_workbench(
    config: dict[str, Any],
    request: dict[str, Any],
    *,
    workspace: Path,
    corpus: Path,
    directory: Path,
    config_sha: str,
    model: Any,
) -> tuple[dict[str, ModuleType], Any, Any, dict[str, str], Any]:
    modules, manifest = components()
    contract = "\n\n".join(
        "SOURCE " + Path(name).name + "\n\n" + _contract_file(name).read_text(encoding="utf-8")
        for name in CONTRACT_FILES
    )
    sources = dict(config["sources"])
    sources["peer-request"] = json.dumps(request, ensure_ascii=False, indent=2, allow_nan=False)
    sources["peer-workbench-contract"] = contract
    code_files = {
        **{
            name: identity((HERE / name).read_bytes())
            for name in ("peer_workbench.py", "peer_protocol.py", "COMPONENTS.json")
        },
        **{name: identity(_contract_file(name).read_bytes()) for name in CONTRACT_FILES},
    }

    source = modules["source_tools"].SourceService(
        corpus,
        directory / "workbench/source-service",
        max_cache_bytes=config.get("max_cache_bytes", 65536),
    )
    wb_config = {
        "task": config["task"],
        "workspace": str(workspace),
        "state_dir": str(directory / "workbench"),
        "corpus_root": str(corpus),
        "sources": sources,
        "obligations": config.get("obligations", []),
        "receivers": config.get("receivers", {}),
        "max_file_bytes": config.get("max_file_bytes", 2097152),
        "run_id": request["run_id"] + "/" + request["owner"] + "/" + str(request["revision"]),
    }
    workbench = modules["workbench_agent"].Workbench(wb_config, source_service=source)
    sources["workbench-tool-contract"] = json.dumps(
        workbench.tool_contract,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    write_json(
        directory / "input-identities.json",
        {
            "schema": "peer-workbench-input-identities/1",
            "config_sha256": config_sha,
            "request_sha256": digest(canonical(request)),
            "adapter": code_files,
            "acquired": manifest,
            "entry_sources": {k: identity(v.encode("utf-8")) for k, v in sources.items()},
            "workbench_tool_contract_sha256": workbench.contract_sha256,
            "injected_model": model is not None,
            "continuing_duties": config.get("obligations", []),
        },
    )

    if model is None:
        transport = config["model"]
        timeout = transport.get("timeout_seconds", 900)
        if transport["kind"] == "mailbox":
            model = modules["model_bridges"].MailboxModel(directory / "model", timeout)
        else:
            model = modules["model_bridges"].CommandModel(
                transport["argv"],
                directory / "model",
                cwd=transport.get("cwd"),
                timeout_seconds=timeout,
            )

    return modules, source, workbench, sources, model


def run_peer_turn(
    config: dict[str, Any],
    request: dict[str, Any],
    *,
    model: Any = None,
) -> dict[str, Any]:
    """Run one fresh bounded model/tool turn and prepare its exact peer return."""
    config_copy = detached(config)
    request_copy = detached(request)
    workspace, corpus, evidence, config_sha = validate(config_copy, request_copy)
    need(model is None or callable(model), "Injected model must be callable")

    directory = evidence / request_copy["owner"] / f"revision-{request_copy['revision']:06d}"
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "config.json", config_copy)
    write_json(directory / "request.json", request_copy)

    started = time.monotonic()
    result: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    peer: PeerTools | None = None
    workbench: Any = None
    source: Any = None

    try:
        try:
            modules, source, workbench, sources, active_model = _setup_peer_workbench(
                config_copy,
                request_copy,
                workspace=workspace,
                corpus=corpus,
                directory=directory,
                config_sha=config_sha,
                model=model,
            )
            peer = PeerTools(config_copy, request_copy, workbench, directory)
            tools = workbench.tools()
            tools.update(
                peer_context=peer.context, peer_read=peer.read, peer_stage_return=peer.stage
            )

            result = modules["agent_runtime"].run_agent(
                task=config_copy["task"],
                sources=sources,
                model=active_model,
                tools=tools,
                max_steps=config_copy.get("max_steps", 12),
            )
            write_json(directory / "run-result.json", result)
            response = peer.finish(result, config_sha)
            write_json(directory / "peer-response.json", response)
        except Exception as exc:
            response = None
            error = {"type": type(exc).__name__, "message": str(exc)}
            if result is None:
                result = {
                    "status": "adapter_error",
                    "model_calls": 0,
                    "tool_calls": 0,
                    "error": error,
                    "trace": [],
                }
                write_json(directory / "run-result.json", result)

        report = _build_turn_report(
            config_copy,
            request_copy,
            result=result,
            response=response,
            error=error,
            peer=peer,
            workbench=workbench,
            started=started,
        )
        write_json(directory / "adapter-result.json", report)
        return {
            "ok": report["ok"],
            "response": response,
            "report": report,
            "evidence_dir": str(directory),
        }
    finally:
        if source is not None:
            source.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    try:
        result = run_peer_turn(
            decode_json(Path(args.config).read_bytes()),
            decode_json(sys.stdin.buffer.read()),
        )
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(
            json.dumps(
                {
                    "status": "setup_rejection",
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                },
                allow_nan=False,
            ),
            file=sys.stderr,
        )
        return 2

    if result["ok"]:
        sys.stdout.buffer.write(canonical(result["response"]) + b"\n")
        return 0

    print(
        json.dumps(
            {
                "status": "worker_not_returned",
                "evidence_dir": result["evidence_dir"],
                "model_status": result["report"]["model_status"],
                "error": result["report"]["error"],
            },
            allow_nan=False,
        ),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
