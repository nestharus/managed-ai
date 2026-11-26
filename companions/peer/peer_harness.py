#!/usr/bin/env python3
"""Small cooperative subprocess/inbox/checkpoint harness; see docs/PROTOCOL.md."""
from __future__ import annotations

import argparse
import base64
import binascii
from collections import deque
import datetime as dt
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

try:
    import resource
except ImportError:
    resource = None

try:
    from .peer_protocol import (
        HARNESS_CONFIG_SCHEMA,
        HARNESS_RESULT_SCHEMA,
        OWNER_STATUSES,
        PEER_ENTRY_FIELDS,
        PROCESS_SCHEMA,
        REQUEST_SCHEMA,
        RETURN_SCHEMA,
        atomic_write_bytes,
        atomic_write_json,
        canonical,
        digest,
        fields,
        identifier,
        loads_strict as load_json,
        path_conflicts,
        positive_number,
        target_path,
    )
except ImportError:
    from peer_protocol import (
        HARNESS_CONFIG_SCHEMA,
        HARNESS_RESULT_SCHEMA,
        OWNER_STATUSES,
        PEER_ENTRY_FIELDS,
        PROCESS_SCHEMA,
        REQUEST_SCHEMA,
        RETURN_SCHEMA,
        atomic_write_bytes,
        atomic_write_json,
        canonical,
        digest,
        fields,
        identifier,
        loads_strict as load_json,
        path_conflicts,
        positive_number,
        target_path,
    )


VERSION = "study83-peer-harness/1"
_WIN_MINIMAL_ENV_KEYS = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "PATH")


class Rejection(Exception):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path, value):
    atomic_write_json(path, value, indent=2)


def _child_cpu_times():
    if resource is None:
        return 0.0, 0.0
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return float(usage.ru_utime), float(usage.ru_stime)


def _worker_environment(extra_env):
    if os.name == "nt":
        env = {key: os.environ[key] for key in _WIN_MINIMAL_ENV_KEYS if key in os.environ}
        env.setdefault("PATH", os.defpath)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(extra_env)
        return env
    return {"PATH": os.defpath, "LANG": "C.UTF-8", **extra_env}


def _env_policy_description():
    if os.name == "nt":
        return "Minimal Windows OS keys + PYTHONUTF8=1; explicit worker env only"
    return "PATH=os.defpath; LANG=C.UTF-8; explicit worker env only"


def _pipe_reader(item, channel, stream, events):
    fd = stream.fileno()
    try:
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            events.put((item, channel, chunk))
    finally:
        events.put((item, channel, None))


def validate_config(config):
    fields(config, ("schema", "run_id", "brief", "workers", "limits"))
    if config["schema"] != HARNESS_CONFIG_SCHEMA or not isinstance(config["run_id"], str) or not config["run_id"]:
        raise ValueError("Invalid schema or run_id")
    if not isinstance(config["workers"], list) or not config["workers"]:
        raise ValueError("workers must be a nonempty array")
    names = set()
    for worker in config["workers"]:
        fields(worker, ("owner", "command", "input"), ("env",))
        if not identifier(worker["owner"]) or worker["owner"] in names:
            raise ValueError("Invalid or duplicate owner")
        names.add(worker["owner"])
        command = worker["command"]
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError("command must be a nonempty string array")
        environment = worker.get("env", {})
        if not isinstance(environment, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items()):
            raise ValueError("env must map strings to strings")
    integers = ("parallelism", "max_turns", "max_output_bytes", "max_artifact_bytes")
    numbers = ("wall_seconds", "turn_seconds", "terminate_grace_seconds")
    fields(config["limits"], integers + numbers)
    if any(not positive_number(config["limits"][key], True) for key in integers):
        raise ValueError("Integer limits must be positive integers")
    if any(not positive_number(config["limits"][key]) for key in numbers):
        raise ValueError("Time limits must be positive finite numbers")


