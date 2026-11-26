"""Qualify the owner operation for reading completed import acknowledgements."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

if __package__:
    pass
else:
    import sys as _sys
    from pathlib import Path as _Path

    _repo = str(_Path(__file__).resolve().parents[3])
    if _repo not in _sys.path:
        _sys.path.insert(0, _repo)

from managed_ai.core.storage import write_json
from managed_ai.store.records.record_import import import_records, read_receipt
from managed_ai.store.system import System


def specification(ident):
    return {
        "format": "managed-ai-import/1",
        "id": ident,
        "records": [{"id": ident, "kind": "observation", "content": ident}],
    }


class ImportReceiptTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name) / "store"
        System.initialize(root, {"contribution": "Check batch retry and progress."})
        self.system = System(root)

    def test_completed_acknowledgements_remain_origin_identities(self):
        system = self.system
        completed = specification("complete-import")
        interrupted = specification("interrupted-import")
        foreign = specification("foreign-import")

        accepted = import_records(system, completed)
        self.assertEqual(read_receipt(system, completed), accepted)
        with (
            patch.object(system, "put", side_effect=RuntimeError("interruption")),
            self.assertRaisesRegex(RuntimeError, "interruption"),
        ):
            import_records(system, interrupted)
        self.assertFalse(read_receipt(system, interrupted)["complete"])
        other = import_records(system, foreign)
        write_json(
            system.work / ".system/imports/foreign-import.json",
            dict(other, instance_id="another-store"),
        )
        with self.assertRaisesRegex(ValueError, "another store or import identity"):
            read_receipt(system, foreign)

        system.put(dict(completed["records"][0], content="new head"), 1)
        self.assertEqual(read_receipt(system, completed), accepted)
        self.assertEqual(list(system.completed_import_records()), accepted["records"])

    def test_completed_retry_requires_the_as_issued_heads(self):
        spec = specification("observation")
        (row,) = spec["records"]
        system = self.system
        issued = import_records(system, spec)
        receipt = system.work / ".system/imports/observation.json"
        # An as-issued receipt can have an older writer's whitespace.
        receipt.write_bytes(receipt.read_bytes() + b"\n")
        original = receipt.read_bytes()
        with patch(
            "managed_ai.store.records.record_import.write_json",
            side_effect=AssertionError("receipt rewrite"),
        ):
            self.assertEqual(import_records(system, spec), issued)
            for revision, content in [(2, "different content"), (3, row["content"])]:
                with self.subTest(revision=revision):
                    system.put(dict(row, content=content), revision - 1)
                    head = system.record_path(row["id"]).read_bytes()
                    with self.assertRaises(ValueError):
                        import_records(system, spec)
                    self.assertEqual(system.record_path(row["id"]).read_bytes(), head)
                    self.assertEqual(receipt.read_bytes(), original)
                    self.assertEqual(read_receipt(system, spec), issued)
                    self.assertEqual(list(system.completed_import_records()), issued["records"])

    def test_existing_head_requires_exact_nested_import_content(self):
        spec = specification("sample")
        (row,) = spec["records"]
        row["content"] = {"readings": [1]}
        system = self.system
        system.put(dict(row, content={"readings": [True]}), 0)
        before = system.record_path("sample").read_bytes()
        with self.assertRaisesRegex(ValueError, "conflicts with existing"):
            import_records(system, spec)
        self.assertEqual(system.record_path("sample").read_bytes(), before)
        self.assertIsNone(read_receipt(system, spec))

    def test_partial_retry_refuses_a_revised_acknowledged_prefix(self):
        spec = specification("batch")
        spec["records"].append({"id": "later", "kind": "note", "content": "Later"})
        system = self.system
        put = system.put

        def stop_before_later(row, expected):
            if row["id"] == "later":
                raise OSError("stopped before next head")
            return put(row, expected)

        with (
            patch.object(system, "put", stop_before_later),
            self.assertRaisesRegex(OSError, "stopped before next head"),
        ):
            import_records(system, spec)
        partial = read_receipt(system, spec)
        self.assertEqual(partial["records"][0]["revision"], 1)
        partial_path = system.work / ".system/imports/batch.json"
        before = partial_path.read_bytes()
        system.put(spec["records"][0], 1)
        with self.assertRaisesRegex(ValueError, "acknowledged head changed"):
            import_records(system, spec)
        self.assertEqual(before, partial_path.read_bytes())
        self.assertFalse(system.record_path("later").exists())

    def test_resume_after_head_precedes_its_progress_acknowledgement(self):
        spec = specification("batch")
        spec["records"].append({"id": "second", "kind": "note", "content": "Second row"})
        system = self.system

        def stop_at_second_progress(path, value):
            if len(value["records"]) == 2:
                raise OSError("progress interrupted")
            write_json(path, value)

        with (
            patch("managed_ai.store.records.record_import.write_json", stop_at_second_progress),
            self.assertRaisesRegex(OSError, "progress interrupted"),
        ):
            import_records(system, spec)
        partial = read_receipt(system, spec)
        self.assertFalse(partial["complete"])
        self.assertEqual([r["id"] for r in partial["records"]], ["batch"])
        heads = [system.record_path(r["id"]).read_bytes() for r in spec["records"]]
        self.assertEqual(list(system.completed_import_records()), [])
        issued = import_records(system, spec)
        self.assertTrue(issued["complete"])
        self.assertEqual(issued["records"][:1], partial["records"])
        self.assertEqual([r["revision"] for r in issued["records"]], [1, 1])
        self.assertEqual([system.record_path(r["id"]).read_bytes() for r in spec["records"]], heads)
        self.assertEqual(list(system.completed_import_records()), issued["records"])
