"""Working releases without companions/workbench verify cleanly; present workbench manifests remain strictly gated."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import zipfile


RELEASE = Path(__file__).resolve().parent
ENGINE = RELEASE.parent
PACKAGE = runpy.run_path(str(RELEASE / "package.py"))


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _write_json(path, value):
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _build_minimal_bundle(root):
    core = root / "engine"
    (core / "release").mkdir(parents=True)
    (core / "runtime/native").mkdir(parents=True)
    rows = [{"id": "guide", "source": "guide.md"}]
    (core / "release/current_sources.json").write_text(
        json.dumps({"edition": "working-test", "bodies": rows}), encoding="utf-8"
    )
    (core / "runtime/native/catalogue.json").write_text(
        json.dumps({"entries": rows}), encoding="utf-8"
    )
    (core / "release/navigation.json").write_text("{}", encoding="utf-8")
    (core / "guide.md").write_bytes(b"# Guide\n\nWorking release body.\n")
    subprocess.run(
        [sys.executable, "-B", str(RELEASE / "refresh_guidance.py"), "--root", str(core)],
        check=True,
        capture_output=True,
        text=True,
    )
    return core


def _write_workbench(root, files):
    workbench = root / "companions/workbench"
    workbench.mkdir(parents=True, exist_ok=True)
    manifest_files = {}
    for rel, content in files.items():
        target = workbench / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        manifest_files[rel] = {
            "source": rel,
            "identity": {"bytes": len(content), "sha256": _sha256(content)},
        }
    _write_json(
        workbench / "COMPONENTS.json",
        {"schema": "peer-workbench-acquired-components/1", "files": manifest_files},
    )
    return workbench


def _write_sample_domain(bundle):
    domain_dir = bundle / "domains/work-sample"
    note = domain_dir / "sources/guide.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note_bytes = b"# Working sample\n\nBounded domain body for working-release verification.\n"
    note.write_bytes(note_bytes)
    catalogue_raw = _write_json(
        domain_dir / "catalogue.json",
        {
            "format": "managed-ai-source-catalogue/1",
            "sources": [
                {
                    "bytes": len(note_bytes),
                    "exposure": "available",
                    "id": "work-sample/guide.md",
                    "kind": "guide",
                    "path": "domains/work-sample/sources/guide.md",
                    "sha256": _sha256(note_bytes),
                    "title": "Working sample",
                }
            ],
        },
    )
    _write_json(
        domain_dir / "domain.json",
        {
            "catalogue": {"path": "catalogue.json", "sha256": _sha256(catalogue_raw)},
            "engine_api": "managed-ai-runtime/1",
            "entry_sources": ["work-sample/guide.md"],
            "format": "managed-ai-domain/1",
            "id": "work-sample",
            "requires_domains": [],
            "schemas": {},
            "source_root": "../..",
            "tools": [],
            "version": "1",
        },
    )
    return domain_dir / "domain.json"


class WorkingReleaseVerificationTest(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "store and deployment initialization require POSIX fcntl")
    def test_working_release_without_workbench_verifies_packages_and_extracts(self):
        with TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            bundle = tmp / "working-release"
            shutil.copytree(
                ENGINE,
                bundle / "engine",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            domain_json = _write_sample_domain(bundle)
            store = bundle / "stores/work"
            deployment = bundle / "deployments/work"
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(bundle / "engine/runtime/manage_store.py"),
                    "--root",
                    str(store),
                    "init",
                    "--instance-id",
                    "working-release-test",
                    "--goal-text",
                    "Verify a working release without companions/workbench.",
                    "--domain",
                    str(domain_json),
                ],
                cwd=bundle,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(bundle / "engine/runtime/select_deployment.py"),
                    "--deployment",
                    str(deployment),
                    "init",
                    "--store",
                    str(store),
                    "--engine",
                    str(bundle / "engine"),
                ],
                cwd=bundle,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertFalse((bundle / "companions").exists())

            # Direct verify_bundle() call on the real working release
            core = (bundle / "engine").resolve(strict=True)
            tree = PACKAGE["members"](bundle, core)
            native = {
                name[len("engine/"):]: row
                for name, row in tree.items()
                if name.startswith("engine/") and name != "engine/MANIFEST.json"
            }
            PACKAGE["verify_bundle"](bundle, core, native)

            # End-to-end packaging without companions/workbench
            archive = tmp / "working-release.zip"
            packed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(RELEASE / "package.py"),
                    "--root",
                    str(bundle),
                    "--output",
                    str(archive),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(packed.stdout)
            self.assertEqual(payload["status"], "packaged")
            self.assertTrue((bundle / "MANIFEST.json").is_file())
            self.assertTrue((bundle / "engine/MANIFEST.json").is_file())
            with zipfile.ZipFile(archive) as zf:
                self.assertIsNone(zf.testzip())
                extracted_parent = tmp / "extracted"
                zf.extractall(extracted_parent)

            extracted = extracted_parent / bundle.name
            dep_status = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(extracted / "engine/runtime/select_deployment.py"),
                    "--deployment",
                    str(extracted / "deployments/work"),
                    "status",
                ],
                cwd=extracted,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(dep_status.stdout)["instance_id"], "working-release-test")

            sys_status = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(extracted / "engine/runtime/select_deployment.py"),
                    "--deployment",
                    str(extracted / "deployments/work"),
                    "system",
                    "status",
                ],
                cwd=extracted,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(sys_status.stdout)["instance_id"], "working-release-test")

            # Also succeeds when companions/ exists without companions/workbench/
            harness = bundle / "companions/harness"
            harness.mkdir(parents=True)
            (harness / "PROTOCOL.md").write_text("# Harness\n", encoding="utf-8")
            refreshed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(RELEASE / "package.py"),
                    "--root",
                    str(bundle),
                    "--refresh-only",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(refreshed.stdout)["status"], "verified")

    def test_minimal_working_bundle_without_workbench_verifies_and_packages(self):
        with TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            bundle = tmp / "bundle"
            _build_minimal_bundle(bundle)
            archive = tmp / "minimal-working.zip"
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(RELEASE / "package.py"),
                    "--root",
                    str(bundle),
                    "--output",
                    str(archive),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(result.stdout)["status"], "packaged")
            self.assertTrue(archive.is_file())

    def test_workbench_present_enforces_manifest_identity_and_safety(self):
        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            workbench = _write_workbench(
                bundle,
                {
                    "workbench_agent.py": b"AGENT = 1\n",
                    "controller/agent_runtime.py": b"RUNTIME = 1\n",
                },
            )
            ok = subprocess.run(
                [sys.executable, "-B", str(RELEASE / "package.py"), "--root", str(bundle), "--refresh-only"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(ok.stdout)["status"], "verified")

            # Tampered component file fails before rewriting MANIFEST.json
            prior_manifest = (bundle / "MANIFEST.json").read_bytes()
            (workbench / "workbench_agent.py").write_bytes(b"AGENT = 2\n")
            tampered = subprocess.run(
                [sys.executable, "-B", str(RELEASE / "package.py"), "--root", str(bundle), "--refresh-only"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(tampered.returncode, 0)
            self.assertIn("Shared component identity changed: workbench_agent.py", tampered.stderr)
            self.assertEqual((bundle / "MANIFEST.json").read_bytes(), prior_manifest)

            # Present companions/workbench without COMPONENTS.json must still fail
            (workbench / "COMPONENTS.json").unlink()
            missing_manifest = subprocess.run(
                [sys.executable, "-B", str(RELEASE / "package.py"), "--root", str(bundle), "--refresh-only"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(missing_manifest.returncode, 0)
            self.assertIn("Missing shared workbench component manifest", missing_manifest.stderr)

    def test_workbench_manifest_rejects_traversal_missing_and_malformed(self):
        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            workbench = _write_workbench(bundle, {"workbench_agent.py": b"AGENT = 1\n"})

            # Path traversal
            _write_json(
                workbench / "COMPONENTS.json",
                {
                    "schema": "peer-workbench-acquired-components/1",
                    "files": {
                        "../escape.py": {
                            "identity": {"bytes": 1, "sha256": _sha256(b"x")}
                        }
                    },
                },
            )
            with self.assertRaisesRegex(ValueError, "Unsafe shared component path"):
                PACKAGE["verify_workbench_components"](bundle)

            # Missing declared component
            _write_json(
                workbench / "COMPONENTS.json",
                {
                    "schema": "peer-workbench-acquired-components/1",
                    "files": {
                        "missing.py": {
                            "identity": {"bytes": 1, "sha256": _sha256(b"x")}
                        }
                    },
                },
            )
            with self.assertRaisesRegex(ValueError, "Missing shared component: missing.py"):
                PACKAGE["verify_workbench_components"](bundle)

            # Boolean bytes in identity entry
            _write_json(
                workbench / "COMPONENTS.json",
                {
                    "schema": "peer-workbench-acquired-components/1",
                    "files": {
                        "workbench_agent.py": {
                            "identity": {"bytes": True, "sha256": _sha256(b"AGENT = 1\n")}
                        }
                    },
                },
            )
            with self.assertRaisesRegex(ValueError, "Invalid shared component identity entry"):
                PACKAGE["verify_workbench_components"](bundle)

            # Duplicate JSON key
            (workbench / "COMPONENTS.json").write_text(
                '{"files": {"workbench_agent.py": {}}, "files": {}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                PACKAGE["verify_workbench_components"](bundle)

    def test_workbench_rejects_all_case_variants_even_when_populated(self):
        def rename_exact(path, new_name):
            # Two-step rename works on case-insensitive filesystems (e.g., NTFS, APFS).
            staging = path.with_name(new_name + ".tmpcase")
            path.rename(staging)
            staging.rename(path.with_name(new_name))

        # 1. Wrong-case Companions/harness (without workbench)
        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            harness = bundle / "Companions/harness"
            harness.mkdir(parents=True)
            (harness / "PROTOCOL.md").write_text("# Harness\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "expected companions, observed Companions"):
                PACKAGE["verify_workbench_components"](bundle)

        # 2. Populated Companions/workbench (wrong case on companions)
        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            _write_workbench(bundle, {"workbench_agent.py": b"AGENT = 1\n"})
            rename_exact(bundle / "companions", "Companions")
            with self.assertRaisesRegex(ValueError, "expected companions, observed Companions"):
                PACKAGE["verify_workbench_components"](bundle)

        # 3. Populated companions/Workbench (wrong case on workbench, valid COMPONENTS.json inside)
        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            workbench = _write_workbench(bundle, {"workbench_agent.py": b"AGENT = 1\n"})
            rename_exact(workbench, "Workbench")
            with self.assertRaisesRegex(
                ValueError, "expected companions/workbench, observed companions/Workbench"
            ):
                PACKAGE["verify_workbench_components"](bundle)

        # 4. Wrong-case components.json inside companions/workbench
        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            workbench = _write_workbench(bundle, {"workbench_agent.py": b"AGENT = 1\n"})
            rename_exact(workbench / "COMPONENTS.json", "components.json")
            with self.assertRaisesRegex(
                ValueError, "expected COMPONENTS.json, observed components.json"
            ):
                PACKAGE["verify_workbench_components"](bundle)

        # 5. Wrong-case component leaf filename (Workbench_Agent.py)
        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            workbench = _write_workbench(bundle, {"workbench_agent.py": b"AGENT = 1\n"})
            rename_exact(workbench / "workbench_agent.py", "Workbench_Agent.py")
            with self.assertRaisesRegex(
                ValueError, "expected workbench_agent.py, observed Workbench_Agent.py"
            ):
                PACKAGE["verify_workbench_components"](bundle)

        # 6. Wrong-case component subdirectory (Controller/agent_runtime.py)
        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            workbench = _write_workbench(bundle, {"controller/agent_runtime.py": b"RT = 1\n"})
            rename_exact(workbench / "controller", "Controller")
            with self.assertRaisesRegex(
                ValueError, "expected controller, observed Controller"
            ):
                PACKAGE["verify_workbench_components"](bundle)

    @unittest.skipIf(os.name == "nt", "POSIX symlink test")
    def test_workbench_rejects_symlink_directory_or_component(self):
        with TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            bundle = tmp / "bundle"
            _build_minimal_bundle(bundle)
            outside = tmp / "outside_wb"
            outside.mkdir()
            (bundle / "companions").mkdir()
            (bundle / "companions/workbench").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "ordinary files and directories"):
                PACKAGE["verify_workbench_components"](bundle)

            (bundle / "companions/workbench").unlink()
            workbench = _write_workbench(bundle, {"workbench_agent.py": b"AGENT = 1\n"})
            ext_file = tmp / "ext.py"
            ext_file.write_bytes(b"AGENT = 1\n")
            (workbench / "workbench_agent.py").unlink()
            (workbench / "workbench_agent.py").symlink_to(ext_file)
            with self.assertRaisesRegex(ValueError, "ordinary files and directories"):
                PACKAGE["verify_workbench_components"](bundle)

    def test_compressed_text_rejects_decompression_bomb(self):
        with TemporaryDirectory() as temporary:
            bomb = Path(temporary) / "bomb.gz"
            # 1 MiB of ASCII spaces compresses to ~1 KiB; enforce bound strictly below it.
            bomb.write_bytes(gzip.compress(b" " * (1024 * 1024)))
            self.assertLess(bomb.stat().st_size, 4096)
            with self.assertRaisesRegex(ValueError, "Compressed text exceeds byte bound"):
                PACKAGE["check_compressed_text"](bomb, max_bytes=4096)
            # Exact-boundary payload still passes.
            exact = Path(temporary) / "exact.gz"
            exact.write_bytes(gzip.compress(b"a" * 4096))
            PACKAGE["check_compressed_text"](exact, max_bytes=4096)

    def test_release_readers_reject_duplicate_keys_and_nonfinite_json(self):
        refresh = runpy.run_path(str(RELEASE / "refresh_guidance.py"))
        with TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            dup = tmp / "dup.json"
            dup.write_text('{"edition": "a", "edition": "b"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                PACKAGE["read_json"](dup)

            nan_file = tmp / "nan.json"
            nan_file.write_text('{"bytes": NaN}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "nonfinite JSON"):
                PACKAGE["read_json"](nan_file)

            bundle = tmp / "bundle"
            core = _build_minimal_bundle(bundle)
            (core / "release/navigation.json").write_text('{"a": 1, "a": 2}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                refresh["render"](core)

            (core / "release/navigation.json").write_text('{"score": Infinity}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "nonfinite JSON"):
                refresh["render"](core)

            (core / "release/navigation.json").write_text("{}\n", encoding="utf-8")
            (core / "release/current_sources.json").write_text(
                '{"edition": "x", "edition": "y", "bodies": []}\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                refresh["render"](core)

    def test_compressed_prefixes_normalize_trailing_slash(self):
        from types import SimpleNamespace

        store_declared = SimpleNamespace(
            root=Path("."),
            domain_selection=SimpleNamespace(
                content_addressed_basis_prefixes=["evidence/custom-blobs", "evidence/custom-blobs/"]
            ),
        )
        self.assertEqual(
            PACKAGE["_store_compressed_prefixes"](store_declared),
            ("evidence/custom-blobs/",),
        )
        with TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            dom = tmp / "domain.json"
            _write_json(dom, {"content_addressed_basis_prefixes": ["evidence/a", "evidence/b/"]})
            store_dir = tmp / "store"
            _write_json(store_dir / "system.json", {"domains": [{"path": "../domain.json"}]})
            store_manifest = SimpleNamespace(root=store_dir, domain_selection=None)
            self.assertEqual(
                PACKAGE["_store_compressed_prefixes"](store_manifest),
                ("evidence/a/", "evidence/b/"),
            )


if __name__ == "__main__":
    unittest.main()
