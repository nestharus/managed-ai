"""Unit tests for the domain-neutral managed_ai_information.store.ingest API."""

import hashlib
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from managed_ai_information.core.storage import exclusive  # noqa: E402
from managed_ai_information.store.ingest import (  # noqa: E402
    DIGEST_REL_DIR,
    apply_desired_records,
    canonical_bytes,
    compute_digest_basis,
    dump_json,
    file_digest_entry,
    git_head_sha,
    load_existing_records,
    record_matches_desired,
    slugify_id,
)
from managed_ai_information.store.system import System  # noqa: E402


class StoreIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="managed-ai-w20-ingest-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _init_store(self, name: str = "store") -> Path:
        root = self.tmp / name
        System.initialize(
            root,
            {"contribution": "Exercise domain-neutral store ingestion helpers."},
            instance_id=name,
        )
        return root

    def _make_dir_link(self, link_path: Path, target_dir: Path) -> None:
        if os.name == "nt":
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.addCleanup(
                lambda p=link_path: subprocess.run(
                    ["cmd", "/c", "rmdir", str(p)], capture_output=True, check=False
                )
            )
        else:
            os.symlink(target_dir, link_path)

    def test_slugify_canonical_dump_json_and_git_head_sha(self) -> None:
        self.assertEqual(slugify_id("mod", "Core/Storage.py"), "mod-core-storage-py")
        self.assertEqual(slugify_id("item", "!!!"), "item-item")
        long_id = slugify_id("mod", "a" * 200)
        self.assertLessEqual(len(long_id), 120)
        self.assertEqual(long_id, slugify_id("mod", "a" * 200))

        self.assertEqual(canonical_bytes({"b": 1, "a": 2}), b'{\n  "a": 2,\n  "b": 1\n}\n')
        self.assertEqual(dump_json({"b": 1, "a": 2}), '{\n  "a": 2,\n  "b": 1\n}')
        with self.assertRaises(ValueError):
            canonical_bytes({"bad": float("nan")})
        with self.assertRaises(ValueError):
            dump_json({"bad": float("inf")})

        # Non-git directory returns 'unknown'; real git repo returns 40-hex HEAD
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertEqual(git_head_sha(plain), "unknown")

        git_bin = shutil.which("git")
        if git_bin is not None:
            repo = self.tmp / "git-repo"
            repo.mkdir()
            (repo / "hello.txt").write_text("hi\n", encoding="utf-8")
            for args in (
                [git_bin, "-C", str(repo), "init"],
                [git_bin, "-C", str(repo), "config", "user.email", "test@example.com"],
                [git_bin, "-C", str(repo), "config", "user.name", "Test"],
                [git_bin, "-C", str(repo), "add", "hello.txt"],
                [git_bin, "-C", str(repo), "commit", "-m", "init"],
            ):
                p = subprocess.run(args, capture_output=True, text=True, check=False)
                self.assertEqual(p.returncode, 0, p.stderr)
            sha_val = git_head_sha(repo)
            self.assertEqual(len(sha_val), 40)

    def test_file_digest_entry_and_symlink_traversal_guards(self) -> None:
        src_root = self.tmp / "src-root"
        (src_root / "pkg").mkdir(parents=True)
        sample = src_root / "pkg/mod.py"
        sample.write_bytes(b"VALUE = 42\n")

        verified: set[Path] = set()
        entry = file_digest_entry(src_root, "pkg/mod.py", verified_dirs=verified)
        self.assertEqual(entry["path"], "pkg/mod.py")
        self.assertEqual(entry["bytes"], len(b"VALUE = 42\n"))
        self.assertEqual(entry["sha256"], hashlib.sha256(b"VALUE = 42\n").hexdigest())

        # Path traversal and non-ordinary directories fail closed
        for bad_rel in ("../outside.py", "pkg/../../outside.py", "/abs/path.py"):
            with self.assertRaises(ValueError):
                file_digest_entry(src_root, bad_rel)

        outside = self.tmp / "outside-dir"
        outside.mkdir()
        (outside / "secret.py").write_bytes(b"SECRET = 1\n")
        linked = src_root / "linked-pkg"
        self._make_dir_link(linked, outside)
        with self.assertRaises(ValueError):
            file_digest_entry(src_root, "linked-pkg/secret.py")

        # Directory target at file position fails closed
        with self.assertRaises(ValueError):
            file_digest_entry(src_root, "pkg")

    def test_apply_desired_records_dry_run_idempotency_basis_and_owner_lock(self) -> None:
        store_root = self._init_store("ingest-e2e")
        src_root = self.tmp / "sample-src"
        (src_root / "alpha").mkdir(parents=True)
        f_path = src_root / "alpha/one.txt"
        f_path.write_bytes(b"version-1\n")

        f_entry = file_digest_entry(src_root, "alpha/one.txt")
        rel_basis, basis_bytes, basis_sha = compute_digest_basis(
            "rec-alpha", source_repo="sample-src", files=[f_entry]
        )
        self.assertEqual(rel_basis, f"{DIGEST_REL_DIR}/rec-alpha.json")

        desired = [
            {
                "id": "rec-alpha",
                "kind": "note",
                "content": {"summary": "First edition"},
                "relations": [{"relation": "serves", "target": "goal"}],
                "basis": [rel_basis],
                "active": True,
            },
            {
                "id": "rec-beta",
                "kind": "note",
                "content": {"summary": "Second record"},
                "relations": [{"relation": "depends-on", "target": "rec-alpha"}],
                "basis": [],
                "active": True,
            },
        ]
        basis_map = {"rec-alpha": {rel_basis: basis_sha}, "rec-beta": {}}
        pending = {"rec-alpha": basis_bytes}

        with System(store_root) as sys_obj:
            # 1. Dry-run is strictly read-only (zero digest files written, zero records written)
            plan = apply_desired_records(
                sys_obj,
                desired,
                basis_map,
                batch_prefix="ingest-test",
                pending_digests=pending,
                dry_run=True,
            )
            self.assertFalse(plan["applied"])
            self.assertTrue(plan["dry_run"])
            self.assertEqual(plan["added"], ["rec-alpha", "rec-beta"])
            self.assertEqual(plan["written_count"], 0)
            self.assertFalse((sys_obj.work / rel_basis).exists())
            self.assertNotIn("rec-alpha", load_existing_records(sys_obj))

            # 2. Apply writes digest pin + records under exclusive owner.lock with 0 anomalies
            res1 = apply_desired_records(
                sys_obj,
                desired,
                basis_map,
                batch_prefix="ingest-test",
                pending_digests=pending,
                dry_run=False,
            )
            self.assertTrue(res1["applied"])
            self.assertEqual(res1["written_count"], 2)
            self.assertEqual(res1["lint_anomalies"], 0)
            self.assertTrue((sys_obj.work / rel_basis).is_file())
            existing = load_existing_records(sys_obj)
            self.assertTrue(
                record_matches_desired(existing["rec-alpha"], desired[0], {rel_basis: basis_sha})
            )

            # 3. Re-running with identical desired state is 100% idempotent (0 writes)
            res2 = apply_desired_records(
                sys_obj,
                desired,
                basis_map,
                batch_prefix="ingest-test",
                pending_digests=pending,
                dry_run=False,
            )
            self.assertFalse(res2["applied"])
            self.assertEqual(res2["written_count"], 0)
            self.assertEqual(res2["unchanged_count"], 2)

            # C78A-O2: JSON type drift (1 vs True) is detected; non-bool active fails closed
            existing_int = {
                "kind": "note",
                "active": True,
                "content": {"flag": 1},
                "relations": [],
                "basis": [],
            }
            desired_bool = {
                "kind": "note",
                "active": True,
                "content": {"flag": True},
                "relations": [],
                "basis": [],
            }
            self.assertFalse(record_matches_desired(existing_int, desired_bool, {}))
            for bad_active in (1, "yes", None):
                with self.assertRaises(ValueError):
                    record_matches_desired(
                        existing_int,
                        {"kind": "note", "active": bad_active, "content": {"flag": 1}},
                        {},
                    )
                with self.assertRaises(ValueError):
                    apply_desired_records(
                        sys_obj,
                        [
                            {
                                "id": "rec-bad-active",
                                "kind": "note",
                                "active": bad_active,
                                "content": {"summary": "bad"},
                                "relations": [],
                                "basis": [],
                            }
                        ],
                        {"rec-bad-active": {}},
                        batch_prefix="ingest-bad-active",
                        dry_run=True,
                    )

            # 4. Updating basis file updates ONLY rec-alpha (rec-beta stays unchanged)
            f_path.write_bytes(b"version-2\n")
            f_entry2 = file_digest_entry(src_root, "alpha/one.txt")
            _, basis_bytes2, basis_sha2 = compute_digest_basis(
                "rec-alpha", source_repo="sample-src", files=[f_entry2]
            )
            res3 = apply_desired_records(
                sys_obj,
                desired,
                {"rec-alpha": {rel_basis: basis_sha2}, "rec-beta": {}},
                batch_prefix="ingest-test",
                pending_digests={"rec-alpha": basis_bytes2},
                dry_run=False,
            )
            self.assertTrue(res3["applied"])
            self.assertEqual(res3["updated"], ["rec-alpha"])
            self.assertEqual(res3["unchanged_count"], 1)
            self.assertEqual(sys_obj.show("rec-alpha")["revision"], 2)

            # C78A-O1: size-mismatch digest pin overwrites cleanly without reading stale body
            dest_pin = sys_obj.work / rel_basis
            dest_pin.write_bytes(b"{" + b" " * 512 + b"}")
            res_pin = apply_desired_records(
                sys_obj,
                desired,
                {"rec-alpha": {rel_basis: basis_sha}, "rec-beta": {}},
                batch_prefix="ingest-pin-reset",
                pending_digests={"rec-alpha": basis_bytes},
                dry_run=False,
            )
            self.assertTrue(res_pin["applied"])
            self.assertEqual(dest_pin.read_bytes(), basis_bytes)

            # 5. Dangling relation is detected in lint_anomalies
            dangling = [
                {
                    "id": "rec-dangling",
                    "kind": "note",
                    "content": {"summary": "Points at missing ID"},
                    "relations": [{"relation": "depends-on", "target": "missing-target-id"}],
                    "basis": [],
                    "active": True,
                }
            ]
            res_bad = apply_desired_records(
                sys_obj, dangling, {"rec-dangling": {}}, batch_prefix="ingest-bad", dry_run=False
            )
            self.assertEqual(res_bad["lint_anomalies"], 1)

            # 6. Verify owner.lock contention refuses concurrent apply when lock is held
            lock_path = sys_obj.ex.root / "owner.lock"
            with exclusive(lock_path):
                # Re-acquiring exclusive lock in a subprocess must fail (non-blocking)
                probe = subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-c",
                        (
                            "import sys; sys.path.insert(0, sys.argv[1]); "
                            "from pathlib import Path; "
                            "from managed_ai_information.core.storage import exclusive; "
                            "exclusive(Path(sys.argv[2])).__enter__()"
                        ),
                        str(_REPO / "src"),
                        str(lock_path),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(probe.returncode, 0)

    def test_write_digest_basis_rejects_symlink_digest_dir(self) -> None:
        store_root = self._init_store("ingest-symlink")
        outside = self.tmp / "outside-digests"
        outside.mkdir()
        with System(store_root) as sys_obj:
            sources_dir = sys_obj.work / "sources"
            sources_dir.mkdir(exist_ok=True)
            self._make_dir_link(sources_dir / "repo-digests", outside)
            rel_basis, basis_bytes, basis_sha = compute_digest_basis(
                "rec-evil", source_repo="x", files=[]
            )
            with self.assertRaises(ValueError):
                apply_desired_records(
                    sys_obj,
                    [
                        {
                            "id": "rec-evil",
                            "kind": "note",
                            "content": {"summary": "Symlink guard test"},
                            "relations": [],
                            "basis": [rel_basis],
                            "active": True,
                        }
                    ],
                    {"rec-evil": {rel_basis: basis_sha}},
                    batch_prefix="ingest-evil",
                    pending_digests={"rec-evil": basis_bytes},
                    dry_run=False,
                )


if __name__ == "__main__":
    unittest.main()
