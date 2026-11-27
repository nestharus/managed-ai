#!/usr/bin/env python3
"""Live end-to-end integration and schema-migration harness against real `membus` v2."""

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

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
    SignalEvent,
)
from managed_ai_information.store.coordination_contract import CoordinationFacade  # noqa: E402
from managed_ai_information.store.system import System  # noqa: E402

REPORT_FORMAT = "managed-ai-membus-live-verification/1"
_LISTEN_RE = re.compile(r"^listening http=(127\.0\.0\.1:\d+)$")


def is_membus_v2_binary(path: Path) -> bool:
    """Return True if `path` is an executable Membus v2 binary supporting `serve --http-listen`."""
    if not path.is_file():
        return False
    try:
        proc = subprocess.run(  # noqa: S603
            [str(path), "serve", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return proc.returncode == 0 and "--http-listen" in proc.stdout


def find_membus_binary(explicit: str | Path | None = None) -> Path | None:
    """Locate a Membus v2 binary via explicit arg, MEMBUS_BIN, PATH, or sibling checkout."""
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    env_bin = os.environ.get("MEMBUS_BIN")
    if env_bin:
        candidates.append(Path(env_bin))
    which_bin = shutil.which("membus")
    if which_bin:
        candidates.append(Path(which_bin))

    exe_name = "membus.exe" if os.name == "nt" else "membus"
    for depth in (1, 2):
        if len(REPO_ROOT.parents) > depth:
            parent = REPO_ROOT.parents[depth]
            candidates.append(parent / "membus" / "trunk" / "target" / "release" / exe_name)
            candidates.append(parent / "membus" / "trunk" / "target" / "debug" / exe_name)

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve() if candidate.exists() else candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        if is_membus_v2_binary(resolved):
            return resolved
    return None


def _cleanup_temp_dir(path: Path) -> None:
    for attempt in range(10):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 9:
                shutil.rmtree(path, ignore_errors=True)
                return
            time.sleep(0.05)


class EphemeralMembusDaemon:
    """Isolated ephemeral `membus serve` subprocess bound to a free localhost port."""

    def __init__(self, binary: Path, *, startup_timeout: float = 15.0) -> None:
        self.binary = Path(binary)
        self.startup_timeout = startup_timeout
        self._tmp_path: Path | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []
        self._threads: list[threading.Thread] = []
        self.base_url: str = ""

    @property
    def data_dir(self) -> Path:
        if self._tmp_path is None:
            raise RuntimeError("EphemeralMembusDaemon is not running")
        return self._tmp_path

    def __enter__(self) -> EphemeralMembusDaemon:
        self._tmp_path = Path(tempfile.mkdtemp(prefix="membus-live-verify-"))
        data_dir = self._tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        ready_event = threading.Event()
        listen_addr: list[str] = []

        self._proc = subprocess.Popen(  # noqa: S603
            [
                str(self.binary),
                "serve",
                "--data-dir",
                str(data_dir),
                "--http-listen",
                "127.0.0.1:0",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        stdout_pipe = self._proc.stdout
        stderr_pipe = self._proc.stderr

        def _drain_stdout() -> None:
            if stdout_pipe is None:
                return
            for raw in stdout_pipe:
                line = raw.rstrip("\r\n")
                self._stdout_lines.append(line)
                match = _LISTEN_RE.match(line.strip())
                if match and not listen_addr:
                    listen_addr.append(match.group(1))
                    ready_event.set()

        def _drain_stderr() -> None:
            if stderr_pipe is None:
                return
            for raw in stderr_pipe:
                self._stderr_lines.append(raw.rstrip("\r\n"))

        t_out = threading.Thread(target=_drain_stdout, name="membus-stdout", daemon=True)
        t_err = threading.Thread(target=_drain_stderr, name="membus-stderr", daemon=True)
        t_out.start()
        t_err.start()
        self._threads = [t_out, t_err]

        deadline = time.monotonic() + self.startup_timeout
        while not ready_event.is_set() and time.monotonic() < deadline:
            if self._proc.poll() is not None:
                break
            ready_event.wait(timeout=0.05)

        if not listen_addr:
            err_tail = "\n".join(self._stderr_lines[-20:])
            out_tail = "\n".join(self._stdout_lines[-20:])
            self.stop()
            raise RuntimeError(
                f"membus daemon failed to bind HTTP listener within {self.startup_timeout}s "
                f"(stdout={out_tail!r}, stderr={err_tail!r})"
            )

        self.base_url = f"http://{listen_addr[0]}"
        client = MembusV2Backend(self.base_url, timeout=5.0)
        while time.monotonic() < deadline:
            try:
                health = client.health()
                if isinstance(health, dict) and "epoch" in health:
                    return self
            except Exception:
                time.sleep(0.05)

        self.stop()
        raise RuntimeError(f"membus HTTP listener at {self.base_url} failed health readiness")

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5.0)
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._threads = []
        tmp = self._tmp_path
        self._tmp_path = None
        if tmp is not None:
            _cleanup_temp_dir(tmp)

    def __exit__(self, *_args: object) -> None:
        self.stop()


def _check_health(client: MembusV2Backend) -> dict[str, Any]:
    health = client.health()
    if int(health["epoch"]) < 1:
        raise AssertionError(f"unexpected health epoch: {health!r}")
    if health.get("slot_role") != "active":
        raise AssertionError(f"expected active slot_role: {health!r}")
    return {
        "epoch": int(health["epoch"]),
        "latest_seq": int(health["latest_seq"]),
        "slot_role": str(health["slot_role"]),
    }


def _check_kv_cas_and_delete(client: MembusV2Backend) -> dict[str, Any]:
    ns, key = "live_kv", "records/alpha-1"
    r1 = client.put_record(ns, key, b"payload-v1", expected_version=0)
    if r1.version < 1:
        raise AssertionError(f"expected positive version on create, got {r1.version}")
    got1 = client.get_record(ns, key)
    if got1 is None or got1.value != b"payload-v1" or got1.version != r1.version:
        raise AssertionError(f"get_record mismatch after create: {got1!r} vs {r1!r}")

    conflict_absent = False
    try:
        client.put_record(ns, key, b"dup-create", expected_version=0)
    except BackendConflictError as err:
        conflict_absent = err.status == 409
    if not conflict_absent:
        raise AssertionError("expected 409 BackendConflictError on duplicate expected_version=0")

    r2 = client.put_record(ns, key, b"payload-v2", expected_version=r1.version)
    if r2.version <= r1.version:
        raise AssertionError(f"expected strictly increasing version: {r1.version} -> {r2.version}")
    got2 = client.get_record(ns, key)
    if got2 is None or got2.value != b"payload-v2" or got2.version != r2.version:
        raise AssertionError(f"get_record mismatch after update: {got2!r} vs {r2!r}")

    conflict_stale = False
    try:
        client.put_record(ns, key, b"stale-update", expected_version=r1.version)
    except BackendConflictError as err:
        conflict_stale = err.status == 409
    if not conflict_stale:
        raise AssertionError("expected 409 BackendConflictError on stale expected_version")

    delete_conflict = False
    try:
        client.delete_record(ns, key, expected_version=r1.version)
    except BackendConflictError as err:
        delete_conflict = err.status == 409
    if not delete_conflict:
        raise AssertionError("expected 409 BackendConflictError on stale delete_record")

    deleted = client.delete_record(ns, key, expected_version=r2.version)
    if not deleted:
        raise AssertionError("delete_record returned False for existing key")
    if client.get_record(ns, key) is not None:
        raise AssertionError("deleted key still present on get_record")

    missing_delete = client.delete_record(ns, "records/never-existed")
    return {
        "created_version": r1.version,
        "updated_version": r2.version,
        "absent_conflict_409": conflict_absent,
        "stale_conflict_409": conflict_stale,
        "delete_conflict_409": delete_conflict,
        "deleted": deleted,
        "missing_delete_returned": missing_delete,
    }


def _check_blobs_cas(client: MembusV2Backend) -> dict[str, Any]:
    ns = "live_blobs"
    payload = b"\x00\x01\xffmanaged-ai-live-cas-blob-payload"
    digest = client.put_blob(ns, payload)
    expected = sha(payload)
    if digest != expected:
        raise AssertionError(f"blob sha mismatch: {digest} != {expected}")
    if not client.has_blob(ns, digest):
        raise AssertionError("has_blob returned False for stored blob")
    if client.has_blob(ns, "0" * 64):
        raise AssertionError("has_blob returned True for missing blob")
    fetched = client.get_blob(ns, digest)
    if fetched != payload:
        raise AssertionError("get_blob payload mismatch")
    if client.get_blob(ns, "0" * 64) is not None:
        raise AssertionError("get_blob returned non-None for missing blob")
    return {"sha256": digest, "bytes": len(payload)}


def _check_search_visibility_isolation(client: MembusV2Backend) -> dict[str, Any]:
    ns = "live_search"
    pub_rec = client.put_record(
        ns,
        "doc-pub",
        b"UniquePublicCanary991 detailed research finding body",
        expected_version=0,
        visibility="Public",
        metadata={"kind": "finding", "title": "AlphaPublicHeadline991", "tags": ["live"]},
    )
    meta_rec = client.put_record(
        ns,
        "doc-meta",
        b"SecretMetadataOnlyBody772 never indexed anywhere",
        expected_version=0,
        visibility="MetadataOnly",
        metadata={"kind": "finding", "title": "BetaMetaHeadline772", "tags": ["meta"]},
    )
    client.put_record(
        ns,
        "doc-sealed",
        b"UltraSealedD12Payload883 strictly forbidden",
        expected_version=0,
        visibility="Sealed",
        metadata={"kind": "finding", "title": "GammaSealedHeadline883", "tags": ["sealed"]},
    )

    pub_bm25 = [h.doc_id for h in client.search("UniquePublicCanary991", namespace=ns, mode="Bm25")]
    pub_lit = [
        h.doc_id
        for h in client.search("UniquePublicCanary991", namespace=ns, mode="LiteralSubstring")
    ]
    if pub_bm25 != ["doc-pub"] or pub_lit != ["doc-pub"]:
        raise AssertionError(f"Public doc not found: bm25={pub_bm25}, lit={pub_lit}")

    meta_title = [h.doc_id for h in client.search("BetaMetaHeadline772", namespace=ns, mode="Bm25")]
    meta_body_bm25 = client.search("SecretMetadataOnlyBody772", namespace=ns, mode="Bm25")
    meta_body_lit = client.search(
        "SecretMetadataOnlyBody772", namespace=ns, mode="LiteralSubstring"
    )
    if meta_title != ["doc-meta"] or meta_body_bm25 or meta_body_lit:
        raise AssertionError(
            f"MetadataOnly isolation failed: title={meta_title}, "
            f"body_bm25={meta_body_bm25}, body_lit={meta_body_lit}"
        )

    sealed_body = client.search("UltraSealedD12Payload883", namespace=ns, mode="LiteralSubstring")
    sealed_title = client.search("GammaSealedHeadline883", namespace=ns, mode="Bm25")
    if sealed_body or sealed_title:
        raise AssertionError(f"Sealed leak detected: body={sealed_body}, title={sealed_title}")

    # Downgrade Public -> Sealed MUST tombstone prior Tantivy doc!
    client.put_record(
        ns,
        "doc-pub",
        b"NowSealedConfidentialBody991",
        expected_version=pub_rec.version,
        visibility="Sealed",
        metadata={"kind": "finding", "title": "AlphaPublicHeadline991"},
    )
    after_downgrade_body = client.search(
        "UniquePublicCanary991", namespace=ns, mode="LiteralSubstring"
    )
    after_downgrade_title = client.search("AlphaPublicHeadline991", namespace=ns, mode="Bm25")
    if after_downgrade_body or after_downgrade_title:
        raise AssertionError(
            f"Public->Sealed downgrade failed to tombstone: "
            f"body={after_downgrade_body}, title={after_downgrade_title}"
        )

    # delete_record MUST tombstone MetadataOnly headline too!
    client.delete_record(ns, "doc-meta", expected_version=meta_rec.version)
    after_delete_title = client.search("BetaMetaHeadline772", namespace=ns, mode="Bm25")
    if after_delete_title:
        raise AssertionError(f"delete_record failed to tombstone: {after_delete_title}")
    if client.search("AlphaPublicHeadline991", namespace=ns, limit=0) != []:
        raise AssertionError("search(limit=0) should return empty list")

    return {
        "public_found": True,
        "metadata_only_body_hidden": True,
        "sealed_hidden": True,
        "public_to_sealed_tombstoned": True,
        "delete_tombstoned": True,
    }


def _check_fenced_leases(client: MembusV2Backend) -> dict[str, Any]:
    ns, res = "live_leases", "store-owner"
    g1 = client.acquire_lease(ns, res, "actor-a", ttl_ms=15_000)
    if g1.generation != 1 or not client.validate_lease(g1):
        raise AssertionError(f"unexpected lease grant 1: {g1!r}")

    contended_code = ""
    try:
        client.acquire_lease(ns, res, "actor-b", ttl_ms=15_000)
    except BackendContendedError as err:
        contended_code = f"{err.status}:{err.code}"
    if contended_code != "409:lease_held":
        raise AssertionError(f"expected 409:lease_held contention, got {contended_code!r}")

    renewed = client.renew_lease(g1, ttl_ms=30_000)
    if renewed.generation != 1 or renewed.expires_at_ms < g1.expires_at_ms:
        raise AssertionError(f"renew_lease mismatch: {renewed!r}")
    if not client.validate_lease(renewed):
        raise AssertionError("renewed lease failed validate_lease")

    released = client.release_lease(renewed)
    if not released or client.validate_lease(renewed):
        raise AssertionError("release_lease did not invalidate grant")
    if client.release_lease(renewed):
        raise AssertionError("second release_lease on already-released grant should return False")

    g2 = client.acquire_lease(ns, res, "actor-b", ttl_ms=15_000)
    if g2.generation != 2:
        raise AssertionError(f"expected monotonic generation 2, got {g2.generation}")
    client.release_lease(g2)
    return {
        "gen1": g1.generation,
        "contended": contended_code,
        "gen2": g2.generation,
        "idempotent_release_false": True,
    }


def _check_sse_signals(client: MembusV2Backend) -> dict[str, Any]:
    ns = "live_signals"
    start_seq = int(client.health()["latest_seq"])
    received: queue.Queue[SignalEvent | Exception] = queue.Queue()

    def _reader() -> None:
        stream = client.stream_signals(ns, prefix="evt-", since_seq=start_seq, timeout=6.0)
        try:
            for ev in stream:
                received.put(ev)
                if ev.kind == "delete":
                    break
        except Exception as exc:
            received.put(exc)
        finally:
            close_fn = getattr(stream, "close", None)
            if callable(close_fn):
                close_fn()

    thread = threading.Thread(target=_reader, name="live-sse-reader", daemon=True)
    thread.start()
    time.sleep(0.15)
    client.put_record(ns, "evt-1", b"signal-body")
    client.delete_record(ns, "evt-1")

    events: list[SignalEvent] = []
    for _ in range(2):
        item = received.get(timeout=5.0)
        if isinstance(item, Exception):
            raise item
        events.append(item)
    thread.join(timeout=2.0)

    if [e.key for e in events] != ["evt-1", "evt-1"]:
        raise AssertionError(f"unexpected SSE keys: {events!r}")
    if [e.kind for e in events] != ["put", "delete"]:
        raise AssertionError(f"unexpected SSE kinds: {events!r}")
    if not (start_seq < events[0].seq < events[1].seq):
        raise AssertionError(f"non-monotonic SSE seqs: {start_seq}, {events!r}")
    return {
        "events": [{"seq": e.seq, "key": e.key, "kind": e.kind} for e in events],
    }


def _check_coordination_facade(client: MembusV2Backend, scratch_dir: Path) -> dict[str, Any]:
    store_root = scratch_dir / "coord-store"
    System.initialize(store_root, {"contribution": "Live Membus v2 coordination check."})
    system = System(store_root)
    facade = CoordinationFacade(system, backend=client)

    lease1 = client.acquire_lease("live_coord", "store", "coord-a", ttl_ms=30_000)
    rec1 = {
        "id": "live-note-1",
        "kind": "note",
        "basis": [],
        "content": {"summary": "First live membus fenced consolidation."},
    }
    receipt1 = facade.commit_consolidation_delta(lease1, {"live-note-1": 0}, [rec1])
    if receipt1.committed_count != 1 or receipt1.lease_generation != 1:
        raise AssertionError(f"unexpected coord receipt 1: {receipt1!r}")

    client.release_lease(lease1)
    lease2 = client.acquire_lease("live_coord", "store", "coord-b", ttl_ms=30_000)
    rec2 = dict(rec1, content={"summary": "Second live membus fenced consolidation."})
    receipt2 = facade.commit_consolidation_delta(lease2, {"live-note-1": 1}, [rec2])
    if receipt2.lease_generation != 2:
        raise AssertionError(f"unexpected coord receipt 2: {receipt2!r}")

    stale_rejected = False
    try:
        facade.commit_consolidation_delta(lease1, {"live-note-1": 2}, [rec2])
    except BackendContendedError:
        stale_rejected = True
    if not stale_rejected:
        raise AssertionError("stale gen-1 lease was not rejected by CoordinationFacade")
    client.release_lease(lease2)

    start_seq = int(client.health()["latest_seq"])
    client.put_record("live_coord_sig", "delta:1", b"committed")
    sig_stream = facade.subscribe_signals(
        "live_coord_sig", prefix="delta:", since_seq=start_seq, timeout=5.0
    )
    try:
        first_sig = next(sig_stream)
    finally:
        close_fn = getattr(sig_stream, "close", None)
        if callable(close_fn):
            close_fn()
    if first_sig.key != "delta:1":
        raise AssertionError(f"unexpected facade signal key: {first_sig!r}")
    return {
        "gen1_commit_rev": system.record_store.show("live-note-1")["revision"],
        "stale_gen1_rejected": stale_rejected,
        "signal_key": first_sig.key,
    }


def _check_multi_route_and_migration(client: MembusV2Backend, scratch_dir: Path) -> dict[str, Any]:
    ns = "live_migration"
    schema_v1 = b'{"format":"managed-ai-test-schema/1","version":1}'
    schema_v2 = b'{"format":"managed-ai-test-schema/2","version":2}'
    schema_v3 = b'{"format":"managed-ai-test-schema/3","version":3}'
    if client.get_collection(ns) is not None:
        raise AssertionError(f"unexpected preexisting collection {ns}")
    reg = client.register_collection(ns, schema_version=1, schema_bytes=schema_v1)
    if reg["schema_version"] != 1 or reg["migration_state"] != "Active":
        raise AssertionError(f"unexpected collection registration: {reg!r}")

    route_a = EmbeddedFileBackend(scratch_dir / "route-a")
    with MultiRouteBackend(route_a, client, mode="single", route_b_timeout_ms=2000) as router:
        controller = SchemaMigrationController(router=router, catalog_backend=client)

        # 1. Expand -> Dual-Write -> Verify -> Commit (v1 -> v2)
        st_exp = controller.begin_expand(
            ns,
            from_version=1,
            target_version=2,
            target_sha256=sha(schema_v2),
            target_schema_bytes=schema_v2,
        )
        cat_exp = client.get_collection(ns)
        if (
            st_exp.phase != "expanding"
            or cat_exp is None
            or cat_exp["compatibility_mode"] != "ExpandDual"
        ):
            raise AssertionError(f"expand phase mismatch: {st_exp!r} / {cat_exp!r}")

        controller.enable_dual_write(routing_mode="dual-sync")
        router.put_record(ns, "mig-rec-1", b"dual-sync-payload-1", expected_version=0)
        blob_sha = router.put_blob(ns, b"dual-sync-blob-1")
        parity = router.parity_summary()
        if parity["counts"]["matched_ops"] != 2 or parity["counts"]["mismatched_ops"] != 0:
            raise AssertionError(f"dual-sync parity mismatch: {parity!r}")
        if not client.has_blob(ns, blob_sha):
            raise AssertionError("dual-sync blob missing on live membus route_b")

        live_seq = int(client.health()["latest_seq"])
        controller.record_verification(verified_through=live_seq, mismatch_count=0, anomalies=0)
        cat_adv = client.get_collection(ns)
        if (
            cat_adv is None
            or cat_adv["migration_state"]["Migrating"]["verified_through"] != live_seq
        ):
            raise AssertionError(f"advance watermark mismatch: {cat_adv!r}")

        st_commit = controller.commit_cutover(cutover_seq=live_seq)
        cat_commit = client.get_collection(ns)
        if (
            st_commit.phase != "committed"
            or router.mode != "canary-cutover"
            or cat_commit is None
            or cat_commit["schema_version"] != 2
            or cat_commit["migration_state"] != "Active"
            or cat_commit["compatibility_mode"] != "Strict"
        ):
            raise AssertionError(f"commit cutover mismatch: {st_commit!r} / {cat_commit!r}")

        router.put_record(ns, "mig-canary-only", b"post-cutover-route-b-only", expected_version=0)
        if route_a.get_record(ns, "mig-canary-only") is not None:
            raise AssertionError("canary-cutover leaked write to route_a")
        if client.get_record(ns, "mig-canary-only") is None:
            raise AssertionError("canary-cutover write missing on live membus route_b")

        # 2. Expand -> Shadow-Async -> Abort (v2 -> v3)
        controller.begin_expand(
            ns,
            from_version=2,
            target_version=3,
            target_sha256=sha(schema_v3),
            target_schema_bytes=schema_v3,
        )
        controller.enable_dual_write(routing_mode="shadow-async")
        router.put_record(ns, "mig-shadow-1", b"shadow-async-payload")
        if not router.flush_shadow(timeout=5.0):
            raise AssertionError("shadow-async flush timed out")

        st_abort = controller.abort_rollback("live harness simulated canary drift")
        cat_abort = client.get_collection(ns)
        mode_after_abort: str = router.mode
        if (
            st_abort.phase != "aborted"
            or mode_after_abort != "single"
            or cat_abort is None
            or cat_abort["schema_version"] != 2
            or cat_abort["migration_state"] != "Active"
        ):
            raise AssertionError(f"abort rollback mismatch: {st_abort!r} / {cat_abort!r}")

        final_parity = router.parity_summary()

    return {
        "committed_version": cat_commit["schema_version"],
        "aborted_reverted_version": cat_abort["schema_version"],
        "parity_counts": final_parity["counts"],
    }


def run_live_verification(base_url: str, scratch_dir: Path) -> dict[str, Any]:
    """Run the full live Membus v2 integration suite against an isolated running daemon."""
    client = MembusV2Backend(base_url, timeout=10.0)
    t0 = time.perf_counter()
    checks: dict[str, Any] = {
        "health": _check_health(client),
        "kv_cas_and_delete": _check_kv_cas_and_delete(client),
        "blobs_cas": _check_blobs_cas(client),
        "search_visibility_isolation": _check_search_visibility_isolation(client),
        "fenced_leases": _check_fenced_leases(client),
        "sse_signals": _check_sse_signals(client),
        "coordination_facade": _check_coordination_facade(client, scratch_dir),
        "multi_route_and_migration": _check_multi_route_and_migration(client, scratch_dir),
    }
    return {
        "format": REPORT_FORMAT,
        "ok": True,
        "base_url": base_url,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        "checks": checks,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run live end-to-end integration tests against an ephemeral membus v2.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--membus-bin",
        type=Path,
        default=None,
        help="Explicit path to a membus v2 executable (defaults to MEMBUS_BIN / PATH / sibling).",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    binary = find_membus_binary(args.membus_bin)
    if binary is None:
        err = {
            "format": REPORT_FORMAT,
            "ok": False,
            "error": "membus_v2_binary_not_found",
            "message": "No compatible membus v2 executable found (use --membus-bin or MEMBUS_BIN).",
        }
        print(json.dumps(err, indent=2 if args.pretty else None), file=sys.stderr)
        return 2

    try:
        with (
            EphemeralMembusDaemon(binary) as daemon,
            tempfile.TemporaryDirectory(prefix="membus-live-scratch-") as scratch,
        ):
            report = run_live_verification(daemon.base_url, Path(scratch))
            report["membus_bin"] = str(binary)
    except Exception as exc:
        err = {
            "format": REPORT_FORMAT,
            "ok": False,
            "membus_bin": str(binary),
            "error": type(exc).__name__,
            "message": str(exc),
        }
        print(json.dumps(err, indent=2 if args.pretty else None), file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
