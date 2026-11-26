#!/usr/bin/env python3
"""Contract checks using temporary runtime directories outside the distribution."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.dont_write_bytecode = True
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from tests._paths import ENGINE as ROOT  # noqa: E402
from tests._paths import cleanup_tempdir  # noqa: E402

# isort: split
from managed_ai.core.exchange.transport import collector  # noqa: E402

HERE = Path(__file__).resolve().parent
TOOL = ROOT / "core" / "exchange" / "transport" / "collector.py"
OBSERVATIONS: list[dict] = []

# A ready/release handshake tests fresh CLI invocations while the actual child
# is still open. The deadline also bounds cleanup when an assertion fails.
GATED_CHILD = r"""
import os, pathlib, sys, time
folder = pathlib.Path(sys.argv[1])
with (folder / "invocations.txt").open("ab", buffering=0) as out:
    out.write((str(os.getpid()) + "\n").encode())
os.write(1, b"first\x00\n")
os.write(2, b"diagnostic:\xff\n")
(folder / "ready").write_text("ready")
deadline = time.monotonic() + 6
while not (folder / "release").exists() and time.monotonic() < deadline:
    time.sleep(0.01)
os.write(1, b"last\n")
(folder / "child-finished").write_text("finished")
sys.exit(7)
"""


def utc() -> str:
    return datetime.now(UTC).isoformat()


def until(predicate, seconds: float = 8) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def compact(value):
    """Keep observed material while avoiding repeated full request/argv copies."""
    if isinstance(value, list):
        return [compact(item) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key == "origin_request":
            out[key] = {k: item[k] for k in ("job_id", "consumer", "origin_brief")}
        else:
            out[key] = compact(item)
    return out


class CollectorBehavior(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="g60-collector-contract-")
        self.folder = Path(self.temporary.name)
        self.root = self.folder / "state"
        self.brief = self.folder / "brief.md"
        self.brief.write_text("Origin brief v1.\n", encoding="utf-8")
        self.note = {"test": self._testMethodName, "actions": []}
        OBSERVATIONS.append(self.note)
        self.gated = False
        self.owned_workers: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        if self.gated:
            (self.folder / "release").write_text("release for cleanup")
            until(lambda: (self.folder / "child-finished").exists())
        for worker in self.owned_workers:
            if worker.poll() is None:
                try:
                    worker.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=3)
        cleanup_tempdir(self.temporary)

    def cli(self, *args: str, expected_code: int = 0, env: dict[str, str] | None = None):
        process = subprocess.run(
            [sys.executable, "-B", str(TOOL), "--root", str(self.root), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            env=env,
        )
        try:
            response = json.loads(process.stdout)
        except ValueError:
            response = None
        self.note["actions"].append(
            {
                "argv": list(args),
                "returncode": process.returncode,
                "stderr": process.stderr.decode("utf-8", errors="replace"),
                "response": compact(response),
            }
        )
        self.assertEqual(process.returncode, expected_code, process.stderr.decode(errors="replace"))
        if expected_code == 0:
            self.assertEqual(process.stderr, b"")
            self.assertIsNotNone(response)
        return response

    def admit(self, argv: list[str], job_id: str = "job-1", consumer: str = "reviewer"):
        return self.cli(
            "admit",
            "--consumer",
            consumer,
            "--cwd",
            str(self.folder),
            "--brief",
            str(self.brief),
            job_id,
            "--",
            *argv,
        )

    def directory(self, job_id: str = "job-1") -> Path:
        return self.root / "jobs" / hashlib.sha256(job_id.encode()).hexdigest()

    def view(self, *args: str):
        views = self.cli("receive", "reviewer", *args)
        self.assertEqual(len(views), 1)
        return views[0]

    def terminal(self):
        self.assertTrue(
            until(lambda: (self.directory() / "result.json").exists()),
            "worker did not publish a result within the bounded wait",
        )
        return self.view()

    def bytes(self, view: dict, stream: str) -> bytes:
        material = view["streams"][stream]
        self.assertTrue(material["present"])
        body = base64.b64decode(material["base64"], validate=True)
        self.assertEqual(material["byte_count"], len(body))
        self.assertEqual(material["sha256"], hashlib.sha256(body).hexdigest())
        return body

    def gated_argv(self) -> list[str]:
        self.gated = True
        return [sys.executable, "-c", GATED_CHILD, str(self.folder)]

    def test_restart_binary_results_and_current_context(self) -> None:
        argv = self.gated_argv()
        self.assertEqual(self.admit(argv)["admission"], "queued")
        self.assertEqual(self.admit(argv)["admission"], "already-recorded")
        self.assertEqual(self.view()["status"], "queued")
        self.assertEqual(self.cli("receive", "another-consumer"), [])
        self.assertEqual(self.cli("resume")[0]["dispatch"], "requested")
        self.assertTrue(until(lambda: (self.folder / "ready").exists()))
        opened = self.view()
        self.assertEqual(opened["status"], "running")
        self.assertEqual(self.bytes(opened, "stdout"), b"first\x00\n")
        self.assertEqual(self.bytes(opened, "stderr"), b"diagnostic:\xff\n")
        self.assertIsNone(opened["streams"]["stderr"]["utf8_text"])
        self.assertEqual(self.cli("resume")[0]["dispatch"], "already-attempted")
        (self.folder / "release").write_text("finish")
        completed = self.terminal()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["returncode"], 7)
        self.assertIsNone(completed["result"]["signal"])
        self.assertEqual(completed["stream_integrity"], "verified")
        self.assertEqual(self.bytes(completed, "stdout"), b"first\x00\nlast\n")
        self.assertEqual(self.bytes(completed, "stderr"), b"diagnostic:\xff\n")
        self.assertEqual(self.cli("resume")[0]["dispatch"], "already-attempted")
        self.assertEqual(len((self.folder / "invocations.txt").read_bytes().splitlines()), 1)
        self.assertEqual(self.view()["result"], completed["result"])
        origin = completed["origin_request"]["origin_brief"]
        self.brief.write_text("Current brief v2.\n", encoding="utf-8")
        changed = self.view()
        self.assertEqual(changed["interpretation"], "current-interpretation-required")
        self.assertEqual(changed["origin_request"]["origin_brief"], origin)
        self.assertEqual(changed["result"], completed["result"])
        alternate = self.folder / "other-current.md"
        alternate.write_text("Origin brief v1.\n", encoding="utf-8")
        self.assertEqual(
            self.view("--current-brief", str(alternate))["interpretation"], "origin-brief-unchanged"
        )
        self.brief.unlink()
        missing = self.view()
        self.assertEqual(missing["interpretation"], "current-context-unavailable")
        self.assertEqual(missing["status"], "completed")
        self.assertEqual(self.bytes(missing, "stdout"), b"first\x00\nlast\n")
        self.assertEqual(missing["semantic_acceptance"], "not-established-by-delivery")
        self.assertEqual(missing["effect_authority"], "none-granted-by-delivery")
        self.assertEqual(self.cli("list")[0]["job_id"], "job-1")

    def test_launch_failure_has_no_invented_exit(self) -> None:
        self.admit([str(self.folder / "nonexistent-program")])
        self.cli("resume")
        view = self.terminal()
        self.assertEqual(view["status"], "launch-failed")
        self.assertIsNone(view["result"]["returncode"])
        self.assertIsNone(view["result"]["signal"])
        self.assertTrue(view["result"]["launch_error"])
        self.assertEqual(self.bytes(view, "stdout"), b"")
        self.assertEqual(self.bytes(view, "stderr"), b"")
        self.assertEqual(self.cli("resume")[0]["dispatch"], "already-attempted")

    @unittest.skipUnless(os.name == "posix", "POSIX signal exit encoding")
    def test_signal_exit_is_actual_child_outcome(self) -> None:
        script = (
            "import os,signal; os.write(1,b'before signal'); os.kill(os.getpid(),signal.SIGTERM)"
        )
        self.admit([sys.executable, "-c", script])
        self.cli("resume")
        view = self.terminal()
        self.assertEqual(view["status"], "completed")
        self.assertEqual(view["result"]["returncode"], -signal.SIGTERM)
        self.assertEqual(view["result"]["signal"], signal.SIGTERM)
        self.assertEqual(self.bytes(view, "stdout"), b"before signal")

    def record_dispatch(self) -> str:
        """Inject the documented pre-spawn durable boundary into fresh test state."""
        token = uuid.uuid4().hex
        request = collector.read_json(self.directory() / "request.json")
        collector.write_json(
            self.directory() / "dispatch.json",
            {
                "attempt_id": token,
                "requested_at": utc(),
                "request_sha256": collector.sha(collector.encode(request)),
            },
        )
        self.note["fixture"] = "fresh valid dispatch marker written before any worker spawn"
        return token

    def test_worker_loss_keeps_bytes_and_unknown_exit(self) -> None:
        self.admit(self.gated_argv())
        token = self.record_dispatch()
        detach = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        with (self.folder / "owned-worker.log").open("wb") as log:
            worker = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(TOOL),
                    "--root",
                    str(self.root),
                    "_worker",
                    self.directory().name,
                    token,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                **detach,
            )
        self.owned_workers.append(worker)
        self.assertTrue(until(lambda: (self.folder / "ready").exists()))
        self.assertEqual(self.view()["status"], "running")
        worker.kill()
        code = worker.wait(timeout=3)
        if os.name == "posix":
            self.assertEqual(code, -signal.SIGKILL)
        else:
            self.assertNotEqual(code, 0)
        lost = self.view()
        self.assertEqual(lost["status"], "uncertain")
        self.assertNotIn("result", lost)
        self.assertEqual(self.bytes(lost, "stdout"), b"first\x00\n")
        self.assertEqual(self.cli("resume")[0]["dispatch"], "already-attempted")
        (self.folder / "release").write_text("finish")
        self.assertTrue(until(lambda: (self.folder / "child-finished").exists()))
        final = self.view()
        self.assertEqual(final["status"], "uncertain")
        self.assertNotIn("result", final)
        self.assertEqual(self.bytes(final, "stdout"), b"first\x00\nlast\n")
        self.assertEqual(self.bytes(final, "stderr"), b"diagnostic:\xff\n")
        self.assertEqual(len((self.folder / "invocations.txt").read_bytes().splitlines()), 1)
        self.assertFalse((self.directory() / "result.json").exists())

    def test_dispatch_without_worker_stays_visible_without_retry(self) -> None:
        self.admit([sys.executable, "-c", "raise RuntimeError('this must never be dispatched')"])
        self.record_dispatch()
        self.assertEqual(self.cli("resume")[0]["dispatch"], "already-attempted")
        view = self.view()
        self.assertEqual(view["status"], "uncertain")
        self.assertNotIn("result", view)
        self.assertFalse(view["streams"]["stdout"]["present"])
        self.assertFalse((self.directory() / "started.json").exists())

    def test_changed_identity_material_is_rejected(self) -> None:
        argv = [sys.executable, "-c", "print('original')"]
        self.admit(argv)
        preserved = (self.directory() / "request.json").read_bytes()
        self.cli(
            "admit",
            "--consumer",
            "reviewer",
            "--cwd",
            str(self.folder),
            "--brief",
            str(self.brief),
            "job-1",
            "--",
            sys.executable,
            "-c",
            "print('changed')",
            expected_code=2,
        )
        self.assertEqual((self.directory() / "request.json").read_bytes(), preserved)
        self.assertFalse((self.directory() / "dispatch.json").exists())

    def test_owner_lock_excludes_second_cli(self) -> None:
        self.admit([sys.executable, "-c", "print('queued')"])
        env = dict(os.environ, MANAGED_AI_DOMAIN_ADMISSION_SECONDS="0.1")
        with collector.exclusive(self.root / "owner.lock"):
            self.cli("resume", expected_code=75, env=env)
        self.assertFalse((self.directory() / "dispatch.json").exists())
        self.assertEqual(self.view()["status"], "queued")

    def test_altered_stream_is_visible_with_integrity_diagnostic(self) -> None:
        self.admit([sys.executable, "-c", "print('original')"])
        self.cli("resume")
        first = self.terminal()
        self.assertEqual(first["status"], "completed")
        (self.directory() / "stdout.bin").write_bytes(b"changed after collection\x00")
        changed = self.view()
        self.assertEqual(changed["status"], "result-integrity-unresolved")
        self.assertEqual(changed["stream_integrity"], "mismatch")
        self.assertEqual(self.bytes(changed, "stdout"), b"changed after collection\x00")
        self.assertEqual(changed["result"], first["result"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
