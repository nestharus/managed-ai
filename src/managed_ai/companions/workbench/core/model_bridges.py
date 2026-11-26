"""Caller-mediated model transports; neither transport implements model inference."""
from __future__ import annotations

import math
import os
from pathlib import Path
import subprocess
import sys
import time

try:
    from .io_utils import (
        atomic_bytes,
        atomic_json,
        canonical_bytes as encode,
        loads_strict,
        sha256_bytes,
        sync_dir,
    )
except ImportError:
    _src_root = Path(__file__).resolve().parents[4]
    if str(_src_root) not in sys.path:
        sys.path.insert(0, str(_src_root))
    from managed_ai.companions.workbench.core.io_utils import (
        atomic_bytes,
        atomic_json,
        canonical_bytes as encode,
        loads_strict,
        sha256_bytes,
        sync_dir,
    )


def _positive_timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_seconds must be a finite positive number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    return timeout


class MailboxModel:
    """Emit exact callback requests and wait for explicitly bound host responses."""

    def __init__(self, directory, timeout_seconds=900):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = _positive_timeout(timeout_seconds)
        self.calls = 0

    def set_step(self, step):
        if type(step) is not int or step < 1:
            raise ValueError("step must be a positive integer")
        self.calls = step - 1

    def resume_at(self, ordinal):
        if type(ordinal) is not int or ordinal < 0:
            raise ValueError("ordinal must be a nonnegative integer")
        self.calls = ordinal

    def __call__(self, request):
        self.calls += 1
        stem = f"{self.calls:04d}"
        digest = sha256_bytes(encode(request))
        request_path = self.directory / (stem + ".request.json")
        response_path = self.directory / (stem + ".response.json")
        if response_path.exists():
            raise FileExistsError("Preexisting response cannot answer a new request: " + str(response_path))
        envelope = {
            "schema": "g60-model-mailbox-request/1",
            "request_sha256": digest,
            "request": request,
            "transport": "host-mediated model callback; no provider inference is implemented here",
        }
        atomic_json(request_path, envelope, exclusive=True)
        started = time.monotonic()
        while not response_path.exists():
            if time.monotonic() - started > self.timeout_seconds:
                raise TimeoutError("Host response not received for " + stem)
            time.sleep(0.1)
        body = response_path.read_bytes()
        response = loads_strict(body)
        if not isinstance(response, dict) or response.get("request_sha256") != digest:
            raise ValueError("Model response is not bound to this exact request")
        if "action" not in response:
            raise ValueError("Model response has no action")
        atomic_json(self.directory / (stem + ".transport.json"), {
            "schema": "g60-model-mailbox-return/1",
            "request_sha256": digest,
            "response_sha256": sha256_bytes(body),
            "response_bytes": len(body),
            "elapsed_seconds": time.monotonic() - started,
            "provider_inference_calls": None,
        }, exclusive=True)
        return response["action"]


class CommandModel:
    """Run a configured command: request JSON on stdin, action JSON on stdout."""

    def __init__(self, argv, directory, *, cwd=None, timeout_seconds=900):
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) for x in argv):
            raise ValueError("argv must be a nonempty list of strings")
        self.argv = list(argv)
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.cwd = str(Path(cwd).resolve()) if cwd else None
        self.timeout_seconds = _positive_timeout(timeout_seconds)
        self.calls = 0

    def set_step(self, step):
        if type(step) is not int or step < 1:
            raise ValueError("step must be a positive integer")
        self.calls = step - 1

    def resume_at(self, ordinal):
        if type(ordinal) is not int or ordinal < 0:
            raise ValueError("ordinal must be a nonnegative integer")
        self.calls = ordinal

    def __call__(self, request):
        self.calls += 1
        stem = f"{self.calls:04d}"
        request_bytes = encode(request) + b"\n"
        request_path = self.directory / (stem + ".request.json")
        atomic_bytes(request_path, request_bytes, exclusive=True)
        stdout_path = self.directory / (stem + ".stdout")
        stderr_path = self.directory / (stem + ".stderr")
        started = time.monotonic()
        status = {
            "argv": self.argv,
            "cwd": self.cwd,
            "request_sha256": sha256_bytes(request_bytes),
        }
        try:
            with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
                run = subprocess.run(
                    self.argv,
                    input=request_bytes,
                    stdout=stdout,
                    stderr=stderr,
                    cwd=self.cwd,
                    timeout=self.timeout_seconds,
                    check=False,
                )
                stdout.flush()
                os.fsync(stdout.fileno())
                stderr.flush()
                os.fsync(stderr.fileno())
            sync_dir(self.directory)
            status["returncode"] = run.returncode
            if run.returncode:
                raise RuntimeError("Configured model command failed with exit " + str(run.returncode))
            return loads_strict(stdout_path.read_bytes())
        except Exception as exc:
            status["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            status["elapsed_seconds"] = time.monotonic() - started
            atomic_json(self.directory / (stem + ".transport.json"), status, exclusive=True)


def write_mailbox_response(directory, step, action, *, producer="host-supplied model participant"):
    """Atomically deliver a model-produced action to one exact outstanding request."""
    directory = Path(directory).resolve()
    stem = f"{int(step):04d}"
    request = loads_strict((directory / (stem + ".request.json")).read_bytes())
    if sha256_bytes(encode(request["request"])) != request["request_sha256"]:
        raise ValueError("Request envelope digest mismatch")
    atomic_json(directory / (stem + ".response.json"), {
        "schema": "g60-model-mailbox-response/1",
        "request_sha256": request["request_sha256"],
        "producer": producer,
        "action": action,
    }, exclusive=True)
