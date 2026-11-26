#!/usr/bin/env python3
"""Durable one-owner collector for explicitly admitted, finite local jobs.

Admit records a request; resume dispatches it at most once; receive returns actual
bytes and a conservative outcome. No shell, automatic retry, semantic acceptance,
or follow-up command execution is provided. See docs/engine/core/README.md for the support scope.
"""
from __future__ import annotations

import argparse
import base64
from importlib.machinery import ModuleSpec
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable
import uuid


# A detached worker is invoked by absolute script path in both source and deployed engines.
if not __package__:
    sys.dont_write_bytecode = True  # A worker may run from an exact, pinned engine.
    _engine = Path(__file__).resolve().parents[3]
    if _engine.name == 'managed_ai' and (_engine / '__init__.py').is_file():
        _src = str(_engine.parent)
        if _src not in sys.path:
            sys.path.insert(0, _src)
    elif 'managed_ai' not in sys.modules:
        _init = _engine / '__init__.py'
        if _init.is_file():
            _spec = importlib.util.spec_from_file_location(
                'managed_ai', str(_init), submodule_search_locations=[str(_engine)],
            )
        else:
            _spec = ModuleSpec('managed_ai', None, is_package=True)
            _spec.submodule_search_locations = [str(_engine)]
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules['managed_ai'] = _mod
        if _spec.loader is not None:
            _spec.loader.exec_module(_mod)
    __package__ = 'managed_ai.core.exchange.transport'

from ...ownership import NativeLayout
from ...storage import (
    AdmissionContended,
    admission_deadline,
    encode,
    exclusive,
    lock_file_nonblocking,
    now,
    owner_admission,
    read_json,
    sha,
    shared,
    sync_dir,
    unlock_file,
    write_json,
)

native_owner = NativeLayout.owner_of


