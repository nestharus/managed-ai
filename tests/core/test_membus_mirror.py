"""Unit and live Membus v2 end-to-end tests for store-to-Membus mirroring."""

import base64
import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
for _p in (_REPO / "src", _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.ci.verify_membus_live import (  # noqa: E402
    EphemeralMembusDaemon,
    find_membus_binary,
)

from managed_ai_information.core.membus_mirror import (  # noqa: E402
    MIRROR_REPORT_FORMAT,
    SEALED_CUSTODY_FORMAT,
    MembusStoreMirror,
    build_mirror_config,
)
from managed_ai_information.core.storage import OCC_SEALED, ObjectStore, encode, sha  # noqa: E402
from managed_ai_information.core.storage_backend import MembusV2Backend  # noqa: E402

_MEMBUS_V2_BIN = find_membus_binary()


def _find_crw_store() -> Path | None:
    env_override = os.environ.get("MANAGED_AI_CRW_STORE")
    candidates: list[Path] = []
    if env_override:
        candidates.append(Path(env_override))
    for parent in (_REPO.parent, *_REPO.parents[:2]):
        candidates.append(parent / "workspaces" / "code-review-workflows" / "stores" / "crw")
    for cand in candidates:
        if (cand / "system.json").is_file() and (cand / "work" / "records").is_dir():
            return cand
    return None


def _write_fixture_store(root: Path, *, include_crw_terms: bool = False) -> dict[str, str]:
    (root / "work" / "records").mkdir(parents=True, exist_ok=True)
    (root / "exchange" / "objects").mkdir(parents=True, exist_ok=True)
    obj_store = ObjectStore(root / "exchange" / "objects")
    blob_a = obj_store.put(b"mirror-blob-alpha-content")
    blob_b = obj_store.put(b"mirror-blob-beta-content", compress=True)

    sys_doc = {
        "format": "g60-agent-system/0.1",
        "instance_id": "mirror-fixture",
        "store_schema": "managed-ai-store/1",
        "schema_bindings": {"note": "0" * 64},
    }
    (root / "system.json").write_bytes(encode(sys_doc))

    records: list[dict[str, Any]] = [
        {
            "id": "pub-doc-1",
            "kind": "note",
            "active": True,
            "revision": 1,
            "content": {
                "title": "Boundary migration guide",
                "summary": "Covers schema migration and boundary contracts.",
            },
        },
        {
            "id": "meta-doc-1",
            "kind": "index",
            "active": True,
            "revision": 1,
            "content": {
                "title": "Public Index Title",
                "body_marker": "meta-only-hidden-phrase",
            },
        },
        {
            "id": "avail-sealed-1",
            "kind": "evidence",
            "availability": "sealed",
            "active": True,
            "revision": 1,
            "content": {"summary": "record-sealed-unindexed-phrase"},
        },
        {
            "id": "basis-sealed-1",
            "kind": "evidence",
            "active": True,
            "revision": 1,
            "basis": [{"path": "sealed/item.txt", "availability": "sealed"}],
            "content": {"summary": "basis-sealed-unindexed-phrase"},
        },
    ]
    if include_crw_terms:
        records.extend(
            [
                {
                    "id": "crw-ctrl-o31",
                    "kind": "crw-control",
                    "active": True,
                    "revision": 1,
                    "content": {
                        "title": "Symlink custody guard",
                        "summary": "Prevent symlink traversal on writes.",
                    },
                },
                {
                    "id": "crw-ctrl-o26",
                    "kind": "crw-control",
                    "active": True,
                    "revision": 1,
                    "content": {
                        "title": "Install verification",
                        "summary": "Verify clean install and first-run delivery.",
                    },
                },
            ]
        )

    for rec in records:
        (root / "work" / "records" / f"{rec['id']}.json").write_bytes(encode(rec))

    # Deliberately malformed JSON for explicit pre-sealed ID to prove it is NEVER parsed!
    (root / "work" / "records" / "explicit-sealed-1.json").write_bytes(
        b"NOT_EVEN_JSON explicit-sealed-unparsed-phrase"
    )
    return {"blob_a": blob_a, "blob_b": blob_b}


class _MockMembusState:
    def __init__(self) -> None:
        self.seq = 0
        self.kv: dict[tuple[str, str], dict[str, Any]] = {}
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.search_docs: dict[tuple[str, str], dict[str, Any]] = {}
        self.collections: dict[str, dict[str, Any]] = {}
        self.tamper_next_get_key: str | None = None
        self.fail_put_key: str | None = None


class _MockMembusServer:
    """Minimal loopback HTTP server speaking the Membus v2 wire contract."""

    def __init__(self) -> None:
        self.state = _MockMembusState()
        state = self.state

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, _fmt: str, *_args: object) -> None:
                return

            def _send_json(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length", "0"))
                return self.rfile.read(length) if length > 0 else b""

            def do_HEAD(self) -> None:
                parts = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
                if len(parts) == 4 and parts[0] == "v2" and parts[1] == "blobs":
                    ns = urllib.parse.unquote(parts[2])
                    digest = urllib.parse.unquote(parts[3])
                    self.send_response(200 if (ns, digest) in state.blobs else 404)
                    self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()

            def do_GET(self) -> None:
                path = urllib.parse.urlsplit(self.path).path
                if path == "/v2/health":
                    self._send_json(
                        200, {"epoch": 1, "latest_seq": state.seq, "slot_role": "active"}
                    )
                    return
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[:3] == ["v2", "catalog", "collections"]:
                    ns = urllib.parse.unquote(parts[3])
                    col = state.collections.get(ns)
                    if col is None:
                        self._send_json(404, {"error": "not_found", "message": "missing"})
                    else:
                        self._send_json(200, col)
                    return
                if len(parts) == 4 and parts[:2] == ["v2", "blobs"]:
                    ns, digest = urllib.parse.unquote(parts[2]), urllib.parse.unquote(parts[3])
                    raw = state.blobs.get((ns, digest))
                    if raw is None:
                        self._send_json(404, {"error": "not_found", "message": "missing"})
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if len(parts) >= 4 and parts[:2] == ["v2", "kv"]:
                    ns = urllib.parse.unquote(parts[2])
                    key = "/".join(urllib.parse.unquote(p) for p in parts[3:])
                    rec = state.kv.get((ns, key))
                    if rec is None:
                        self._send_json(404, {"error": "not_found", "message": "missing"})
                        return
                    payload = dict(rec)
                    if state.tamper_next_get_key == key:
                        payload["value"] = list(b"tampered")
                    self._send_json(200, payload)
                    return
                self._send_json(404, {"error": "not_found", "message": path})

            def do_PUT(self) -> None:
                path = urllib.parse.urlsplit(self.path).path
                parts = path.strip("/").split("/")
                raw = self._read_body()
                if len(parts) == 3 and parts[:2] == ["v2", "blobs"]:
                    ns = urllib.parse.unquote(parts[2])
                    digest = sha(raw)
                    state.blobs[(ns, digest)] = raw
                    state.seq += 1
                    self._send_json(200, {"sha256_hex": digest, "seq": state.seq})
                    return
                if len(parts) >= 4 and parts[:2] == ["v2", "kv"]:
                    ns = urllib.parse.unquote(parts[2])
                    key = "/".join(urllib.parse.unquote(p) for p in parts[3:])
                    if state.fail_put_key == key:
                        self._send_json(500, {"error": "internal", "message": "injected failure"})
                        return
                    body = json.loads(raw.decode("utf-8"))
                    state.seq += 1
                    val_bytes = base64.b64decode(body["value_b64"])
                    search_op = body.get("search_op") or {}
                    vis = "Public"
                    if "Delete" in search_op:
                        vis = "Sealed"
                        state.search_docs.pop((ns, key), None)
                    elif "Index" in search_op:
                        idx = search_op["Index"]
                        vis = str(idx.get("visibility", "Public"))
                        state.search_docs[(ns, key)] = dict(idx)
                    state.kv[(ns, key)] = {
                        "meta": {"version": state.seq},
                        "value": list(val_bytes),
                        "visibility": vis,
                    }
                    self._send_json(200, {"seq": state.seq, "versions": [state.seq]})
                    return
                self._send_json(404, {"error": "not_found", "message": path})

            def do_POST(self) -> None:
                path = urllib.parse.urlsplit(self.path).path
                parts = path.strip("/").split("/")
                body = json.loads(self._read_body().decode("utf-8"))
                if path == "/v2/catalog/collections":
                    desc = body["descriptor"]
                    ns = desc["namespace"]
                    col = {
                        "namespace": ns,
                        "schema_version": desc["schema_version"],
                        "compatibility_mode": "Strict",
                        "migration_state": "Active",
                    }
                    state.collections[ns] = col
                    self._send_json(200, col)
                    return
                if (
                    len(parts) == 6
                    and parts[:3] == ["v2", "catalog", "collections"]
                    and parts[4] == "migrations"
                ):
                    ns = urllib.parse.unquote(parts[3])
                    action = parts[5]
                    col = state.collections[ns]
                    if action == "begin":
                        col["compatibility_mode"] = "ExpandDual"
                        col["migration_state"] = {
                            "Migrating": {
                                "target_version": body["target_version"],
                                "verified_through": 0,
                            }
                        }
                    elif action == "advance":
                        col["migration_state"]["Migrating"]["verified_through"] = body[
                            "verified_through"
                        ]
                    elif action == "commit":
                        col["schema_version"] = body["target_version"]
                        col["compatibility_mode"] = "Strict"
                        col["migration_state"] = "Active"
                    elif action == "abort":
                        col["compatibility_mode"] = "Strict"
                        col["migration_state"] = "Active"
                    self._send_json(200, col)
                    return
                if path == "/v2/search/query":
                    q = str(body.get("query", "")).lower()
                    ns_filter = body.get("namespace")
                    kind_filter = body.get("kind")
                    limit = int(body.get("limit", 20))
                    hits = []
                    for doc in state.search_docs.values():
                        if ns_filter and doc["namespace"] != ns_filter:
                            continue
                        if kind_filter and doc["kind"] != kind_filter:
                            continue
                        hay = f"{doc['title']}\n{doc['body']}".lower()
                        if q and q in hay:
                            hits.append(
                                {
                                    "namespace": doc["namespace"],
                                    "doc_id": doc["doc_id"],
                                    "kind": doc["kind"],
                                    "title": doc["title"],
                                    "score": float(hay.count(q)),
                                    "visibility": doc["visibility"],
                                }
                            )
                    hits.sort(key=lambda h: (-h["score"], h["doc_id"]))
                    self._send_json(200, {"hits": hits[:limit]})
                    return
                self._send_json(404, {"error": "not_found", "message": path})

        self._httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        host, port = self._httpd.server_address
        self.base_url = f"http://{host}:{port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> _MockMembusServer:
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2.0)


