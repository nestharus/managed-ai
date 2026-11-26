"""Regression tests for bounded exposure.json and chunked outside_view_records."""

if not __package__:
    import sys as _sys
    from pathlib import Path as _Path

    _src = str(_Path(__file__).resolve().parents[3] / "src")
    if _src not in _sys.path:
        _sys.path.insert(0, _src)

import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from managed_ai_information.core.formats import (
    RECORD_ID_CHUNK_FORMAT,
    TURN_EXPOSURE_FORMAT,
    TURN_VIEW_FORMAT,
)
from managed_ai_information.core.storage import (
    MAX_BYTES,
    encode,
    exclusive,
    read_json,
    sha,
    strict_json_loads,
)
from managed_ai_information.store.records.object_audit import audit_objects, gc_objects
from managed_ai_information.store.system import System
from managed_ai_information.store.turns.explain_delivery import captured_view, explain_context
from managed_ai_information.store.turns.record_scope import RecordSelection, TurnScope
from managed_ai_information.store.turns.turn_retirement import (
    execute_turn_retirement,
    plan_turn_retirement,
)
from managed_ai_information.store.turns.view_chunks import (
    OUTSIDE_VIEW_INLINE_LIMIT,
    RECORD_ID_CHUNK_MAX_BYTES,
    hydrate_outside_view_records,
    outside_view_record_count,
)


class BoundedViewChunkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store_root = self.base / "store"
        System.initialize(self.store_root, {"contribution": "Test bounded view chunking."})
        self.system = System(self.store_root)
        self.reply = self.base / "reply.md"
        self.reply.write_text("Turn reply.\n", encoding="utf-8")
        self.output = self.base / "output.md"
        self.output.write_text("Adopted output.\n", encoding="utf-8")

    def _prepare_huge_outside_turn(self, turn: str, outside_ids: list[str]) -> dict[str, Any]:
        orig_route = TurnScope.route

        def fake_route(
            scope: TurnScope, request: dict[str, Any], feedback: Any, all_feedback: bool
        ) -> RecordSelection:
            sel = orig_route(scope, request, feedback, all_feedback)
            return RecordSelection(
                records=sel.records,
                omitted=sel.omitted,
                outside=list(outside_ids),
                origins=sel.origins,
                routes=sel.routes,
                guard=sel.guard,
            )

        with mock.patch.object(TurnScope, "route", fake_route):
            return self.system.context(
                turn,
                {"task": f"Prepare bounded view for {turn}.", "targets": ["outputs/result.md"]},
            )

    def _complete_and_adopt(self, turn: str) -> None:
        self.system.receive(turn, self.reply, "tester", [f"outputs/result.md={self.output}"])
        (self.system.work / "outputs").mkdir(parents=True, exist_ok=True)
        self.system.adopt(turn, "outputs/result.md", f"Adopt {turn}.")

    def test_large_outside_view_records_exceeding_max_bytes_spills_recovers_audits_and_retires(
        self,
    ) -> None:
        # 100,000 synthetic record IDs encode to ~2.7 MiB > MAX_BYTES (2 MiB).
        outside_ids = [f"rec-{idx:06d}-inventory-item" for idx in range(100_000)]
        self.assertGreater(len(encode(outside_ids)), MAX_BYTES)

        with exclusive(self.system.ex.root / "owner.lock"):
            res = self._prepare_huge_outside_turn("huge-turn", outside_ids)
            turn_dir = self.system.work / ".system" / "turns" / "huge-turn"

            # 1. exposure.json and result["view"] stay O(selected) bounded.
            exposure_path = turn_dir / "exposure.json"
            self.assertLess(len(exposure_path.read_bytes()), 64 * 1024)
            exposure_doc = read_json(exposure_path)
            self.assertEqual(exposure_doc["format"], TURN_EXPOSURE_FORMAT)
            self.assertNotIn("outside_view_records", exposure_doc)
            self.assertEqual(exposure_doc["outside_view_record_count"], len(outside_ids))
            self.assertNotIn("outside_view_records", res["view"])
            self.assertEqual(res["view"]["outside_view_record_count"], len(outside_ids))

            # 2. view.json, full_view_object, and selection.json stay compact (< MAX_BYTES).
            view_path = turn_dir / "view.json"
            self.assertLess(len(view_path.read_bytes()), MAX_BYTES // 4)
            view_disk = read_json(view_path)
            self.assertEqual(view_disk["format"], TURN_VIEW_FORMAT)
            self.assertNotIn("outside_view_records", view_disk)
            self.assertEqual(view_disk["outside_view_record_count"], len(outside_ids))
            chunk_hashes = view_disk["outside_view_record_objects"]
            self.assertGreaterEqual(len(chunk_hashes), 5)

            sel_disk = read_json(turn_dir / "selection.json")
            self.assertEqual(sha(encode(sel_disk["view"])), exposure_doc["full_view_object"])
            self.assertNotIn("outside_view_records", sel_disk["view"])

            # 3. Every chunk object is compressed and strictly <= 512 KiB uncompressed.
            for chunk_sha in chunk_hashes:
                deflate_path = self.system.ex.objects.directory / f"{chunk_sha}.deflate"
                self.assertTrue(deflate_path.is_file(), f"expected compressed chunk {chunk_sha}")
                raw_chunk = self.system.ex.objects.read(chunk_sha)
                self.assertLessEqual(len(raw_chunk), RECORD_ID_CHUNK_MAX_BYTES)
                chunk_doc = strict_json_loads(raw_chunk, "chunk")
                self.assertEqual(chunk_doc["format"], RECORD_ID_CHUNK_FORMAT)

            # 4. system.view(turn) transparently reassembles the exact ordered list.
            recovered_view = self.system.view("huge-turn")
            self.assertEqual(recovered_view["outside_view_records"], outside_ids)
            self.assertEqual(recovered_view["outside_view_record_count"], len(outside_ids))

            # 5. explain_context reports exact count in O(1) without reading chunks.
            orig_read = self.system.ex.objects.read
            read_keys: list[str] = []

            def spy_read(
                key: str,
                *,
                retained: bool = False,
                expected_bytes: int | None = None,
                dir_fd: int | None = None,
            ) -> bytes:
                read_keys.append(key)
                return orig_read(
                    key, retained=retained, expected_bytes=expected_bytes, dir_fd=dir_fd
                )

            with mock.patch.object(self.system.ex.objects, "read", side_effect=spy_read):
                explained = self.system.explain_context("huge-turn")
            self.assertEqual(explained["outside_counts"]["outside_view_records"], len(outside_ids))
            chunk_set = set(chunk_hashes)
            self.assertEqual(chunk_set & set(read_keys), set())

            # 6. Retire working copy, then verify audit_objects and gc_objects preserve chunks.
            self._complete_and_adopt("huge-turn")
            plan = plan_turn_retirement(self.system, turns=["huge-turn"])
            self.assertEqual([row["turn"] for row in plan["eligible"]], ["huge-turn"])
            execute_turn_retirement(
                self.system,
                plan=plan,
                expected_plan=plan["plan_sha256"],
                reason="Retire huge bounded turn.",
            )
            self.assertFalse(turn_dir.exists())

            # Working copy is gone; only exchange/packets remains, yet all chunks stay reachable.
            orphan = self.system.ex.objects.put(b"unreachable-garbage-object")
            audit_before = audit_objects(self.system)
            unreachable_keys = {row["object"] for row in audit_before["unreachable"]}
            self.assertEqual(unreachable_keys, {orphan})
            self.assertEqual(chunk_set & unreachable_keys, set())

            gc_objects(
                self.system,
                plan=audit_before,
                expected_plan=audit_before["plan_sha256"],
                reason="Reap only truly unreachable object.",
            )

            audit_after = audit_objects(self.system)
            self.assertEqual(audit_after["unreachable_count"], 0)
            self.assertEqual(self.system.view("huge-turn")["outside_view_records"], outside_ids)
            self.assertEqual(
                self.system.explain_context("huge-turn")["outside_counts"]["outside_view_records"],
                len(outside_ids),
            )

    def test_retire_fails_closed_when_chunk_missing_or_corrupted(self) -> None:
        outside_ids = [f"rec-fail-{idx:06d}-payload" for idx in range(30_000)]
        self.assertGreater(len(encode(outside_ids)), OUTSIDE_VIEW_INLINE_LIMIT)

        with exclusive(self.system.ex.root / "owner.lock"):
            self._prepare_huge_outside_turn("corrupt-turn", outside_ids)
            self._complete_and_adopt("corrupt-turn")

            view_disk = read_json(
                self.system.work / ".system" / "turns" / "corrupt-turn" / "view.json"
            )
            chunk_sha = view_disk["outside_view_record_objects"][0]
            chunk_path = self.system.ex.objects.directory / f"{chunk_sha}.deflate"
            original_bytes = chunk_path.read_bytes()

            # Case A: Corrupted chunk bytes -> view() raises and retirement refuses.
            chunk_path.write_bytes(b"not-valid-zlib-stream")
            with self.assertRaises(ValueError):
                self.system.view("corrupt-turn")
            plan_corrupt = plan_turn_retirement(self.system, turns=["corrupt-turn"])
            inelig_corrupt = {row["turn"]: row["reasons"] for row in plan_corrupt["ineligible"]}
            self.assertIn("corrupt-turn", inelig_corrupt)
            self.assertIn("captured_recovery_failed", inelig_corrupt["corrupt-turn"])

            # Case B: Missing chunk object -> view() raises and retirement refuses.
            chunk_path.unlink()
            with self.assertRaises((OSError, ValueError)):
                self.system.view("corrupt-turn")
            plan_missing = plan_turn_retirement(self.system, turns=["corrupt-turn"])
            inelig_missing = {row["turn"]: row["reasons"] for row in plan_missing["ineligible"]}
            self.assertIn("corrupt-turn", inelig_missing)
            self.assertIn("captured_recovery_failed", inelig_missing["corrupt-turn"])

            # Restore intact chunk -> retirement succeeds cleanly.
            chunk_path.write_bytes(original_bytes)
            plan_ok = plan_turn_retirement(self.system, turns=["corrupt-turn"])
            self.assertEqual([row["turn"] for row in plan_ok["eligible"]], ["corrupt-turn"])

    def test_small_turn_stays_inline_and_historical_un_chunked_view_remains_readable(self) -> None:
        with exclusive(self.system.ex.root / "owner.lock"):
            res = self.system.context("small-turn", {"task": "Small store check."})
            turn_dir = self.system.work / ".system" / "turns" / "small-turn"
            exposure_doc = read_json(turn_dir / "exposure.json")
            view_disk = read_json(turn_dir / "view.json")

            self.assertNotIn("outside_view_records", exposure_doc)
            self.assertNotIn("outside_view_records", res["view"])
            self.assertEqual(
                exposure_doc["outside_view_record_count"], len(view_disk["outside_view_records"])
            )
            self.assertIn("outside_view_records", view_disk)
            self.assertEqual(
                view_disk["outside_view_record_count"], len(view_disk["outside_view_records"])
            )
            self.assertNotIn("outside_view_record_objects", view_disk)

            # Legacy un-chunked view.json (no count, no chunk refs, no format).
            legacy_view = dict(view_disk)
            legacy_view.pop("outside_view_record_count", None)
            legacy_view.pop("format", None)
            legacy_view["outside_view_records"] = ["legacy-a", "legacy-b"]
            legacy_sha = self.system.ex.objects.put(encode(legacy_view))
            legacy_request = {
                "materials": [{"path": ".system/turns/legacy-turn/view.json", "sha256": legacy_sha}]
            }
            loaded = captured_view(self.system.ex, "legacy-turn", legacy_request)
            self.assertEqual(loaded["outside_view_records"], ["legacy-a", "legacy-b"])
            self.assertEqual(outside_view_record_count(loaded), 2)

            self.assertEqual(
                explain_context(self.system.ex, "small-turn")["outside_counts"][
                    "outside_view_records"
                ],
                len(view_disk["outside_view_records"]),
            )

    def test_hydrate_rejects_malformed_chunk_metadata_and_count_mismatch(self) -> None:
        bad_format_sha = self.system.ex.objects.put(
            encode({"format": "wrong-format/1", "records": ["a"]}), compress=True
        )
        with self.assertRaises(ValueError):
            hydrate_outside_view_records(
                self.system.ex.objects,
                {"outside_view_record_objects": [bad_format_sha], "outside_view_record_count": 1},
            )

        valid_sha = self.system.ex.objects.put(
            encode({"format": RECORD_ID_CHUNK_FORMAT, "records": ["a", "b"]}), compress=True
        )
        with self.assertRaises(ValueError):
            hydrate_outside_view_records(
                self.system.ex.objects,
                {"outside_view_record_objects": [valid_sha], "outside_view_record_count": 3},
            )
        with self.assertRaises(ValueError):
            hydrate_outside_view_records(
                self.system.ex.objects,
                {
                    "outside_view_records": ["a", "b"],
                    "outside_view_record_objects": [valid_sha],
                    "outside_view_record_count": 2,
                },
            )


if __name__ == "__main__":
    unittest.main()