def name(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError(f"nonempty {label} of at most 500 characters required")
    return value


def worker_active(directory: Path) -> bool:
    with (directory / "worker.lock").open("a+b") as stream:
        try:
            lock_file_nonblocking(stream, exclusive=True)
        except BlockingIOError:
            return True
        unlock_file(stream)
        return False


def material(path: Path) -> dict[str, Any]:
    try:
        body = path.read_bytes()
    except FileNotFoundError:
        return {"path": str(path), "present": False}
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    return {"path": str(path), "present": True, "byte_count": len(body),
            "sha256": sha(body), "base64": base64.b64encode(body).decode("ascii"),
            "utf8_text": text}


class Collector:
    def __init__(self, root: Path, *, check_job_owner: Callable[[str | None], None] | None = None) -> None:
        self._selected_owner = check_job_owner
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs = self.root / "jobs"
        self.jobs.mkdir(exist_ok=True)
        sync_dir(self.root)

    def _check_job_owner(self, job_id: str | None) -> None:
        if self._selected_owner is not None:
            self._selected_owner(job_id)
            return
        owner = native_owner(self.root)
        if owner is not None:
            raise ValueError(f'carrier belongs to the native runtime; use manage_store.py --root {owner} run for the selected turn')

    def admit(self, job_id: str, argv: list[str], cwd: Path, brief: Path,
              consumer: str, *, dispatch_binding_sha256: str | None = None,
              before_new_admission: Callable[[], None] | None = None) -> dict[str, Any]:
        """Admission is explicit; this method records but does not start a job."""
        name(job_id, "job identity")
        self._check_job_owner(job_id)
        name(consumer, "consumer")
        if not argv or any(not isinstance(item, str) or "\0" in item for item in argv):
            raise ValueError("nonempty argv with no NUL characters required")
        workdir = cwd.resolve(strict=True)
        if not workdir.is_dir():
            raise ValueError("cwd must be an existing directory")
        origin = material(brief.resolve(strict=True))
        if origin.get("utf8_text") is None:
            raise ValueError("originating brief must be UTF-8 text")
        request = {"version": 1, "job_id": job_id, "consumer": consumer,
                   "argv": argv, "cwd": str(workdir), "origin_brief": origin,
                   "execution_admission": "explicit-local-argv", "stdin": "null"}
        if dispatch_binding_sha256 is not None:
            request["dispatch_binding_sha256"] = dispatch_binding_sha256
        key = sha(job_id.encode("utf-8"))
        directory = self.jobs / key
        if directory.exists():
            old = read_json(directory / "request.json")
            if old.get("dispatch_binding_sha256") != dispatch_binding_sha256:
                raise ValueError("job already admitted with another binding; receive under its original scope")
            if {k: v for k, v in old.items() if k != "admitted_at"} != request:
                raise ValueError("job identity already names a different request; choose a new identity")
            if dispatch_binding_sha256 is not None:
                sync_dir(self.jobs)
            return {"job_id": job_id, "admission": "already-recorded", "key": key}
        request["admitted_at"] = now()
        if before_new_admission is not None:
            before_new_admission()
        staging = self.jobs / (".admission-" + uuid.uuid4().hex)
        staging.mkdir()
        write_json(staging / "request.json", request)
        os.rename(staging, directory)
        sync_dir(self.jobs)
        return {"job_id": job_id, "admission": "queued", "key": key}

    def resume(self, job_id: str | None = None) -> list[dict[str, Any]]:
        """Resume one selected exchange job, or ordinary standalone admitted jobs."""
        self._check_job_owner(job_id)
        if job_id is None:
            directories = sorted(self.jobs.iterdir())
        else:
            name(job_id, 'job identity')
            directory = self.jobs / sha(job_id.encode('utf-8'))
            if not directory.is_dir():
                raise ValueError('selected job is unavailable')
            directories = [directory]
        reports: list[dict[str, Any]] = []
        for directory in directories:
            if directory.name.startswith(".") or not directory.is_dir():
                continue
            try:
                request = read_json(directory / "request.json")
                if directory.name != sha(request["job_id"].encode("utf-8")):
                    raise ValueError("job identity does not match directory")
                if (directory / "dispatch.json").exists():
                    reports.append({"job_id": request["job_id"], "dispatch": "already-attempted"})
                    continue
                token = uuid.uuid4().hex
                dispatch = {"attempt_id": token, "requested_at": now(),
                            "request_sha256": sha(encode(request))}
                # Persist the uncertainty boundary BEFORE asking the OS to launch.
                write_json(directory / "dispatch.json", dispatch)
                with (directory / "worker.log").open("ab") as log:
                    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                              if os.name == "nt" else {"start_new_session": True})
                    process = subprocess.Popen(
                        [sys.executable, "-B", str(Path(__file__).resolve()), "--root", str(self.root),
                         "_worker", directory.name, token],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        close_fds=True, **detach,
                    )
                reports.append({"job_id": request["job_id"], "dispatch": "requested",
                                "worker_pid_hint": process.pid})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                reports.append({"directory": str(directory), "dispatch": "unresolved",
                                "error": str(exc)})
        return reports

    def views(self, consumer: str | None = None,
              current_brief: Path | None = None, *,
              job_id: str | None = None) -> list[dict[str, Any]]:
        """Return actual captured bytes, including partial bytes for uncertain work."""
        if consumer is not None:
            name(consumer, "consumer")
        if job_id is not None:
            name(job_id, "job identity")
            directories = [self.jobs / sha(job_id.encode("utf-8"))]
        else:
            directories = sorted(self.jobs.iterdir())
        views: list[dict[str, Any]] = []
        for directory in directories:
            if job_id is None and (directory.name.startswith(".") or not directory.is_dir()):
                continue
            try:
                request = read_json(directory / "request.json")
                stored_job_id = name(request["job_id"], "job identity")
                if directory.name != sha(stored_job_id.encode("utf-8")):
                    raise ValueError("job identity does not match directory")
                if consumer is not None and request["consumer"] != consumer:
                    continue
                # Read the terminal record before stream bodies. Its publication
                # follows the worker's stream fsyncs; otherwise this is a snapshot.
                terminal = (read_json(directory / "result.json")
                            if (directory / "result.json").exists() else None)
                streams = {which: material(directory / (which + ".bin"))
                           for which in ("stdout", "stderr")}
                view: dict[str, Any] = {
                    "job_id": request["job_id"], "consumer": request["consumer"],
                    "origin_request": request, "streams": streams,
                    "semantic_acceptance": "not-established-by-delivery",
                    "effect_authority": "none-granted-by-delivery", "observed_at": now(),
                }
                if terminal is not None:
                    dispatch = read_json(directory / "dispatch.json")
                    if (terminal["attempt_id"] != dispatch["attempt_id"] or
                            dispatch["request_sha256"] != sha(encode(request))):
                        raise ValueError("request/attempt/result binding mismatch")
                    valid = all(streams[key].get("present") and
                                streams[key].get("sha256") == terminal["streams"][key]["sha256"] and
                                streams[key].get("byte_count") == terminal["streams"][key]["byte_count"]
                                for key in streams)
                    view.update(status=terminal["status"] if valid else "result-integrity-unresolved",
                                result=terminal, stream_integrity="verified" if valid else "mismatch")
                elif (directory / "dispatch.json").exists():
                    active = worker_active(directory)
                    view.update(status="running" if active else "uncertain",
                                detail=("worker lock is currently held" if active else
                                        "attempt recorded without terminal result or active worker lock; "
                                        "worker may be starting, lost, or interrupted"))
                else:
                    view.update(status="queued", detail="explicitly admitted; never dispatched")
                try:
                    path = current_brief or Path(request["origin_brief"]["path"])
                    current = material(path.resolve(strict=True))
                    view["current_brief"] = current
                    view["interpretation"] = (
                        "origin-brief-unchanged" if current["sha256"] == request["origin_brief"]["sha256"]
                        else "current-interpretation-required"
                    )
                except (OSError, ValueError, KeyError) as exc:
                    view.update(interpretation="current-context-unavailable", current_context_error=str(exc))
                views.append(view)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                # Corrupt/unattributable work is discoverable even in a named
                # consumer view; omission would hide a potentially relevant job.
                unresolved = {"directory": str(directory), "status": "record-unresolved",
                              "error": str(exc), "requested_consumer": consumer}
                if job_id is not None:
                    unresolved["requested_job_id"] = job_id
                views.append(unresolved)
        return views

    def worker(self, key: str, token: str) -> None:
        if len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
            raise ValueError("invalid job key")
        directory = self.jobs / key
        with exclusive(directory / "worker.lock"):
            dispatch = read_json(directory / "dispatch.json")
            request = read_json(directory / "request.json")
            if token != dispatch["attempt_id"] or dispatch["request_sha256"] != sha(encode(request)):
                raise ValueError("worker does not match admitted attempt")
            if (directory / "started.json").exists() or (directory / "result.json").exists():
                raise ValueError("an execution was already attempted; refusing repeat")
            # This durable marker deliberately precedes Popen. A crash in that
            # gap is uncertain and cannot safely be repaired by rerunning argv.
            write_json(directory / "started.json", {"worker_pid": os.getpid(),
                       "attempt_id": token, "started_at": now()})
            result: dict[str, Any] = {"attempt_id": token, "returncode": None,
                                      "signal": None, "status": "launch-failed"}
            with (directory / "stdout.bin").open("xb") as stdout, \
                    (directory / "stderr.bin").open("xb") as stderr:
                try:
                    child = subprocess.Popen(request["argv"], cwd=request["cwd"],
                                             stdin=subprocess.DEVNULL, stdout=stdout,
                                             stderr=stderr, close_fds=True)
                except OSError as exc:
                    result["launch_error"] = str(exc)
                else:
                    write_json(directory / "running.json", {"child_pid": child.pid,
                               "attempt_id": token, "spawned_at": now()})
                    returncode = child.wait()
                    result.update(status="completed", returncode=returncode,
                                  signal=-returncode if returncode < 0 else None)
                stdout.flush()
                stderr.flush()
                os.fsync(stdout.fileno())
                os.fsync(stderr.fileno())
            result["streams"] = {}
            for which in ("stdout", "stderr"):
                body = (directory / (which + ".bin")).read_bytes()
                result["streams"][which] = {"sha256": sha(body), "byte_count": len(body)}
            result["finished_at"] = now()
            write_json(directory / "result.json", result)


