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
    """In-memory reference state implementing the Membus v2 HTTP/JSON/SSE contract."""

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
                    cur_ver = 0 if existing is None else int(existing["meta"]["version"])
                    exp = req.get("expected", "Any")
                    if (
                        isinstance(exp, dict)
                        and "Version" in exp
                        and int(exp["Version"]) != cur_ver
                    ):
                        self._send_json(
                            409,
                            {"error": "version_mismatch", "message": "expected version mismatch"},
                        )
                        return
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
                    if isinstance(search_op, dict) and "Index" in search_op:
                        doc = search_op["Index"]
                        if doc.get("visibility") != "Sealed":
                            state.search_docs[(ns, key)] = doc
                    else:
                        state.search_docs.pop((ns, key), None)
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
                    cur_ver = int(existing["meta"]["version"])
                    exp = req.get("expected", "Any")
                    if (
                        isinstance(exp, dict)
                        and "Version" in exp
                        and int(exp["Version"]) != cur_ver
                    ):
                        self._send_json(409, {"error": "conflict", "message": "version conflict"})
                        return
                    state.kv.pop((ns, key), None)
                    state.search_docs.pop((ns, key), None)
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
                ns = req.get("namespace")
                kind = req.get("kind")
                limit = int(req.get("limit", 20))
                hits = []
                with state.lock:
                    for doc in state.search_docs.values():
                        if ns is not None and doc["namespace"] != ns:
                            continue
                        if kind is not None and doc["kind"] != kind:
                            continue
                        hay = f"{doc.get('title', '')}\n{doc.get('body', '')}".lower()
                        if needle and needle in hay:
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
                        self._send_json(423, {"error": "lease_held", "message": "already held"})
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
                        self._send_json(
                            409, {"error": "lease_expired", "message": "inactive lease"}
                        )
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

    def test_membus_v2_backend_tls_visibility_blobs_leases_and_sse_signals(self) -> None:
        # Non-loopback plaintext HTTP is rejected fail-closed.
        for bad_url in (
            "http://10.0.0.8:8080",
            "http://membus.example.com:8080",
            "ftp://127.0.0.1:8080",
            "https://user:pass@membus.example.com",
        ):
            with self.assertRaises(ValueError):
                MembusV2Backend(bad_url)

        # Loopback HTTP and non-loopback HTTPS are accepted.
        self.assertIsInstance(MembusV2Backend("https://membus.internal:8443"), StorageBackend)
        self.assertIsInstance(MembusV2Backend("http://localhost:8080"), StorageBackend)
        self.assertIsInstance(MembusV2Backend("http://[::1]:8080"), StorageBackend)

        server, state, base_url = _start_membus_v2_server()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        client = MembusV2Backend(base_url, token="test-token")

        self.assertEqual(client.health()["status"], "ok")

        # KV put/get/conflict + SearchVisibility isolation.
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

        # Sealed record NEVER sent search_op to server, and NEVER appears in search.
        sealed_ops = [op for ns, k, op in state.search_ops_seen if k == "rec-sealed"]
        self.assertEqual(sealed_ops, [None])
        self.assertEqual(client.search("ultra-sealed-d12-body-token", namespace="research"), [])
        self.assertEqual(client.search("Sealed Headline", namespace="research"), [])
        # MetadataOnly indexes title, NOT body.
        self.assertEqual(client.search("secret-meta-body-never-indexed", namespace="research"), [])
        self.assertEqual(
            [h.doc_id for h in client.search("MetadataOnly Headline", namespace="research")],
            ["rec-meta"],
        )
        # Public indexes body.
        self.assertEqual(
            [h.doc_id for h in client.search("unique-public-token", namespace="research")],
            ["rec-public"],
        )

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

        # Leases: acquire, contention, validate, renew, release.
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
        self.assertEqual([ev.key for ev in all_signals], ["rec-public", "rec-meta", "rec-sealed"])
        tail = list(client.stream_signals("research", since_seq=all_signals[0].seq))
        self.assertEqual([ev.key for ev in tail], ["rec-meta", "rec-sealed"])

        # Delete record.
        self.assertTrue(client.delete_record("research", "rec-public", expected_version=1))
        self.assertIsNone(client.get_record("research", "rec-public"))

    def test_embedded_file_backend_protocol_visibility_and_leases(self) -> None:
        backend = EmbeddedFileBackend(self.base / "embedded")
        self.assertIsInstance(backend, StorageBackend)
        rec = backend.put_record(
            "sw", "k1", b"alpha payload", visibility="Public", metadata={"title": "Alpha"}
        )
        self.assertEqual(rec.version, 1)
        backend.put_record(
            "sw", "k-sealed", b"sealed secret", visibility="Sealed", metadata={"title": "Hidden"}
        )
        self.assertEqual([h.doc_id for h in backend.search("alpha", namespace="sw")], ["k1"])
        self.assertEqual(backend.search("sealed", namespace="sw"), [])
        self.assertEqual(backend.search("Hidden", namespace="sw"), [])

        digest = backend.put_blob("sw", b"blob-data")
        self.assertTrue(backend.has_blob("sw", digest))
        self.assertEqual(backend.get_blob("sw", digest), b"blob-data")

        grant = backend.acquire_lease("sw", "owner", "h1", ttl_ms=5_000)
        self.assertTrue(backend.validate_lease(grant))
        with self.assertRaises(BackendContendedError):
            backend.acquire_lease("sw", "owner", "h2", ttl_ms=5_000)
        self.assertTrue(backend.release_lease(grant))

    def test_multi_route_shadow_async_fault_and_timeout_isolation(self) -> None:
        route_a = EmbeddedFileBackend(self.base / "route_a")
        route_b_fail = _FailingOrSlowBackend(self.base / "route_b_fail", fail=True)
        router = MultiRouteBackend(
            route_a, route_b_fail, mode="shadow-async", route_b_timeout_ms=80
        )
        self.addCleanup(router.close)

        # Route B errors NEVER fail Route A.
        res = router.put_record("ns", "k1", b"payload-1")
        self.assertEqual(res.value, b"payload-1")
        self.assertTrue(router.flush_shadow(timeout=2.0))
        summary = router.parity_summary()
        self.assertEqual(summary["counts"]["route_b_errors"], 1)
        self.assertEqual(summary["counts"]["mismatched_ops"], 1)

        # Route B hang NEVER stalls Route A and times out cleanly.
        route_b_slow = _FailingOrSlowBackend(self.base / "route_b_slow", delay_s=0.6)
        router_slow = MultiRouteBackend(
            route_a, route_b_slow, mode="shadow-async", route_b_timeout_ms=60
        )
        self.addCleanup(router_slow.close)
        t0 = time.perf_counter()
        res2 = router_slow.put_record("ns", "k2", b"payload-2")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertEqual(res2.value, b"payload-2")
        self.assertLess(elapsed_ms, 250.0)
        self.assertTrue(router_slow.flush_shadow(timeout=2.0))
        slow_summary = router_slow.parity_summary()
        self.assertEqual(slow_summary["counts"]["route_b_timeouts"], 1)
        self.assertEqual(router_slow.route_status()["authoritative"], "route_a")

    def test_multi_route_dual_sync_canary_and_schema_migration_governance(self) -> None:
        server, state, base_url = _start_membus_v2_server()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        route_a = EmbeddedFileBackend(self.base / "mig_a")
        route_b = MembusV2Backend(base_url)
        router = MultiRouteBackend(route_a, route_b, mode="single")
        self.addCleanup(router.close)

        controller = SchemaMigrationController(router=router, catalog_backend=route_b)
        target_sha = sha(b'{"schema":"v2"}')

        # Lossy downgrade refused fail-closed.
        with self.assertRaises(ValueError):
            controller.begin_expand(
                "research", from_version=2, target_version=1, target_sha256=target_sha
            )

        # Expand -> Dual-Write -> Verify -> Commit.
        st1 = controller.begin_expand(
            "research", from_version=1, target_version=2, target_sha256=target_sha
        )
        self.assertEqual(st1.phase, "expanding")
        # Out-of-order cutover refused.
        with self.assertRaises(ValueError):
            controller.commit_cutover(cutover_seq=10)

        st2 = controller.enable_dual_write(routing_mode="dual-sync")
        self.assertEqual(st2.phase, "dual-write")
        self.assertEqual(router.mode, "dual-sync")

        # Dual-sync write hits both Route A and Route B with exact parity.
        router.put_record("research", "doc-1", b"dual-write-body")
        self.assertEqual(router.parity_summary()["counts"]["matched_ops"], 1)
        got_b = route_b.get_record("research", "doc-1")
        assert got_b is not None
        self.assertEqual(got_b.value, b"dual-write-body")

        # Verification rejects mismatches and non-monotonic watermarks.
        with self.assertRaises(ValueError):
            controller.record_verification(verified_through=5, mismatch_count=1)
        st3 = controller.record_verification(verified_through=5, mismatch_count=0)
        self.assertEqual(st3.phase, "verified")
        with self.assertRaises(ValueError):
            controller.record_verification(verified_through=4, mismatch_count=0)
        # Cutover refused while verified_through < cutover_seq.
        with self.assertRaises(ValueError):
            controller.commit_cutover(cutover_seq=10)

        controller.record_verification(verified_through=10, mismatch_count=0)
        st4 = controller.commit_cutover(cutover_seq=10)
        self.assertEqual(st4.phase, "committed")
        self.assertEqual(router.mode, "canary-cutover")
        self.assertEqual(router.route_status()["authoritative"], "route_b")

        # Canary-cutover writes authoritatively to Route B.
        router.put_record("research", "doc-2", b"canary-only")
        self.assertIsNone(route_a.get_record("research", "doc-2"))
        got_b2 = route_b.get_record("research", "doc-2")
        assert got_b2 is not None
        self.assertEqual(got_b2.value, b"canary-only")

        # Abort/rollback path restores router to single (Route A) and notifies catalog.
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


if __name__ == "__main__":
    unittest.main()
