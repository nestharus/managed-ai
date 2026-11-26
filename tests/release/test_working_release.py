"""Working releases without companions/peer verify cleanly; present peer manifests remain strictly gated."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile


REPO = Path(__file__).resolve().parents[3]
ENGINE = REPO / "engine"
RELEASE = ENGINE / "release"
PACKAGE = runpy.run_path(str(RELEASE / "package.py"))
REFRESH = runpy.run_path(str(RELEASE / "refresh_guidance.py"))


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _write_json(path, value):
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def _build_minimal_bundle(root, *, include_siblings=False):
    core = root / "engine"
    (core / "release").mkdir(parents=True)
    (core / "store").mkdir(parents=True)
    rows = [{"id": "guide", "source": "guide.md"}]
    (core / "guide.md").write_bytes(b"# Guide\n\nWorking release body.\n")
    if include_siblings:
        (root / "optional/collective").mkdir(parents=True)
        (root / "optional/collective/README.md").write_bytes(b"# Collective\n\nOptional body.\n")
        (root / "patterns/continuous-consolidation").mkdir(parents=True)
        (root / "patterns/continuous-consolidation/README.md").write_bytes(b"# Patterns\n\nPattern body.\n")
        rows.extend([
            {"id": "prototype/collective-work/README.md", "source": "../optional/collective/README.md"},
            {"id": "prototype/patterns/continuous-consolidation/README.md",
             "source": "../patterns/continuous-consolidation/README.md"},
        ])
    (core / "release/current_sources.json").write_text(
        json.dumps({"edition": "working-test", "bodies": rows}), encoding="utf-8"
    )
    (core / "store/catalogue.json").write_text(
        json.dumps({"entries": rows}), encoding="utf-8"
    )
    (core / "release/navigation.json").write_text("{}", encoding="utf-8")
    subprocess.run(
        [sys.executable, "-B", str(RELEASE / "refresh_guidance.py"), "--root", str(core)],
        check=True,
        capture_output=True,
        text=True,
    )
    return core


def _write_peer_components(root, files):
    companions = root / "companions"
    peer = companions / "peer"
    peer.mkdir(parents=True, exist_ok=True)
    manifest_files = {}
    for rel, content in files.items():
        target = companions / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        manifest_files[rel] = {
            "source": rel,
            "identity": {"bytes": len(content), "sha256": _sha256(content)},
        }
    _write_json(
        peer / "COMPONENTS.json",
        {"schema": "peer-workbench-acquired-components/1", "files": manifest_files},
    )
    return peer


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
    def test_working_release_without_peer_verifies_packages_and_extracts(self):
        with TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            bundle = tmp / "working-release"
            shutil.copytree(
                ENGINE,
                bundle / "engine",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            for sibling in ("optional", "patterns"):
                if (REPO / sibling).is_dir():
                    shutil.copytree(
                        REPO / sibling,
                        bundle / sibling,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                    )
            domain_json = _write_sample_domain(bundle)
            store = bundle / "stores/work"
            deployment = bundle / "deployments/work"
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(bundle / "engine/store/manage_store.py"),
                    "--root",
                    str(store),
                    "init",
                    "--instance-id",
                    "working-release-test",
                    "--goal-text",
                    "Verify a working release without companions/peer.",
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
                    str(bundle / "engine/deployment/select_deployment.py"),
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

            core = (bundle / "engine").resolve(strict=True)
            tree = PACKAGE["members"](bundle, core)
            native = {
                name[len("engine/"):]: row
                for name, row in tree.items()
                if name.startswith("engine/") and name != "engine/MANIFEST.json"
            }
            PACKAGE["verify_bundle"](bundle, core, native, tree=tree)

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
            self.assertTrue((bundle / "engine/SOURCES.md").is_file())
            with zipfile.ZipFile(archive) as zf:
                self.assertIsNone(zf.testzip())
                extracted_parent = tmp / "extracted"
                zf.extractall(extracted_parent)

            extracted = extracted_parent / bundle.name
            dep_status = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(extracted / "engine/deployment/select_deployment.py"),
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
                    str(extracted / "engine/deployment/select_deployment.py"),
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

            # Also succeeds when companions/ exists without companions/peer/
            pub = bundle / "companions/publication_review"
            pub.mkdir(parents=True)
            (pub / "PUBLICATION.md").write_text("# Publication\n", encoding="utf-8")
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

    def test_minimal_working_bundle_and_sibling_optional_patterns_package(self):
        with TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            bundle = tmp / "bundle"
            core = _build_minimal_bundle(bundle, include_siblings=True)
            cat = json.loads((core / "store/catalogue.json").read_text(encoding="utf-8"))
            by_id = {e["id"]: e for e in cat["entries"]}
            self.assertEqual(by_id["guide"]["source_root_relative"], "../..")
            self.assertEqual(by_id["guide"]["body_path"], "engine/guide.md")
            self.assertEqual(by_id["prototype/collective-work/README.md"]["body_path"], "optional/collective/README.md")
            self.assertEqual(
                by_id["prototype/patterns/continuous-consolidation/README.md"]["body_path"],
                "patterns/continuous-consolidation/README.md",
            )
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

    def test_peer_present_enforces_manifest_identity_and_safety(self):
        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            peer = _write_peer_components(
                bundle,
                {
                    "workbench/core/workbench_agent.py": b"AGENT = 1\n",
                    "workbench/controller/agent_runtime.py": b"RUNTIME = 1\n",
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
            (bundle / "companions/workbench/core/workbench_agent.py").write_bytes(b"AGENT = 2\n")
            tampered = subprocess.run(
                [sys.executable, "-B", str(RELEASE / "package.py"), "--root", str(bundle), "--refresh-only"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(tampered.returncode, 0)
            self.assertIn("Shared component identity changed: workbench/core/workbench_agent.py", tampered.stderr)
            self.assertEqual((bundle / "MANIFEST.json").read_bytes(), prior_manifest)

            # Present companions/peer without COMPONENTS.json must still fail
            (peer / "COMPONENTS.json").unlink()
            missing_manifest = subprocess.run(
                [sys.executable, "-B", str(RELEASE / "package.py"), "--root", str(bundle), "--refresh-only"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(missing_manifest.returncode, 0)
            self.assertIn("Missing shared peer component manifest", missing_manifest.stderr)

    def test_peer_manifest_rejects_traversal_missing_and_malformed(self):
        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            peer = _write_peer_components(bundle, {"workbench/core/workbench_agent.py": b"AGENT = 1\n"})

            _write_json(
                peer / "COMPONENTS.json",
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

            _write_json(
                peer / "COMPONENTS.json",
                {
                    "schema": "peer-workbench-acquired-components/1",
                    "files": {
                        "workbench/core/missing.py": {
                            "identity": {"bytes": 1, "sha256": _sha256(b"x")}
                        }
                    },
                },
            )
            with self.assertRaisesRegex(ValueError, "Missing shared component: workbench/core/missing.py"):
                PACKAGE["verify_workbench_components"](bundle)

            _write_json(
                peer / "COMPONENTS.json",
                {
                    "schema": "peer-workbench-acquired-components/1",
                    "files": {
                        "workbench/core/workbench_agent.py": {
                            "identity": {"bytes": True, "sha256": _sha256(b"AGENT = 1\n")}
                        }
                    },
                },
            )
            with self.assertRaisesRegex(ValueError, "Invalid shared component identity entry"):
                PACKAGE["verify_workbench_components"](bundle)

            (peer / "COMPONENTS.json").write_text(
                '{"files": {"workbench/core/workbench_agent.py": {}}, "files": {}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                PACKAGE["verify_workbench_components"](bundle)

    def test_peer_rejects_all_case_variants_even_when_populated(self):
        def rename_exact(path, new_name):
            staging = path.with_name(new_name + ".tmpcase")
            path.rename(staging)
            staging.rename(path.with_name(new_name))

        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            pub = bundle / "Companions/publication_review"
            pub.mkdir(parents=True)
            (pub / "PUBLICATION.md").write_text("# Publication\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "expected companions, observed Companions"):
                PACKAGE["verify_workbench_components"](bundle)

        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            _write_peer_components(bundle, {"workbench/core/workbench_agent.py": b"AGENT = 1\n"})
            rename_exact(bundle / "companions", "Companions")
            with self.assertRaisesRegex(ValueError, "expected companions, observed Companions"):
                PACKAGE["verify_workbench_components"](bundle)

        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            peer = _write_peer_components(bundle, {"workbench/core/workbench_agent.py": b"AGENT = 1\n"})
            rename_exact(peer, "Peer")
            with self.assertRaisesRegex(ValueError, "expected peer, observed Peer"):
                PACKAGE["verify_workbench_components"](bundle)

        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            peer = _write_peer_components(bundle, {"workbench/core/workbench_agent.py": b"AGENT = 1\n"})
            rename_exact(peer / "COMPONENTS.json", "components.json")
            with self.assertRaisesRegex(ValueError, "expected COMPONENTS.json, observed components.json"):
                PACKAGE["verify_workbench_components"](bundle)

        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            _write_peer_components(bundle, {"workbench/core/workbench_agent.py": b"AGENT = 1\n"})
            rename_exact(bundle / "companions/workbench/core/workbench_agent.py", "Workbench_Agent.py")
            with self.assertRaisesRegex(
                ValueError, "expected workbench/core/workbench_agent.py, observed workbench/core/Workbench_Agent.py"
            ):
                PACKAGE["verify_workbench_components"](bundle)

        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            _build_minimal_bundle(bundle)
            _write_peer_components(bundle, {"workbench/controller/agent_runtime.py": b"RT = 1\n"})
            rename_exact(bundle / "companions/workbench/controller", "Controller")
            with self.assertRaisesRegex(
                ValueError, "expected workbench/controller, observed workbench/Controller"
            ):
                PACKAGE["verify_workbench_components"](bundle)

    @unittest.skipIf(os.name == "nt", "POSIX symlink test")
    def test_peer_rejects_symlink_directory_or_component(self):
        with TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            bundle = tmp / "bundle"
            _build_minimal_bundle(bundle)
            outside = tmp / "outside_peer"
            outside.mkdir()
            (bundle / "companions").mkdir()
            (bundle / "companions/peer").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "ordinary files and directories"):
                PACKAGE["verify_workbench_components"](bundle)

            (bundle / "companions/peer").unlink()
            _write_peer_components(bundle, {"workbench/core/workbench_agent.py": b"AGENT = 1\n"})
            ext_file = tmp / "ext.py"
            ext_file.write_bytes(b"AGENT = 1\n")
            target = bundle / "companions/workbench/core/workbench_agent.py"
            target.unlink()
            target.symlink_to(ext_file)
            with self.assertRaisesRegex(ValueError, "ordinary files and directories"):
                PACKAGE["verify_workbench_components"](bundle)

    def test_compressed_text_rejects_decompression_bomb(self):
        with TemporaryDirectory() as temporary:
            bomb = Path(temporary) / "bomb.gz"
            bomb.write_bytes(gzip.compress(b" " * (1024 * 1024)))
            self.assertLess(bomb.stat().st_size, 4096)
            with self.assertRaisesRegex(ValueError, "Compressed text exceeds byte bound"):
                PACKAGE["check_compressed_text"](bomb, max_bytes=4096)
            exact = Path(temporary) / "exact.gz"
            exact.write_bytes(gzip.compress(b"a" * 4096))
            PACKAGE["check_compressed_text"](exact, max_bytes=4096)

    def test_release_readers_reject_duplicate_keys_and_nonfinite_json(self):
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
                REFRESH["render"](core)

            (core / "release/navigation.json").write_text('{"score": Infinity}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "nonfinite JSON"):
                REFRESH["render"](core)

            (core / "release/navigation.json").write_text("{}\n", encoding="utf-8")
            (core / "release/current_sources.json").write_text(
                '{"edition": "x", "edition": "y", "bodies": []}\n', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                REFRESH["render"](core)

    def test_compressed_prefixes_fallback_enforces_domain_sha256_pin(self):
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
            dom_raw = _write_json(dom, {"content_addressed_basis_prefixes": ["evidence/a", "evidence/b/"]})
            store_dir = tmp / "store"
            _write_json(
                store_dir / "system.json",
                {"domains": [{"path": "../domain.json", "sha256": _sha256(dom_raw)}]},
            )
            store_manifest = SimpleNamespace(root=store_dir, domain_selection=None)
            self.assertEqual(
                PACKAGE["_store_compressed_prefixes"](store_manifest),
                ("evidence/a/", "evidence/b/"),
            )

            # W4-O7: Missing sha256 pin must be rejected
            _write_json(store_dir / "system.json", {"domains": [{"path": "../domain.json"}]})
            with self.assertRaisesRegex(ValueError, "exact SHA-256 pin"):
                PACKAGE["_store_compressed_prefixes"](store_manifest)

            # W4-O7: Mismatched sha256 pin must be rejected
            _write_json(
                store_dir / "system.json",
                {"domains": [{"path": "../domain.json", "sha256": "0" * 64}]},
            )
            with self.assertRaisesRegex(ValueError, "Domain descriptor pin mismatch"):
                PACKAGE["_store_compressed_prefixes"](store_manifest)

    def test_refresh_guidance_rejects_reparse_point_and_escape(self):
        # W4-CB8: Reparse-point stat must be rejected even on Windows where is_symlink() may be False
        fake_reparse = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o644,
            st_reparse_tag=0xA0000003,
            st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
        )
        self.assertTrue(REFRESH["_is_reparse_or_symlink"](fake_reparse))
        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            core = _build_minimal_bundle(bundle)
            guide = core / "guide.md"
            orig_lstat = Path.lstat

            def patched_lstat(path_self):
                if path_self.resolve() == guide.resolve():
                    return fake_reparse
                return orig_lstat(path_self)

            with mock.patch.object(Path, "lstat", patched_lstat):
                with self.assertRaisesRegex(ValueError, "ordinary canonical source required"):
                    REFRESH["render"](core)

            # Traversal escaping above bundle root must be rejected
            (core / "release/current_sources.json").write_text(
                json.dumps({"edition": "working-test", "bodies": [{"id": "guide", "source": "../../escape.md"}]}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "escapes allowed package root"):
                REFRESH["render"](core)

    def test_c26_1_and_c26_2_refuse_symlink_and_junction_clobber_targets(self):
        with TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            bundle = tmp / "bundle"
            core = _build_minimal_bundle(bundle)
            ext = tmp / "external_secret.txt"
            ext.write_bytes(b"KEEP-ME-UNTOUCHED\n")

            if os.name == "nt":
                # Windows real NTFS junction tests for --root on both refresh_guidance.py and package.py
                junc_core = tmp / "junc_core"
                subprocess.run(["cmd", "/c", "mklink", "/J", str(junc_core), str(core)], check=True, capture_output=True)
                ref_junc = subprocess.run(
                    [sys.executable, "-B", str(RELEASE / "refresh_guidance.py"), "--root", str(junc_core)],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(ref_junc.returncode, 0)
                self.assertIn("ordinary engine directory required", ref_junc.stderr)

                junc_bundle = tmp / "junc_bundle"
                subprocess.run(["cmd", "/c", "mklink", "/J", str(junc_bundle), str(bundle)], check=True, capture_output=True)
                pkg_junc = subprocess.run(
                    [sys.executable, "-B", str(RELEASE / "package.py"), "--root", str(junc_bundle), "--refresh-only"],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(pkg_junc.returncode, 0)
                self.assertIn("ordinary files and directories", pkg_junc.stderr)

                # Junction parent for --output must be rejected before writing
                real_out_dir = tmp / "real_out_dir"
                real_out_dir.mkdir()
                junc_out_dir = tmp / "junc_out_dir"
                subprocess.run(["cmd", "/c", "mklink", "/J", str(junc_out_dir), str(real_out_dir)], check=True, capture_output=True)
                pkg_out_junc = subprocess.run(
                    [sys.executable, "-B", str(RELEASE / "package.py"), "--root", str(bundle),
                     "--output", str(junc_out_dir / "out.zip")],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(pkg_out_junc.returncode, 0)
                self.assertIn("ordinary files and directories", pkg_out_junc.stderr)
                self.assertFalse((real_out_dir / "out.zip").exists())

                # Mocked reparse point on SOURCES.md must be refused by refresh() before staging/replace
                fake_reparse = SimpleNamespace(
                    st_mode=stat.S_IFREG | 0o644,
                    st_reparse_tag=0xA0000003,
                    st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
                )
                sources_md = core / "SOURCES.md"
                sources_md.write_text("# Sources\n", encoding="utf-8")
                orig_lstat = Path.lstat

                def patched_lstat(path_self):
                    if os.path.normcase(os.path.abspath(path_self)) == os.path.normcase(os.path.abspath(sources_md)):
                        return fake_reparse
                    return orig_lstat(path_self)

                with mock.patch.object(Path, "lstat", patched_lstat):
                    with self.assertRaisesRegex(ValueError, "ordinary projection target required"):
                        REFRESH["refresh"](core, include_sources_md=True)
            else:
                # POSIX real symlink clobber tests for C26-1 (SOURCES.md, CURRENT-RELEASE.json, --root)
                subprocess.run(
                    [sys.executable, "-B", str(RELEASE / "refresh_guidance.py"), "--root", str(core), "--write-sources-md"],
                    check=True,
                    capture_output=True,
                )
                for target_name in ("SOURCES.md", "CURRENT-RELEASE.json"):
                    target = core / target_name
                    backup = target.read_bytes()
                    target.unlink()
                    target.symlink_to(ext)
                    res = subprocess.run(
                        [sys.executable, "-B", str(RELEASE / "refresh_guidance.py"),
                         "--root", str(core), "--write-sources-md"],
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(res.returncode, 0, target_name)
                    self.assertIn("ordinary projection target required", res.stderr)
                    self.assertEqual(ext.read_bytes(), b"KEEP-ME-UNTOUCHED\n")
                    target.unlink()
                    target.write_bytes(backup)

                link_core = tmp / "link_core"
                link_core.symlink_to(core)
                res_root = subprocess.run(
                    [sys.executable, "-B", str(RELEASE / "refresh_guidance.py"), "--root", str(link_core)],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(res_root.returncode, 0)
                self.assertIn("ordinary engine directory required", res_root.stderr)

                # C26-2: package.py refuses symlink --output (--replace and dangling) and symlink --root
                out_link = tmp / "out.zip"
                out_link.symlink_to(ext)
                res_rep = subprocess.run(
                    [sys.executable, "-B", str(RELEASE / "package.py"), "--root", str(bundle),
                     "--output", str(out_link), "--replace"],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(res_rep.returncode, 0)
                self.assertIn("ordinary files and directories", res_rep.stderr)
                self.assertEqual(ext.read_bytes(), b"KEEP-ME-UNTOUCHED\n")

                missing_ext = tmp / "never_create.zip"
                dangling = tmp / "dangling.zip"
                dangling.symlink_to(missing_ext)
                res_dang = subprocess.run(
                    [sys.executable, "-B", str(RELEASE / "package.py"), "--root", str(bundle),
                     "--output", str(dangling)],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(res_dang.returncode, 0)
                self.assertIn("ordinary files and directories", res_dang.stderr)
                self.assertFalse(missing_ext.exists())

                link_bundle = tmp / "link_bundle"
                link_bundle.symlink_to(bundle)
                res_pkg_root = subprocess.run(
                    [sys.executable, "-B", str(RELEASE / "package.py"), "--root", str(link_bundle), "--refresh-only"],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(res_pkg_root.returncode, 0)
                self.assertIn("ordinary files and directories", res_pkg_root.stderr)

    def test_declarative_components_stamping_gitignore_and_allow_abbrev(self):
        with TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            bundle = tmp / "bundle"
            _build_minimal_bundle(bundle)
            # .gitignore in root & domains must be admitted cleanly (only .git is refused)
            (bundle / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
            (bundle / "domains/sample").mkdir(parents=True)
            (bundle / "domains/sample/.gitignore").write_text("__pycache__/\n", encoding="utf-8")
            # Declarative COMPONENTS.json (omitting "identity" in Git checkouts)
            comp_file = bundle / "companions/workbench/core/io_utils.py"
            comp_file.parent.mkdir(parents=True)
            comp_raw = b"IO = 1\n"
            comp_file.write_bytes(comp_raw)
            peer = bundle / "companions/peer"
            peer.mkdir(parents=True)
            _write_json(
                peer / "COMPONENTS.json",
                {
                    "schema": "peer-workbench-acquired-components/1",
                    "files": {"workbench/core/io_utils.py": {"source": "workbench/core/io_utils.py"}},
                },
            )
            archive = tmp / "stamped.zip"
            res = subprocess.run(
                [sys.executable, "-B", str(RELEASE / "package.py"), "--root", str(bundle), "--output", str(archive)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(res.stdout)["status"], "packaged")
            # Exported bundle has stamped identity in COMPONENTS.json + generated SOURCES.md & MANIFEST.json
            stamped = json.loads((peer / "COMPONENTS.json").read_text(encoding="utf-8"))
            self.assertEqual(
                stamped["files"]["workbench/core/io_utils.py"]["identity"],
                {"bytes": len(comp_raw), "sha256": _sha256(comp_raw)},
            )
            self.assertTrue((bundle / "engine/SOURCES.md").is_file())
            manifest = json.loads((bundle / "MANIFEST.json").read_text(encoding="utf-8"))
            self.assertIn(".gitignore", manifest["members"])
            self.assertIn("domains/sample/.gitignore", manifest["members"])
            self.assertIn("engine/SOURCES.md", manifest["members"])
            # W5-B11: allow_abbrev=False refuses abbreviated flags on package.py & refresh_guidance.py
            bad_pkg = subprocess.run(
                [sys.executable, "-B", str(RELEASE / "package.py"), "--root", str(bundle), "--ref"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(bad_pkg.returncode, 0)
            bad_ref = subprocess.run(
                [sys.executable, "-B", str(RELEASE / "refresh_guidance.py"), "--ro", str(bundle / "engine")],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(bad_ref.returncode, 0)


if __name__ == "__main__":
    unittest.main()