class MembusMirrorUnitTests(unittest.TestCase):
    def test_dry_run_apply_sealed_custody_and_parity_over_mock_server(self) -> None:
        with tempfile.TemporaryDirectory(prefix="membus-mirror-unit-") as tmp:
            store_root = Path(tmp) / "store"
            blobs = _write_fixture_store(store_root)
            cfg = build_mirror_config(
                "unit_ns",
                sealed_ids=["explicit-sealed-1"],
                metadata_only_ids=["meta-doc-1"],
            )
            mirror = MembusStoreMirror()

            # 1. Dry-run plan classifies all 5 records without reading explicit-sealed-1 payload
            plan = mirror.plan(store_root, cfg)
            rep = plan.to_report()
            self.assertEqual(rep["format"], MIRROR_REPORT_FORMAT)
            self.assertEqual(rep["mode"], "dry-run")
            self.assertEqual(rep["record_count"], 5)
            self.assertEqual(rep["blob_count"], 2)
            self.assertEqual(
                rep["visibility_counts"], {"Public": 1, "MetadataOnly": 1, "Sealed": 3}
            )
            sealed_reasons = {r["record_id"]: r["reason"] for r in rep["sealed_records"]}
            self.assertEqual(sealed_reasons["explicit-sealed-1"], "explicit-sealed-id")
            self.assertEqual(sealed_reasons["avail-sealed-1"], "record-availability-sealed")
            self.assertEqual(sealed_reasons["basis-sealed-1"], "basis-availability-sealed")

            with _MockMembusServer() as server:
                backend = MembusV2Backend(server.base_url, timeout=5.0)
                apply_rep = mirror.apply(store_root, backend, cfg, verify_blob_bytes=True)
                self.assertEqual(apply_rep["status"], "passed")
                self.assertTrue(apply_rep["collection_registered"])
                self.assertEqual(apply_rep["migration"]["final_phase"], "committed")
                self.assertEqual(apply_rep["migration"]["final_router_mode"], "canary-cutover")
                self.assertEqual(apply_rep["migration"]["target_version"], 2)
                self.assertEqual(apply_rep["records_mirrored"], 5)
                self.assertEqual(apply_rep["blobs_mirrored"], 2)
                self.assertEqual(apply_rep["route_parity"]["mismatched_ops"], 0)

                # Sealed payloads must ONLY hold opaque custody JSON and NEVER appear in search
                sealed_rec = backend.get_record("unit_ns", "records/explicit-sealed-1")
                assert sealed_rec is not None
                custody_doc = json.loads(sealed_rec.value.decode("utf-8"))
                self.assertEqual(custody_doc["format"], SEALED_CUSTODY_FORMAT)
                self.assertEqual(custody_doc["visibility"], "Sealed")
                self.assertNotIn(
                    "explicit-sealed-unparsed-phrase", sealed_rec.value.decode("utf-8")
                )

                for marker in (
                    "explicit-sealed-unparsed-phrase",
                    "record-sealed-unindexed-phrase",
                    "basis-sealed-unindexed-phrase",
                    "meta-only-hidden-phrase",
                ):
                    res = mirror.search(backend, marker, namespace="unit_ns")
                    self.assertEqual(res["hit_count"], 0, msg=f"leaked marker {marker}")

                # Public record and MetadataOnly title are searchable
                pub_hits = mirror.search(backend, "migration", namespace="unit_ns")
                self.assertEqual([h["doc_id"] for h in pub_hits["hits"]], ["records/pub-doc-1"])
                meta_hits = mirror.search(backend, "Public Index Title", namespace="unit_ns")
                self.assertEqual([h["doc_id"] for h in meta_hits["hits"]], ["records/meta-doc-1"])

                # Both CAS blobs mirrored with exact SHA-256 parity
                self.assertEqual(
                    backend.get_blob("unit_ns", blobs["blob_a"]), b"mirror-blob-alpha-content"
                )
                self.assertEqual(
                    backend.get_blob("unit_ns", blobs["blob_b"]), b"mirror-blob-beta-content"
                )

                # Second apply is idempotent and reuses existing blobs
                second = mirror.apply(store_root, backend, cfg)
                self.assertEqual(second["blobs_mirrored"], 0)
                self.assertEqual(second["blobs_reused"], 2)
                self.assertEqual(second["migration"]["from_version"], 2)
                self.assertEqual(second["migration"]["target_version"], 3)

                # Tampering remote record fails verify_parity cleanly
                server.state.tamper_next_get_key = "records/pub-doc-1"
                bad_parity = mirror.verify_parity(store_root, backend, cfg)
                self.assertEqual(bad_parity["status"], "failed")
                self.assertEqual(bad_parity["mismatched_records"], ["pub-doc-1"])

                # Second namespace dry-run, apply, verify-parity, and search round-trip
                server.state.tamper_next_get_key = None
                second_cfg = build_mirror_config("second_ns", sealed_ids=["explicit-sealed-1"])
                out_dry = mirror.plan(store_root, second_cfg).to_report()
                self.assertEqual(out_dry["mode"], "dry-run")

                out_app = mirror.apply(store_root, backend, second_cfg)
                self.assertEqual(out_app["status"], "passed")

                out_ver = mirror.verify_parity(store_root, backend, second_cfg)
                self.assertEqual(out_ver["status"], "passed")

                out_s = mirror.search(backend, "boundary", namespace="second_ns")
                self.assertEqual(out_s["hit_count"], 1)

                with self.assertRaises(ValueError):
                    build_mirror_config("   ")

    def test_packed_occ_sealed_blob_skipped_and_mid_mirror_error_aborts_cleanly(self) -> None:
        """C71A-1 & C71A-2: skip OCC_SEALED packed blobs; abort migration on mid-mirror error."""
        sealed_digest = "a" * 64
        with tempfile.TemporaryDirectory(prefix="membus-mirror-c71a-") as tmp:
            store_root = Path(tmp) / "store"
            blobs = _write_fixture_store(store_root)
            cfg = build_mirror_config("c71a_ns", sealed_ids=["explicit-sealed-1"])
            mirror = MembusStoreMirror()

            orig_iter = ObjectStore.iter_objects

            def _iter_with_sealed(obj_self: ObjectStore):
                yield from orig_iter(obj_self)
                yield {
                    "object": sealed_digest,
                    "storage": "packed",
                    "occ": OCC_SEALED,
                    "raw_bytes": 128,
                }

            orig_read = ObjectStore.read

            def _guard_read(obj_self: ObjectStore, key: str, **kwargs: Any) -> bytes:
                if key == sealed_digest:
                    raise AssertionError("OCC_SEALED packed blob must never be read")
                return orig_read(obj_self, key, **kwargs)

            ObjectStore.iter_objects = _iter_with_sealed  # type: ignore[method-assign]
            ObjectStore.read = _guard_read  # type: ignore[method-assign]
            try:
                plan = mirror.plan(store_root, cfg)
                self.assertEqual(plan.skipped_sealed_blobs, (sealed_digest,))
                self.assertEqual(plan.to_report()["skipped_sealed_blobs"], [sealed_digest])
                self.assertNotIn(sealed_digest, [d for d, _ in plan.blobs])
                self.assertEqual(len(plan.blobs), 2)

                with _MockMembusServer() as server:
                    backend = MembusV2Backend(server.base_url, timeout=5.0)
                    # Inject mid-mirror Route B fault -> must abort collection to Active/Strict
                    server.state.fail_put_key = "records/pub-doc-1"
                    with self.assertRaises(RuntimeError):
                        mirror.apply(store_root, backend, cfg)
                    col_after_fail = backend.get_collection("c71a_ns")
                    assert col_after_fail is not None
                    self.assertEqual(col_after_fail["migration_state"], "Active")
                    self.assertEqual(col_after_fail["compatibility_mode"], "Strict")
                    self.assertEqual(col_after_fail["schema_version"], 1)

                    # Clearing the fault allows retry to succeed cleanly while skipping OCC_SEALED
                    server.state.fail_put_key = None
                    rep = mirror.apply(store_root, backend, cfg)
                    self.assertEqual(rep["status"], "passed")
                    self.assertEqual(rep["migration"]["final_phase"], "committed")
                    self.assertFalse(backend.has_blob("c71a_ns", sealed_digest))
                    self.assertTrue(backend.has_blob("c71a_ns", blobs["blob_a"]))
            finally:
                ObjectStore.iter_objects = orig_iter  # type: ignore[method-assign]
                ObjectStore.read = orig_read  # type: ignore[method-assign]


