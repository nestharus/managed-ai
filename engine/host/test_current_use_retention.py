"""Behavioral retention checks using small actual stores and exported recovery."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
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