def main(*, validate_root=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--admission-deadline", type=float, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    admit = sub.add_parser("admit", allow_abbrev=False, help="explicitly admit one local argv job")
    admit.add_argument("job_id")
    admit.add_argument("--consumer", required=True)
    admit.add_argument("--cwd", type=Path, required=True)
    admit.add_argument("--brief", type=Path, required=True)
    admit.add_argument("argv", nargs=argparse.REMAINDER)
    sub.add_parser("resume", allow_abbrev=False, help="start only admitted jobs with no recorded attempt")
    sub.add_parser("list", allow_abbrev=False, help="show all work and actual stream snapshots")
    receive = sub.add_parser("receive", allow_abbrev=False, help="show actual material for a named consumer")
    receive.add_argument("consumer")
    receive.add_argument("--current-brief", type=Path)
    internal = sub.add_parser("_worker", allow_abbrev=False, help=argparse.SUPPRESS)
    internal.add_argument("key")
    internal.add_argument("token")
    args = parser.parse_args()
    try:
        collector = Collector(validate_root(args.root) if validate_root else args.root)
        if args.command == "_worker":
            collector.worker(args.key, args.token)
            return
        read_only = args.command in {"list", "receive"}
        guard = shared if read_only else exclusive
        deadline = admission_deadline(args.admission_deadline)
        with owner_admission(
                lambda guards: guards.enter_context(guard(collector.root / "owner.lock", allow_readonly_open=read_only)),
                deadline=deadline):
            if args.command == "admit":
                argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
                out = collector.admit(args.job_id, argv, args.cwd, args.brief, args.consumer)
            elif args.command == "resume":
                out = collector.resume()
            elif args.command == "receive":
                out = collector.views(args.consumer, args.current_brief)
            else:
                out = collector.views()
        sys.stdout.buffer.write(encode(out))
    except AdmissionContended as exc:
        print(json.dumps(exc.result(args.command)), file=sys.stderr)
        raise SystemExit(75)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        parser.exit(2, f"collector: {exc}\n")


if __name__ == "__main__":
    main()
