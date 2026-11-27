"""Tests for managed-ai-coordination information-layer contract."""

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from managed_ai_information.core.storage import MAX_BYTES, encode, exclusive, sha  # noqa: E402
from managed_ai_information.core.storage_backend import (  # noqa: E402
    BackendConflictError,
    BackendContendedError,
    EmbeddedFileBackend,
    LeaseGrant,
)
from managed_ai_information.store.coordination_contract import (  # noqa: E402
    ARTIFACT_RECEIPT_FORMAT,
    BUNDLE_REF_FORMAT,
    COMMIT_RECEIPT_FORMAT,
    COORDINATION_CONTRACT_FORMAT,
    CoordinationFacade,
    attach_artifact,
    commit_consolidation_delta,
    resolve_context_bundle,
)
from managed_ai_information.store.system import System  # noqa: E402
from managed_ai_information.store.turns.record_scope import RecordSelection, TurnScope  # noqa: E402


class CoordinationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store_root = self.base / "store"
        System.initialize(self.store_root, {"contribution": "Test coordination contract."})
        self.system = System(self.store_root)
        self.backend = EmbeddedFileBackend(self.base / "backend")
        self.facade = CoordinationFacade(self.system, backend=self.backend)

    def test_resolve_context_bundle_stays_bounded_for_chunked_large_views(self) -> None:
        outside_ids = [f"coord-rec-{idx:06d}-inventory" for idx in range(95_000)]
        self.assertGreater(len(encode(outside_ids)), MAX_BYTES)

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

        with exclusive(self.system.ex.root / "owner.lock"):
            with mock.patch.object(TurnScope, "route", fake_route):
                self.system.context("coord-turn", {"task": "Prepare bounded coordination bundle."})

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
                bundle = resolve_context_bundle(self.system, "coord-turn")

        self.assertEqual(bundle.format, BUNDLE_REF_FORMAT)
        self.assertEqual(bundle.turn, "coord-turn")
        self.assertEqual(bundle.outside_view_record_count, len(outside_ids))
        self.assertGreaterEqual(len(bundle.outside_view_record_objects), 4)
        self.assertEqual(bundle.bundle_bytes, len(encode(bundle.to_dict())))
        self.assertLessEqual(bundle.bundle_bytes, MAX_BYTES)
        # Chunk objects are NOT hydrated when resolving the bounded bundle handle.
        self.assertEqual(set(bundle.outside_view_record_objects) & set(read_keys), set())

    def test_attach_artifact_returns_verified_cas_receipt_and_rejects_bad_inputs(self) -> None:
        payload = b"coordination-artifact-bytes\n"
        receipt = self.facade.attach_artifact(
            "coord-turn-1", payload, "analysis-output", {"producer": "coordinator", "step": 1}
        )
        self.assertEqual(receipt.format, ARTIFACT_RECEIPT_FORMAT)
        self.assertEqual(receipt.sha256, sha(payload))
        self.assertEqual(receipt.bytes, len(payload))
        self.assertEqual(self.system.ex.objects.read(receipt.sha256), payload)

        with self.assertRaises(ValueError):
            attach_artifact(
                self.system.ex.objects, "t1", b"x" * (MAX_BYTES + 1), "blob", {"ok": True}
            )
        with self.assertRaises(ValueError):
            attach_artifact(self.system.ex.objects, "t1", b"ok", "blob", {"bad": float("nan")})

    def test_commit_consolidation_delta_atomic_put_batch_and_persisted_fencing(self) -> None:
        self.assertEqual(self.facade.contract_format, COORDINATION_CONTRACT_FORMAT)
        lease = self.backend.acquire_lease("software", "store", "coord-worker", ttl_ms=60_000)

        rec_a = {
            "id": "note-alpha",
            "kind": "note",
            "basis": [],
            "content": {"summary": "First consolidated note."},
        }
        rec_b = {
            "id": "note-beta",
            "kind": "note",
            "basis": [],
            "content": {"summary": "Second consolidated note."},
        }

        # Acquires exclusive(owner.lock) automatically and commits atomically via put_batch.
        receipt = self.facade.commit_consolidation_delta(
            lease, {"note-alpha": 0, "note-beta": 0}, [rec_a, rec_b]
        )
        self.assertEqual(receipt.format, COMMIT_RECEIPT_FORMAT)
        self.assertEqual(receipt.committed_count, 2)
        self.assertEqual(receipt.lease_generation, lease.generation)
        self.assertEqual(self.system.record_store.show("note-alpha")["revision"], 1)
        self.assertEqual(self.system.record_store.show("note-beta")["revision"], 1)

        # Validation error on record #2 MUST leave record #1 untouched (atomic put_batch).
        rec_a_v2 = dict(rec_a, content={"summary": "Updated alpha."})
        bad_rec = {
            "id": "bad-note",
            "kind": "note",
            "basis": [{"id": "nonexistent-basis"}],
            "content": {"summary": "Invalid basis shape."},
        }
        with self.assertRaises(ValueError):
            self.facade.commit_consolidation_delta(
                lease, {"note-alpha": 1, "bad-note": 0}, [rec_a_v2, bad_rec]
            )
        self.assertEqual(self.system.record_store.show("note-alpha")["revision"], 1)
        self.assertFalse(self.system.record_store.has_record("bad-note"))

        # Optimistic-CAS mismatch rejects BEFORE mutating ANY record in the batch.
        rec_b_stale = dict(rec_b, content={"summary": "Stale beta."})
        with self.assertRaises(BackendConflictError):
            self.facade.commit_consolidation_delta(
                lease, {"note-alpha": 1, "note-beta": 0}, [rec_a_v2, rec_b_stale]
            )
        self.assertEqual(self.system.record_store.show("note-alpha")["revision"], 1)

        # Expired lease rejected fail-closed.
        expired_lease = LeaseGrant(
            namespace=lease.namespace,
            resource=lease.resource,
            holder=lease.holder,
            generation=lease.generation,
            token=lease.token,
            expires_at_ms=1,
        )
        with self.assertRaises(BackendContendedError):
            commit_consolidation_delta(
                self.system,
                expired_lease,
                {"note-alpha": 1},
                [rec_a_v2],
                backend=self.backend,
            )

        # Release gen 1 and acquire gen 2 -> gen 2 commits, stale gen 1 fails even after restart!
        self.assertTrue(self.backend.release_lease(lease))
        lease_gen2 = self.backend.acquire_lease(
            "software", "store", "coord-worker-2", ttl_ms=60_000
        )
        self.assertEqual(lease_gen2.generation, 2)
        r2 = self.facade.commit_consolidation_delta(lease_gen2, {"note-alpha": 1}, [rec_a_v2])
        self.assertEqual(r2.lease_generation, 2)
        self.assertEqual(self.system.record_store.show("note-alpha")["revision"], 2)

        # Fresh CoordinationFacade with backend=None reloads persisted fence and rejects gen 1!
        restarted_facade = CoordinationFacade(self.system, backend=None)
        with self.assertRaises(BackendContendedError):
            restarted_facade.commit_consolidation_delta(lease, {"note-alpha": 2}, [rec_a_v2])

        # Signal subscription via CoordinationFacade.
        self.backend.put_record("software", "rec:note-alpha", b"v2")
        events = list(self.facade.subscribe_signals("software", prefix="rec:"))
        self.assertEqual([ev.key for ev in events], ["rec:note-alpha"])


if __name__ == "__main__":
    unittest.main()
