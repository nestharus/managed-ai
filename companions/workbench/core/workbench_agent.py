"""Source-grounded local workbench around the unchanged Study83 callable controller."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import time

try:
    from .config import DEFAULT_CONTROLLER_DIR, build_model_transport, load_controller
    from .io_utils import (
        BasisMismatchError,
        atomic_bytes,
        atomic_json,
        canonical_bytes as encode,
        is_reparse_or_symlink,
        loads_strict,
        pin_file as pin,
        sha256_bytes,
        sync_dir,
    )
    from .model_bridges import write_mailbox_response
except ImportError:
    from config import DEFAULT_CONTROLLER_DIR, build_model_transport, load_controller
    from io_utils import (
        BasisMismatchError,
        atomic_bytes,
        atomic_json,
        canonical_bytes as encode,
        is_reparse_or_symlink,
        loads_strict,
        pin_file as pin,
        sha256_bytes,
        sync_dir,
    )
    from model_bridges import write_mailbox_response


def inside(root, relative):
    """Reject escaping paths and symlinks; this is cooperative scoping, not an OS sandbox."""
    if not isinstance(relative, str) or not relative:
        raise ValueError("A nonempty relative path is required")
    logical = PurePosixPath(relative)
    if logical.is_absolute() or any(part in {"..", "."} for part in logical.parts) or "\\" in relative:
        raise ValueError("Path must be a clean relative POSIX path")
    root = Path(root).resolve()
    path = root
    for part in logical.parts:
        path = path / part
        if is_reparse_or_symlink(path):
            raise ValueError("Symlink or reparse point is outside this tool contract: " + relative)
    if not path.resolve().is_relative_to(root):
        raise ValueError("Path escapes the workspace")
    return path


class WorkbenchBasisMismatch(BasisMismatchError):
    """A continuation would mix different declared receiving inputs."""


class Workbench:
    """Host-configured source service, workspace operations and receiving commands."""

    def __init__(self, config, *, source_service=None, resume=False, _source_search=None):
        self.config = loads_strict(encode(config))
        self.workspace = Path(config["workspace"]).resolve()
        self.state = Path(config["state_dir"]).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        if self.state.is_relative_to(self.workspace):
            raise ValueError("state_dir must be outside model-editable workspace")
        self.task = config["task"]
        self.run_id = config.get("run_id", self.state.name)
        if not isinstance(self.task, str):
            raise TypeError("task must be a string")
        self.requirements = config.get("requirements", self.task)
        self.obligations = config.get("obligations", [])
        self.max_file_bytes = int(config.get("max_file_bytes", 2 * 1024 * 1024))
        if self.max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        self.initial_sources = config.get("sources", {})
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in self.initial_sources.items()):
            raise TypeError("sources must map string IDs to complete string bodies")
        if source_service is None:
            try:
                from .source_tools import SourceService
            except ImportError:
                from source_tools import SourceService
            source_state = Path(config.get("source_state_dir", self.state / "source-service")).resolve()
            if source_state.is_relative_to(self.workspace):
                raise ValueError("source_state_dir must be outside model-editable workspace")
            source_service = SourceService(
                config["corpus_root"], source_state,
                max_cache_bytes=config.get("max_cache_bytes", 65536),
                _search=_source_search,
            )
        self.source_service = source_service
        self.source_receipt_ids = []
        self.selection_receipt_ids = []
        self.receivers = config.get("receivers", {})
        self.receiver_input_pins = {}
        for name, receiver in self.receivers.items():
            argv = receiver["argv"]
            if not isinstance(argv, list) or not argv or any(not isinstance(x, str) for x in argv):
                raise ValueError("Receiver argv must be a nonempty string list")
            paths = receiver.get("inputs", [])
            self.receiver_input_pins[name] = {str(Path(p).resolve()): pin(p) for p in paths}
        self.check_count = 0
        self.accepted = []
        self.workspace_observations = {}
        self.tool_contract = {
            "source_discover": {"query": "string", "limit": "optional integer, default 8"},
            "source_read": {"paths": "array of relative corpus paths; complete bodies returned"},
            "source_inventory": {"refresh": "optional boolean; default false"},
            "source_refresh": {},
            "source_receipts": {},
            "source_stats": {},
            "workspace_list": {},
            "workspace_read": {"paths": "array of relative workspace paths"},
            "workspace_write": {"files": "mapping relative workspace path to complete UTF-8 text"},
            "receiving_check": {"name": "one of the host-supplied receiver names", "available": self.receivers},
            "accepted_artifacts": {},
            "accepted_read": {"check_id": "one recorded acceptance ID", "paths": "optional array of accepted relative paths"},
        }
        self.contract_sha256 = sha256_bytes(encode(self.tool_contract))
        self.basis = {
            "task_sha256": sha256_bytes(self.task.encode("utf-8")),
            "requirements": self.requirements,
            "requirements_sha256": sha256_bytes(encode(self.requirements)),
            "entry_sources": {
                key: {
                    "bytes": len(value.encode("utf-8")),
                    "sha256": sha256_bytes(value.encode("utf-8")),
                }
                for key, value in self.initial_sources.items()
            },
            "tool_contract_sha256": self.contract_sha256,
            "receiver_input_pins": self.receiver_input_pins,
            "obligations": self.obligations,
        }
        entry_path = self.state / "entry-basis.json"
        if resume:
            if not entry_path.is_file():
                raise ValueError("Cannot resume without the original workbench entry basis")
            if loads_strict(entry_path.read_bytes()) != self.basis:
                raise WorkbenchBasisMismatch("Current workbench inputs differ from the original entry basis")
        else:
            atomic_json(entry_path, self.basis, exclusive=True)

    def checkpoint(self):
        """Describe completed adapter state, excluding replaceable cache/index payloads."""
        source_state = self.source_service.checkpoint_state()
        return loads_strict(encode({
            "schema": "g60-workbench-checkpoint/2",
            "basis_sha256": sha256_bytes(encode(self.basis)),
            "source_receipt_ids": self.source_receipt_ids,
            "selection_receipt_ids": self.selection_receipt_ids,
            "check_count": self.check_count,
            "accepted": self.accepted,
            "workspace_observations": self.workspace_observations,
            "source_inventory_id": source_state["source_inventory_id"],
            "source_counters": source_state["source_counters"],
        }))

    def restore(self, checkpoint):
        """Restore recorded associations, never infer a new source observation."""
        value = loads_strict(encode(checkpoint))
        if not isinstance(value, dict) or value.get("schema") != "g60-workbench-checkpoint/2":
            raise ValueError("Unknown workbench checkpoint")
        if value.get("basis_sha256") != sha256_bytes(encode(self.basis)):
            raise WorkbenchBasisMismatch("Checkpoint belongs to another workbench basis")
        ledger = self.source_service.ledger
        for key, category in (("source_receipt_ids", "sources"),
                              ("selection_receipt_ids", "selections")):
            ids = value.get(key)
            if (not isinstance(ids, list) or any(not isinstance(item, str) for item in ids)
                    or len(ids) != len(set(ids)) or any(item not in ledger[category] for item in ids)):
                raise ValueError("Checkpoint references unavailable " + category + " receipts")
        count = value.get("check_count")
        if type(count) is not int or count < 0:
            raise ValueError("Invalid receiving counter")
        accepted = value.get("accepted")
        if not isinstance(accepted, list):
            raise ValueError("Invalid accepted-artifact checkpoint")
        for record in accepted:
            if not isinstance(record, dict) or record.get("basis") != self.basis:
                raise ValueError("Accepted artifact has a different receiving basis")
            if record.get("receiver") not in self.receivers:
                raise ValueError("Accepted artifact names an unavailable receiver")
            check_id = record.get("check_id", "")
            if (not isinstance(check_id, str) or not check_id.startswith("check-")
                    or not check_id[6:].isdigit() or not 1 <= int(check_id[6:]) <= count):
                raise ValueError("Accepted artifact has an invalid receiving identity")
            use = record.get("source_use_receipt", {})
            receipt_id = use.get("receipt_id")
            if receipt_id not in ledger["uses"] or {k: v for k, v in use.items() if k != "receipt_id"} != ledger["uses"][receipt_id]:
                raise ValueError("Accepted artifact use receipt is unavailable or changed")
            if record.get("artifacts") != use.get("artifacts"):
                raise ValueError("Accepted artifact manifest differs from its use receipt")
            objects = record.get("stored_objects")
            if not isinstance(objects, dict) or set(objects) != set(record["artifacts"]):
                raise ValueError("Accepted artifact bytes were not retained")
            for relative, expected in record["artifacts"].items():
                reference = objects[relative]
                if reference != "accepted-objects/" + expected["sha256"]:
                    raise ValueError("Unexpected accepted object location")
                if pin(inside(self.state, reference)) != expected:
                    raise ValueError("Stored accepted bytes differ from their manifest")
        observations = value.get("workspace_observations")
        if not isinstance(observations, dict):
            raise ValueError("Checkpoint has no tracked working-path observations")
        changed = []
        for relative, expected in observations.items():
            path = inside(self.workspace, relative)
            if expected is not None and (not isinstance(expected, dict)
                    or set(expected) != {"bytes", "sha256"}
                    or type(expected["bytes"]) is not int or expected["bytes"] < 0
                    or not isinstance(expected["sha256"], str) or len(expected["sha256"]) != 64):
                raise ValueError("Invalid working-path identity")
            if path.is_file():
                observed = pin(path)
            elif path.exists():
                observed = {"kind": "not-an-ordinary-file"}
            else:
                observed = None
            if observed != expected:
                changed.append({"path": relative, "expected": expected, "observed": observed})
        if changed:
            raise WorkbenchBasisMismatch(
                "Previously read, written or received working paths differ from the retained continuation.",
                code="workspace_changed",
                details={"changed_paths": changed},
            )

        def _selector_mismatch(message):
            raise WorkbenchBasisMismatch(message)

        # A recovered inventory preserves its old membership/hash guard. Its
        # index and cache remain absent; fresh discovery still pays preparation.
        self.source_service.restore_state(
            value.get("source_inventory_id"),
            value.get("source_counters"),
            on_selector_mismatch=_selector_mismatch,
        )
        self.source_receipt_ids = value["source_receipt_ids"]
        self.selection_receipt_ids = value["selection_receipt_ids"]
        self.check_count = count
        self.accepted = accepted
        self.workspace_observations = observations

    def _store_accepted(self, artifacts):
        objects = {}
        for relative, expected in artifacts.items():
            data = inside(self.workspace, relative).read_bytes()
            observed = {"bytes": len(data), "sha256": sha256_bytes(data)}
            if observed != expected:
                raise ValueError("Artifact changed before accepted-byte custody: " + relative)
            reference = "accepted-objects/" + expected["sha256"]
            destination = inside(self.state, reference)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if pin(destination) != expected:
                    raise ValueError("Conflicting stored accepted object")
            else:
                try:
                    atomic_bytes(destination, data, exclusive=True)
                except FileExistsError:
                    if pin(destination) != expected:
                        raise ValueError("Conflicting stored accepted object")
            objects[relative] = reference
        return objects

    def accepted_read(self, arguments):
        matches = [record for record in self.accepted if record["check_id"] == arguments.get("check_id")]
        if len(matches) != 1:
            raise ValueError("An exact recorded acceptance ID is required")
        record = matches[0]
        paths = arguments.get("paths", list(record["artifacts"]))
        if not isinstance(paths, list) or not paths or any(path not in record["artifacts"] for path in paths):
            raise ValueError("Select paths from that accepted artifact manifest")
        files = {}
        for relative in paths:
            expected = record["artifacts"][relative]
            if expected["bytes"] > self.max_file_bytes:
                raise ValueError("Accepted body exceeds the declared read bound: " + relative)
            data = inside(self.state, record["stored_objects"][relative]).read_bytes()
            if {"bytes": len(data), "sha256": sha256_bytes(data)} != expected:
                raise ValueError("Stored accepted object changed")
            files[relative] = {"body": data.decode("utf-8"), **expected}
        return {
            "check_id": record["check_id"],
            "files": files,
            "scope": "Exact historical accepted bytes; this does not accept different current workspace bytes.",
        }

    def _remember(self, result, bucket):
        # SourceService's contract returns one stable receipt ID per operation.
        receipt_id = result.get("receipt_id")
        if receipt_id and receipt_id not in bucket:
            bucket.append(receipt_id)
        return result

    def source_discover(self, arguments):
        result = self.source_service.discover(arguments["query"], limit=arguments.get("limit", 8))
        return self._remember(result, self.selection_receipt_ids)

    def source_read(self, arguments):
        paths = arguments.get("paths")
        if paths is None and "path" in arguments:
            paths = [arguments["path"]]
        if not isinstance(paths, list) or not paths or any(not isinstance(p, str) for p in paths):
            raise ValueError("paths must be a nonempty list of source paths")
        return {
            "sources": [
                self._remember(self.source_service.read(path), self.source_receipt_ids)
                for path in paths
            ]
        }

    def source_inventory(self, arguments):
        return self.source_service.inventory(refresh=arguments.get("refresh", False))

    def workspace_list(self, arguments):
        manifest = {}
        for path in sorted(self.workspace.rglob("*")):
            if path.is_symlink():
                continue
            if path.is_file():
                relative = path.relative_to(self.workspace).as_posix()
                inside(self.workspace, relative)
                manifest[relative] = pin(path)
        return {"files": manifest}

    def workspace_read(self, arguments):
        paths = arguments.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError("paths must be a nonempty list")
        result = {}
        for relative in paths:
            path = inside(self.workspace, relative)
            if path.stat().st_size > self.max_file_bytes:
                raise ValueError("File exceeds declared read bound: " + relative)
            data = path.read_bytes()
            sha = sha256_bytes(data)
            result[relative] = {"body": data.decode("utf-8"), "bytes": len(data), "sha256": sha}
            self.workspace_observations[relative] = {"bytes": len(data), "sha256": sha}
        return {"files": result}

    def workspace_write(self, arguments):
        files = arguments.get("files")
        if not isinstance(files, dict) or not files or any(not isinstance(x, str) for x in files.values()):
            raise ValueError("files must be a nonempty mapping of paths to text")
        prepared = []
        for relative, body in files.items():
            path = inside(self.workspace, relative)
            data = body.encode("utf-8")
            if len(data) > self.max_file_bytes:
                raise ValueError("File exceeds declared write bound: " + relative)
            if path.exists() and not path.is_file():
                raise ValueError("Target is not an ordinary file: " + relative)
            prepared.append((relative, path, data))
        result = {}
        for relative, path, data in prepared:
            atomic_bytes(path, data, exclusive=False)
            result[relative] = pin(path)
            self.workspace_observations[relative] = result[relative]
        return {"written": result, "scope": "Each file is atomically replaced; a multi-file write is not a transaction."}

    def receiving_check(self, arguments):
        name = arguments["name"]
        if name not in self.receivers:
            raise ValueError("No such configured receiving command")
        receiver = self.receivers[name]
        for path, expected in self.receiver_input_pins[name].items():
            if pin(path) != expected:
                raise ValueError("Configured receiver input changed: " + path)
        artifacts = receiver.get("artifacts", [])
        before = {relative: pin(inside(self.workspace, relative)) for relative in artifacts}
        self.workspace_observations.update(before)
        self.check_count += 1
        check_id = f"check-{self.check_count:04d}"
        directory = self.state / "receiving" / check_id
        directory.mkdir(parents=True)
        argv = [part.replace("{workspace}", str(self.workspace)).replace("{receiving_dir}", str(directory))
                for part in receiver["argv"]]
        record = {
            "id": check_id, "name": name, "argv": argv, "cwd": str(self.workspace),
            "basis": self.basis, "source_receipt_ids": list(self.source_receipt_ids),
            "selection_receipt_ids": list(self.selection_receipt_ids), "artifacts_before": before,
        }
        atomic_json(directory / "invocation.json", record, exclusive=True)
        started = time.monotonic()
        try:
            with (directory / "stdout.txt").open("xb") as out, (directory / "stderr.txt").open("xb") as err:
                result = subprocess.run(
                    argv, cwd=self.workspace, stdout=out, stderr=err,
                    timeout=receiver.get("timeout_seconds", 60), check=False,
                )
                out.flush()
                os.fsync(out.fileno())
                err.flush()
                os.fsync(err.fileno())
            sync_dir(directory)
            record["returncode"] = result.returncode
        except Exception as exc:
            record["returncode"] = None
            record["error"] = {"type": type(exc).__name__, "message": str(exc)}
        record["elapsed_seconds"] = time.monotonic() - started
        after = {
            relative: pin(inside(self.workspace, relative)) for relative in artifacts
            if inside(self.workspace, relative).is_file()
        }
        record["artifacts_after"] = after
        for relative in artifacts:
            self.workspace_observations[relative] = after.get(relative)
        record["accepted"] = record["returncode"] == 0 and before == after and bool(artifacts)
        record["acceptance_scope"] = "The named configured receiver returned zero against unchanged named artifact bytes; semantic adequacy remains the receiver's contract."
        for stream in ["stdout", "stderr"]:
            path = directory / (stream + ".txt")
            data = path.read_bytes() if path.exists() else b""
            record[stream + "_identity"] = {"bytes": len(data), "sha256": sha256_bytes(data)}
            record[stream] = data[:32768].decode("utf-8", errors="replace")
            record[stream + "_truncated"] = len(data) > 32768
        if record["accepted"]:
            stored_objects = self._store_accepted(after)
            use = self.source_service.record_use(
                use_id=self.run_id + "/" + check_id, task=self.task, requirements=self.requirements,
                source_receipt_ids=list(self.source_receipt_ids),
                selection_receipt_ids=list(self.selection_receipt_ids),
                artifacts=after, obligations=self.obligations,
            )
            record["source_use_receipt"] = use
            self.accepted.append({
                "check_id": check_id, "receiver": name, "artifacts": after,
                "basis": self.basis, "source_use_receipt": use,
                "stored_objects": stored_objects,
            })
            atomic_json(self.state / "accepted-artifacts.json", self.accepted)
        atomic_json(directory / "result.json", record, exclusive=True)
        return record

    def tools(self):
        return {
            "source_discover": self.source_discover,
            "source_read": self.source_read,
            "source_inventory": self.source_inventory,
            "source_refresh": lambda args: self.source_service.refresh(),
            "source_receipts": lambda args: self.source_service.receipts(),
            "source_stats": lambda args: self.source_service.stats(),
            "workspace_list": self.workspace_list,
            "workspace_read": self.workspace_read,
            "workspace_write": self.workspace_write,
            "receiving_check": self.receiving_check,
            "accepted_artifacts": lambda args: {"accepted": self.accepted},
            "accepted_read": self.accepted_read,
        }


def run(config):
    controller = Path(config.get("controller_dir", DEFAULT_CONTROLLER_DIR)).resolve()
    run_agent, _ = load_controller(controller)
    workbench = Workbench(config)
    try:
        model_config = config["model"]
        model = build_model_transport(model_config)
        sources = dict(workbench.initial_sources)
        if "workbench-tool-contract" in sources:
            raise ValueError("Reserved entry source ID: workbench-tool-contract")
        sources["workbench-tool-contract"] = json.dumps(
            workbench.tool_contract, ensure_ascii=False, indent=2, allow_nan=False,
        )
        started = time.monotonic()
        result = run_agent(
            task=workbench.task, sources=sources, model=model,
            tools=workbench.tools(), max_steps=config.get("max_steps", 12),
        )
        result["workbench"] = {
            "elapsed_seconds": time.monotonic() - started,
            "controller": {name: pin(controller / name) for name in ["agent_runtime.py", "evidence_support.py"]},
            "basis": workbench.basis,
            "source_stats": workbench.source_service.stats(),
            "accepted_artifacts": workbench.accepted,
            "transport": model_config["kind"],
            "provider_inference_calls": None,
            "provider_tokens": None,
            "provider_prices": None,
        }
        atomic_json(workbench.state / "source-receipts.json", workbench.source_service.receipts(), exclusive=True)
        atomic_json(workbench.state / "run-result.json", result, exclusive=True)
        return result
    finally:
        workbench.source_service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("run")
    start.add_argument("--config", required=True)
    response = commands.add_parser("respond")
    response.add_argument("--mailbox", required=True)
    response.add_argument("--step", required=True, type=int)
    response.add_argument("--action", required=True, help="Path to the actual model-produced action JSON")
    response.add_argument("--producer", default="host-supplied model participant")
    args = parser.parse_args()
    if args.command == "respond":
        write_mailbox_response(args.mailbox, args.step, loads_strict(Path(args.action).read_bytes()), producer=args.producer)
        print(json.dumps({"delivered": True, "step": args.step}, allow_nan=False))
        return 0
    result = run(loads_strict(Path(args.config).read_bytes()))
    print(json.dumps({
        "status": result["status"],
        "model_calls": result["model_calls"],
        "tool_calls": result["tool_calls"],
        "accepted_artifacts": len(result["workbench"]["accepted_artifacts"]),
    }, allow_nan=False))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
