"""Live end-to-end integration tests against an isolated ephemeral `membus` v2 daemon.

Includes always-run unit tests for Membus v2 wire helpers plus live daemon tests that
automatically skip cleanly when no compatible Membus v2 binary is available (e.g. minimal
Docker/CI containers), while executing full live RocksDB + Tantivy + Lease + SSE + A/B
Multi-Route + Expand-Contract Migration verification on dev machines with `membus` v2.
"""

import io
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _p in (_REPO / "src", _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.ci.verify_membus_live import (  # noqa: E402
    REPORT_FORMAT,
    EphemeralMembusDaemon,
    find_membus_binary,
    is_membus_v2_binary,
    run_live_verification,
)

from managed_ai_information.core.storage_backend_protocol import (  # noqa: E402
    build_collection_registration,
    build_search_op,
    parse_sse_signals,
)

_MEMBUS_V2_BIN = find_membus_binary()


class MembusWireHelperUnitTests(unittest.TestCase):
    def test_parse_sse_signals_normalizes_membus_pascalcase_kinds(self) -> None:
        ev_put = (
            '{"seq":5,"epoch":1,"namespace":"ns","key":[107,49],'
            '"version":5,"kind":"Put","value_hint":[118]}'
        )
        ev_del = (
            '{"seq":6,"epoch":1,"namespace":"ns","key":[107,49],'
            '"version":6,"kind":"Delete","value_hint":null}'
        )
        raw = io.BytesIO(
            (
                'event: subscribed\ndata: {"epoch": 1, "start_seq": 0}\n\n'
                f"event: signal\nid: 5\ndata: {ev_put}\n\n"
                f"event: signal\nid: 6\ndata: {ev_del}\n\n"
            ).encode()
        )
        events = list(parse_sse_signals(raw))
        self.assertEqual([e.kind for e in events], ["put", "delete"])
        self.assertEqual([e.key for e in events], ["k1", "k1"])
        self.assertEqual(events[0].value_hint, b"v")
        self.assertIsNone(events[1].value_hint)

    def test_build_search_op_and_collection_registration_wire_shapes(self) -> None:
        sealed = build_search_op("ns", "k1", b"secret", "Sealed", {"title": "Hidden"})
        self.assertEqual(sealed, {"Delete": {"namespace": "ns", "doc_id": "k1"}})
        meta_only = build_search_op("ns", "k2", b"secret-body", "MetadataOnly", {"title": "Shown"})
        self.assertEqual(meta_only["Index"]["body"], "")
        self.assertEqual(meta_only["Index"]["title"], "Shown")

        reg = build_collection_registration("live_ns", schema_version=1, schema_bytes=b'{"v":1}')
        self.assertEqual(reg["descriptor"]["namespace"], "live_ns")
        self.assertEqual(len(reg["descriptor"]["schema_sha256"]), 32)
        self.assertEqual(reg["descriptor"]["compatible_versions"], [1])
        with self.assertRaises(ValueError):
            build_collection_registration("live_ns", schema_version=0)

    def test_is_membus_v2_binary_rejects_missing_path(self) -> None:
        self.assertFalse(is_membus_v2_binary(Path("definitely-not-a-membus-binary")))


@unittest.skipUnless(
    _MEMBUS_V2_BIN is not None,
    "compatible membus v2 executable not available in environment",
)
class MembusLiveIntegrationTests(unittest.TestCase):
    def test_full_live_membus_v2_end_to_end_and_migration_lifecycle(self) -> None:
        assert _MEMBUS_V2_BIN is not None
        with (
            EphemeralMembusDaemon(_MEMBUS_V2_BIN) as daemon,
            tempfile.TemporaryDirectory(prefix="membus-live-test-") as scratch,
        ):
            report = run_live_verification(daemon.base_url, Path(scratch))

        self.assertEqual(report["format"], REPORT_FORMAT)
        self.assertTrue(report["ok"])
        checks = report["checks"]
        self.assertGreaterEqual(checks["health"]["epoch"], 1)
        self.assertTrue(checks["kv_cas_and_delete"]["absent_conflict_409"])
        self.assertTrue(checks["kv_cas_and_delete"]["stale_conflict_409"])
        self.assertTrue(checks["kv_cas_and_delete"]["deleted"])
        self.assertEqual(len(checks["blobs_cas"]["sha256"]), 64)
        self.assertTrue(checks["search_visibility_isolation"]["public_to_sealed_tombstoned"])
        self.assertTrue(checks["search_visibility_isolation"]["delete_tombstoned"])
        self.assertEqual(checks["fenced_leases"]["contended"], "409:lease_held")
        self.assertEqual(checks["fenced_leases"]["gen2"], 2)
        self.assertTrue(checks["fenced_leases"]["idempotent_release_false"])
        self.assertEqual(
            [e["kind"] for e in checks["sse_signals"]["events"]],
            ["put", "delete"],
        )
        self.assertTrue(checks["coordination_facade"]["stale_gen1_rejected"])
        self.assertEqual(checks["multi_route_and_migration"]["committed_version"], 2)
        self.assertEqual(checks["multi_route_and_migration"]["aborted_reverted_version"], 2)
        self.assertEqual(checks["multi_route_and_migration"]["parity_counts"]["mismatched_ops"], 0)
        self.assertEqual(checks["multi_route_and_migration"]["parity_counts"]["route_b_errors"], 0)


if __name__ == "__main__":
    unittest.main()
