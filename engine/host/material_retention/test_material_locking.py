"""Regression tests for managed_material locking, strict JSON, and orphan handling (SO2(1), A5)."""
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

IMPL = Path(__file__).with_name("managed_material.py")
spec = importlib.util.spec_from_file_location("managed_material_lock_test", IMPL)
material = importlib.util.module_from_spec(spec)
spec.loader.exec_module(material)


class MaterialRetentionHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store_dir = self.root / "store"
        self.store = material.Store(self.store_dir, create=True)

    def make_spec(self, ident, body, **extra):
        src = self.root / f"{ident}.txt"
        src.write_bytes(body)
        doc = {
            "format": "managed-material-selection/1",
            "id": ident,
            "roots": [{"label": "selected", "path": str(src)}],
            "current": [{"role": "current-output", "path": "selected"}],
            "account": f"Capture {ident}.",
            **extra,
        }
        spec_path = self.root / f"{ident}.json"
        spec_path.write_text(json.dumps(doc))
        return spec_path

    # --- A5: Strict JSON serialization and parsing (allow_nan=False & duplicate-key rejection) ---
    def test_strict_json_rejects_nan_infinity_and_duplicate_keys(self):
        with self.assertRaises(ValueError):
            material.js({"bad": math.nan})
        with self.assertRaises(ValueError):
            material.js({"bad": math.inf})
        with self.assertRaises(ValueError):
            material.js({"bad": -math.inf})

        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            material.loads_json(b'{"a":1,"a":2}')
        with self.assertRaisesRegex(ValueError, "Nonfinite JSON"):
            material.loads_json(b'{"a":NaN}')

        bad_spec = self.root / "bad-spec.json"
        bad_spec.write_text('{"format":"managed-material-selection/1","id":"bad","id":"dup","roots":[]}')
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            material.snapshot(self.store, bad_spec)

        nan_spec = self.root / "nan-spec.json"
        nan_spec.write_text('{"format":"managed-material-selection/1","id":"bad","score":NaN,"roots":[]}')
        with self.assertRaisesRegex(ValueError, "Nonfinite JSON"):
            material.snapshot(self.store, nan_spec)

    # --- SO2(1): Cooperative file locking prevents lost updates across concurrent snapshots ---
    def test_concurrent_snapshots_preserve_all_objects_and_occurrences(self):
        # Seed store with initial snapshot so both concurrent writers open an existing index.
        seed = self.make_spec("seed", b"seed body\n")
        material.snapshot(self.store, seed)

        specs = [self.make_spec(f"worker-{i}", f"payload-{i}-unique-body\n".encode()) for i in range(4)]
        procs = [
            subprocess.Popen(
                [sys.executable, "-B", str(IMPL), "snapshot", "--store", str(self.store_dir), "--spec", str(sp)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for sp in specs
        ]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
            results.append(json.loads(out))

        fresh = material.Store(self.store_dir)
        # Every concurrent worker's body and occurrence must survive in index.json and verify cleanly!
        for i in range(4):
            body = f"payload-{i}-unique-body\n".encode()
            self.assertIn(material.sha(body), fresh.objects)
            self.assertEqual(fresh.materialize(material.sha(body)), body)
            verified = material.verify(fresh, occurrence=f"worker-{i}")
            self.assertGreater(verified["objects_verified"], 0)
        # Current selection must also verify cleanly.
        material.verify(fresh)

    def test_nonblocking_store_lock_detects_active_holder(self):
        ready = self.root / "locked.flag"
        release = self.root / "release.flag"
        holder_code = "\n".join([
            "import importlib.util, sys, time",
            "from pathlib import Path",
            "spec = importlib.util.spec_from_file_location('mm', sys.argv[1])",
            "mm = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(mm)",
            "with mm.store_lock(Path(sys.argv[2])):",
            "    Path(sys.argv[3]).write_text('ready')",
            "    deadline = time.monotonic() + 15",
            "    while not Path(sys.argv[4]).exists() and time.monotonic() < deadline:",
            "        time.sleep(0.05)",
        ])
        proc = subprocess.Popen(
            [sys.executable, "-B", "-c", holder_code, str(IMPL),
             str(self.store_dir), str(ready), str(release)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.exists():
                out, err = proc.communicate(timeout=5)
                self.fail(f"Lock holder subprocess failed (rc={proc.returncode}): {err or out}")
            with self.assertRaises(BlockingIOError):
                with material.store_lock(self.store_dir, nonblocking=True):
                    pass
        finally:
            release.write_text("go")
            proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0)

    def test_retire_reloads_and_refuses_stale_selection(self):
        folder = self.root / "retire-me"
        folder.mkdir()
        (folder / "old.txt").write_bytes(b"obsolete data")
        req = self.root / "init.json"
        req.write_text(json.dumps({
            "format": "managed-material-selection/1",
            "id": "initial",
            "roots": [{"label": "obsolete", "path": str(folder)}],
            "current": [],
            "materialization_groups": [{"prefix": "obsolete", "state": "retirement-candidate"}],
        }))
        first = material.snapshot(self.store, req)
        proposal = {
            "format": "managed-material-retirement-proposal/1",
            "prefixes": ["obsolete"],
            "protected_prefixes": ["active"],
            "account": "Test stale retirement guard.",
        }
        plan = material.retirement_plan(self.store, proposal)
        plan_path = self.root / "plan.json"
        plan_path.write_bytes(material.js(plan))

        # Another Store instance advances CURRENT.json.
        other_store = material.Store(self.store_dir)
        second_spec = self.make_spec("second", b"newer current")
        material.snapshot(other_store, second_spec)

        # Calling retire on the older Store handle must reload under lock and refuse stale expected_selection.
        with self.assertRaisesRegex(ValueError, "Current selection changed since receiving"):
            material.retire(self.store, plan_path, material.sha(plan_path.read_bytes()), first["selection"])
        self.assertTrue(folder.exists())

    # --- SO2(1): Orphaned occurrence prevention and explicit reporting ---
    def test_selection_write_failure_does_not_create_orphan_occurrence(self):
        first_spec = self.make_spec("first", b"first bytes")
        material.snapshot(self.store, first_spec)

        real_immutable = material.immutable
        def fail_selection(path, raw):
            if Path(path).parent.name == "selections":
                raise OSError("selection disk full")
            return real_immutable(path, raw)

        second_spec = self.make_spec("second", b"second bytes")
        with patch.object(material, "immutable", side_effect=fail_selection):
            with self.assertRaisesRegex(OSError, "selection disk full"):
                material.snapshot(self.store, second_spec)

        # Because occurrence pointer is written AFTER selection succeeds, no orphan occurrence exists!
        self.assertFalse((self.store_dir / "occurrences/second.json").exists())
        self.assertEqual(material.unpublished_occurrences(self.store), [])
        last = json.loads((self.store_dir / "events.jsonl").read_bytes().splitlines()[-1])
        self.assertFalse(last["occurrence_retained"])
        self.assertFalse(last["selection_published"])

    def test_failed_current_replace_records_explicit_unpublished_occurrence(self):
        first_spec = self.make_spec("first", b"first bytes")
        first = material.snapshot(self.store, first_spec)

        real_replace = material.replace_json
        def refuse_current(path, value):
            if Path(path).name == "CURRENT.json":
                raise OSError("pointer unavailable")
            return real_replace(path, value)

        second_spec = self.make_spec("second", b"retained bytes")
        with patch.object(material, "replace_json", side_effect=refuse_current):
            with self.assertRaisesRegex(OSError, "pointer unavailable"):
                material.snapshot(self.store, second_spec)

        # Preserves existing contract while eliminating silent orphan inconsistency.
        self.assertEqual(material.current_selection(self.store)[0], first["selection"])
        self.assertTrue((self.store_dir / "occurrences/second.json").is_file())
        last = json.loads((self.store_dir / "events.jsonl").read_bytes().splitlines()[-1])
        self.assertFalse(last["selection_published"])
        self.assertTrue(last["occurrence_retained"])
        self.assertIn("manifest", last)
        self.assertIn("selection", last)
        self.assertIn("recovery", last)

        unpublished = material.unpublished_occurrences(self.store)
        self.assertEqual(len(unpublished), 1)
        self.assertEqual(unpublished[0]["id"], "second")
        self.assertEqual(unpublished[0]["manifest"], last["manifest"])
        self.assertEqual(unpublished[0]["selection"], last["selection"])

    # --- C4-1(a): Read ops reload Store under shared lock when another writer updated the store ---
    def test_reused_store_handle_reloads_on_read_operations(self):
        first_spec = self.make_spec("first", b"first body")
        material.snapshot(self.store, first_spec)
        reused = material.Store(self.store_dir)

        # Another Store handle commits a new snapshot with new objects.
        writer = material.Store(self.store_dir)
        second_spec = self.make_spec("second", b"brand new body from writer")
        material.snapshot(writer, second_spec)

        # Reused handle had stale objects before reload; verify/restore/export must reload cleanly!
        verified = material.verify(reused)
        self.assertGreater(verified["objects_verified"], 0)
        restored_dir = self.root / "restored-from-reused"
        material.restore(reused, restored_dir)
        self.assertEqual((restored_dir / "selected").read_bytes(), b"brand new body from writer")
        archive = self.root / "exported-from-reused.zip"
        material.export(reused, archive)
        self.assertTrue(archive.is_file())

    # --- C4-1(b)+(c): Pre-lock store creates .store.lock on shared read & thread nonblocking raises ---
    def test_shared_lock_creates_lockfile_and_thread_nonblocking_raises(self):
        # Remove .store.lock to simulate a pre-lock or extracted-export store.
        lock_file = self.store_dir / ".store.lock"
        if lock_file.exists():
            lock_file.unlink()
        with material.store_lock(self.store_dir, shared=True):
            self.assertTrue(lock_file.exists())

        # Cross-thread nonblocking=True must immediately raise BlockingIOError without blocking on rlock.
        import threading
        entered = threading.Event()
        release = threading.Event()
        def hold():
            with material.store_lock(self.store_dir):
                entered.set()
                release.wait(timeout=10)
        t = threading.Thread(target=hold)
        t.start()
        try:
            self.assertTrue(entered.wait(timeout=5))
            with self.assertRaises(BlockingIOError):
                with material.store_lock(self.store_dir, nonblocking=True):
                    pass
        finally:
            release.set()
            t.join(timeout=5)

    # --- C4-2: Hard-crash orphan (no snapshot-failed event) & retain_artifact failure detected ---
    def test_unpublished_occurrences_detects_hard_crash_and_failed_retain_artifact(self):
        first_spec = self.make_spec("first", b"first published")
        material.snapshot(self.store, first_spec)

        # 1. Simulate abrupt SIGKILL between occurrence pointer write and CURRENT.json replacement:
        # snapshot-started is written, occurrence/manifest/selection exist, NO snapshot-failed event!
        crash_spec = self.make_spec("hard-crash", b"hard crash payload")
        def simulate_sigkill(path, value):
            if Path(path).name == "CURRENT.json":
                raise BaseException("simulated SIGKILL")
            return real_replace(path, value)
        real_replace = material.replace_json
        with patch.object(material, "replace_json", side_effect=simulate_sigkill):
            with self.assertRaises(BaseException):
                material.snapshot(self.store, crash_spec)
        # Confirm no snapshot-failed was logged for hard-crash.
        events = [material.loads_json(line) for line in (self.store_dir / "events.jsonl").read_bytes().splitlines()]
        self.assertFalse(any(e.get("kind") == "snapshot-failed" and e.get("id") == "hard-crash" for e in events))

        # 2. Simulate failed retain_artifact() at CURRENT.json replacement.
        extra = self.root / "extra-evidence.bin"
        extra.write_bytes(b"historical artifact bytes")
        def fail_current_os(path, value):
            if Path(path).name == "CURRENT.json":
                raise OSError("current pointer busy")
            return real_replace(path, value)
        with patch.object(material, "replace_json", side_effect=fail_current_os):
            with self.assertRaisesRegex(OSError, "current pointer busy"):
                material.retain_artifact(self.store, extra, "evidence", "Retain failed test.")

        unpublished = {row["id"]: row for row in material.unpublished_occurrences(self.store)}
        self.assertIn("hard-crash", unpublished)
        self.assertEqual(unpublished["hard-crash"]["reason"], "unconfirmed-occurrence-not-published")
        self.assertIsNotNone(unpublished["hard-crash"]["manifest"])
        self.assertIsNotNone(unpublished["hard-crash"]["selection"])

        retained_id = "retained-evidence-" + material.sha(b"historical artifact bytes")[:16]
        self.assertIn(retained_id, unpublished)
        self.assertEqual(unpublished[retained_id]["reason"], "historical-artifact-retention-failed-before-current-publish")
        self.assertEqual(unpublished[retained_id]["error"], "current pointer busy")
        self.assertNotIn("first", unpublished)


if __name__ == "__main__":
    unittest.main(verbosity=2)
