"""Unit and live Membus v2 integration tests for first-class selectable store backend."""

import json
import os
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO = Path(__file__).resolve().parents[2]
for _entry in (str(_REPO / "src"), str(_REPO)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from scripts.ci.verify_membus_live import (  # noqa: E402
    EphemeralMembusDaemon,
    find_membus_binary,
)

from managed_ai_information.core.formats import BATCH_FORMAT  # noqa: E402
from managed_ai_information.core.storage import (  # noqa: E402
    MAX_BYTES,
    encode,
    exclusive,
    read_json,
    write_json,
)
from managed_ai_information.core.storage_backend import (  # noqa: E402
    BackendUnavailableError,
    MembusV2Backend,
)
from managed_ai_information.store.cli.command_parser import (  # noqa: E402
    is_readonly_action,
    parser,
)
from managed_ai_information.store.records.backend_bridge import (  # noqa: E402
    backend_cutover,
    backend_rollback,
    backend_status,
    backend_sync,
    parse_backend_config,
)
from managed_ai_information.store.records.record_batch import (  # noqa: E402
    put_batch as direct_put_batch,
)
from managed_ai_information.store.system import System  # noqa: E402

_MEMBUS_V2_BIN = find_membus_binary()
_MANAGE_STORE = _REPO / "src/managed_ai_information/store/manage_store.py"


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _init_store(root: Path, instance_id: str = "membus-primary-test") -> Path:
    System.initialize(
        root,
        {"contribution": "Exercise selectable Membus v2 store backend cleanly."},
        instance_id=instance_id,
    )
    return root


def _run_manage_store(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-B", str(_MANAGE_STORE), "--root", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


class BackendBridgeUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="managed-ai-w19-backend-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_embedded_default_and_fail_closed_config_validation(self) -> None:
        cfg = parse_backend_config(None)
        self.assertFalse(cfg.is_membus)
        self.assertEqual(cfg.to_descriptor(), {"type": "embedded"})

        cfg_emb = parse_backend_config({"type": "embedded"})
        self.assertFalse(cfg_emb.is_membus)

        valid = parse_backend_config(
            {
                "type": "membus-v2",
                "url": "http://127.0.0.1:8787",
                "namespace": "alpha_ns",
                "mode": "dual-sync",
                "token_env": "MY_MEMBUS_TOKEN",
            }
        )
        self.assertTrue(valid.is_membus)
        self.assertEqual(valid.mode, "dual-sync")
        self.assertEqual(valid.token_env, "MY_MEMBUS_TOKEN")
        self.assertEqual(valid.to_descriptor()["token_env"], "MY_MEMBUS_TOKEN")

        # Reject non-loopback http://, credentials, unknown keys, bad type/mode, remote opts
        bad_configs: list[object] = [
            "not-a-dict",
            {"type": "unknown"},
            {"type": "embedded", "url": "http://127.0.0.1:8787"},
            {"type": "membus-v2", "url": "http://example.com:8787", "namespace": "ns"},
            {"type": "membus-v2", "url": "http://user:pass@127.0.0.1:8787", "namespace": "ns"},
            {"type": "membus-v2", "url": "http://127.0.0.1:8787", "namespace": ""},
            {"type": "membus-v2", "url": "http://127.0.0.1:8787", "namespace": "ns", "mode": "bad"},
            {
                "type": "membus-v2",
                "url": "http://127.0.0.1:8787",
                "namespace": "ns",
                "extra": True,
            },
            {
                "type": "membus-v2",
                "url": "http://127.0.0.1:8787",
                "namespace": "ns",
                "token_env": "   ",
            },
        ]
        for bad in bad_configs:
            with self.assertRaises(ValueError, msg=f"expected refusal for {bad!r}"):
                parse_backend_config(bad)

        # Invalid backend block in system.json fails closed on System(root)
        root = _init_store(self.tmp / "bad-store")
        desc = read_json(root / "system.json")
        desc["backend"] = {
            "type": "membus-v2",
            "url": "http://198.51.100.10:8787",
            "namespace": "bad_ns",
            "mode": "primary",
        }
        write_json(root / "system.json", desc)
        with self.assertRaises(ValueError):
            System(root)

    def test_discover_search_mode_fallback_and_fail_closed(self) -> None:
        root = _init_store(self.tmp / "fallback-store")
        with System(root) as sys_obj:
            with exclusive(sys_obj.ex.root / "owner.lock"):
                sys_obj.put(
                    {
                        "id": "note-alpha",
                        "kind": "note",
                        "active": True,
                        "content": {"title": "Alpha migration note", "summary": "keepalive pool"},
                        "relations": [],
                        "basis": [],
                    },
                    0,
                )

            with self.assertRaises(ValueError):
                sys_obj.discover({"query": "alpha", "scopes": ["records"], "search_mode": "bogus"})

            # Explicit bm25 on embedded store fails closed (even when query is empty)
            with self.assertRaises(ValueError):
                sys_obj.discover({"query": "alpha", "scopes": ["records"], "search_mode": "bm25"})
            with self.assertRaises(ValueError):
                sys_obj.discover({"query": "", "scopes": ["records"], "search_mode": "bm25"})

            # Auto on embedded store runs local lexical seamlessly
            res_auto = sys_obj.discover(
                {"query": "alpha", "scopes": ["records"], "search_mode": "auto"}
            )
            self.assertIn("record:note-alpha", [c["ref"] for c in res_auto["candidates"]])
            self.assertNotIn("backend_search", res_auto)

        # Configure unreachable loopback membus-v2 in shadow-async mode
        dead_port = _unused_loopback_port()
        desc = read_json(root / "system.json")
        desc["backend"] = {
            "type": "membus-v2",
            "url": f"http://127.0.0.1:{dead_port}",
            "namespace": "offline_ns",
            "mode": "shadow-async",
        }
        write_json(root / "system.json", desc)

        with System(root) as sys_obj:
            # shadow-async write succeeds locally even when daemon is offline
            with exclusive(sys_obj.ex.root / "owner.lock"):
                committed = sys_obj.put(
                    {
                        "id": "note-offline",
                        "kind": "note",
                        "active": True,
                        "content": {"title": "Offline resilient write"},
                        "relations": [],
                        "basis": [],
                    },
                    0,
                )
            self.assertEqual(committed["revision"], 1)
            sys_obj.backend_bridge.flush(timeout=2.0)

            # auto gracefully falls back to local lexical HeadIndex v3
            res_fb = sys_obj.discover(
                {
                    "query": "resilient",
                    "scopes": ["records"],
                    "content": True,
                    "search_mode": "auto",
                }
            )
            self.assertEqual(res_fb["backend_search"]["engine"], "lexical-fallback")
            self.assertIn("record:note-offline", [c["ref"] for c in res_fb["candidates"]])

            # explicit bm25 fails closed when daemon is unreachable
            with self.assertRaises(BackendUnavailableError):
                sys_obj.discover(
                    {"query": "resilient", "scopes": ["records"], "search_mode": "bm25"}
                )

            # backend status reports healthy=False cleanly without crashing
            st = backend_status(sys_obj)
            self.assertFalse(st["healthy"])
            self.assertIn("error", st)

            # rollback cleanly restores embedded mode while preserving non-backend descriptor keys
            sys_obj.descriptor["deployment_overlay_marker"] = "preserve-me"
            rb = backend_rollback(sys_obj, reason="daemon offline")
            self.assertEqual(rb["backend"], {"type": "embedded"})
            self.assertNotIn("backend", read_json(root / "system.json"))
            self.assertEqual(sys_obj.descriptor.get("deployment_overlay_marker"), "preserve-me")

    def test_mirror_record_oversize_and_non_ordinary_guards(self) -> None:
        root = _init_store(self.tmp / "guard-store")
        dead_port = _unused_loopback_port()
        desc = read_json(root / "system.json")
        desc["backend"] = {
            "type": "membus-v2",
            "url": f"http://127.0.0.1:{dead_port}",
            "namespace": "guard_ns",
            "mode": "shadow-async",
        }
        write_json(root / "system.json", desc)
        with System(root) as sys_obj:
            # Oversize record file (> MAX_BYTES) skips cleanly before read_bytes without crashing
            big_path = root / "work/records/oversize-rec.json"
            big_path.write_bytes(b"{" + b" " * (MAX_BYTES + 64) + b"}")
            sys_obj.backend_bridge.mirror_record({"id": "oversize-rec", "kind": "note"})
            # Non-ordinary target (directory at record path) fails closed with ValueError
            dir_path = root / "work/records/dir-rec.json"
            dir_path.mkdir()
            with self.assertRaises(ValueError):
                sys_obj.backend_bridge.mirror_record({"id": "dir-rec", "kind": "note"})

    def test_cli_backend_parser_readonly_classification(self) -> None:
        cli = parser()
        st_args = cli.parse_args(["--root", str(self.tmp), "backend", "status"])
        self.assertTrue(is_readonly_action(st_args))

        for sub_argv in (
            ["backend", "sync", "--url", "http://127.0.0.1:8787", "--namespace", "ns"],
            ["backend", "cutover", "--url", "http://127.0.0.1:8787", "--namespace", "ns"],
            ["backend", "rollback"],
        ):
            parsed = cli.parse_args(["--root", str(self.tmp), *sub_argv])
            self.assertFalse(is_readonly_action(parsed))

        disc = cli.parse_args(
            ["--root", str(self.tmp), "discover", "--query", "x", "--search-mode", "bm25"]
        )
        self.assertEqual(disc.search_mode, "bm25")


@unittest.skipUnless(_MEMBUS_V2_BIN is not None, "membus v2 release binary unavailable")
class LiveMembusPrimaryBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="managed-ai-w19-live-membus-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_live_membus_dual_write_bm25_sealed_isolation_cutover_rollback_and_lease_404(
        self,
    ) -> None:
        assert _MEMBUS_V2_BIN is not None
        root = _init_store(self.tmp / "live-store")
        blob_bytes = b"verified non-sealed basis evidence payload for wave19\n"
        sealed_canary_initial = "zxqprimarysealed7731"
        sealed_canary_live = "zxqlivesealed8842"

        # Seed initial records + one CAS blob + one sealed record before sync
        with System(root) as sys_obj, exclusive(sys_obj.ex.root / "owner.lock"):
            blob_sha = sys_obj.ex.objects.put(blob_bytes)
            (root / "work/sources/evidence.txt").write_bytes(blob_bytes)
            sys_obj.put(
                {
                    "id": "dec-routing",
                    "kind": "decision",
                    "active": True,
                    "content": {
                        "title": "Keepalive routing decision",
                        "summary": "Adopt persistent HTTP pooling for membus throughput.",
                    },
                    "relations": [],
                    "basis": ["sources/evidence.txt"],
                },
                0,
            )
            self.assertEqual(len(blob_sha), 64)
            sys_obj.put(
                {
                    "id": "note-lexical",
                    "kind": "note",
                    "active": True,
                    "content": {
                        "title": "Tantivy indexing note",
                        "summary": "Fast BM25 retrieval alongside HeadIndex fallback.",
                    },
                    "relations": [],
                    "basis": [],
                },
                0,
            )
            # Write a sealed record file directly (same format as mirrored sealed entries)
            (root / "work/records/sealed-seed.json").write_bytes(
                encode(
                    {
                        "id": "sealed-seed",
                        "kind": "evidence",
                        "availability": "sealed",
                        "active": True,
                        "revision": 1,
                        "content": {"summary": sealed_canary_initial},
                        "relations": [],
                        "basis": [],
                    }
                )
            )

        with EphemeralMembusDaemon(_MEMBUS_V2_BIN) as daemon:
            ns = "w19_live_primary"

            # 1. CLI `backend status` on embedded default
            p_st0 = _run_manage_store(root, "backend", "status")
            self.assertEqual(p_st0.returncode, 0, p_st0.stderr)
            st0 = json.loads(p_st0.stdout)
            self.assertEqual(st0["backend"], {"type": "embedded"})
            self.assertTrue(st0["healthy"])

            # 2. CLI `backend sync` with dual-sync persists backend config & mirrors records+blobs
            p_sync = _run_manage_store(
                root,
                "backend",
                "sync",
                "--url",
                daemon.base_url,
                "--namespace",
                ns,
                "--mode",
                "dual-sync",
                "--verify-blob-bytes",
            )
            self.assertEqual(p_sync.returncode, 0, p_sync.stderr)
            sync_rep = json.loads(p_sync.stdout)
            self.assertEqual(sync_rep["backend"]["mode"], "dual-sync")
            self.assertEqual(sync_rep["mirror"]["status"], "passed")
            self.assertEqual(sync_rep["mirror"]["migration"]["final_phase"], "verified")
            self.assertGreaterEqual(sync_rep["mirror"]["visibility_counts"]["Sealed"], 1)
            self.assertEqual(sync_rep["mirror"]["parity_verification"]["status"], "passed")

            # 3. Live writes (System.put, RecordStore.put, System.put_batch, direct put_batch)
            # all automatically mirror to live membus without double-mirroring
            with System(root) as sys_obj:
                with exclusive(sys_obj.ex.root / "owner.lock"):
                    sys_obj.put(
                        {
                            "id": "dec-live-put",
                            "kind": "decision",
                            "active": True,
                            "content": {
                                "title": "Live dual-sync put verification",
                                "summary": "Quasar telemetry routing token verified live.",
                            },
                            "relations": [],
                            "basis": [],
                        },
                        0,
                    )
                    sys_obj.record_store.put(
                        {
                            "id": "note-direct-store-put",
                            "kind": "note",
                            "active": True,
                            "content": {
                                "title": "Direct record_store put",
                                "summary": "Pulsar beacon written via RecordStore._replace hook.",
                            },
                            "relations": [],
                            "basis": [],
                        },
                        0,
                    )
                    # Sealed record written via live put must ONLY emit Sealed custody envelope
                    sys_obj.put(
                        {
                            "id": "sealed-live-put",
                            "kind": "note",
                            "active": True,
                            "content": {
                                "title": "Restricted entry",
                                "availability": "sealed",
                                "summary": sealed_canary_live,
                            },
                            "relations": [],
                            "basis": [],
                        },
                        0,
                    )
                    sys_obj.put_batch(
                        {
                            "format": BATCH_FORMAT,
                            "id": "w19-live-batch-1",
                            "instance_id": sys_obj.instance_id,
                            "operations": [
                                {
                                    "id": "note-live-batch",
                                    "expected_revision": 0,
                                    "mode": "replace",
                                    "record": {
                                        "kind": "note",
                                        "active": True,
                                        "content": {
                                            "title": "Batch nebula note",
                                            "summary": "Committed via guarded put_batch.",
                                        },
                                        "relations": [],
                                        "basis": [],
                                    },
                                }
                            ],
                        }
                    )
                    direct_put_batch(
                        sys_obj,
                        {
                            "format": BATCH_FORMAT,
                            "id": "w19-live-direct-batch-2",
                            "instance_id": sys_obj.instance_id,
                            "operations": [
                                {
                                    "id": "note-direct-batch",
                                    "expected_revision": 0,
                                    "mode": "replace",
                                    "record": {
                                        "kind": "note",
                                        "active": True,
                                        "content": {
                                            "title": "Direct module put_batch",
                                            "summary": "Magnetar comet committed via record_batch.",
                                        },
                                        "relations": [],
                                        "basis": [],
                                    },
                                }
                            ],
                        },
                    )

                # Verify 100% live parity (put + put_batch records + blob match exact SHA)
                st_live = backend_status(sys_obj, verify_blob_bytes=True)
                self.assertTrue(st_live["healthy"])
                self.assertEqual(st_live["parity"]["status"], "passed", st_live["parity"])
                self.assertGreaterEqual(st_live["parity"]["sealed_verified"], 2)

                # 4. Live Tantivy BM25 discovery (bm25 + auto + --kind filter)
                bm25_res = sys_obj.discover(
                    {"query": "quasar", "scopes": ["records"], "search_mode": "bm25"}
                )
                self.assertEqual(bm25_res["backend_search"]["engine"], "membus-bm25")
                self.assertEqual(
                    [c["ref"] for c in bm25_res["candidates"]], ["record:dec-live-put"]
                )

                auto_batch = sys_obj.discover(
                    {"query": "nebula", "scopes": ["records"], "search_mode": "auto"}
                )
                self.assertEqual(auto_batch["backend_search"]["engine"], "membus-bm25")
                self.assertEqual(
                    [c["ref"] for c in auto_batch["candidates"]], ["record:note-live-batch"]
                )

                # Direct record_store.put + direct record_batch.put_batch hits are live in Tantivy
                direct_store_hit = sys_obj.discover(
                    {"query": "pulsar", "scopes": ["records"], "search_mode": "bm25"}
                )
                self.assertEqual(
                    [c["ref"] for c in direct_store_hit["candidates"]],
                    ["record:note-direct-store-put"],
                )
                direct_batch_hit = sys_obj.discover(
                    {"query": "magnetar", "scopes": ["records"], "search_mode": "bm25"}
                )
                self.assertEqual(
                    [c["ref"] for c in direct_batch_hit["candidates"]],
                    ["record:note-direct-batch"],
                )

                # Kind filter excludes non-matching kinds
                kind_miss = sys_obj.discover(
                    {
                        "query": "quasar",
                        "scopes": ["records"],
                        "kinds": ["note"],
                        "search_mode": "bm25",
                    }
                )
                self.assertEqual(kind_miss["candidates"], [])

                kind_hit = sys_obj.discover(
                    {
                        "query": "quasar",
                        "scopes": ["records"],
                        "kinds": ["decision"],
                        "search_mode": "bm25",
                    }
                )
                self.assertEqual(
                    [c["ref"] for c in kind_hit["candidates"]], ["record:dec-live-put"]
                )

                # 5. Sealed 0-leak verification in both discover(bm25) and direct Tantivy search
                for canary in (sealed_canary_initial, sealed_canary_live):
                    leak_disc = sys_obj.discover(
                        {"query": canary, "scopes": ["records"], "search_mode": "bm25"}
                    )
                    self.assertEqual(leak_disc["candidates"], [], f"sealed leaked: {canary}")

            # Verify System.__exit__ closed pooled sockets cleanly
            self.assertIsNone(sys_obj.backend_bridge._client)
            self.assertIsNone(sys_obj.backend_bridge._router)

            with MembusV2Backend(daemon.base_url) as client:
                for canary in (sealed_canary_initial, sealed_canary_live):
                    for mode in ("Bm25", "LiteralSubstring"):
                        hits = client.search(canary, namespace=ns, mode=mode, limit=10)
                        self.assertEqual(hits, [], f"Tantivy {mode} leaked {canary}")
                # Custody record contains only opaque sealed marker, never canary text
                custody = client.get_record(ns, "records/sealed-live-put")
                self.assertIsNotNone(custody)
                assert custody is not None
                custody_doc = json.loads(custody.value)
                self.assertEqual(custody_doc["format"], "managed-ai-sealed-custody/1")
                self.assertEqual(custody_doc["visibility"], "Sealed")
                self.assertNotIn(sealed_canary_live.encode("utf-8"), custody.value)

                # 6. Verify membus @ 97b4e93 lease release 404 -> False idempotency
                grant = client.acquire_lease(ns, "w19-lease-res", "holder-a", 15_000)
                self.assertTrue(client.validate_lease(grant))
                self.assertTrue(client.release_lease(grant))
                self.assertFalse(client.release_lease(grant))

            # 7. CLI `backend cutover --mode primary` + CLI `discover --search-mode bm25`
            p_cut = _run_manage_store(root, "backend", "cutover", "--mode", "primary")
            self.assertEqual(p_cut.returncode, 0, p_cut.stderr)
            cut_rep = json.loads(p_cut.stdout)
            self.assertEqual(cut_rep["backend"]["mode"], "primary")
            self.assertEqual(cut_rep["mirror"]["migration"]["final_phase"], "committed")

            p_disc = _run_manage_store(
                root,
                "discover",
                "--query",
                "quasar",
                "--scope",
                "records",
                "--search-mode",
                "bm25",
            )
            self.assertEqual(p_disc.returncode, 0, p_disc.stderr)
            disc_cli = json.loads(p_disc.stdout)
            self.assertEqual(disc_cli["backend_search"]["engine"], "membus-bm25")
            self.assertEqual([c["ref"] for c in disc_cli["candidates"]], ["record:dec-live-put"])

            # 8. CLI `backend rollback` cleanly restores zero-daemon embedded mode
            p_rb = _run_manage_store(root, "backend", "rollback", "--reason", "e2e rollback check")
            self.assertEqual(p_rb.returncode, 0, p_rb.stderr)
            rb_rep = json.loads(p_rb.stdout)
            self.assertEqual(rb_rep["backend"], {"type": "embedded"})
            self.assertEqual(rb_rep["previous_backend"]["mode"], "primary")

            p_st_after = _run_manage_store(root, "backend", "status")
            self.assertEqual(p_st_after.returncode, 0, p_st_after.stderr)
            self.assertEqual(json.loads(p_st_after.stdout)["backend"], {"type": "embedded"})

            # Direct Python helper: sync (verified) -> re-target namespace -> cutover -> rollback
            with System(root) as sys_obj, exclusive(sys_obj.ex.root / "owner.lock"):
                sync2 = backend_sync(sys_obj, url=daemon.base_url, namespace=ns)
                self.assertEqual(sync2["mirror"]["status"], "passed")
                self.assertEqual(sync2["mirror"]["migration"]["final_phase"], "verified")
                ns2 = "w19_live_retargeted"
                sync_retarget = backend_sync(sys_obj, namespace=ns2)
                self.assertEqual(sync_retarget["backend"]["namespace"], ns2)
                self.assertEqual(read_json(root / "system.json")["backend"]["namespace"], ns2)
                cut2 = backend_cutover(
                    sys_obj, url=daemon.base_url, namespace=ns2, mode="dual-sync"
                )
                self.assertEqual(cut2["backend"]["mode"], "dual-sync")
                self.assertEqual(cut2["mirror"]["migration"]["final_phase"], "committed")
                rb2 = backend_rollback(sys_obj)
                self.assertEqual(rb2["backend"], {"type": "embedded"})


if __name__ == "__main__":
    unittest.main()
