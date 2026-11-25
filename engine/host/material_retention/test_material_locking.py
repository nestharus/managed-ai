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


if __name__ == "__main__":
    unittest.main(verbosity=2)
