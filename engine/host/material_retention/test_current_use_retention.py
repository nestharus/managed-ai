"""Behavioral retention checks using small actual stores and exported recovery."""
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile


IMPL = Path(os.environ.get("MATERIAL_IMPLEMENTATION", Path(__file__).with_name("managed_material.py")))
spec = importlib.util.spec_from_file_location("material_under_test", IMPL)
material = importlib.util.module_from_spec(spec)
spec.loader.exec_module(material)


class RetentionContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = material.Store(self.root / "store", create=True)

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, name, body, **options):
        source = self.root / (name + ".txt")
        source.write_bytes(body)
        value = {"format": "managed-material-selection/1", "id": name,
                 "roots": [{"label": "selected", "path": str(source)}],
                 "current": [{"role": "current-output", "path": "selected"}],
                 "account": "Selected useful test body.", **options}
        path = self.root / (name + ".json")
        path.write_text(json.dumps(value))
        return material.snapshot(self.store, path)

    def exported_store(self, name):
        archive = self.root / (name + ".zip")
        result = material.export(self.store, archive)
        target = self.root / (name + "-restored-store")
        with zipfile.ZipFile(archive) as z:
            z.extractall(target)
        recovered = material.Store(target)
        material.verify(recovered)
        return recovered, result

    def test_default_keeps_current_bytes_without_ancestry(self):
        first = self.capture("first", b"superseded source\n")
        obsolete = material.sha(b"superseded source\n")
        current = b"Current chosen contribution: capacity differs from intention.\n"
        second = self.capture("second", current)
        recovered, report = self.exported_store("current")
        portable = recovered.root / "managed_material.py"
        self.assertEqual(portable.read_bytes(), IMPL.read_bytes())
        for args in (["verify"], ["restore", "--output", str(self.root / "portable-output")]):
            subprocess.run([sys.executable, "-I", str(portable), *args, "--store", ".", "--occurrence", "second"],
                           cwd=recovered.root, check=True, capture_output=True)
        self.assertEqual((self.root / "portable-output/selected").read_bytes(), current)
        self.assertNotIn(obsolete, recovered.objects)
        self.assertNotIn(first["manifest"], material.selection_closure(recovered, second["selection"])[2])
        self.assertEqual(report["retained_selections"], 1)
        material.restore(recovered, self.root / "current-output")
        self.assertEqual((self.root / "current-output/selected").read_bytes(), current)
        # Omitting a body from an export does not delete it from the source store.
        self.assertEqual(self.store.materialize(obsolete), b"superseded source\n")

    def test_explicit_legacy_history_remains_recoverable(self):
        first = self.capture("first", b"needed earlier evidence\n")
        self.capture("second", b"current body\n", history=[first["manifest"]])
        recovered, report = self.exported_store("explicit")
        self.assertEqual(report["historical_manifests"], 2)
        material.restore(recovered, self.root / "previous-output", occurrence="first")
        self.assertEqual((self.root / "previous-output/selected").read_bytes(), b"needed earlier evidence\n")
        material.restore(recovered, self.root / "later-output")
        self.assertEqual((self.root / "later-output/selected").read_bytes(), b"current body\n")

    def test_append_history_is_an_explicit_choice(self):
        self.capture("first", b"first observation\n")
        self.capture("second", b"second observation\n", retention_policy="append-history")
        recovered, report = self.exported_store("appended")
        self.assertEqual(report["historical_manifests"], 2)
        self.assertEqual(report["retained_selections"], 2)
        self.assertEqual(recovered.materialize(material.sha(b"first observation\n")), b"first observation\n")

    def test_conflicting_policy_refuses_before_new_selection(self):
        first = self.capture("first", b"still current\n")
        before = (self.store.root / "CURRENT.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "current-use"):
            self.capture("conflict", b"not published\n", retention_policy="current-use",
                         history=[first["manifest"]])
        self.assertEqual((self.store.root / "CURRENT.json").read_bytes(), before)
        self.assertFalse(self.last_event()["selection_published"])

    def last_event(self):
        return json.loads((self.store.root / "events.jsonl").read_bytes().splitlines()[-1])

    def test_failed_pointer_replace_retains_occurrence_but_keeps_the_previous_selection(self):
        first = self.capture("first", b"previous bytes")
        replace_pointer = material.replace_json

        def refuse_current(path, value):
            if path.name == "CURRENT.json":
                raise OSError("pointer unavailable")
            replace_pointer(path, value)

        with patch.object(material, "replace_json", refuse_current), self.assertRaisesRegex(OSError, "pointer unavailable"):
            self.capture("second", b"retained bytes")

        self.assertEqual(material.current_selection(self.store)[0], first["selection"])
        self.assertFalse(self.last_event()["selection_published"])
        self.assertTrue((self.store.root / "occurrences/second.json").is_file())
        with self.assertRaisesRegex(ValueError, "Occurrence identity already exists"):
            self.capture("second", b"retained bytes")

    def test_reporting_failures_leave_the_new_selection_published(self):
        for failure in ("statistics", "completion event"):
            with self.subTest(failure=failure):
                occurrence = "selected-" + failure.split()[0]
                body = occurrence.encode()
                record_event = self.store.event

                def fail_completion(kind, data):
                    if kind == "snapshot-completed":
                        raise OSError(failure)
                    record_event(kind, data)

                fault = (patch.object(self.store, "stats", side_effect=OSError(failure))
                         if failure == "statistics" else patch.object(self.store, "event", fail_completion))
                with fault, self.assertRaisesRegex(OSError, failure):
                    self.capture(occurrence, body)

                key, selected = material.current_selection(self.store)
                self.assertEqual(selected["occurrence"], occurrence)
                self.assertEqual(self.store.materialize(selected["current"][0]["object"]), body)
                event = self.last_event()
                self.assertEqual(event["kind"], "snapshot-failed")
                self.assertTrue(event["selection_published"])
                self.assertEqual(event["selection"], key)
                self.assertEqual(event["manifest"], selected["manifest"])
                with self.assertRaisesRegex(ValueError, "Occurrence identity already exists"):
                    self.capture(occurrence, body)

    def test_current_use_excludes_old_transport_and_retirement(self):
        source = self.root / "obsolete"
        source.mkdir()
        archive = source / "old.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("old.txt", b"unneeded earlier transport payload")
        original = archive.read_bytes()
        request = self.root / "initial.json"
        request.write_text(json.dumps({"format": "managed-material-selection/1", "id": "old-transport",
            "roots": [{"label": "obsolete", "path": str(source)}], "current": [],
            "materialization_groups": [{"prefix": "obsolete", "state": "retirement-candidate"}]}))
        first = material.snapshot(self.store, request)
        proposal = {"format": "managed-material-retirement-proposal/1", "prefixes": ["obsolete"],
                    "protected_prefixes": ["current-output"], "account": "Disposable test transport is no longer used."}
        plan = material.retirement_plan(self.store, proposal)
        plan_path = self.root / "retirement.json"
        plan_path.write_bytes(material.js(plan))
        material.retire(self.store, plan_path, material.sha(plan_path.read_bytes()), first["selection"])
        self.assertFalse(source.exists())
        self.capture("current", b"selected current artifact\n")
        recovered, report = self.exported_store("pruned")
        self.assertEqual(report["retained_lifecycle_receipts"], 0)
        self.assertEqual(report["retained_selections"], 1)
        self.assertEqual(report["historical_manifests"], 1)
        self.assertNotIn(material.sha(original), recovered.objects)
        self.assertEqual(self.store.materialize(material.sha(original)), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
