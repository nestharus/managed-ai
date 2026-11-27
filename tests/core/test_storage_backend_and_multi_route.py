"""Tests for StorageBackend, MembusV2Backend, MultiRouteBackend, and SchemaMigrationController."""

import base64
import json
import secrets
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from managed_ai_information.core.multi_route import (  # noqa: E402
    MultiRouteBackend,
    SchemaMigrationController,
)
from managed_ai_information.core.storage import sha  # noqa: E402
from managed_ai_information.core.storage_backend import (  # noqa: E402
    BackendConflictError,
    BackendContendedError,
    EmbeddedFileBackend,
    MembusV2Backend,
    StorageBackend,
    StorageBackendError,
)


class _MembusV2State:
    """Strict reference state matching live membus @ f33857f6 HTTP/JSON/SSE wire schemas."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.kv: dict[tuple[str, str], dict[str, Any]] = {}
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.search_docs: dict[tuple[str, str], dict[str, Any]] = {}
        self.search_ops_seen: list[tuple[str, str, dict[str, Any] | None]] = []
        self.leases: dict[tuple[str, str], dict[str, Any]] = {}
        self.lease_gens: dict[tuple[str, str], int] = {}
        self.signals: list[dict[str, Any]] = []
        self.migrations: list[tuple[str, str, dict[str, Any]]] = []
        self.seq = 0
        self.corrupt_next_blob_get = False


def _expected_matches(expected: Any, existing: dict[str, Any] | None) -> bool:
    if expected == "Any":
        return True
    if expected == "Absent":
        return existing is None
    if isinstance(expected, dict) and "Version" in expected:
        if existing is None:
            return False
        return int(existing["meta"]["version"]) == int(expected["Version"])
    return False


def _apply_search_op(state: _MembusV2State, search_op: Any) -> None:
    if not isinstance(search_op, dict):
        return
    if "Index" in search_op:
        doc = search_op["Index"]
        if doc.get("visibility") != "Sealed":
            state.search_docs[(doc["namespace"], doc["doc_id"])] = doc
    elif "Delete" in search_op:
        dl = search_op["Delete"]
        state.search_docs.pop((dl["namespace"], dl["doc_id"]), None)


def _start_membus_v2_server() -> tuple[ThreadingHTTPServer, _MembusV2State, str]:
    state = _MembusV2State()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            del format, args

        def _send_json(self, status: int, payload: Any) -> None:
            raw = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0"))
            return self.rfile.read(length) if length > 0 else b""

        def do_HEAD(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/")]
            if len(parts) == 4 and parts[0] == "v2" and parts[1] == "blobs":
                with state.lock:
                    exists = (parts[2], parts[3]) in state.blobs
                self.send_response(200 if exists else 404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/")]
            if parsed.path == "/v2/health":
                self._send_json(200, {"status": "ok", "service": "membus-v2-test"})
                return
            if len(parts) == 4 and parts[0] == "v2" and parts[1] == "kv":
                with state.lock:
                    row = state.kv.get((parts[2], parts[3]))
                if row is None:
                    self._send_json(404, {"error": "not_found", "message": "missing key"})
                    return
                self._send_json(200, row)
                return
            if len(parts) == 4 and parts[0] == "v2" and parts[1] == "blobs":
                with state.lock:
                    blob = state.blobs.get((parts[2], parts[3]))
                    corrupt = state.corrupt_next_blob_get
                    state.corrupt_next_blob_get = False
                if blob is None:
                    self._send_json(404, {"error": "not_found", "message": "missing blob"})
                    return
                out = b"corrupted-payload" if corrupt else blob
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            if (
                len(parts) == 4
                and parts[0] == "v2"
                and parts[1] == "signals"
                and parts[3] == "stream"
            ):
                qs = urllib.parse.parse_qs(parsed.query)
                ns = parts[2]
                prefix = qs.get("prefix", [None])[0]
                since_raw = qs.get("since_seq", [None])[0]
                since_seq = int(since_raw) if since_raw is not None else None
                with state.lock:
                    events = [
                        ev
                        for ev in state.signals
                        if ev["namespace"] == ns
                        and (since_seq is None or ev["seq"] > since_seq)
                        and (prefix is None or bytes(ev["key"]).decode("utf-8").startswith(prefix))
                    ]
                chunks = [b'event: subscribed\ndata: {"ok": true}\n\n']
                for ev in events:
                    chunks.append(
                        f"event: signal\nid: {ev['seq']}\ndata: {json.dumps(ev)}\n\n".encode()
                    )
                payload = b"".join(chunks)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self._send_json(404, {"error": "not_found", "message": parsed.path})

        def do_PUT(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/")]
            raw = self._read_body()
            if len(parts) == 3 and parts[0] == "v2" and parts[1] == "blobs":
                digest = sha(raw)
                with state.lock:
                    state.blobs[(parts[2], digest)] = raw
                self._send_json(200, {"sha256_hex": digest})
                return
            if len(parts) == 4 and parts[0] == "v2" and parts[1] == "kv":
                ns, key = parts[2], parts[3]
                req = json.loads(raw.decode("utf-8"))
                val = base64.b64decode(req["value_b64"].encode("ascii"))
                with state.lock:
                    existing = state.kv.get((ns, key))
                    if not _expected_matches(req.get("expected", "Any"), existing):
                        self._send_json(
                            409, {"error": "conflict", "message": "expected version mismatch"}
                        )
                        return
                    cur_ver = 0 if existing is None else int(existing["meta"]["version"])
                    next_ver = cur_ver + 1
                    state.seq += 1
                    vis = req.get("visibility", "Public")
                    state.kv[(ns, key)] = {
                        "key": list(key.encode("utf-8")),
                        "value": list(val),
                        "visibility": vis,
                        "meta": {
                            "version": next_ver,
                            "flags": 0,
                            "ts_ms": int(time.time() * 1000),
                            "crc32": 0,
                        },
                    }
                    search_op = req.get("search_op")
                    state.search_ops_seen.append((ns, key, search_op))
                    _apply_search_op(state, search_op)
                    state.signals.append(
                        {
                            "seq": state.seq,
                            "epoch": 1,
                            "namespace": ns,
                            "key": list(key.encode("utf-8")),
                            "version": next_ver,
                            "kind": "put",
                        }
                    )
                    self._send_json(200, {"seq": state.seq, "epoch": 1, "versions": [next_ver]})
                return
            self._send_json(404, {"error": "not_found", "message": parsed.path})

        def do_DELETE(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/")]
            raw = self._read_body()
            if len(parts) == 4 and parts[0] == "v2" and parts[1] == "kv":
                ns, key = parts[2], parts[3]
                req = json.loads(raw.decode("utf-8")) if raw else {}
                with state.lock:
                    existing = state.kv.get((ns, key))
                    if existing is None:
                        self._send_json(404, {"error": "not_found", "message": "missing key"})
                        return
                    if not _expected_matches(req.get("expected", "Any"), existing):
                        self._send_json(409, {"error": "conflict", "message": "version conflict"})
                        return
                    cur_ver = int(existing["meta"]["version"])
                    state.kv.pop((ns, key), None)
                    _apply_search_op(state, req.get("search_op"))
                    state.seq += 1
                    self._send_json(200, {"seq": state.seq, "epoch": 1, "versions": [cur_ver + 1]})
                return
            self._send_json(404, {"error": "not_found", "message": parsed.path})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/")]
            req = json.loads(self._read_body().decode("utf-8"))
            if parsed.path == "/v2/search/query":
                needle = str(req.get("query", "")).lower()
                ns, kind, limit = req.get("namespace"), req.get("kind"), int(req.get("limit", 20))
                hits = []
                with state.lock:
                    for doc in state.search_docs.values():
                        if ns is not None and doc["namespace"] != ns:
                            continue
                        if kind is not None and doc["kind"] != kind:
                            continue
                        if (
                            needle
                            and needle in f"{doc.get('title', '')}\n{doc.get('body', '')}".lower()
                        ):
                            hits.append(
                                {
                                    "namespace": doc["namespace"],
                                    "doc_id": doc["doc_id"],
                                    "kind": doc["kind"],
                                    "title": doc["title"],
                                    "score": 1.0,
                                    "visibility": doc["visibility"],
                                }
                            )
                self._send_json(200, {"hits": hits[:limit]})
                return
            if parsed.path == "/v2/leases/acquire":
                key = (req["namespace"], req["resource"])
                now_ms = int(time.time() * 1000)
                with state.lock:
                    cur = state.leases.get(key)
                    if cur is not None and cur["expires_at_ms"] > now_ms:
                        # Live membus @ f33857f6 returns 409 + {"error": "lease_held"}!
                        self._send_json(409, {"error": "lease_held", "message": "already held"})
                        return
                    gen = state.lease_gens.get(key, 0) + 1
                    state.lease_gens[key] = gen
                    grant = {
                        "namespace": req["namespace"],
                        "resource": req["resource"],
                        "holder": req["holder"],
                        "generation": gen,
                        "token": secrets.token_hex(12),
                        "expires_at_ms": now_ms + int(req["ttl_ms"]),
                    }
                    state.leases[key] = grant
                self._send_json(200, grant)
                return
            if parsed.path in ("/v2/leases/renew", "/v2/leases/release", "/v2/leases/validate"):
                key = (req["namespace"], req["resource"])
                now_ms = int(time.time() * 1000)
                with state.lock:
                    cur = state.leases.get(key)
                    valid = (
                        cur is not None
                        and cur["expires_at_ms"] > now_ms
                        and cur["holder"] == req["holder"]
                        and cur["generation"] == req["generation"]
                        and cur["token"] == req["token"]
                    )
                    if parsed.path == "/v2/leases/validate":
                        self._send_json(200, {"ok": valid})
                        return
                    if parsed.path == "/v2/leases/release":
                        if valid:
                            state.leases.pop(key, None)
                        self._send_json(200, {"ok": valid})
                        return
                    if not valid:
                        self._send_json(409, {"error": "lease_expired", "message": "inactive"})
                        return
                    assert cur is not None
                    cur["expires_at_ms"] = now_ms + int(req["ttl_ms"])
                    self._send_json(200, cur)
                return
            if (
                len(parts) == 6
                and parts[0] == "v2"
                and parts[1] == "catalog"
                and parts[2] == "collections"
                and parts[4] == "migrations"
            ):
                ns, action = parts[3], parts[5]
                if action == "begin":
                    # Enforce exact live membus BeginMigrationReq wire schema ([u8; 32] + Vec<u8>).
                    sha_arr = req.get("target_sha256")
                    schema_arr = req.get("target_schema_bytes")
                    if (
                        not isinstance(sha_arr, list)
                        or len(sha_arr) != 32
                        or not all(isinstance(x, int) and 0 <= x <= 255 for x in sha_arr)
                        or not isinstance(schema_arr, list)
                        or not all(isinstance(x, int) and 0 <= x <= 255 for x in schema_arr)
                    ):
                        self._send_json(
                            422, {"error": "unprocessable", "message": "invalid BeginMigrationReq"}
                        )
                        return
                with state.lock:
                    state.migrations.append((ns, action, req))
                self._send_json(200, {"ok": True, "namespace": ns, "action": action})
                return
            self._send_json(404, {"error": "not_found", "message": parsed.path})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = str(server.server_address[0])
    port = int(server.server_address[1])
    return server, state, f"http://{host}:{port}"


class _FailingOrSlowBackend(EmbeddedFileBackend):
    def __init__(self, root: Path, *, fail: bool = False, delay_s: float = 0.0) -> None:
        super().__init__(root)
        self.fail = fail
        self.delay_s = delay_s

    def put_record(self, *args: Any, **kwargs: Any) -> Any:
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        if self.fail:
            raise RuntimeError("simulated route_b outage")
        return super().put_record(*args, **kwargs)


class StorageBackendAndMultiRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def _start_server_with_cleanup(self) -> tuple[_MembusV2State, str]:
        server, state, base_url = _start_membus_v2_server()
        # LIFO order: register server_close first so shutdown() runs BEFORE server_close().
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return state, base_url

    def test_membus_v2_backend_wire_exactness_tls_visibility_leases_and_sse(self) -> None:
        for bad_url in (
            "http://10.0.0.8:8080",
            "http://membus.example.com:8080",
            "ftp://127.0.0.1:8080",
            "https://user:pass@membus.example.com",
        ):
            with self.assertRaises(ValueError):
                MembusV2Backend(bad_url)

        self.assertIsInstance(MembusV2Backend("https://membus.internal:8443"), StorageBackend)
        self.assertIsInstance(MembusV2Backend("http://localhost:8080"), StorageBackend)
        self.assertIsInstance(MembusV2Backend("http://[::1]:8080"), StorageBackend)

        state, base_url = self._start_server_with_cleanup()
        client = MembusV2Backend(base_url, token="test-token")
        self.assertEqual(client.health()["status"], "ok")

        # expected_version=0 sends "Absent" (succeeds on absent key, 409 Conflict once present).
        r1 = client.put_record(
            "research",
            "rec-public",
            b"unique-public-token body text",
            expected_version=0,
            visibility="Public",
            metadata={"kind": "finding", "title": "Public Title"},
        )
        self.assertEqual(r1.version, 1)
        got = client.get_record("research", "rec-public")
        assert got is not None
        self.assertEqual(got.value, b"unique-public-token body text")
        with self.assertRaises(BackendConflictError):
            client.put_record("research", "rec-public", b"stale", expected_version=0)

        client.put_record(
            "research",
            "rec-meta",
            b"secret-meta-body-never-indexed",
            visibility="MetadataOnly",
            metadata={"kind": "finding", "title": "MetadataOnly Headline"},
        )
        client.put_record(
            "research",
            "rec-sealed",
            b"ultra-sealed-d12-body-token",
            visibility="Sealed",
            metadata={"kind": "finding", "title": "Sealed Headline"},
        )

        # Sealed emits Delete tombstone (never Index or sealed body text).
        sealed_ops = [op for _, k, op in state.search_ops_seen if k == "rec-sealed"]
        self.assertEqual(
            sealed_ops, [{"Delete": {"namespace": "research", "doc_id": "rec-sealed"}}]
        )
        self.assertEqual(client.search("ultra-sealed-d12-body-token", namespace="research"), [])
        self.assertEqual(client.search("Sealed Headline", namespace="research"), [])
        self.assertEqual(client.search("secret-meta-body-never-indexed", namespace="research"), [])
        self.assertEqual(
            [h.doc_id for h in client.search("MetadataOnly Headline", namespace="research")],
            ["rec-meta"],
        )
        self.assertEqual(
            [h.doc_id for h in client.search("unique-public-token", namespace="research")],
            ["rec-public"],
        )

        # Downgrading a Public record to Sealed tombstones its prior search entry.
        client.put_record(
            "research",
            "rec-public",
            b"now-sealed-body",
            expected_version=1,
            visibility="Sealed",
            metadata={"kind": "finding", "title": "Public Title"},
        )
        self.assertEqual(client.search("unique-public-token", namespace="research"), [])
        self.assertEqual(client.search("Public Title", namespace="research"), [])

        # Blobs: put, has, get, and integrity failure on corruption.
        blob_bytes = b"immutable-cas-payload-123"
        digest = client.put_blob("research", blob_bytes)
        self.assertEqual(digest, sha(blob_bytes))
        self.assertTrue(client.has_blob("research", digest))
        self.assertFalse(client.has_blob("research", "0" * 64))
        self.assertEqual(client.get_blob("research", digest), blob_bytes)
        self.assertIsNone(client.get_blob("research", "0" * 64))
        state.corrupt_next_blob_get = True
        with self.assertRaises(StorageBackendError):
            client.get_blob("research", digest)

        # Leases: live membus 409 + lease_held maps cleanly to BackendContendedError!
        grant = client.acquire_lease("research", "store-owner", "worker-a", ttl_ms=10_000)
        self.assertEqual(grant.generation, 1)
        self.assertTrue(client.validate_lease(grant))
        with self.assertRaises(BackendContendedError):
            client.acquire_lease("research", "store-owner", "worker-b", ttl_ms=10_000)
        renewed = client.renew_lease(grant, ttl_ms=20_000)
        self.assertGreaterEqual(renewed.expires_at_ms, grant.expires_at_ms)
        self.assertTrue(client.release_lease(renewed))
        self.assertFalse(client.validate_lease(renewed))

        # SSE signals: cursor & prefix filtering.
        all_signals = list(client.stream_signals("research", prefix="rec-"))
        self.assertEqual(
            [ev.key for ev in all_signals],
            ["rec-public", "rec-meta", "rec-sealed", "rec-public"],
        )
        tail = list(client.stream_signals("research", since_seq=all_signals[1].seq))
        self.assertEqual([ev.key for ev in tail], ["rec-sealed", "rec-public"])

        # delete_record emits search Delete tombstone so MetadataOnly headline disappears too.
        self.assertTrue(client.delete_record("research", "rec-meta", expected_version=1))
        self.assertIsNone(client.get_record("research", "rec-meta"))
        self.assertEqual(client.search("MetadataOnly Headline", namespace="research"), [])

    def test_embedded_file_backend_restart_continuity_and_visibility(self) -> None:
        emb_root = self.base / "embedded"
        backend = EmbeddedFileBackend(emb_root)
        self.assertIsInstance(backend, StorageBackend)
        rec = backend.put_record(
            "sw", "k1", b"alpha payload", visibility="Public", metadata={"title": "Alpha"}
        )
        self.assertEqual(rec.version, 1)
        backend.put_record(
            "sw", "k-sealed", b"sealed secret", visibility="Sealed", metadata={"title": "Hidden"}
        )
        grant1 = backend.acquire_lease("sw", "owner", "h1", ttl_ms=30_000)
        self.assertEqual(grant1.generation, 1)

        # Reopen fresh EmbeddedFileBackend instance -> search index, leases, and gens survive!
        reopened = EmbeddedFileBackend(emb_root)
        self.assertEqual([h.doc_id for h in reopened.search("alpha", namespace="sw")], ["k1"])
        self.assertEqual(reopened.search("sealed", namespace="sw"), [])
        self.assertEqual(reopened.search("Hidden", namespace="sw"), [])
        self.assertTrue(reopened.validate_lease(grant1))
        with self.assertRaises(BackendContendedError):
            reopened.acquire_lease("sw", "owner", "h2", ttl_ms=30_000)
        self.assertTrue(reopened.release_lease(grant1))
        grant2 = reopened.acquire_lease("sw", "owner", "h2", ttl_ms=30_000)
        self.assertEqual(grant2.generation, 2)

    def test_multi_route_shadow_async_isolation_lazy_worker_and_close_guard(self) -> None:
        route_a = EmbeddedFileBackend(self.base / "route_a")
        # single mode with route_b=None spawns ZERO background workers.
        with MultiRouteBackend(route_a, mode="single") as single_router:
            self.assertIsNone(single_router._worker)
            single_router.put_record("ns", "k0", b"zero")
            self.assertIsNone(single_router._worker)
        with self.assertRaises(RuntimeError):
            single_router.put_record("ns", "k-after-close", b"nope")

        # Route B errors NEVER fail Route A.
        route_b_fail = _FailingOrSlowBackend(self.base / "route_b_fail", fail=True)
        with MultiRouteBackend(
            route_a, route_b_fail, mode="shadow-async", route_b_timeout_ms=80
        ) as router:
            res = router.put_record("ns", "k1", b"payload-1")
            self.assertEqual(res.value, b"payload-1")
            self.assertTrue(router.flush_shadow(timeout=2.0))
            summary = router.parity_summary(recent_limit=0)
            self.assertEqual(summary["recent_events"], [])
            self.assertEqual(summary["counts"]["route_b_errors"], 1)
            self.assertEqual(summary["counts"]["mismatched_ops"], 1)

        # Route B hang NEVER stalls Route A, times out cleanly, and bounds worker threads.
        route_b_slow = _FailingOrSlowBackend(self.base / "route_b_slow", delay_s=0.4)
        with MultiRouteBackend(
            route_a, route_b_slow, mode="shadow-async", route_b_timeout_ms=50, max_shadow_workers=1
        ) as router_slow:
            t0 = time.perf_counter()
            res2 = router_slow.put_record("ns", "k2", b"payload-2")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.assertEqual(res2.value, b"payload-2")
            self.assertLess(elapsed_ms, 250.0)
            self.assertTrue(router_slow.flush_shadow(timeout=2.0))
            slow_summary = router_slow.parity_summary()
            self.assertEqual(slow_summary["counts"]["route_b_timeouts"], 1)

    def test_multi_route_dual_sync_canary_and_schema_migration_governance(self) -> None:
        state, base_url = self._start_server_with_cleanup()
        route_a = EmbeddedFileBackend(self.base / "mig_a")
        route_b = MembusV2Backend(base_url)

        with MultiRouteBackend(route_a, route_b, mode="single") as router:
            controller = SchemaMigrationController(router=router, catalog_backend=route_b)
            target_sha = sha(b'{"schema":"v2"}')

            with self.assertRaises(ValueError):
                controller.begin_expand(
                    "research", from_version=2, target_version=1, target_sha256=target_sha
                )

            st1 = controller.begin_expand(
                "research",
                from_version=1,
                target_version=2,
                target_sha256=target_sha,
                target_schema_bytes=b'{"schema":"v2"}',
            )
            self.assertEqual(st1.phase, "expanding")
            with self.assertRaises(ValueError):
                controller.commit_cutover(cutover_seq=10)

            controller.enable_dual_write(routing_mode="dual-sync")
            router.put_record("research", "doc-1", b"dual-write-body")
            self.assertEqual(router.parity_summary()["counts"]["matched_ops"], 1)

            with self.assertRaises(ValueError):
                controller.record_verification(verified_through=5, mismatch_count=1)
            controller.record_verification(verified_through=5, mismatch_count=0)
            with self.assertRaises(ValueError):
                controller.record_verification(verified_through=4, mismatch_count=0)
            with self.assertRaises(ValueError):
                controller.commit_cutover(cutover_seq=10)
            with self.assertRaises(ValueError):
                controller.commit_cutover(cutover_seq=True)  # type: ignore[arg-type]

            controller.record_verification(verified_through=10, mismatch_count=0)
            st4 = controller.commit_cutover(cutover_seq=10)
            self.assertEqual(st4.phase, "committed")
            self.assertEqual(router.mode, "canary-cutover")

            # Canary-cutover increments canary_ops without inflating matched_ops.
            router.put_record("research", "doc-2", b"canary-only")
            self.assertIsNone(route_a.get_record("research", "doc-2"))
            parity_after_canary = router.parity_summary()
            self.assertEqual(parity_after_canary["counts"]["matched_ops"], 1)
            self.assertEqual(parity_after_canary["counts"]["canary_ops"], 1)

            # Abort/rollback restores single mode and notifies catalog.
            controller.begin_expand(
                "research", from_version=2, target_version=3, target_sha256=sha(b'{"schema":"v3"}')
            )
            controller.enable_dual_write(routing_mode="shadow-async")
            aborted = controller.abort_rollback("canary soak drift")
            self.assertEqual(aborted.phase, "aborted")
            self.assertEqual(router.mode, "single")
            self.assertEqual(
                [act for _, act, _ in state.migrations],
                ["begin", "advance", "advance", "commit", "begin", "abort"],
            )

        # Dual-sync Route B failure explicitly states Route A already committed.
        route_b_broken = _FailingOrSlowBackend(self.base / "mig_b_fail", fail=True)
        with (
            MultiRouteBackend(route_a, route_b_broken, mode="dual-sync") as broken_router,
            self.assertRaisesRegex(RuntimeError, "route_a already committed"),
        ):
            broken_router.put_record("research", "doc-partial", b"data")

        # EmbeddedFileBackend refuses non-ordinary leaves (e.g. directory masquerading as record).
        guard_backend = EmbeddedFileBackend(self.base / "ordinary_guard")
        (guard_backend.kv_dir / sha(b"research\0poisoned")).mkdir()
        with self.assertRaisesRegex(ValueError, "ordinary"):
            guard_backend.put_record("research", "poisoned", b"payload")
        with self.assertRaisesRegex(ValueError, "ordinary"):
            EmbeddedFileBackend(self.base / "ordinary_guard")


if __name__ == "__main__":
    unittest.main()
