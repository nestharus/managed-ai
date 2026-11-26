#!/usr/bin/env python3
"""Source-backed model worker for the accepted finite peer harness.

See docs/PEER-CONTRACT.md. This module owns no scheduler or model inference.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import math
from pathlib import Path
import sys
import time
from types import ModuleType

try:
    from .peer_protocol import (
        OWNER_STATUSES,
        REQUEST_SCHEMA,
        RETURN_SCHEMA,
        WORKBENCH_CONFIG_SCHEMA as CONFIG_SCHEMA,
        WORKBENCH_STATE_SCHEMA as STATE_SCHEMA,
        atomic_write_json,
        canonical,
        canonical_absolute as absolute,
        detached,
        digest,
        fields,
        identifier,
        identity,
        integer,
        loads_strict as decode_json,
        need,
        path_conflicts,
        scoped_file,
        target_path as relative,
        validate_peer_entry,
    )
except ImportError:
    from peer_protocol import (
        OWNER_STATUSES,
        REQUEST_SCHEMA,
        RETURN_SCHEMA,
        WORKBENCH_CONFIG_SCHEMA as CONFIG_SCHEMA,
        WORKBENCH_STATE_SCHEMA as STATE_SCHEMA,
        atomic_write_json,
        canonical,
        canonical_absolute as absolute,
        detached,
        digest,
        fields,
        identifier,
        identity,
        integer,
        loads_strict as decode_json,
        need,
        path_conflicts,
        scoped_file,
        target_path as relative,
        validate_peer_entry,
    )


HERE = Path(__file__).resolve().parent
COMPANIONS_ROOT = HERE.parent
COUNT_KEYS = ("model_calls", "tool_calls", "peer_read_calls", "peer_read_bytes")
CONTRACT_FILES = ("docs/PEER-CONTRACT.md",)
_MODULES = None


def write_json(path, value):
    atomic_write_json(path, value, exclusive=True)


def components():
    """Load declared workbench modules in an isolated package namespace and measure live identities."""
    global _MODULES
    manifest = decode_json((HERE / "COMPONENTS.json").read_bytes())
    need(
        isinstance(manifest, dict)
        and manifest.get("schema") == "peer-workbench-acquired-components/1"
        and isinstance(manifest.get("files"), dict),
        "Invalid peer component manifest",
    )
    # Import order follows module dependencies; COMPONENTS.json keys are companions/-relative.
    roles = (
        ("io_utils", "core"),
        ("config", "core"),
        ("model_bridges", "core"),
        ("source_tools", "core"),
        ("evidence_support", "controller"),
        ("agent_runtime", "controller"),
        ("workbench_agent", "core"),
    )
    role_names = {name for name, _ in roles}
    aliases = {}
    measured_files = {}
    for name, item in manifest["files"].items():
        need(isinstance(item, dict), "Component entry must be an object: " + str(name))
        fields(item, ("source",), ("identity",))
        need(item["source"] == name, "Component source path differs from manifest key: " + str(name))
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
    missing = [name for name, _ in roles if name not in aliases]
    need(not missing, "Missing peer component: " + ", ".join(missing))
    effective_manifest = {**manifest, "files": measured_files}
    basis = (COMPANIONS_ROOT, digest(canonical(measured_files)))
    if _MODULES is not None and _MODULES[0] == basis:
        return _MODULES[1], effective_manifest

    pkg_root = "_g60_peer_" + digest(str(COMPANIONS_ROOT).encode("utf-8"))[:16]
    subpackages = {
        pkg_root: COMPANIONS_ROOT / "workbench",
        pkg_root + ".core": COMPANIONS_ROOT / "workbench/core",
        pkg_root + ".controller": COMPANIONS_ROOT / "workbench/controller",
    }
    created = list(subpackages) + [f"{pkg_root}.{sub}.{name}" for name, sub in roles]
    prior = {key: sys.modules.get(key) for key in created}
    loaded = {}
    try:
        for pkg_name, pkg_dir in subpackages.items():
            pkg = ModuleType(pkg_name)
            pkg.__path__ = [str(pkg_dir)]
            pkg.__package__ = pkg_name
            sys.modules[pkg_name] = pkg
        for name, sub in roles:
            mod_name = f"{pkg_root}.{sub}.{name}"
            spec = importlib.util.spec_from_file_location(mod_name, aliases[name])
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
    _MODULES = (basis, loaded)
    return loaded, effective_manifest


def validate(config, request):
    fields(
        config,
        ("schema", "task", "sources", "corpus_root", "evidence_root", "model"),
        ("max_steps", "max_file_bytes", "max_cache_bytes", "receivers", "obligations"),
    )
    need(config["schema"] == CONFIG_SCHEMA and isinstance(config["task"], str), "Invalid worker configuration schema/task")
    need(
        isinstance(config["sources"], dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in config["sources"].items()),
        "sources must map strings to complete strings",
    )
    need(
        not {"peer-request", "peer-workbench-contract", "workbench-tool-contract"} & config["sources"].keys(),
        "Reserved entry source identity",
    )
    for name, default, minimum in (("max_steps", 12, 0), ("max_file_bytes", 2097152, 1), ("max_cache_bytes", 65536, 0)):
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
            and model["argv"]
            and all(isinstance(s, str) and s for s in model["argv"]),
            "model.argv must be a nonempty string array",
        )
        if "cwd" in model:
            absolute(model["cwd"], "model.cwd", directory=True)
    else:
        raise ValueError("model.kind must be mailbox or command")
    timeout = model.get("timeout_seconds", 900)
    need(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0, "Invalid model timeout")

    fields(
        request,
        (
            "schema", "run_id", "owner", "turn", "revision", "brief", "input",
            "state", "inbox", "peers", "workspace", "limits_remaining",
        ),
    )
    need(request["schema"] == REQUEST_SCHEMA and isinstance(request["run_id"], str) and request["run_id"], "Invalid peer request schema/run")
    need(identifier(request["owner"]), "Invalid request owner")
    need(integer(request["turn"], 1) and integer(request["revision"], 1), "Invalid request turn/revision")
    workspace = absolute(request["workspace"], "workspace", directory=True)
    corpus = absolute(config["corpus_root"], "corpus_root", directory=True)
    evidence = absolute(config["evidence_root"], "evidence_root")
    need(not evidence.is_relative_to(workspace), "Evidence must remain outside the editable workspace")
    need(not evidence.is_relative_to(corpus), "Evidence must remain outside the source corpus")
    need(not workspace.is_relative_to(corpus) and not corpus.is_relative_to(workspace), "Workspace and corpus must be separate")
    need(isinstance(request["inbox"], list), "Inbox must be an array")
    message_ids = set()
    for message in request["inbox"]:
        fields(message, ("id", "from", "to", "payload"))
        need(
            isinstance(message["id"], str)
            and message["id"]
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
    fields(limits, ("wall_seconds", "turns", "turn_seconds", "max_output_bytes", "max_artifact_bytes"))
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
    config_sha = digest(canonical(config))
    previous = request["state"]
    if request["revision"] == 1:
        need(previous is None, "First adapter turn requires null prior state")
    else:
        fields(
            previous,
            (
                "schema", "owner", "revision", "worker_config_sha256", "model_state",
                "continuing_duties", "completed_turns", "accounting", "last_turn",
            ),
        )
        need(
            previous["schema"] == STATE_SCHEMA
            and previous["owner"] == request["owner"]
            and previous["revision"] == request["revision"] - 1,
            "Prior adapter state belongs to another owner/revision",
        )
        need(previous["worker_config_sha256"] == config_sha, "Worker configuration changed across owner turns")
        need(canonical(previous["continuing_duties"]) == canonical(config.get("obligations", [])), "Continuing duty objects changed")
        need(
            integer(previous["completed_turns"], 1) and previous["completed_turns"] == request["revision"] - 1,
            "Prior completed-turn account is invalid",
        )
        fields(previous["accounting"], COUNT_KEYS)
        need(all(integer(previous["accounting"][k]) for k in COUNT_KEYS), "Prior callback/read counts are invalid")
    return workspace, corpus, evidence, config_sha


class PeerTools:
    def __init__(self, config, request, workbench, directory):
        self.config, self.request, self.workbench = config, request, workbench
        self.directory = directory
        self.staged = None
        self.reads = []
        self.max_file = config.get("max_file_bytes", 2097152)

    def context(self, arguments):
        fields(arguments, ())
        return {"request": detached(self.request)}

    def read(self, arguments):
        record = {
            "attempt": len(self.reads) + 1, "arguments": detached(arguments),
            "successful": False, "io_bytes": 0, "selected_body_bytes": 0,
        }

        def observed(path):
            data = path.read_bytes()
            record["io_bytes"] += len(data)
            return data

        try:
            fields(arguments, ("owner", "paths"))
            owner, paths = arguments["owner"], arguments["paths"]
            need(owner in self.request["peers"], "Peer is outside this launch-time snapshot")
            need(
                isinstance(paths, list)
                and paths
                and all(isinstance(p, str) for p in paths)
                and len(paths) == len(set(paths)),
                "paths must be a nonempty unique string array",
            )
            for p in paths:
                relative(p)
            peer = self.request["peers"][owner]
            revision = peer["revision"]
            need(revision > 0, "Peer has no captured contribution in this request")
            response_path = absolute(peer["return_path"], "peer.return_path", file=True)
            manifest_path = absolute(peer["manifest_path"], "peer.manifest_path", file=True)
            artifacts_dir = absolute(peer["artifacts_dir"], "peer.artifacts_dir", directory=True)
            response_body, manifest_body = observed(response_path), observed(manifest_path)
            response, manifest = decode_json(response_body), decode_json(manifest_body)
            need(
                response.get("schema") == RETURN_SCHEMA
                and response.get("owner") == owner
                and response.get("revision") == revision
                and isinstance(response.get("files"), dict),
                "Captured response identity mismatch",
            )
            need(isinstance(manifest, list), "Captured manifest must be an array")
            entries, decoded = {}, {}
            for item in manifest:
                fields(item, ("path", "bytes", "sha256", "owner", "revision"))
                name = item["path"]
                relative(name)
                need(
                    name not in entries
                    and item["owner"] == owner
                    and item["revision"] == revision
                    and integer(item["bytes"]),
                    "Captured manifest identity mismatch",
                )
                entries[name] = item
            need(
                set(entries) == set(response["files"]) and not path_conflicts(entries),
                "Captured response/manifest membership mismatch",
            )
            for name, value in response["files"].items():
                need(isinstance(value, str), "Captured returned file must be base64")
                body = base64.b64decode(value, validate=True)
                need(
                    identity(body) == {k: entries[name][k] for k in ("bytes", "sha256")},
                    "Captured returned bytes disagree with manifest: " + name,
                )
                decoded[name] = body
            result = {
                "owner": owner,
                "revision": revision,
                "response_identity": identity(response_body),
                "manifest_identity": identity(manifest_body),
                "files": {},
                "scope": "captured structurally accepted peer contribution; no semantic acceptance or latest-currentness claim",
            }
            for name in paths:
                need(name in entries, "Selected path is absent from the captured manifest: " + name)
                need(entries[name]["bytes"] <= self.max_file, "Selected peer body exceeds per-file bound: " + name)
                path = scoped_file(artifacts_dir, name)
                need(path.stat().st_size <= self.max_file, "Selected captured file exceeds per-file bound: " + name)
                body = observed(path)
                need(body == decoded[name], "Captured file body differs from accepted returned bytes: " + name)
                record["selected_body_bytes"] += len(body)
                item = identity(body)
                try:
                    item.update(encoding="utf-8", body=body.decode("utf-8"))
                except UnicodeDecodeError:
                    item.update(encoding="base64", base64=base64.b64encode(body).decode("ascii"))
                result["files"][name] = item
            record.update(
                successful=True,
                owner=owner,
                revision=revision,
                response_identity=result["response_identity"],
                manifest_identity=result["manifest_identity"],
                selected_files={name: identity(decoded[name]) for name in paths},
            )
            return result
        except Exception as exc:
            record["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            self.reads.append(record)
            write_json(self.directory / f"peer-read-{record['attempt']:04d}.json", record)

    def stage(self, arguments):
        fields(arguments, ("status", "state", "obligations", "ack", "send", "files"))
        status = arguments["status"]
        need(status in OWNER_STATUSES, "Invalid staged owner status")
        need(isinstance(arguments["obligations"], list), "Staged obligations must be an array")
        need(status != "complete" or not arguments["obligations"], "Complete owner cannot retain finite obligations")
        ack = arguments["ack"]
        need(
            isinstance(ack, list) and all(isinstance(x, str) for x in ack) and len(ack) == len(set(ack)),
            "ack must be a unique string array",
        )
        need(set(ack) <= {m["id"] for m in self.request["inbox"]}, "Cannot acknowledge a message absent from this request")
        sends = arguments["send"]
        need(isinstance(sends, list), "send must be an array")
        owners = set(self.request["peers"]) | {self.request["owner"]}
        seen = {}
        for message in sends:
            fields(message, ("id", "to", "payload"))
            need(identifier(message["id"]) and message["to"] in owners, "Invalid outgoing message identity/destination")
            body = canonical({"to": message["to"], "payload": message["payload"]})
            need(message["id"] not in seen or seen[message["id"]] == body, "Changed outgoing message under reused local identity")
            seen[message["id"]] = body
        names = arguments["files"]
        need(
            isinstance(names, list) and all(isinstance(x, str) for x in names) and len(names) == len(set(names)),
            "files must be a unique string array",
        )
        for name in names:
            relative(name)
        need(not path_conflicts(names), "Staged file/directory conflict")
        captured, pins = {}, {}
        total = 0
        for name in names:
            path = scoped_file(self.workbench.workspace, name)
            need(path.stat().st_size <= self.max_file, "Staged file exceeds per-file bound: " + name)
            body = path.read_bytes()
            need(len(body) <= self.max_file, "Staged file exceeds per-file bound: " + name)
            total += len(body)
            need(total <= self.request["limits_remaining"]["max_artifact_bytes"], "Staged files exceed current harness artifact allowance")
            captured[name], pins[name] = body, identity(body)
        self.staged = {"arguments": detached(arguments), "captured": captured, "pins": pins}
        return {
            "staged": True,
            "status": status,
            "finite_obligations": len(arguments["obligations"]),
            "acknowledgments": len(ack),
            "messages": len(sends),
            "files": pins,
            "scope": "candidate only; messages/acknowledgments take effect only through host acceptance of a successful return",
        }

    def finish(self, result, config_sha):
        need(result["status"] == "complete", "Model turn did not complete: " + result["status"])
        need(self.staged is not None, "Completed model turn has no staged peer return")
        staged = self.staged
        for name, body in staged["captured"].items():
            need(scoped_file(self.workbench.workspace, name).read_bytes() == body, "Selected workspace file changed after staging: " + name)
        previous = self.request["state"]
        accounting = {k: 0 for k in COUNT_KEYS} if previous is None else dict(previous["accounting"])
        accounting["model_calls"] += result["model_calls"]
        accounting["tool_calls"] += result["tool_calls"]
        accounting["peer_read_calls"] += sum(x["successful"] for x in self.reads)
        accounting["peer_read_bytes"] += sum(x["selected_body_bytes"] for x in self.reads if x["successful"])
        state = {
            "schema": STATE_SCHEMA,
            "owner": self.request["owner"],
            "revision": self.request["revision"],
            "worker_config_sha256": config_sha,
            "model_state": staged["arguments"]["state"],
            "continuing_duties": detached(self.config.get("obligations", [])),
            "completed_turns": 1 if previous is None else previous["completed_turns"] + 1,
            "accounting": accounting,
            "last_turn": {
                "evidence_dir": str(self.directory),
                "request_sha256": digest(canonical(self.request)),
                "model_status": result["status"],
                "scope": "local completed model turn; host admission is subsequent",
            },
        }
        return {
            "schema": RETURN_SCHEMA,
            "owner": self.request["owner"],
            "revision": self.request["revision"],
            "state": state,
            "status": staged["arguments"]["status"],
            "obligations": staged["arguments"]["obligations"],
            "ack": staged["arguments"]["ack"],
            "send": staged["arguments"]["send"],
            "files": {name: base64.b64encode(body).decode("ascii") for name, body in staged["captured"].items()},
        }


def run_peer_turn(config, request, *, model=None):
    """Run one fresh bounded model/tool turn and prepare its exact peer return."""
    config, request = detached(config), detached(request)
    workspace, corpus, evidence, config_sha = validate(config, request)
    need(model is None or callable(model), "Injected model must be callable")
    directory = evidence / request["owner"] / f"revision-{request['revision']:06d}"
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "config.json", config)
    write_json(directory / "request.json", request)
    started = time.monotonic()
    result, response, error, peer, workbench, source = None, None, None, None, None, None
    try:
        try:
            modules, manifest = components()
            contract = "\n\n".join(
                "SOURCE " + Path(name).name + "\n\n" + (HERE / name).read_text(encoding="utf-8")
                for name in CONTRACT_FILES
            )
            sources = dict(config["sources"])
            sources["peer-request"] = json.dumps(request, ensure_ascii=False, indent=2, allow_nan=False)
            sources["peer-workbench-contract"] = contract
            code_files = {
                name: identity((HERE / name).read_bytes())
                for name in ("peer_workbench.py", "peer_protocol.py", "COMPONENTS.json") + CONTRACT_FILES
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
                workbench.tool_contract, ensure_ascii=False, indent=2, allow_nan=False,
            )
            write_json(directory / "input-identities.json", {
                "schema": "peer-workbench-input-identities/1",
                "config_sha256": config_sha,
                "request_sha256": digest(canonical(request)),
                "adapter": code_files,
                "acquired": manifest,
                "entry_sources": {k: identity(v.encode("utf-8")) for k, v in sources.items()},
                "workbench_tool_contract_sha256": workbench.contract_sha256,
                "injected_model": model is not None,
                "continuing_duties": config.get("obligations", []),
            })
            peer = PeerTools(config, request, workbench, directory)
            tools = workbench.tools()
            tools.update(peer_context=peer.context, peer_read=peer.read, peer_stage_return=peer.stage)
            if model is None:
                transport = config["model"]
                timeout = transport.get("timeout_seconds", 900)
                if transport["kind"] == "mailbox":
                    model = modules["model_bridges"].MailboxModel(directory / "model", timeout)
                else:
                    model = modules["model_bridges"].CommandModel(
                        transport["argv"], directory / "model",
                        cwd=transport.get("cwd"), timeout_seconds=timeout,
                    )
            result = modules["agent_runtime"].run_agent(
                task=config["task"], sources=sources, model=model,
                tools=tools, max_steps=config.get("max_steps", 12),
            )
            write_json(directory / "run-result.json", result)
            response = peer.finish(result, config_sha)
            write_json(directory / "peer-response.json", response)
        except Exception as exc:
            response = None
            error = {"type": type(exc).__name__, "message": str(exc)}
            if result is None:
                result = {"status": "adapter_error", "model_calls": 0, "tool_calls": 0, "error": error, "trace": []}
                write_json(directory / "run-result.json", result)
        reads = peer.reads if peer is not None else []
        report = {
            "schema": "peer-workbench-result/1",
            "ok": response is not None,
            "owner": request["owner"],
            "revision": request["revision"],
            "model_status": result["status"],
            "model_calls": result["model_calls"],
            "tool_calls": result["tool_calls"],
            "peer_read_attempts": len(reads),
            "peer_read_calls": sum(x["successful"] for x in reads),
            "peer_read_bytes": sum(x["selected_body_bytes"] for x in reads if x["successful"]),
            "peer_read_io_bytes_all_attempts": sum(x["io_bytes"] for x in reads),
            "source_stats": workbench.source_service.stats() if workbench is not None else None,
            "accepted_artifacts": workbench.accepted if workbench is not None else [],
            "continuing_duties": config.get("obligations", []),
            "returned_file_bytes": sum(len(base64.b64decode(x)) for x in response["files"].values()) if response else 0,
            "response_identity": identity(canonical(response) + b"\n") if response else None,
            "per_turn_model_limit": config.get("max_steps", 12),
            "run_wide_model_limit_enforced": False,
            "elapsed_seconds": time.monotonic() - started,
            "error": error,
            "scope": "local model/tool observations and prepared peer return; harness admission and semantic whole-use receiving are subsequent",
            "provider_inference_calls": None,
            "provider_tokens": None,
            "provider_prices": None,
        }
        write_json(directory / "adapter-result.json", report)
        return {"ok": report["ok"], "response": response, "report": report, "evidence_dir": str(directory)}
    finally:
        if source is not None:
            source.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        result = run_peer_turn(decode_json(Path(args.config).read_bytes()), decode_json(sys.stdin.buffer.read()))
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(json.dumps({
            "status": "setup_rejection",
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }, allow_nan=False), file=sys.stderr)
        return 2
    if result["ok"]:
        sys.stdout.buffer.write(canonical(result["response"]) + b"\n")
        return 0
    print(json.dumps({
        "status": "worker_not_returned",
        "evidence_dir": result["evidence_dir"],
        "model_status": result["report"]["model_status"],
        "error": result["report"]["error"],
    }, allow_nan=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
