"""Regression tests for Cycle 72 production bug fixes (C72-1 through C72-7)."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from managed_ai_information.companions.representation import representation  # noqa: E402
from managed_ai_information.core.storage import sha  # noqa: E402
from managed_ai_information.deployment import selection  # noqa: E402
from managed_ai_information.host.continuing_work import scope_receive  # noqa: E402
from managed_ai_information.host.material_retention import (  # noqa: E402
    config as mat_config,
)
from managed_ai_information.host.material_retention import (  # noqa: E402
    snapshot_ops,
    store_core,
)
from managed_ai_information.store.records import records as records_mod  # noqa: E402
from managed_ai_information.store.system import System  # noqa: E402


def _build_forged_zip(claimed_size: int) -> bytes:
    """Build a deterministic ZIP whose compressed stream exceeds 4 KiB and lies about size."""
    prefix = b"".join(hashlib.sha256(f"blk-{i}".encode()).digest() for i in range(256))
    payload = prefix + (b"\x00" * 65536)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("bomb.bin", payload)
    raw = bytearray(buf.getvalue())
    lfh = raw.find(b"PK\x03\x04")
    cdfh = raw.find(b"PK\x01\x02")
    assert lfh >= 0 and cdfh >= 0
    struct.pack_into("<I", raw, lfh + 22, claimed_size)
    struct.pack_into("<I", raw, cdfh + 24, claimed_size)
    return bytes(raw)


class C72MaterialRetentionTests(unittest.TestCase):
    """C72-1 (bounded deflate/ZIP decompression) and C72-7 (bounded ordinary spec file)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_c72_1_managed_deflate_bomb_bounded(self) -> None:
        limits = mat_config.RetentionLimits(max_member_bytes=1024)
        store = store_core.Store(self.root / "mat-store", create=True, limits=limits)
        payload = b"\x00" * 65536
        payload_sha = sha(payload)
        comp = zlib.compressobj(level=9, wbits=-15)
        deflated = comp.compress(payload) + comp.flush()
        self.assertLess(len(deflated), 512)
        hint = f"objects/{payload_sha[:2]}/{payload_sha[2:]}.deflate"
        with self.assertRaisesRegex(ValueError, "Managed encoded body size limit"):
            store.ingest(deflated, hint)

    def test_c72_1_forged_zip_member_size_header_bounded(self) -> None:
        limits = mat_config.RetentionLimits(max_member_bytes=1024)
        forged = _build_forged_zip(claimed_size=10)

        # Real forged ZIP never expands into a 65 KiB decoded object (either raises
        # ZIP member size limit or falls back to raw encoded bytes on CRC mismatch).
        store = store_core.Store(self.root / "zip-store", create=True, limits=limits)
        try:
            key = store.ingest(forged, "forged.zip")
        except ValueError as exc:
            self.assertIn("ZIP member size limit", str(exc))
        else:
            self.assertEqual(store.objects[key]["kind"], "zip-recipe")
            recipe = store.recipe(key)
            self.assertEqual(recipe["members"][0]["semantic_content"], "unavailable")
            self.assertIsNone(recipe["members"][0]["content"])

        # Prove _ingest_zip_member strictly bounds zf.read(max_member_bytes + 1)
        # even when a stream ignores info.file_size and yields > max_member_bytes.
        store2 = store_core.Store(self.root / "zip-store-2", create=True, limits=limits)
        read_args: list[int | None] = []

        class TrackingBombStream:
            def __enter__(self) -> TrackingBombStream:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                read_args.append(size)
                return b"\x00" * (size if size >= 0 else 65536)

        with (
            patch.object(zipfile.ZipFile, "open", return_value=TrackingBombStream()),
            self.assertRaisesRegex(ValueError, "ZIP member size limit"),
        ):
            store2.ingest(forged, "forged.zip")
        self.assertEqual(read_args, [1025])

    def test_c72_7_snapshot_rejects_non_regular_and_oversized_spec_file(self) -> None:
        limits = mat_config.RetentionLimits(max_member_bytes=128)
        store = store_core.Store(self.root / "snap-store", create=True, limits=limits)
        spec_dir = self.root / "spec-as-dir"
        spec_dir.mkdir()
        with self.assertRaisesRegex(ValueError, "existing ordinary file"):
            snapshot_ops.snapshot(store, spec_dir)

        big_spec = self.root / "big-spec.json"
        big_spec.write_text('{"pad": "' + ("a" * 512) + '"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Selection spec exceeds"):
            snapshot_ops.snapshot(store, big_spec)


class C72SelectionDomainTests(unittest.TestCase):
    """C72-2: _check_required_domain path validation, ordinary files, and sibling pins."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / "workspaces" / "demo" / "store"
        self.runtime.mkdir(parents=True)

    def _write_system(self, domains: list[dict[str, str]]) -> None:
        (self.runtime / "system.json").write_text(
            json.dumps({"format": "test", "domains": domains}), encoding="utf-8"
        )

    def test_c72_2_accepts_sha_pinned_sibling_domain_and_rejects_unsafe_paths(self) -> None:
        sibling = self.root / "sibling-domain"
        sibling.mkdir()
        manifest = sibling / "domain.json"
        raw = json.dumps({"id": "research", "version": 1}).encode("utf-8")
        manifest.write_bytes(raw)
        digest = sha(raw)

        # Valid SHA-256-pinned sibling relative path (matching live workspaces/*) succeeds.
        self._write_system([{"path": "../../../sibling-domain/domain.json", "sha256": digest}])
        selection._check_required_domain(self.runtime, "research")

        # Reject absolute, drive-relative, backslash, and control-character paths.
        for bad_path in (
            "/etc/passwd",
            "C:/Windows/win.ini",
            "C:domain.json",
            "..\\sibling-domain\\domain.json",
            "domain\x00.json",
            "   ",
        ):
            self._write_system([{"path": bad_path, "sha256": digest}])
            with self.assertRaisesRegex(ValueError, "invalid domain binding path"):
                selection._check_required_domain(self.runtime, "research")

    def test_c72_2_rejects_non_ordinary_and_oversized_domain_manifest(self) -> None:
        dom = self.runtime / "domain.json"
        raw = json.dumps({"id": "research", "pad": "x" * 200}).encode("utf-8")
        dom.write_bytes(raw)
        self._write_system([{"path": "domain.json", "sha256": sha(raw)}])

        with (
            patch.object(selection, "is_ordinary_file_stat", return_value=False),
            self.assertRaisesRegex(ValueError, "ordinary selected domain required"),
        ):
            selection._check_required_domain(self.runtime, "research")

        with (
            patch.object(selection, "MAX_BYTES", 64),
            self.assertRaisesRegex(ValueError, "exceeds"),
        ):
            selection._check_required_domain(self.runtime, "research")


class C72RepresentationTests(unittest.TestCase):
    """C72-3 & C72-4: bounded ordinary inputs, broken-symlink guard, and rollback on failure."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_c72_3_read_json_rejects_directory_and_oversized_input(self) -> None:
        as_dir = self.root / "dir.json"
        as_dir.mkdir()
        with self.assertRaisesRegex(ValueError, "existing ordinary file"):
            representation.read_json(as_dir)

        big = self.root / "big.json"
        big.write_text('{"pad": "' + ("z" * 256) + '"}', encoding="utf-8")
        with (
            patch.object(representation, "_MAX_INPUT_BYTES", 64),
            self.assertRaisesRegex(ValueError, "exceeds"),
        ):
            representation.read_json(big)

    def _make_receive_fixture(
        self, *, create_native_return_before_error: bool
    ) -> tuple[
        representation.RepresentationOperations, Path, Path, dict, list[str], dict[str, int]
    ]:
        store = self.root / (
            "store-" + ("with-ret" if create_native_return_before_error else "no-ret")
        )
        work = store / "work"
        ex_root = store / "exchange"
        op_dir = work / "representation-operations" / "turn-1"
        op_dir.mkdir(parents=True)
        ex_root.mkdir(parents=True)

        spec = {
            "format": representation.OPERATION,
            "turn": "turn-1",
            "mode": "develop",
            "task": "test",
            "consumer": "test",
            "representations": [],
            "sources": [],
            "targets": ["out.json"],
            "applications": [
                {
                    "id": "app-1",
                    "sources": [],
                    "representations": [],
                    "operation": "op",
                    "prediction": "pred",
                }
            ],
            "interactions": {"account": "ok", "unknowns": []},
        }
        op_raw = representation.encoded(spec)
        op_sha = representation.digest(op_raw)
        before_raw = b'{"v": 1}\n'
        before_sha = representation.digest(before_raw)
        objects = {op_sha: op_raw, before_sha: before_raw}
        request = {
            "materials": [
                {"path": "representation-operations/turn-1/operation.json", "sha256": op_sha}
            ],
            "targets": [{"path": "out.json", "sha256": before_sha}],
        }
        attempts = {"n": 0}

        class FakeExchange:
            def __init__(self) -> None:
                self.root = ex_root
                self.workspace = work

            def path(self, rel: str) -> Path:
                return work / rel

            def load(self, turn: str) -> tuple[str, dict]:
                return turn, request

            def get(self, key: str) -> bytes:
                return objects[key]

        class FakeSystem:
            def __init__(self) -> None:
                self.root = store
                self.ex = FakeExchange()

            def receive(
                self, turn: str, reply_path: Path, producer: str, proposals: list[str]
            ) -> dict:
                attempts["n"] += 1
                if attempts["n"] == 1:
                    if create_native_return_before_error:
                        tdir = ex_root / "turns" / turn
                        tdir.mkdir(parents=True, exist_ok=True)
                        (tdir / "return.json").write_text("{}", encoding="utf-8")
                    raise RuntimeError("transient receive error")
                return {"turn": turn, "reply": str(reply_path)}

        ops = representation.RepresentationOperations(FakeSystem())
        prop_file = self.root / "candidate.json"
        prop_file.write_text('{"v": 2}\n', encoding="utf-8")
        reply_file = self.root / "reply.txt"
        reply_file.write_text("done\n", encoding="utf-8")
        witness = {
            "format": representation.WITNESS,
            "operation_sha256": op_sha,
            "representations": [],
            "applications": [
                {"id": "app-1", "account": "applied", "outputs": ["out.json"], "remaining": []}
            ],
        }
        return (
            ops,
            op_dir / "bound-return.json",
            reply_file,
            witness,
            [f"out.json={prop_file}"],
            attempts,
        )

    def test_c72_4_receive_rolls_back_bound_return_before_native_commit_and_retries(self) -> None:
        ops, bound_path, reply_file, witness, proposals, attempts = self._make_receive_fixture(
            create_native_return_before_error=False
        )
        with self.assertRaisesRegex(RuntimeError, "transient receive error"):
            ops.receive("turn-1", reply_file, witness, "agent", proposals)
        self.assertFalse(bound_path.exists())

        res = ops.receive("turn-1", reply_file, witness, "agent", proposals)
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(res["turn"], "turn-1")
        self.assertTrue(bound_path.exists())

    def test_c72_4_receive_preserves_bound_return_if_native_return_exists_and_guards_symlink(
        self,
    ) -> None:
        ops, bound_path, reply_file, witness, proposals, _ = self._make_receive_fixture(
            create_native_return_before_error=True
        )
        with self.assertRaisesRegex(RuntimeError, "transient receive error"):
            ops.receive("turn-1", reply_file, witness, "agent", proposals)
        self.assertTrue(bound_path.exists())

        ops2, bound_path2, reply_file2, witness2, proposals2, _ = self._make_receive_fixture(
            create_native_return_before_error=False
        )
        orig_is_reparse = representation._is_reparse_or_symlink

        def fake_reparse(p: Path) -> bool:
            if Path(p) == bound_path2:
                return True
            return orig_is_reparse(p)

        with (
            patch.object(representation, "_is_reparse_or_symlink", side_effect=fake_reparse),
            self.assertRaisesRegex(ValueError, "bound return already exists"),
        ):
            ops2.receive("turn-1", reply_file2, witness2, "agent", proposals2)


class C72ScopeReceiveTests(unittest.TestCase):
    """C72-5: ordinary assessment file check and unfollowed ancestor check before mkdir."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_c72_5_rejects_directory_assessment_and_symlink_target_parent(self) -> None:
        reply = self.root / "reply.txt"
        reply.write_text("ok", encoding="utf-8")
        prop = self.root / "out.md"
        prop.write_text("draft", encoding="utf-8")
        assess_dir = self.root / "assess-dir"
        assess_dir.mkdir()

        class FakeHost:
            def __init__(self) -> None:
                self.config = {"commission": {"id": "c1"}}

            def goal(self) -> dict[str, str]:
                return {"sha256": "abc"}

            def current(self, *_args: object, **_kwargs: object) -> None:
                return None

        scope = {
            "parent_goal": {"sha256": "abc"},
            "sources": [],
            "targets": ["docs/out.md"],
        }
        spec = {
            "account": "acc",
            "producer": "prod",
            "reply": str(reply),
            "proposals": {"out.md": str(prop)},
            "assessment": str(assess_dir),
        }
        with self.assertRaisesRegex(
            ValueError, "Assessment file must be an existing ordinary file"
        ):
            scope_receive._validate_receive_inputs(FakeHost(), "s1", scope, spec)

        work = self.root / "work"
        work.mkdir()

        class AdoptHost:
            def __init__(self) -> None:
                self.work = work

        with (
            patch.object(
                scope_receive,
                "check_unfollowed_ancestors",
                side_effect=ValueError("Target directory must not traverse symlink"),
            ),
            self.assertRaisesRegex(ValueError, "must not traverse symlink"),
        ):
            scope_receive._adopt_remaining_targets(
                AdoptHost(),
                self.root / "scope.json",
                {"id": "s1"},
                sid="s1",
                returned={"producer": "p"},
                account="acc",
                completed_pubs=[],
                completed_outputs=[],
                remaining_targets=["nested/out.md"],
            )
        self.assertFalse((work / "nested").exists())


class C72RecordStoreCaseCollisionTests(unittest.TestCase):
    """C72-6: exact-filename verification across read, _replace/put, and validate_import."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "store"
        goal = {"id": "goal", "kind": "goal", "content": {"contribution": "test case safety"}}
        System.initialize(self.root, goal)
        self.system = System(self.root)
        self.records = self.system.record_store
        self.records.put({"id": "alpha", "kind": "note", "content": {"v": 1}}, 0)

    def test_c72_6_case_mismatch_read_falls_back_and_write_import_fail_closed(self) -> None:
        hot_alpha = self.records.path("alpha")
        self.assertTrue(hot_alpha.is_file())
        # Simulate case-insensitive filesystem where "Alpha.json" resolves to "alpha.json"
        # on disk (exists() == True) while verify_exact_filename reports case mismatch (False).
        orig_path = self.records.path

        def fake_path(ident: str) -> Path:
            if ident == "Alpha":
                return hot_alpha
            return orig_path(ident)

        def fake_verify(path: Path, expected_name: str) -> bool:
            return expected_name == "alpha.json"

        with (
            patch.object(self.records, "path", side_effect=fake_path),
            patch.object(records_mod.PLATFORM, "is_case_insensitive_fs", True),
            patch.object(records_mod.PLATFORM, "verify_exact_filename", side_effect=fake_verify),
        ):
            self.assertFalse(self.records.has_record("Alpha"))
            with self.assertRaises(FileNotFoundError):
                self.records.read("Alpha")

            # Cold-archive fallback succeeds for "Alpha" even when "alpha.json" is hot.
            archived = {
                "format": self.records.format,
                "id": "Alpha",
                "kind": "note",
                "content": {"v": "cold"},
                "relations": [],
                "basis": [],
                "active": True,
                "revision": 1,
                "previous_object": None,
                "updated_at": "2026-01-01T00:00:00Z",
                "retention": {"policy": "append-history", "retained_from_revision": 1},
            }
            archived["sha256"] = hashlib.sha256(
                json.dumps(archived, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            with (
                patch.object(self.records.cold_archive, "has", side_effect=lambda x: x == "Alpha"),
                patch.object(
                    self.records.cold_archive,
                    "read",
                    return_value=(archived, b"{}"),
                ),
            ):
                self.assertTrue(self.records.has_record("Alpha"))
                rec, _ = self.records.read("Alpha")
                self.assertEqual(rec["id"], "Alpha")

            # Put and validate_import fail closed on case-only collision.
            with self.assertRaisesRegex(ValueError, "case-only record identity collision"):
                self.records.put({"id": "Alpha", "kind": "note", "content": {"v": 2}}, 0)

            with self.assertRaisesRegex(ValueError, "case-only record identity collision"):
                self.records.validate_import({"id": "Alpha", "kind": "note", "content": {"v": 2}})


if __name__ == "__main__":
    unittest.main()
