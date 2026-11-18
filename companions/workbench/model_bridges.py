"""Caller-mediated model transports; neither transport implements model inference."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def atomic_json(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive and path.exists():
        raise FileExistsError(str(path))
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temporary.write_bytes(encode(value) + b"\n")
        if exclusive:
            # Link installs one complete file without replacing an earlier response.
            os.link(temporary, path)
            temporary.unlink()
        else:
            os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class MailboxModel:
    """Emit exact callback requests and wait for explicitly bound host responses."""

    def __init__(self, directory, timeout_seconds=900):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        stem = f"{self.calls:04d}"
        digest = hashlib.sha256(encode(request)).hexdigest()
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
        response = json.loads(body)
        if not isinstance(response, dict) or response.get("request_sha256") != digest:
            raise ValueError("Model response is not bound to this exact request")
        if "action" not in response:
            raise ValueError("Model response has no action")
        atomic_json(self.directory / (stem + ".transport.json"), {
            "schema": "g60-model-mailbox-return/1",
            "request_sha256": digest,
            "response_sha256": hashlib.sha256(body).hexdigest(),
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
        self.timeout_seconds = float(timeout_seconds)
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        stem = f"{self.calls:04d}"
        request_bytes = encode(request) + b"\n"
        request_path = self.directory / (stem + ".request.json")
        with request_path.open("xb") as stream:
            stream.write(request_bytes)
        stdout_path = self.directory / (stem + ".stdout")
        stderr_path = self.directory / (stem + ".stderr")
        started = time.monotonic()
        status = {"argv": self.argv, "cwd": self.cwd,
                  "request_sha256": hashlib.sha256(request_bytes).hexdigest()}
        try:
            with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
                run = subprocess.run(self.argv, input=request_bytes, stdout=stdout, stderr=stderr,
                                     cwd=self.cwd, timeout=self.timeout_seconds, check=False)
            status["returncode"] = run.returncode
            if run.returncode:
                raise RuntimeError("Configured model command failed with exit " + str(run.returncode))
            return json.loads(stdout_path.read_bytes())
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
    request = json.loads((directory / (stem + ".request.json")).read_bytes())
    if hashlib.sha256(encode(request["request"])).hexdigest() != request["request_sha256"]:
        raise ValueError("Request envelope digest mismatch")
    atomic_json(directory / (stem + ".response.json"), {
        "schema": "g60-model-mailbox-response/1",
        "request_sha256": request["request_sha256"],
        "producer": producer,
        "action": action,
    }, exclusive=True)
