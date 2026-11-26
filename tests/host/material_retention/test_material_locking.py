"""Regression tests for managed_material locking, strict JSON, and orphan handling (SO2(1), A5)."""

import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from managed_ai_information.host.material_retention import managed_material as material  # noqa: E402

IMPL_MODULE = "managed_ai_information.host.material_retention"


def _subproc_env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC_ROOT) if not existing else str(SRC_ROOT) + os.pathsep + existing
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


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

        with self.assertRaisesRegex(ValueError, "(?i)duplicate JSON key"):
            material.loads_json(b'{"a":1,"a":2}')
        with self.assertRaisesRegex(ValueError, "(?i)nonfinite JSON"):
            material.loads_json(b'{"a":NaN}')

        bad_spec = self.root / "bad-spec.json"
        bad_spec.write_text(
            '{"format":"managed-material-selection/1","id":"bad","id":"dup","roots":[]}'
        )
        with self.assertRaisesRegex(ValueError, "(?i)duplicate JSON key"):
            material.snapshot(self.store, bad_spec)

        nan_spec = self.root / "nan-spec.json"
        nan_spec.write_text(
            '{"format":"managed-material-selection/1","id":"bad","score":NaN,"roots":[]}'
        )
        with self.assertRaisesRegex(ValueError, "(?i)nonfinite JSON"):
            material.snapshot(self.store, nan_spec)

    # --- SO2(1): Cooperative file locking prevents lost updates across concurrent snapshots ---
    def test_concurrent_snapshots_preserve_all_objects_and_occurrences(self):
        # Seed store with initial snapshot so both concurrent writers open an existing index.
        seed = self.make_spec("seed", b"seed body\n")
        material.snapshot(self.store, seed)

        specs = [
            self.make_spec(f"worker-{i}", f"payload-{i}-unique-body\n".encode()) for i in range(4)
        ]
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    IMPL_MODULE,
                    "snapshot",
                    "--store",
                    str(self.store_dir),
                    "--spec",
                    str(sp),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_subproc_env(),
            )
            for sp in specs
        ]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err)
            results.append(json.loads(out))

        fresh = material.Store(self.store_dir)
        # Every concurrent worker's body and occurrence must survive in index.json and verify
        # cleanly!
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
        holder_code = "\n".join(
            [
                "import sys, time",
                "from pathlib import Path",
                "from managed_ai_information.host.material_retention import managed_material as mm",
                "with mm.store_lock(Path(sys.argv[1])):",
                "    Path(sys.argv[2]).write_text('ready')",
                "    deadline = time.monotonic() + 15",
                "    while not Path(sys.argv[3]).exists() and time.monotonic() < deadline:",
                "        time.sleep(0.05)",
            ]
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                holder_code,
                str(self.store_dir),
                str(ready),
                str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_subproc_env(),
        )
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.exists():
                out, err = proc.communicate(timeout=5)
                self.fail(f"Lock holder subprocess failed (rc={proc.returncode}): {err or out}")
            with (
                self.assertRaises(BlockingIOError),
                material.store_lock(self.store_dir, nonblocking=True),
            ):
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
        req.write_text(
            json.dumps(
                {
                    "format": "managed-material-selection/1",
                    "id": "initial",
                    "roots": [{"label": "obsolete", "path": str(folder)}],
                    "current": [],
                    "materialization_groups": [
                        {"prefix": "obsolete", "state": "retirement-candidate"}
                    ],
                }
            )
        )
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

        # Calling retire on the older Store handle must reload under lock and refuse stale
        # expected_selection.
        with self.assertRaisesRegex(ValueError, "Current selection changed since receiving"):
            material.retire(
                self.store, plan_path, material.sha(plan_path.read_bytes()), first["selection"]
            )
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
        with (
            patch.object(material, "immutable", side_effect=fail_selection),
            self.assertRaisesRegex(OSError, "selection disk full"),
        ):
            material.snapshot(self.store, second_spec)

        # Because occurrence pointer is written AFTER selection succeeds, no orphan occurrence
        # exists!
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
        with (
            patch.object(material, "replace_json", side_effect=refuse_current),
            self.assertRaisesRegex(OSError, "pointer unavailable"),
        ):
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

    # --- C4-1(b)+(c): Pre-lock store creates .store.lock on shared read & thread nonblocking raises
    # ---
    def test_shared_lock_creates_lockfile_and_thread_nonblocking_raises(self):
        # Remove .store.lock to simulate a pre-lock or extracted-export store.
        lock_file = self.store_dir / ".store.lock"
        if lock_file.exists():
            lock_file.unlink()
        with material.store_lock(self.store_dir, shared=True):
            self.assertTrue(lock_file.exists())

        # Cross-thread nonblocking=True must immediately raise BlockingIOError without blocking on
        # rlock.
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
            with (
                self.assertRaises(BlockingIOError),
                material.store_lock(self.store_dir, nonblocking=True),
            ):
                pass
        finally:
            release.set()
            t.join(timeout=5)

    # --- C4-2: Hard-crash orphan (no snapshot-failed event) & retain_artifact failure detected ---
    def test_unpublished_occurrences_detects_hard_crash_and_failed_retain_artifact(self):
        first_spec = self.make_spec("first", b"first published")
        material.snapshot(self.store, first_spec)

        # 1. Simulate abrupt SIGKILL between occurrence pointer write and CURRENT.json replacement:
        # snapshot-started is written, occurrence/manifest/selection exist, NO snapshot-failed
        # event!
        crash_spec = self.make_spec("hard-crash", b"hard crash payload")

        def simulate_sigkill(path, value):
            if Path(path).name == "CURRENT.json":
                raise BaseException("simulated SIGKILL")
            return real_replace(path, value)

        real_replace = material.replace_json
        with (
            patch.object(material, "replace_json", side_effect=simulate_sigkill),
            self.assertRaises(BaseException),  # noqa: B017
        ):
            material.snapshot(self.store, crash_spec)
        # Confirm no snapshot-failed was logged for hard-crash.
        events = [
            material.loads_json(line)
            for line in (self.store_dir / "events.jsonl").read_bytes().splitlines()
        ]
        self.assertFalse(
            any(e.get("kind") == "snapshot-failed" and e.get("id") == "hard-crash" for e in events)
        )

        # 2. Simulate failed retain_artifact() at CURRENT.json replacement.
        extra = self.root / "extra-evidence.bin"
        extra.write_bytes(b"historical artifact bytes")

        def fail_current_os(path, value):
            if Path(path).name == "CURRENT.json":
                raise OSError("current pointer busy")
            return real_replace(path, value)

        with (
            patch.object(material, "replace_json", side_effect=fail_current_os),
            self.assertRaisesRegex(OSError, "current pointer busy"),
        ):
            material.retain_artifact(self.store, extra, "evidence", "Retain failed test.")

        unpublished = {row["id"]: row for row in material.unpublished_occurrences(self.store)}
        self.assertIn("hard-crash", unpublished)
        self.assertEqual(
            unpublished["hard-crash"]["reason"], "unconfirmed-occurrence-not-published"
        )
        self.assertIsNotNone(unpublished["hard-crash"]["manifest"])
        self.assertIsNotNone(unpublished["hard-crash"]["selection"])

        retained_id = "retained-evidence-" + material.sha(b"historical artifact bytes")[:16]
        self.assertIn(retained_id, unpublished)
        self.assertEqual(
            unpublished[retained_id]["reason"],
            "historical-artifact-retention-failed-before-current-publish",
        )
        self.assertEqual(unpublished[retained_id]["error"], "current pointer busy")
        self.assertNotIn("first", unpublished)

    def test_decomposed_exports_and_shared_lock_coexistence(self):
        # Verify all key public symbols remain re-exported on managed_material after decomposition.
        for name in (
            "sha",
            "loads_json",
            "js",
            "utc",
            "require",
            "sync_dir",
            "store_lock",
            "immutable",
            "replace_json",
            "deflate",
            "safe_relative",
            "path_state",
            "read_manifest",
            "manifest_closure",
            "current_selection",
            "selection_closure",
            "representation_closure",
            "observed_materialization_state",
            "materialization_state",
            "selected_for_manifest",
            "unpublished_occurrences",
            "Store",
        ):
            self.assertTrue(hasattr(material, name), f"Missing re-export: {name}")

        # Cross-process shared locks coexist while blocking nonblocking exclusive acquisition.
        ready = self.root / "shared.ready"
        release = self.root / "shared.release"
        holder_code = "\n".join(
            [
                "import sys, time",
                "from pathlib import Path",
                "from managed_ai_information.host.material_retention import managed_material as mm",
                "with mm.store_lock(Path(sys.argv[1]), shared=True):",
                "    Path(sys.argv[2]).write_text('ready', encoding='utf-8')",
                "    deadline = time.monotonic() + 15",
                "    while not Path(sys.argv[3]).exists() and time.monotonic() < deadline:",
                "        time.sleep(0.05)",
            ]
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                holder_code,
                str(self.store_dir),
                str(ready),
                str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_subproc_env(),
        )
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists())
            with material.store_lock(self.store_dir, shared=True, nonblocking=True):
                pass
            with (
                self.assertRaises(BlockingIOError),
                material.store_lock(self.store_dir, shared=False, nonblocking=True),
            ):
                pass
        finally:
            release.write_text("go", encoding="utf-8")
            proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0)

    # --- W4-B5: rebuild() acquires shared store_lock on source store ---
    def test_rebuild_acquires_shared_store_lock_and_blocks_behind_exclusive_writer(self):
        seed = self.make_spec("seed", b"rebuild seed payload\n")
        seed_res = material.snapshot(self.store, seed)
        expected_index = material.sha((self.store_dir / "index.json").read_bytes())

        ready = self.root / "rebuild-lock.ready"
        release = self.root / "rebuild-lock.release"
        holder_code = "\n".join(
            [
                "import sys, time",
                "from pathlib import Path",
                "from managed_ai_information.host.material_retention import managed_material as mm",
                "with mm.store_lock(Path(sys.argv[1]), shared=False):",
                "    Path(sys.argv[2]).write_text('ready', encoding='utf-8')",
                "    deadline = time.monotonic() + 15",
                "    while not Path(sys.argv[3]).exists() and time.monotonic() < deadline:",
                "        time.sleep(0.05)",
            ]
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                holder_code,
                str(self.store_dir),
                str(ready),
                str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_subproc_env(),
        )
        rebuilt_dir = self.root / "rebuilt-store"
        rebuild_proc = None
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists())
            rebuild_proc = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    IMPL_MODULE,
                    "rebuild",
                    "--source-store",
                    str(self.store_dir),
                    "--output",
                    str(rebuilt_dir),
                    "--expected-index",
                    expected_index,
                    "--expected-selection",
                    seed_res["selection"],
                    "--account",
                    "Verify rebuild shared lock waits for exclusive writer.",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_subproc_env(),
            )
            # While exclusive lock is held, rebuild must wait rather than read a torn store.
            time.sleep(0.35)
            self.assertIsNone(
                rebuild_proc.poll(), "rebuild exited while exclusive store_lock was held"
            )
        finally:
            release.write_text("go", encoding="utf-8")
            proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0)
        out, err = rebuild_proc.communicate(timeout=30)
        self.assertEqual(rebuild_proc.returncode, 0, err)
        report = json.loads(out)
        self.assertEqual(report["format"], "managed-material-rebuild-result/1")
        self.assertTrue(report["source_untouched"])
        material.verify(material.Store(rebuilt_dir))

    # --- W5-B1: Drive-letter/NUL safe_relative & NTFS junction / symlink guards ---
    def test_safe_relative_rejects_windows_drives_nul_and_traversal(self):
        for bad in (
            "",
            ".",
            "./ok",
            "a/./b",
            "../escape",
            "a/../b",
            "a\\b",
            "C:foo",
            "c:bar/baz",
            "C:/foo",
            "a/C:b",
            "a/b\x00c",
            "/abs/path",
        ):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(ValueError, "Unsafe materialization path"),
            ):
                material.safe_relative(bad)
        for good in ("selected", "a/b/c.txt", "obsolete/"):
            with self.subTest(good=good):
                self.assertTrue(material.safe_relative(good).parts)

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
                    if p.exists() or material.is_reparse_or_symlink(p)
                    else None
                )
            )
            return True
        link_path.symlink_to(target_dir)
        self.addCleanup(lambda p=link_path: p.unlink(missing_ok=True))
        return True

    def test_reparse_point_and_symlink_refused_across_store_restore_retire_rebuild(self):
        seed = self.make_spec("seed", b"junction test payload\n")
        seed_res = material.snapshot(self.store, seed)
        expected_index = material.sha((self.store_dir / "index.json").read_bytes())

        link_store = self.root / "store-link"
        self.assertTrue(self._make_dir_link(link_store, self.store_dir))

        with (
            self.assertRaisesRegex(ValueError, "symlink or reparse point"),
            material.store_lock(link_store),
        ):
            pass
        with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
            material.Store(link_store)

        out_real = self.root / "out-real"
        out_real.mkdir()
        out_link = self.root / "out-link"
        self.assertTrue(self._make_dir_link(out_link, out_real))
        with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
            material.restore(self.store, out_link / "restored")
        with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
            material.export(self.store, out_link / "export.zip")
        with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
            material.rebuild(
                link_store, self.root / "rebuilt-1", expected_index, seed_res["selection"], "test"
            )
        with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
            material.rebuild(
                self.store_dir,
                out_link / "rebuilt-2",
                expected_index,
                seed_res["selection"],
                "test",
            )

        # retain_artifact must refuse files reached through a junction/symlink ancestor.
        extra_dir = self.root / "extra-real"
        extra_dir.mkdir()
        (extra_dir / "evidence.txt").write_bytes(b"evidence")
        extra_link = self.root / "extra-link"
        self.assertTrue(self._make_dir_link(extra_link, extra_dir))
        with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
            material.retain_artifact(self.store, extra_link / "evidence.txt", "ev", "test")

        # retire() and _remove_planned_materializations must NEVER rmtree an NTFS junction or
        # symlink!
        victim = self.root / "victim-dir"
        victim.mkdir()
        precious = victim / "precious.txt"
        precious.write_bytes(b"do not delete me")

        retire_dir = self.root / "retire-target"
        retire_dir.mkdir()
        (retire_dir / "old.txt").write_bytes(b"retirable bytes")
        req = self.root / "retire-spec.json"
        req.write_text(
            json.dumps(
                {
                    "format": "managed-material-selection/1",
                    "id": "retire-occ",
                    "roots": [{"label": "obsolete", "path": str(retire_dir)}],
                    "current": [],
                    "materialization_groups": [
                        {"prefix": "obsolete", "state": "retirement-candidate"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        occ = material.snapshot(self.store, req)
        proposal = {
            "format": "managed-material-retirement-proposal/1",
            "prefixes": ["obsolete"],
            "protected_prefixes": ["active"],
            "account": "Verify junction refusal before rmtree.",
        }
        plan = material.retirement_plan(self.store, proposal)
        plan_path = self.root / "retire-plan.json"
        plan_path.write_bytes(material.js(plan))

        # Swap retire_dir for a junction/symlink pointing to victim!
        import shutil as _shutil

        _shutil.rmtree(retire_dir)
        self.assertTrue(self._make_dir_link(retire_dir, victim))
        with self.assertRaisesRegex(ValueError, "reparse|junction|changed"):
            material.retire(
                self.store, plan_path, material.sha(plan_path.read_bytes()), occ["selection"]
            )
        self.assertTrue(precious.is_file())
        self.assertEqual(precious.read_bytes(), b"do not delete me")

        # Also directly exercise _remove_planned_materializations (simulating TOCTOU after plan
        # check).
        if os.name == "nt":
            with self.assertRaisesRegex(ValueError, "reparse|junction"):
                material._retirement._remove_planned_materializations(
                    self.store,
                    plan["materializations"],
                    [],
                    "p",
                    occ["selection"],
                )
            self.assertTrue(precious.is_file())

    # --- W5-R2 / W7: Decomposed export ZIP bundles miniature package + vendored _platform under
    # python -I ---
    def test_decomposed_export_zip_bundles_all_modules_and_runs_isolated(self):
        import zipfile as _zipfile

        spec = self.make_spec("portable-w5", b"portable wave5 payload\n")
        material.snapshot(self.store, spec)
        archive = self.root / "portable-w5.zip"
        material.export(self.store, archive)
        expected_modules = {
            "managed_material.py",
            "material_retention/__init__.py",
            "material_retention/__main__.py",
            "material_retention/_platform_bridge.py",
            "material_retention/config.py",
            "material_retention/store_io.py",
            "material_retention/retention_closures.py",
            "material_retention/store_core.py",
            "material_retention/snapshot_ops.py",
            "material_retention/transfer_ops.py",
            "material_retention/material_ops.py",
            "material_retention/retirement.py",
            "material_retention/managed_material.py",
            "material_retention/_platform/__init__.py",
            "material_retention/_platform/adapter.py",
            "material_retention/_platform/_common.py",
            "material_retention/_platform/_posix.py",
            "material_retention/_platform/_windows.py",
        }
        with _zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
            self.assertTrue(
                expected_modules <= names,
                f"Missing modules in export ZIP: {expected_modules - names}",
            )
            extracted = self.root / "extracted-portable"
            zf.extractall(extracted)
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                str(extracted / "managed_material.py"),
                "verify",
                "--store",
                ".",
            ],
            cwd=extracted,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc_mod = subprocess.run(
            [sys.executable, "-B", "-m", "material_retention", "verify", "--store", "."],
            cwd=extracted,
            capture_output=True,
            text=True,
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        )
        self.assertEqual(proc_mod.returncode, 0, proc_mod.stderr)
        restored = self.root / "extracted-restored"
        proc_restore = subprocess.run(
            [
                sys.executable,
                "-I",
                str(extracted / "managed_material.py"),
                "restore",
                "--store",
                ".",
                "--output",
                str(restored),
            ],
            cwd=extracted,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc_restore.returncode, 0, proc_restore.stderr)
        self.assertEqual((restored / "selected").read_bytes(), b"portable wave5 payload\n")
        # Re-exporting from inside the extracted standalone recovery archive must also stay
        # self-contained.
        reexport = self.root / "reexported.zip"
        proc_reexport = subprocess.run(
            [
                sys.executable,
                "-I",
                str(extracted / "managed_material.py"),
                "export",
                "--store",
                ".",
                "--output",
                str(reexport),
            ],
            cwd=extracted,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc_reexport.returncode, 0, proc_reexport.stderr)
        self.assertTrue(reexport.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