class Harness:
    def __init__(self, config, config_bytes, root):
        self.config, self.limits, self.root = config, config["limits"], Path(root).resolve()
        self.start = time.monotonic()
        self.cpu_start = _child_cpu_times()
        self.deadline = self.start + self.limits["wall_seconds"]
        self.cancel_requested = False
        self.reason = self.error = None
        self.artifact = None
        self.turn_count = 0
        self.pipe_events = queue.Queue()
        self.active, self.messages = {}, {}
        self.process_records = []
        self.ready, self.queued = deque(), set()
        self.owners = {}
        self.events = (self.root / "events.jsonl").open("a", encoding="utf-8", newline="\n")
        atomic_write_bytes(self.root / "config.json", config_bytes, exclusive=True)
        for directory in ("turns", "returns", "workspaces"):
            (self.root / directory).mkdir()
        self.identity = {
            "name": VERSION,
            "source_sha256": digest(Path(__file__).read_bytes()),
            "config_sha256": digest(config_bytes),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "environment": _env_policy_description(),
        }
        write_json(self.root / "harness-identity.json", self.identity)
        for worker in config["workers"]:
            owner = worker["owner"]
            (self.root / "workspaces" / owner).mkdir()
            (self.root / "returns" / owner).mkdir()
            self.owners[owner] = {
                "worker": worker, "revision": 0, "status": "continue", "state": None,
                "obligations": [], "pending": [], "return_path": None,
                "manifest_path": None, "artifacts_dir": None, "files": {},
            }
            self.enqueue(owner)
        self.event("run_started", roster=list(self.owners))

    def event(self, event, **values):
        self.events.write(canonical({"event": event, "at_utc": utc(), **values}).decode("utf-8") + "\n")
        self.events.flush()
        os.fsync(self.events.fileno())

    def fail(self, reason, error):
        if self.reason is None:
            self.reason, self.error = reason, str(error)
            self.event("run_stopping", reason=reason, error=str(error))

    def enqueue(self, owner):
        if owner not in self.queued and owner not in self.active:
            self.ready.append(owner)
            self.queued.add(owner)

    def peers(self, owner):
        return {
            name: {key: value[key] for key in PEER_ENTRY_FIELDS}
            for name, value in self.owners.items()
            if name != owner
        }

    def launch(self, owner):
        state = self.owners[owner]
        self.turn_count += 1
        turn = self.turn_count
        directory = self.root / "turns" / f"{turn:06d}-{owner}"
        directory.mkdir()
        workspace = self.root / "workspaces" / owner
        request = {
            "schema": REQUEST_SCHEMA,
            "run_id": self.config["run_id"],
            "owner": owner,
            "turn": turn,
            "revision": state["revision"] + 1,
            "brief": self.config["brief"],
            "input": state["worker"]["input"],
            "state": state["state"],
            "inbox": [
                {key: self.messages[mid][key] for key in ("id", "from", "to", "payload")}
                for mid in state["pending"]
            ],
            "peers": self.peers(owner),
            "workspace": str(workspace),
            "limits_remaining": {
                "wall_seconds": max(0.0, self.deadline - time.monotonic()),
                "turns": self.limits["max_turns"] - turn,
                **{key: self.limits[key] for key in ("turn_seconds", "max_output_bytes", "max_artifact_bytes")},
            },
        }
        write_json(directory / "request.json", request)
        item = {
            "owner": owner, "turn": turn, "request": request, "directory": directory,
            "started": time.monotonic(), "started_at_utc": utc(), "process": None,
            "streams": {}, "threads": [], "captures": {},
            "sizes": {"stdout": 0, "stderr": 0}, "observed": 0, "truncated": False,
            "cleanup": {"term_sent": False, "kill_sent": False, "streams_closed_by_host": False},
        }
        item["deadline"] = item["started"] + self.limits["turn_seconds"]
        for channel in ("stdout", "stderr"):
            item["captures"][channel] = (directory / (channel + ".bin")).open("wb")
        environment = _worker_environment(state["worker"].get("env", {}))
        popen_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": workspace,
            "env": environment,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        try:
            with (directory / "request.json").open("rb") as incoming:
                process = subprocess.Popen(state["worker"]["command"], stdin=incoming, **popen_kwargs)
            item["process"] = process
            for channel in ("stdout", "stderr"):
                stream = getattr(process, channel)
                item["streams"][channel] = stream
                reader = threading.Thread(
                    target=_pipe_reader,
                    args=(item, channel, stream, self.pipe_events),
                    daemon=True,
                )
                reader.start()
                item["threads"].append(reader)
            self.active[owner] = item
            self.event(
                "turn_launched", owner=owner, turn=turn, revision=request["revision"],
                pid=process.pid, delivered=[message["id"] for message in request["inbox"]],
            )
        except OSError as exc:
            self.record_process(item, "launch_error", str(exc))
            self.fail("launch_error", f"{owner}: {exc}")

    def close_channel(self, item, channel):
        stream = item["streams"].pop(channel, None)
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def _consume_pipe_event(self, item, channel, data):
        if data is None:
            self.close_channel(item, channel)
            return
        if channel not in item["streams"]:
            return
        item["observed"] += len(data)
        room = max(0, self.limits["max_output_bytes"] - sum(item["sizes"].values()))
        kept = data[:room]
        if kept:
            item["captures"][channel].write(kept)
            item["sizes"][channel] += len(kept)
        if len(kept) != len(data):
            item["truncated"] = True
            self.fail("output_limit", f"{item['owner']} turn {item['turn']} exceeded retained output bound")

    def pump(self, timeout=0.0):
        try:
            if timeout > 0:
                event = self.pipe_events.get(timeout=timeout)
            else:
                event = self.pipe_events.get_nowait()
        except queue.Empty:
            return
        self._consume_pipe_event(*event)
        while True:
            try:
                event = self.pipe_events.get_nowait()
            except queue.Empty:
                break
            self._consume_pipe_event(*event)

    @staticmethod
    def group_exists(item):
        process = item["process"]
        if process is None:
            return False
        if os.name == "nt":
            return process.poll() is None
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    @staticmethod
    def signal_group(item, kind):
        process = item["process"]
        if process is None:
            return
        if os.name == "nt":
            if kind == "term":
                if process.poll() is None:
                    try:
                        process.terminate()
                        item["cleanup"]["term_sent"] = True
                    except OSError:
                        pass
            else:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        pass
                item["cleanup"]["kill_sent"] = True
            return
        sig = signal.SIGTERM if kind == "term" else signal.SIGKILL
        try:
            os.killpg(process.pid, sig)
            item["cleanup"]["term_sent" if kind == "term" else "kill_sent"] = True
        except ProcessLookupError:
            pass

    def finish_process(self, item, reason):
        process = item["process"]
        if os.name == "nt":
            if reason != "completed" or process.poll() is None:
                self.signal_group(item, "term")
        else:
            # Cleanup occurs even after normal leader exit: descendants may retain pipes.
            self.signal_group(item, "term")
        until = time.monotonic() + self.limits["terminate_grace_seconds"]
        while time.monotonic() < until:
            self.pump(0)
            process.poll()
            if not self.group_exists(item) and not item["streams"]:
                break
            self.pump(min(0.01, max(0.0, until - time.monotonic())))
        if self.group_exists(item) or (os.name == "nt" and item["streams"]):
            self.signal_group(item, "kill")
        try:
            process.wait(timeout=self.limits["terminate_grace_seconds"])
        except subprocess.TimeoutExpired:
            self.fail("harness_error", "Local process did not reap after forced termination")
        drain_until = time.monotonic() + min(0.15, self.limits["terminate_grace_seconds"])
        while item["streams"] and time.monotonic() < drain_until:
            self.pump(0.005)
        if item["streams"]:
            item["cleanup"]["streams_closed_by_host"] = True
            item["truncated"] = True
            for channel in list(item["streams"]):
                self.close_channel(item, channel)
        for reader in item["threads"]:
            reader.join(timeout=0.05)
        if reason == "completed" and item["truncated"]:
            reason = "output_limit"
            self.fail("output_limit", "Worker output was truncated during process cleanup")
        self.record_process(item, reason)

    def record_process(self, item, reason, error=None):
        for capture in item["captures"].values():
            capture.flush()
            os.fsync(capture.fileno())
            capture.close()
        process = item["process"]
        record = {
            "schema": PROCESS_SCHEMA,
            "owner": item["owner"],
            "turn": item["turn"],
            "revision": item["request"]["revision"],
            "pid": process.pid if process else None,
            "process_group": process.pid if process else None,
            "started_at_utc": item["started_at_utc"],
            "ended_at_utc": utc(),
            "elapsed_seconds": time.monotonic() - item["started"],
            "returncode": process.returncode if process else None,
            "done_reason": reason,
            "stdout_bytes": item["sizes"]["stdout"],
            "stderr_bytes": item["sizes"]["stderr"],
            "observed_output_bytes": item["observed"],
            "output_truncated": item["truncated"],
            "group_cleanup": item["cleanup"],
            "error": error,
        }
        write_json(item["directory"] / "process.json", record)
        self.process_records.append(record)
        self.event(
            "turn_exited", owner=item["owner"], turn=item["turn"],
            reason=reason, returncode=record["returncode"],
        )

    def _validate_return_messages(self, item, response):
        ack = response["ack"]
        if not isinstance(ack, list) or any(not isinstance(x, str) for x in ack) or len(ack) != len(set(ack)):
            raise ValueError("ack must be a unique string array")
        exposed = {message["id"] for message in item["request"]["inbox"]}
        if set(ack) - exposed:
            raise ValueError("Acknowledgement names a message absent from this request")
        if not isinstance(response["send"], list):
            raise ValueError("send must be an array")
        sends = {}
        for message in response["send"]:
            fields(message, ("id", "to", "payload"))
            if not identifier(message["id"]) or message["to"] not in self.owners:
                raise ValueError("Invalid message ID or recipient")
            mid = item["owner"] + ":" + message["id"]
            identity = canonical({"to": message["to"], "payload": message["payload"]})
            existing = sends.get(mid, self.messages.get(mid))
            if existing is not None and existing["identity"] != identity:
                raise ValueError("Sender-local message ID reused with changed recipient/payload")
            sends[mid] = {
                "id": mid, "from": item["owner"], "to": message["to"],
                "payload": message["payload"], "identity": identity, "acknowledged": False,
            }
        return sends

    def _validate_return_files(self, response):
        if not isinstance(response["files"], dict):
            raise ValueError("files must be an object")
        captured = {}
        for path, encoded in response["files"].items():
            target_path(path)
            if not isinstance(encoded, str):
                raise ValueError("File contents must be base64 strings")
            captured[path] = base64.b64decode(encoded, validate=True)
        if path_conflicts(captured):
            raise ValueError("Owner return contains a file/directory path conflict")
        if sum(len(value) for value in captured.values()) > self.limits["max_artifact_bytes"]:
            raise Rejection("artifact_limit", "Owner checkpoint exceeds artifact byte limit")
        return captured

    def validate_return(self, item):
        raw = (item["directory"] / "stdout.bin").read_bytes()
        try:
            response = load_json(raw)
            fields(
                response,
                ("schema", "owner", "revision", "state", "status", "obligations", "ack", "send", "files"),
                ("reported_usage",),
            )
            if response["schema"] != RETURN_SCHEMA or response["owner"] != item["owner"]:
                raise ValueError("Response schema/owner mismatch")
            if type(response["revision"]) is not int or response["revision"] != item["request"]["revision"]:
                raise ValueError("Response revision mismatch")
            if response["status"] not in OWNER_STATUSES:
                raise ValueError("Invalid owner status")
            if not isinstance(response["obligations"], list):
                raise ValueError("obligations must be an array")
            if response["status"] == "complete" and response["obligations"]:
                raise ValueError("A complete owner cannot retain declared obligations")
            sends = self._validate_return_messages(item, response)
            captured = self._validate_return_files(response)
            return raw, response, sends, captured
        except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error) as exc:
            raise Rejection("protocol_error", f"{item['owner']} turn {item['turn']}: {exc}") from exc

    def accept_return(self, item):
        raw, response, sends, captured = self.validate_return(item)
        owner = item["owner"]
        state = self.owners[owner]
        revision = response["revision"]
        checkpoint = self.root / "returns" / owner / f"{revision:06d}"
        checkpoint.mkdir()
        file_root = checkpoint / "files"
        file_root.mkdir()
        atomic_write_bytes(checkpoint / "response.json", raw, exclusive=True)
        manifest = []
        file_root_resolved = file_root.resolve()
        for path, value in sorted(captured.items()):
            destination = file_root.joinpath(*target_path(path))
            if not destination.resolve().is_relative_to(file_root_resolved):
                raise Rejection("protocol_error", "Target path escapes checkpoint directory: " + path)
            atomic_write_bytes(destination, value, exclusive=True)
            manifest.append({
                "path": path, "bytes": len(value), "sha256": digest(value),
                "owner": owner, "revision": revision,
            })
        manifest_path = checkpoint / "manifest.json"
        write_json(manifest_path, manifest)
        # The accepted bytes are retained in host memory; later peer filesystem
        # mutation cannot silently change the bytes selected for submission.
        for key in ("revision", "status", "state", "obligations"):
            state[key] = response[key]
        state.update(
            return_path=str(checkpoint / "response.json"),
            manifest_path=str(manifest_path),
            artifacts_dir=str(file_root),
            files=captured,
        )
        for mid in response["ack"]:
            state["pending"].remove(mid)
            self.messages[mid]["acknowledged"] = True
            self.event("message_acknowledged", id=mid, owner=owner, turn=item["turn"])
        # No scheduling/closure check occurs between discharging the return and
        # registering all descendants in its fully validated outbound array.
        for mid, message in sends.items():
            if mid in self.messages:
                self.event("duplicate_send", id=mid, owner=owner, turn=item["turn"])
                continue
            self.messages[mid] = message
            recipient = message["to"]
            self.owners[recipient]["pending"].append(mid)
            self.enqueue(recipient)
            self.event("message_admitted", id=mid, sender=owner, recipient=recipient, turn=item["turn"])
        self.event(
            "return_accepted", owner=owner, turn=item["turn"], revision=revision,
            status=state["status"], obligations=len(state["obligations"]), checkpoint=str(checkpoint),
        )
        if state["status"] == "continue" or state["pending"]:
            self.enqueue(owner)

    def complete(self):
        return (
            not self.active
            and all(
                state["revision"] > 0
                and state["status"] == "complete"
                and not state["pending"]
                and not state["obligations"]
                for state in self.owners.values()
            )
        )

    def submit(self):
        files, manifest = {}, []
        for owner, state in self.owners.items():
            for path, value in state["files"].items():
                if path in files:
                    raise Rejection("artifact_conflict", "Multiple owners claim target path: " + path)
                files[path] = value
                manifest.append({
                    "path": path, "bytes": len(value), "sha256": digest(value),
                    "owner": owner, "revision": state["revision"],
                })
        if path_conflicts(files):
            raise Rejection("artifact_conflict", "Cross-owner file/directory target conflict")
        if sum(len(value) for value in files.values()) > self.limits["max_artifact_bytes"]:
            raise Rejection("artifact_limit", "Combined submission exceeds artifact byte limit")
        self.event("epoch_closed", revisions={owner: state["revision"] for owner, state in self.owners.items()})
        staging = self.root / "submission-staging"
        staging.mkdir()
        staging_resolved = staging.resolve()
        for path, value in sorted(files.items()):
            destination = staging.joinpath(*target_path(path))
            if not destination.resolve().is_relative_to(staging_resolved):
                raise Rejection("protocol_error", "Target path escapes submission staging directory: " + path)
            atomic_write_bytes(destination, value, exclusive=True)
        destination = self.root / "submitted"
        staging.rename(destination)
        manifest.sort(key=lambda entry: entry["path"])
        write_json(self.root / "submitted-manifest.json", manifest)
        self.artifact = {
            "root": str(destination),
            "manifest": manifest,
            "manifest_sha256": digest(canonical(manifest)),
        }
        self.reason = "submitted"

    def _poll_active_workers(self):
        for owner, item in list(self.active.items()):
            if self.reason is not None:
                break
            if time.monotonic() >= item["deadline"]:
                self.fail("worker_timeout", f"{owner} turn {item['turn']} exceeded per-turn allowance")
                break
            if item["process"].poll() is None:
                continue
            reason = "completed" if item["process"].returncode == 0 else "worker_failed"
            self.finish_process(item, reason)
            del self.active[owner]
            if self.reason is None and time.monotonic() >= self.deadline:
                self.fail("wall_limit", "Run deadline reached before return admission")
            elif self.reason is None and time.monotonic() >= item["deadline"]:
                self.fail("worker_timeout", "Turn deadline reached before return admission")
            if self.reason is None:
                if reason != "completed":
                    self.fail("worker_failed", f"{owner} exited with code {item['process'].returncode}")
                else:
                    self.accept_return(item)

    def _build_run_result(self):
        cpu_user, cpu_sys = _child_cpu_times()
        return {
            "schema": HARNESS_RESULT_SCHEMA,
            "run_id": self.config["run_id"],
            "submitted": self.reason == "submitted",
            "done_reason": self.reason,
            "error": self.error,
            "elapsed_seconds": time.monotonic() - self.start,
            "total_turns": self.turn_count,
            "owners": {
                owner: {
                    key: state[key]
                    for key in ("revision", "status", "obligations", "return_path", "manifest_path", "artifacts_dir")
                }
                for owner, state in self.owners.items()
            },
            "messages": {
                "admitted": len(self.messages),
                "acknowledged": sum(message["acknowledged"] for message in self.messages.values()),
                "pending": [mid for mid, message in self.messages.items() if not message["acknowledged"]],
            },
            "artifact": self.artifact,
            "harness_identity": self.identity,
            "local_resources": {
                "child_user_cpu_seconds": max(0.0, cpu_user - self.cpu_start[0]),
                "child_system_cpu_seconds": max(0.0, cpu_sys - self.cpu_start[1]),
                "process_wall_seconds_sum": sum(record["elapsed_seconds"] for record in self.process_records),
                "captured_stdout_bytes": sum(record["stdout_bytes"] for record in self.process_records),
                "captured_stderr_bytes": sum(record["stderr_bytes"] for record in self.process_records),
                "observed_output_bytes": sum(record["observed_output_bytes"] for record in self.process_records),
                "remote_model_usage": "unmeasured; worker reported_usage remains unverified in exact returns",
                "limits": self.limits,
            },
        }

    def run(self):
        old_signals = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                old_signals[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda *_: setattr(self, "cancel_requested", True))
            except (OSError, ValueError):
                pass
        try:
            while self.reason is None:
                if self.cancel_requested or (self.root / "CANCEL").exists():
                    self.fail("cancelled", "Local cancellation requested")
                    break
                if time.monotonic() >= self.deadline:
                    self.fail("wall_limit", "Run wall-clock allowance exhausted")
                    break
                self.pump()
                self._poll_active_workers()
                if self.reason is not None:
                    break
                if self.complete():
                    self.submit()
                    break
                while self.ready and len(self.active) < self.limits["parallelism"] and self.reason is None:
                    if self.turn_count >= self.limits["max_turns"]:
                        self.fail("turn_limit", "Total invocation allowance exhausted")
                        break
                    owner = self.ready.popleft()
                    self.queued.remove(owner)
                    self.launch(owner)
                if self.reason is None and not self.active and not self.ready:
                    self.fail("stalled", "No runnable owner; selected completion obligations remain")
                if self.reason is None:
                    self.pump(0.02)
        except Rejection as exc:
            self.fail(exc.reason, str(exc))
        except Exception as exc:
            self.fail("harness_error", f"{type(exc).__name__}: {exc}")
        finally:
            for item in self.active.values():
                self.signal_group(item, "term")
            for item in list(self.active.values()):
                self.finish_process(item, self.reason or "harness_error")
            self.active.clear()
            for sig, handler in old_signals.items():
                try:
                    signal.signal(sig, handler)
                except (OSError, ValueError):
                    pass
        result = self._build_run_result()
        write_json(self.root / "result.json", result)
        self.event("run_finished", reason=self.reason, submitted=result["submitted"], total_turns=self.turn_count)
        self.events.close()
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    try:
        config_bytes = Path(args.config).read_bytes()
        config = load_json(config_bytes)
        validate_config(config)
        root = Path(args.run_dir).resolve()
        root.mkdir(parents=True, exist_ok=False)
        harness = Harness(config, config_bytes, root)
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        print(json.dumps({"done_reason": "setup_rejection", "error": str(exc)}, allow_nan=False), file=sys.stderr)
        return 2
    result = harness.run()
    print(json.dumps({
        "result": str(root / "result.json"),
        "submitted": result["submitted"],
        "done_reason": result["done_reason"],
    }, allow_nan=False))
    return 0 if result["submitted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
