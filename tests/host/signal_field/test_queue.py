"""Regression tests for signal_field/queue.py guards, dispatch allowlist, and worker diagnostics
(AU2, OE1)."""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
QUEUE_PATH = REPO_ROOT / "src/managed_ai_information/host/signal_field/queue.py"
spec = importlib.util.spec_from_file_location("signal_field_queue_under_test", QUEUE_PATH)
queue_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue_mod)

PRODUCT_ROOT = QUEUE_PATH.resolve().parents[2]


def sample_use(ident="u1", mode="live"):
    return {
        "id": ident,
        "mode": mode,
        "question": "What changed?",
        "purpose": "Keep bounded observation current.",
        "sources": ["s1"],
        "observer": {"definition": "test-observer"},
        "scope": {"coverage": "unit"},
    }


class SignalFieldQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "queue.sqlite"

    def run_cli(self, request_obj, *, db=None, raw_bytes=None, extra_args=()):
        db = self.db_path if db is None else db
        payload = raw_bytes if raw_bytes is not None else json.dumps(request_obj).encode("utf-8")
        return subprocess.run(
            [sys.executable, "-B", str(QUEUE_PATH), "--db", str(db), *extra_args],
            input=payload,
            capture_output=True,
        )

    # --- AU2: Product-directory containment & ordinary-file/symlink guards ---
    def test_refuses_database_inside_product_directory(self):
        for candidate in (
            PRODUCT_ROOT / "host/signal_field/forbidden.sqlite",
            PRODUCT_ROOT / "store/forbidden.sqlite",
            PRODUCT_ROOT / "forbidden.sqlite",
        ):
            with self.subTest(candidate=str(candidate)):
                with self.assertRaisesRegex(ValueError, "outside the product directory"):
                    queue_mod.Field(candidate)
                proc = self.run_cli({"op": "status"}, db=candidate)
                self.assertEqual(proc.returncode, 2)
                self.assertIn("outside the product directory", json.loads(proc.stderr)["reason"])
                self.assertFalse(candidate.exists())

    def test_refuses_symlink_and_directory_database_paths(self):
        # Directory as --db is refused.
        db_dir = self.root / "as_dir"
        db_dir.mkdir()
        with self.assertRaisesRegex(ValueError, "ordinary database file"):
            queue_mod.Field(db_dir)

        if os.name == "nt":
            # Symlink creation on Windows may require elevated privileges; test if permitted.
            probe = self.root / "probe_link"
            try:
                probe.symlink_to(self.root / "probe_target")
            except OSError:
                return
            probe.unlink()

        # Direct symlink for --db is refused.
        real_target = self.root / "real.sqlite"
        real_target.write_bytes(b"")
        link_db = self.root / "link.sqlite"
        link_db.symlink_to(real_target)
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            queue_mod.Field(link_db)

        # Symlink parent escaping into product directory is refused.
        link_dir = self.root / "escape_dir"
        link_dir.symlink_to(PRODUCT_ROOT / "host")
        with self.assertRaisesRegex(ValueError, "outside the product directory"):
            queue_mod.Field(link_dir / "escaped.sqlite")

        # Symlinked journal sidecar is refused.
        good_db = self.root / "sidecar_test.sqlite"
        wal_link = Path(str(good_db) + "-wal")
        wal_link.symlink_to(real_target)
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            queue_mod.Field(good_db)

        # Symlinked --request file is refused by CLI.
        req_real = self.root / "req.json"
        req_real.write_text('{"op":"status"}')
        req_link = self.root / "req_link.json"
        req_link.symlink_to(req_real)
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(QUEUE_PATH),
                "--db",
                str(self.db_path),
                "--request",
                str(req_link),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("must not be a symlink", json.loads(proc.stderr)["reason"])

    # --- AU2: Explicit operation allowlist & parameter validation (no blind getattr) ---
    def test_explicit_dispatch_allowlist_and_parameter_validation(self):
        # Internal / private methods must be refused.
        for bad_op in (
            "execute",
            "one",
            "all",
            "mutation",
            "ticket",
            "queue",
            "close",
            "__init__",
            "basis",
        ):
            proc = self.run_cli({"op": bad_op})
            self.assertEqual(proc.returncode, 2)
            self.assertIn("unknown operation", json.loads(proc.stderr)["reason"])

        # Unexpected extra parameters on valid operations must be refused.
        for bad_req, expected in (
            ({"op": "status", "unexpected": 1}, "unsupported parameters"),
            ({"op": "pull", "extra": True}, "unsupported parameters"),
            ({"op": "reconcile", "force": True}, "unsupported parameters"),
            ({"op": "enroll"}, "missing required parameters"),
            (
                {
                    "op": "change",
                    "source": "s1",
                    "epoch": "e1",
                    "position": 1,
                    "value": 1,
                    "notify": "yes",
                },
                "booleans",
            ),
            (
                {"op": "run", "argv": [sys.executable, "-c", "x\x00y"], "budget": 1, "timeout": 5},
                "explicit subprocess argv",
            ),
            (
                {"op": "run", "argv": [sys.executable], "budget": True, "timeout": 5},
                "positive invocation budget",
            ),
        ):
            with self.subTest(bad_req=bad_req):
                proc = self.run_cli(bad_req)
                self.assertEqual(proc.returncode, 2)
                self.assertIn(expected, json.loads(proc.stderr)["reason"])

        # Duplicate keys and non-finite JSON must be refused.
        proc_dup = self.run_cli(None, raw_bytes=b'{"op":"status","op":"pull"}')
        self.assertEqual(proc_dup.returncode, 2)
        self.assertIn("duplicate JSON key", json.loads(proc_dup.stderr)["reason"])

        proc_nan = self.run_cli(
            None, raw_bytes=b'{"op":"run","argv":["x"],"budget":1,"timeout":NaN}'
        )
        self.assertEqual(proc_nan.returncode, 2)
        self.assertIn("nonfinite JSON", json.loads(proc_nan.stderr)["reason"])

        # Abbreviated CLI options must be refused (allow_abbrev=False).
        proc_abbrev = subprocess.run(
            [sys.executable, "-B", str(QUEUE_PATH), "--d", str(self.db_path)],
            input=b'{"op":"status"}',
            capture_output=True,
        )
        self.assertNotEqual(proc_abbrev.returncode, 0)

        # Valid end-to-end CLI operations work cleanly.
        self.assertEqual(self.run_cli({"op": "enroll", "spec": sample_use()}).returncode, 0)
        self.assertEqual(
            self.run_cli(
                {"op": "change", "source": "s1", "epoch": "e1", "position": 1, "value": "v1"}
            ).returncode,
            0,
        )
        pulled = json.loads(self.run_cli({"op": "pull"}).stdout)
        self.assertEqual(pulled["use"], "u1")
        rec = json.loads(
            self.run_cli(
                {
                    "op": "receive",
                    "packet": pulled,
                    "result": {"state": "complete", "account": "Observed v1."},
                }
            ).stdout
        )
        self.assertEqual(rec["status"], "received")

    # --- OE1: Retain returncode, stdout, stderr in structured result and SQLite ---
    def test_worker_failure_diagnostics_retained_and_reconstructible(self):
        with queue_mod.Field(self.db_path) as field:
            field.enroll(sample_use("u1"))
            # 1. Non-zero exit retains returncode, stdout, stderr in return AND SQLite.
            fail_argv = [
                sys.executable,
                "-c",
                (
                    "import sys; sys.stdout.write('partial-out'); "
                    "sys.stderr.write('structured-refusal: bad flag'); sys.exit(7)"
                ),
            ]
            res = field.run(fail_argv, budget=1, timeout=10)
            self.assertEqual(res["status"], "worker-unresolved")
            self.assertEqual(res["worker"]["kind"], "non-zero-exit")
            self.assertEqual(res["worker"]["returncode"], 7)
            self.assertEqual(res["worker"]["stdout"], "partial-out")
            self.assertEqual(res["worker"]["stderr"], "structured-refusal: bad flag")
            self.assertFalse(res["worker"]["stdout_truncated"])
            self.assertFalse(res["worker"]["stderr_truncated"])
            job_id = res["in_flight_job"]

        # Reopen fresh connection (simulating restart) and inspect SQLite-retained worker_outcome!
        with queue_mod.Field(self.db_path) as reopened:
            inspected = reopened.inspect(job_id=job_id)
            self.assertEqual(inspected["worker_outcome"]["kind"], "non-zero-exit")
            self.assertEqual(inspected["worker_outcome"]["returncode"], 7)
            self.assertEqual(inspected["worker_outcome"]["stdout"], "partial-out")
            self.assertEqual(inspected["worker_outcome"]["stderr"], "structured-refusal: bad flag")
            pending = reopened.pending()
            self.assertEqual(pending["items"][0]["worker_outcome"]["returncode"], 7)

            # Retry clears worker_outcome and lets us test distinct failure modes cleanly.
            reopened.retry(job_id, inspected["token"])
            self.assertIsNone(reopened.inspect(job_id=job_id)["worker_outcome"])

            # 2. Launch failure (missing executable) is cleanly distinguishable from non-zero exit.
            missing = str(self.root / "no-such-worker-binary")
            res_launch = reopened.run([missing], budget=1, timeout=10)
            self.assertEqual(res_launch["status"], "worker-unresolved")
            self.assertEqual(res_launch["worker"]["kind"], "launch-error")
            self.assertIsNone(res_launch["worker"]["returncode"])
            self.assertIsNotNone(res_launch["worker"]["errno"])
            inspected_launch = reopened.inspect(job_id=job_id)
            self.assertEqual(inspected_launch["worker_outcome"]["kind"], "launch-error")

            # 3. Zero exit with invalid JSON stdout is distinguishable from non-zero exit.
            reopened.retry(job_id, inspected_launch["token"])
            res_badjson = reopened.run(
                [sys.executable, "-c", "print('not-json')"], budget=1, timeout=10
            )
            self.assertEqual(res_badjson["status"], "worker-unresolved")
            self.assertEqual(res_badjson["worker"]["kind"], "invalid-return")
            self.assertEqual(res_badjson["worker"]["returncode"], 0)
            self.assertIn("not-json", res_badjson["worker"]["stdout"])
            inspected_badjson = reopened.inspect(job_id=job_id)
            self.assertEqual(inspected_badjson["worker_outcome"]["kind"], "invalid-return")

            # 4. Timeout is cleanly distinguishable and retains timeout details.
            reopened.retry(job_id, inspected_badjson["token"])
            res_timeout = reopened.run(
                [sys.executable, "-c", "import time; time.sleep(5)"], budget=1, timeout=0.2
            )
            self.assertEqual(res_timeout["status"], "worker-unresolved")
            self.assertEqual(res_timeout["worker"]["kind"], "timeout")
            self.assertIsNone(res_timeout["worker"]["returncode"])
            self.assertEqual(res_timeout["worker"]["timeout"], 0.2)
            inspected_timeout = reopened.inspect(job_id=job_id)
            self.assertEqual(inspected_timeout["worker_outcome"]["kind"], "timeout")

            # 5. Oversized stderr on non-zero exit is bounded cleanly.
            reopened.retry(job_id, inspected_timeout["token"])
            big_size = queue_mod.MAX_WORKER_STREAM_BYTES + 4096
            res_big = reopened.run(
                [
                    sys.executable,
                    "-c",
                    f"import sys; sys.stderr.write('E' * {big_size}); sys.exit(3)",
                ],
                budget=1,
                timeout=10,
            )
            self.assertEqual(res_big["worker"]["returncode"], 3)
            self.assertTrue(res_big["worker"]["stderr_truncated"])
            self.assertEqual(res_big["worker"]["stderr_bytes"], big_size)
            self.assertEqual(
                len(res_big["worker"]["stderr"].encode("utf-8")), queue_mod.MAX_WORKER_STREAM_BYTES
            )

            # 6. Unhashable worker state (e.g. list) is caught cleanly as invalid-return and
            # recorded in SQLite.
            reopened.retry(job_id, reopened.inspect(job_id=job_id)["token"])
            res_unhashable = reopened.run(
                [
                    sys.executable,
                    "-c",
                    'import json; print(json.dumps({"state": ["complete"], "account": "bad"}))',
                ],
                budget=1,
                timeout=10,
            )
            self.assertEqual(res_unhashable["status"], "worker-unresolved")
            self.assertEqual(res_unhashable["worker"]["kind"], "invalid-return")
            self.assertEqual(res_unhashable["worker"]["returncode"], 0)
            self.assertEqual(
                reopened.inspect(job_id=job_id)["worker_outcome"]["kind"], "invalid-return"
            )

    def test_multijob_worker_unresolved_preserves_prior_returns(self):
        with queue_mod.Field(self.db_path) as field:
            field.enroll(sample_use("u1"))
            field.enroll(sample_use("u2"))
            worker_script = "\n".join(
                [
                    "import json, sys",
                    "pkt = json.loads(sys.stdin.read())",
                    "if pkt['use'] == 'u1':",
                    "    print(json.dumps({'state': 'complete', 'account': 'Done u1.'}))",
                    "else:",
                    "    sys.stderr.write('u2 failed')",
                    "    sys.exit(9)",
                ]
            )
            res = field.run([sys.executable, "-c", worker_script], budget=2, timeout=10)
            self.assertEqual(res["status"], "worker-unresolved")
            self.assertEqual(res["invocations"], 2)
            self.assertEqual(len(res["returns"]), 1)
            self.assertEqual(res["returns"][0]["status"], "received")
            self.assertEqual(res["in_flight_job"], "use:u2")
            self.assertEqual(res["worker"]["returncode"], 9)
            self.assertEqual(res["worker"]["stderr"], "u2 failed")

    def test_legacy_database_without_worker_outcome_column_migrates_cleanly(self):
        legacy_db = self.root / "legacy.sqlite"
        conn = sqlite3.connect(legacy_db)
        conn.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE sources (
            id TEXT PRIMARY KEY,
            epoch TEXT NOT NULL,
            position INTEGER NOT NULL,
            value TEXT NOT NULL
        );
        CREATE TABLE uses (
            id TEXT PRIMARY KEY,
            spec TEXT NOT NULL,
            signature TEXT NOT NULL,
            generation INTEGER NOT NULL,
            latest TEXT,
            last_rejected TEXT
        );
        CREATE TABLE dependencies (
            source TEXT NOT NULL,
            use_id TEXT NOT NULL,
            PRIMARY KEY (source, use_id)
        );
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            use_id TEXT NOT NULL,
            generation INTEGER NOT NULL,
            basis TEXT NOT NULL,
            spec TEXT NOT NULL,
            ticket INTEGER,
            token TEXT,
            gap TEXT,
            attempt TEXT
        );
        CREATE TABLE selected_history (name TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
        INSERT INTO meta VALUES ('format', 'g60-signal-field/1');
        INSERT INTO meta VALUES ('sequence', '0');
        """)
        conn.commit()
        conn.close()

        with queue_mod.Field(legacy_db) as field:
            field.enroll(sample_use("migrated"))
            res = field.run(
                [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"],
                budget=1,
                timeout=10,
            )
            self.assertEqual(res["worker"]["returncode"], 2)
            self.assertEqual(
                field.inspect(job_id=res["in_flight_job"])["worker_outcome"]["stderr"], "boom"
            )

    # --- W5-B3: NTFS junction / symlink ancestors, handle close on init error, pending stall,
    # bounded capture ---
    def _make_dir_link(self, link_path: Path, target_dir: Path) -> bool:
        if os.name == "nt":
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                return False
            self.addCleanup(
                lambda p=link_path: (
                    subprocess.run(["cmd", "/c", "rmdir", str(p)], capture_output=True)
                    if p.exists() or queue_mod._is_reparse_or_symlink(p)
                    else None
                )
            )
            return True
        link_path.symlink_to(target_dir)
        self.addCleanup(lambda p=link_path: p.unlink(missing_ok=True))
        return True

    def test_refuses_ntfs_junction_or_symlink_ancestors_on_db_request_and_planning_snapshot(self):
        real_dir = self.root / "real-dir"
        real_dir.mkdir()
        link_dir = self.root / "link-dir"
        self.assertTrue(self._make_dir_link(link_dir, real_dir))

        # Database beneath a junction/symlink ancestor is refused.
        with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
            queue_mod.Field(link_dir / "queue.sqlite")

        # Request file beneath a junction/symlink ancestor is refused by CLI.
        req_file = real_dir / "req.json"
        req_file.write_text('{"op":"status"}', encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(QUEUE_PATH),
                "--db",
                str(self.db_path),
                "--request",
                str(link_dir / "req.json"),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("symlink or reparse point", json.loads(proc.stderr)["reason"])

        # planning_snapshot refuses DB beneath a junction/symlink ancestor.
        real_db = real_dir / "snap.sqlite"
        with queue_mod.Field(real_db):
            pass
        with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
            queue_mod.planning_snapshot(link_dir / "snap.sqlite", lambda: real_db.read_bytes())

    def test_corrupt_database_closes_sqlite_handle_on_init_failure(self):
        corrupt = self.root / "corrupt.sqlite"
        corrupt.write_bytes(b"not a valid sqlite database header at all")
        with self.assertRaises(sqlite3.DatabaseError):
            queue_mod.Field(corrupt)
        # Immediately unlinking fails with WinError 32 on Windows if the SQLite connection leaked.
        corrupt.unlink()
        self.assertFalse(corrupt.exists())

    def test_pending_oversized_single_job_advances_cursor_without_infinite_loop(self):
        with queue_mod.Field(self.db_path) as field:
            huge = sample_use("a_huge")
            huge["observer"] = {"definition": "D" * 2500}
            field.enroll(huge)
            field.enroll(sample_use("b_normal"))

            collected = []
            cursor = ""
            steps = 0
            while True:
                steps += 1
                self.assertLessEqual(steps, 5, "pending() stalled in an infinite pagination loop")
                page = field.pending(after=cursor, limit=20, max_bytes=1024)
                self.assertLessEqual(len(queue_mod.encoded(page["items"]).encode("utf-8")), 1024)
                self.assertTrue(
                    page["items"], "pending() must not return empty items while more=True"
                )
                self.assertGreater(page["next_after"], cursor)
                collected.extend(page["items"])
                cursor = page["next_after"]
                if not page["more"]:
                    break

            self.assertEqual([item["job"] for item in collected], ["use:a_huge", "use:b_normal"])
            self.assertTrue(collected[0].get("oversized"))
            self.assertGreater(collected[0]["full_bytes"], 1024)
            self.assertNotIn("oversized", collected[1])
            self.assertEqual(collected[1]["desired_spec"]["id"], "b_normal")
            # Full job remains inspectable with an explicit larger bound.
            full = field.inspect(job_id="use:a_huge", max_bytes=8192)
            self.assertEqual(full["desired_spec"]["observer"]["definition"], "D" * 2500)

    def test_run_bounds_worker_stdout_and_stderr_capture(self):
        with queue_mod.Field(self.db_path) as field:
            field.enroll(sample_use("u1"))
            # Oversized stdout exceeding MAX_WORKER_STDOUT_BYTES is bounded and refused cleanly.
            over_out = queue_mod.MAX_WORKER_STDOUT_BYTES + 16384
            res = field.run(
                [
                    sys.executable,
                    "-c",
                    (
                        'import sys; sys.stdout.write(\'{"state":"complete","account":"\' '
                        f"+ 'A' * {over_out} + '\"}}')"
                    ),
                ],
                budget=1,
                timeout=15,
            )
            self.assertEqual(res["status"], "worker-unresolved")
            self.assertEqual(res["worker"]["kind"], "invalid-return")
            self.assertIn("exceeded", res["reason"])
            self.assertTrue(res["worker"]["stdout_truncated"])
            self.assertGreater(res["worker"]["stdout_bytes"], queue_mod.MAX_WORKER_STDOUT_BYTES)
            self.assertEqual(
                len(res["worker"]["stdout"].encode("utf-8")),
                queue_mod.MAX_WORKER_STREAM_BYTES,
            )

            # Large stderr is streamed with bounded memory while retaining exact byte count &
            # SHA-256.
            job_id = res["in_flight_job"]
            field.retry(job_id, field.inspect(job_id=job_id)["token"])
            big_err = queue_mod.MAX_WORKER_STREAM_BYTES * 8
            import hashlib as _hashlib

            expected_err_sha = _hashlib.sha256(b"Z" * big_err).hexdigest()
            res_err = field.run(
                [
                    sys.executable,
                    "-c",
                    f"import sys; sys.stderr.write('Z' * {big_err}); sys.exit(4)",
                ],
                budget=1,
                timeout=15,
            )
            self.assertEqual(res_err["worker"]["returncode"], 4)
            self.assertEqual(res_err["worker"]["stderr_bytes"], big_err)
            self.assertEqual(res_err["worker"]["stderr_sha256"], expected_err_sha)
            self.assertTrue(res_err["worker"]["stderr_truncated"])
            self.assertEqual(
                len(res_err["worker"]["stderr"].encode("utf-8")),
                queue_mod.MAX_WORKER_STREAM_BYTES,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