@unittest.skipUnless(
    _MEMBUS_V2_BIN is not None,
    "compatible membus v2 executable not available in environment",
)
class MembusLiveMirrorTests(unittest.TestCase):
    def test_live_membus_mirror_crw_store_and_tantivy_bm25_search(self) -> None:
        assert _MEMBUS_V2_BIN is not None
        with tempfile.TemporaryDirectory(prefix="membus-crw-mirror-") as tmp:
            copy_store = Path(tmp) / "crw-copy"
            live_crw = _find_crw_store()
            sealed_ids: list[str] = []
            if live_crw is not None:
                shutil.copytree(live_crw, copy_store)
            else:
                _write_fixture_store(copy_store, include_crw_terms=True)
                sealed_ids = ["explicit-sealed-1"]

            # Inject a sealed canary into the isolated temp copy to prove Sealed isolation
            (copy_store / "work" / "records" / "sealed-canary-entry.json").write_bytes(
                encode(
                    {
                        "id": "sealed-canary-entry",
                        "kind": "evidence",
                        "availability": "sealed",
                        "active": True,
                        "revision": 1,
                        "content": {"summary": "zxqsealedcanary9917"},
                    }
                )
            )

            mirror = MembusStoreMirror()
            cfg = build_mirror_config("crw_live_mirror", sealed_ids=sealed_ids)
            with EphemeralMembusDaemon(_MEMBUS_V2_BIN) as daemon:
                backend = MembusV2Backend(daemon.base_url, timeout=15.0)
                app_rep = mirror.apply(copy_store, backend, cfg, verify_blob_bytes=True)
                self.assertEqual(app_rep["status"], "passed")
                self.assertEqual(app_rep["migration"]["final_phase"], "committed")
                self.assertGreaterEqual(app_rep["visibility_counts"]["Sealed"], 1)
                self.assertEqual(app_rep["route_parity"]["mismatched_ops"], 0)
                self.assertEqual(app_rep["route_parity"]["route_b_errors"], 0)

                par_rep = mirror.verify_parity(copy_store, backend, cfg, verify_blob_bytes=True)
                self.assertEqual(par_rep["status"], "passed")
                self.assertGreaterEqual(par_rep["sealed_verified"], 1)

                # Real Tantivy BM25 search finds all 4 required CRW engineering terms
                for term in ("migration", "symlink", "install", "boundary"):
                    q_rep = mirror.search(
                        backend,
                        term,
                        namespace="crw_live_mirror",
                        mode="Bm25",
                        limit=10,
                    )
                    self.assertGreaterEqual(
                        q_rep["hit_count"], 1, msg=f"expected Tantivy BM25 hits for {term!r}"
                    )

                # Sealed canary MUST NEVER surface in BM25 or LiteralSubstring Tantivy search
                for mode in ("Bm25", "LiteralSubstring"):
                    s_rep = mirror.search(
                        backend,
                        "zxqsealedcanary9917",
                        namespace="crw_live_mirror",
                        mode=mode,
                    )
                    self.assertEqual(s_rep["hit_count"], 0, msg=f"sealed leaked in {mode}")


if __name__ == "__main__":
    unittest.main()
