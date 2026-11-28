"""Unit and adversarial security tests for composable domain package export/verify/import."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from managed_ai_information.core.storage import encode, sha
from managed_ai_information.store.selection.domain_package import (
    DOMAIN_PACKAGE_FILES_PREFIX,
    DOMAIN_PACKAGE_FORMAT,
    DOMAIN_PACKAGE_MANIFEST_FILE,
    DomainPackageManifest,
    export_domain_package,
    import_domain_package,
    verify_domain_package,
)
from managed_ai_information.store.system import System


def _make_dir_link(link_path: Path, target_dir: Path) -> None:
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout + proc.stderr)
    else:
        os.symlink(target_dir, link_path)


def _remove_dir_link(link_path: Path) -> None:
    if not link_path.exists() and not link_path.is_symlink():
        return
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "rmdir", str(link_path)], capture_output=True, check=False)
    else:
        link_path.unlink(missing_ok=True)


def _build_sample_domain(domain_dir: Path, domain_id: str = "marketing") -> None:
    (domain_dir / "sources").mkdir(parents=True, exist_ok=True)
    (domain_dir / "workflows").mkdir(parents=True, exist_ok=True)

    guide_bytes = b"# Marketing Domain Guide\n\nPositioning and campaign evidence.\n"
    (domain_dir / "sources/guide.md").write_bytes(guide_bytes)
    workflow_bytes = b"workflow: campaign-review\n"
    (domain_dir / "workflows/campaign.yaml").write_bytes(workflow_bytes)

    catalogue = {
        "format": "managed-ai-source-catalogue/1",
        "sources": [
            {
                "id": f"{domain_id}/guide.md",
                "path": "sources/guide.md",
                "bytes": len(guide_bytes),
                "sha256": sha(guide_bytes),
                "exposure": "available",
                "title": "Marketing guide",
            }
        ],
    }
    cat_bytes = encode(catalogue)
    (domain_dir / "catalogue.json").write_bytes(cat_bytes)

    domain_doc = {
        "format": "managed-ai-domain/1",
        "id": domain_id,
        "version": "1.2.0",
        "engine_api": "managed-ai-runtime/1",
        "source_root": ".",
        "catalogue": {"path": "catalogue.json", "sha256": sha(cat_bytes)},
        "requires_domains": [],
        "entry_sources": [f"{domain_id}/guide.md"],
        "relation_vocabulary": {
            "predicates": ["informs", "validates", "targets-segment"],
            "provenance_self_loop_relations": [],
        },
        "schemas": {
            "campaign-brief": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title"],
                "properties": {"title": {"type": "string", "minLength": 1}},
            }
        },
        "tools": [
            {
                "id": "campaign-review",
                "source_id": f"{domain_id}/guide.md",
            }
        ],
    }
    (domain_dir / "domain.json").write_bytes(encode(domain_doc))


class DomainPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.domain_dir = self.base / "domain-src"
        _build_sample_domain(self.domain_dir)

    def test_deterministic_export_verify_import_and_store_load(self) -> None:
        bundle_a = self.base / "bundles/marketing-a.zip"
        bundle_b = self.base / "bundles/marketing-b.zip"
        pricing = {"model": "open-marketplace", "currency": "USD", "suggested_price": 0}

        manifest_a = export_domain_package(
            self.domain_dir,
            bundle_a,
            license="MIT",
            capabilities=("schemas", "observers", "workflows"),
            pricing_metadata=pricing,
        )
        manifest_b = export_domain_package(
            self.domain_dir,
            bundle_b,
            license="MIT",
            capabilities=("workflows", "schemas", "observers"),
            pricing_metadata=pricing,
        )

        self.assertEqual(manifest_a.format, DOMAIN_PACKAGE_FORMAT)
        self.assertEqual(manifest_a.package_id, "marketing")
        self.assertEqual(manifest_a.version, "1.2.0")
        self.assertEqual(manifest_a.domain_kind, "marketing")
        self.assertEqual(manifest_a.license, "MIT")
        self.assertEqual(manifest_a.capabilities, ("observers", "schemas", "workflows"))
        self.assertEqual(manifest_a.exports_relations, ("informs", "targets-segment", "validates"))
        self.assertEqual(manifest_a.artifact_types, ("campaign-brief",))
        self.assertEqual(manifest_a.workflow_entrypoints, ("campaign-review",))
        self.assertEqual(manifest_a.pricing_metadata, pricing)
        self.assertEqual(manifest_a, manifest_b)
        self.assertEqual(bundle_a.read_bytes(), bundle_b.read_bytes())

        verified = verify_domain_package(bundle_a)
        self.assertEqual(verified.manifest, manifest_a)
        self.assertEqual(verified.bundle_sha256, sha(bundle_a.read_bytes()))
        self.assertEqual(verified.member_count, 4)

        dest = self.base / "imported-domain"
        receipt = import_domain_package(bundle_a, dest)
        self.assertEqual(receipt["format"], DOMAIN_PACKAGE_FORMAT)
        self.assertEqual(receipt["member_count"], 4)
        self.assertTrue((dest / "domain.json").is_file())
        self.assertTrue((dest / DOMAIN_PACKAGE_MANIFEST_FILE).is_file())

        # Imported domain works directly with native System.initialize
        store_dir = self.base / "store"
        System.initialize(
            store_dir,
            {"contribution": "Load imported marketing domain."},
            instance_id="mkt-inst",
            domains=[dest / "domain.json"],
        )
        with System(store_dir) as sys_obj:
            self.assertEqual(sys_obj.domain_selection.domains, ["marketing"])
            self.assertIn("campaign-brief", sys_obj.domain_selection.schemas)

        # Re-exporting imported directory produces identical manifest and bundle bytes
        bundle_c = self.base / "bundles/marketing-c.zip"
        manifest_c = export_domain_package(dest, bundle_c)
        self.assertEqual(manifest_c, manifest_a)
        self.assertEqual(bundle_c.read_bytes(), bundle_a.read_bytes())

    def test_import_refuses_non_empty_destination_unless_overwrite(self) -> None:
        bundle = self.base / "pkg.zip"
        export_domain_package(self.domain_dir, bundle, license="MIT")

        dest = self.base / "existing"
        dest.mkdir()
        (dest / "keep.txt").write_text("existing", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "not empty"):
            import_domain_package(bundle, dest)

        receipt = import_domain_package(bundle, dest, overwrite=True)
        self.assertEqual(receipt["member_count"], 4)
        self.assertTrue((dest / "domain.json").is_file())

    def test_adversarial_zip_slip_and_unsafe_paths_refused_before_writes(self) -> None:
        bad_names = [
            "../escape.txt",
            "files/../escape.txt",
            "files/../../escape.txt",
            "/etc/passwd",
            "files/C:escape.txt",
            "files/sub\\escape.txt",
            "files/CON.txt",
            "files/sub/NUL",
            "files/trailing.",
            "unexpected.txt",
        ]
        for idx, bad_name in enumerate(bad_names):
            with self.subTest(bad_name=bad_name):
                bad_bundle = self.base / f"bad-{idx}.zip"
                manifest = DomainPackageManifest(
                    package_id="evil",
                    version="1.0.0",
                    domain_kind="code",
                    license="MIT",
                    capabilities=("schemas",),
                    requires_domains=(),
                    exports_relations=(),
                    artifact_types=(),
                    workflow_entrypoints=(),
                    content_sha256_tree={"ok.txt": sha(b"ok")},
                )
                with zipfile.ZipFile(bad_bundle, "w") as zf:
                    zf.writestr(DOMAIN_PACKAGE_MANIFEST_FILE, encode(manifest.to_dict()))
                    zf.writestr(DOMAIN_PACKAGE_FILES_PREFIX + "ok.txt", b"ok")
                    zf.writestr(bad_name, b"evil")

                dest = self.base / f"dest-{idx}"
                with self.assertRaises(ValueError):
                    verify_domain_package(bad_bundle)
                with self.assertRaises(ValueError):
                    import_domain_package(bad_bundle, dest)
                self.assertFalse(dest.exists())
                self.assertFalse((self.base / "escape.txt").exists())

    def test_adversarial_symlink_and_special_zip_entries_refused(self) -> None:
        for label, mode in (("symlink", stat.S_IFLNK | 0o777), ("fifo", stat.S_IFIFO | 0o644)):
            with self.subTest(kind=label):
                bundle = self.base / f"{label}.zip"
                payload = b"target"
                manifest = DomainPackageManifest(
                    package_id="symtest",
                    version="1.0.0",
                    domain_kind="code",
                    license="MIT",
                    capabilities=("schemas",),
                    requires_domains=(),
                    exports_relations=(),
                    artifact_types=(),
                    workflow_entrypoints=(),
                    content_sha256_tree={"link.txt": sha(payload)},
                )
                with zipfile.ZipFile(bundle, "w") as zf:
                    zf.writestr(DOMAIN_PACKAGE_MANIFEST_FILE, encode(manifest.to_dict()))
                    zinfo = zipfile.ZipInfo(DOMAIN_PACKAGE_FILES_PREFIX + "link.txt")
                    zinfo.create_system = 3
                    zinfo.external_attr = mode << 16
                    zf.writestr(zinfo, payload)

                with self.assertRaises(ValueError):
                    verify_domain_package(bundle)
                dest = self.base / f"dest-{label}"
                with self.assertRaises(ValueError):
                    import_domain_package(bundle, dest)
                self.assertFalse(dest.exists())

    def test_adversarial_tampered_sha256_missing_extra_and_duplicate_members(self) -> None:
        valid_bundle = self.base / "valid.zip"
        export_domain_package(self.domain_dir, valid_bundle, license="MIT")

        # 1) 1-byte tamper in payload
        tampered = self.base / "tampered.zip"
        with (
            zipfile.ZipFile(valid_bundle, "r") as zin,
            zipfile.ZipFile(tampered, "w") as zout,
        ):
            for info in zin.infolist():
                data = zin.read(info.filename)
                if info.filename.endswith("guide.md"):
                    data = data + b"X"
                zout.writestr(info, data)
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            verify_domain_package(tampered)
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            import_domain_package(tampered, self.base / "dest-tampered")
        self.assertFalse((self.base / "dest-tampered").exists())

        # 2) Missing file declared in manifest
        missing_bundle = self.base / "missing.zip"
        with (
            zipfile.ZipFile(valid_bundle, "r") as zin,
            zipfile.ZipFile(missing_bundle, "w") as zout,
        ):
            for info in zin.infolist():
                if info.filename.endswith("campaign.yaml"):
                    continue
                zout.writestr(info, zin.read(info.filename))
        with self.assertRaisesRegex(ValueError, "payload mismatch"):
            verify_domain_package(missing_bundle)

        # 3) Extra undeclared file inside files/
        extra_bundle = self.base / "extra.zip"
        with (
            zipfile.ZipFile(valid_bundle, "r") as zin,
            zipfile.ZipFile(extra_bundle, "w") as zout,
        ):
            for info in zin.infolist():
                zout.writestr(info, zin.read(info.filename))
            zout.writestr(DOMAIN_PACKAGE_FILES_PREFIX + "sneaky.txt", b"surprise")
        with self.assertRaisesRegex(ValueError, "payload mismatch"):
            verify_domain_package(extra_bundle)

        # 4) Duplicate archive member name
        dup_bundle = self.base / "dup.zip"
        with (
            warnings.catch_warnings(),
            zipfile.ZipFile(valid_bundle, "r") as zin,
            zipfile.ZipFile(dup_bundle, "w") as zout,
        ):
            warnings.simplefilter("ignore", UserWarning)
            for info in zin.infolist():
                data = zin.read(info.filename)
                zout.writestr(info, data)
                if info.filename.endswith("guide.md"):
                    zout.writestr(info, b"shadowed")
        with self.assertRaisesRegex(ValueError, "duplicate archive member"):
            verify_domain_package(dup_bundle)

    def test_adversarial_filesystem_symlink_or_junction_refused(self) -> None:
        # 1) Linked directory inside domain_dir refused on export
        outside = self.base / "outside-secret"
        outside.mkdir()
        (outside / "secret.txt").write_text("do-not-export", encoding="utf-8")
        linked_inside = self.domain_dir / "linked-sub"
        _make_dir_link(linked_inside, outside)
        self.addCleanup(_remove_dir_link, linked_inside)

        with self.assertRaisesRegex(ValueError, "symlink|reparse|junction"):
            export_domain_package(self.domain_dir, self.base / "should-not-exist.zip")
        self.assertFalse((self.base / "should-not-exist.zip").exists())

        _remove_dir_link(linked_inside)

        # 2) Linked dest_dir refused on import
        bundle = self.base / "clean.zip"
        export_domain_package(self.domain_dir, bundle, license="MIT")
        real_target = self.base / "real-dest"
        real_target.mkdir()
        linked_dest = self.base / "linked-dest"
        _make_dir_link(linked_dest, real_target)
        self.addCleanup(_remove_dir_link, linked_dest)

        with self.assertRaisesRegex(ValueError, "symlink|reparse|junction"):
            import_domain_package(bundle, linked_dest)
        self.assertEqual(list(real_target.iterdir()), [])

        # 3) Linked ancestor on bundle refused on verify
        real_bundle_dir = self.base / "real-bdir"
        real_bundle_dir.mkdir()
        (real_bundle_dir / "pkg.zip").write_bytes(bundle.read_bytes())
        linked_bundle_dir = self.base / "linked-bdir"
        _make_dir_link(linked_bundle_dir, real_bundle_dir)
        self.addCleanup(_remove_dir_link, linked_bundle_dir)

        with self.assertRaises(ValueError):
            verify_domain_package(linked_bundle_dir / "pkg.zip")

    def test_manifest_strict_validation_rejects_bad_json_and_non_finite_pricing(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            DomainPackageManifest(
                package_id="mkt",
                version="1.0.0",
                domain_kind="marketing",
                license="MIT",
                capabilities=("schemas",),
                requires_domains=(),
                exports_relations=(),
                artifact_types=(),
                workflow_entrypoints=(),
                content_sha256_tree={},
                pricing_metadata={"amount": float("nan")},
            )

        # Duplicate keys in manifest JSON refused
        dup_json_bundle = self.base / "dup-json.zip"
        with zipfile.ZipFile(dup_json_bundle, "w") as zf:
            zf.writestr(
                DOMAIN_PACKAGE_MANIFEST_FILE,
                b'{"format":"managed-ai-domain-package/1","package_id":"a","package_id":"b"}',
            )
        with self.assertRaises(ValueError):
            verify_domain_package(dup_json_bundle)

        # Unknown extra field in manifest refused
        bad_manifest = {
            "format": DOMAIN_PACKAGE_FORMAT,
            "package_id": "x",
            "version": "1.0.0",
            "domain_kind": "code",
            "license": "MIT",
            "capabilities": [],
            "requires_domains": [],
            "exports_relations": [],
            "artifact_types": [],
            "workflow_entrypoints": [],
            "content_sha256_tree": {},
            "unexpected_field": True,
        }
        with self.assertRaisesRegex(ValueError, "unknown domain package manifest fields"):
            DomainPackageManifest.from_dict(bad_manifest)

        # Round-trip valid manifest dict with JSON
        good = DomainPackageManifest(
            package_id="research-pkg",
            version="2.0.0-alpha.1",
            domain_kind="research",
            license="MIT",
            capabilities=("evals", "observers"),
            requires_domains=("code",),
            exports_relations=("supports", "contradicts"),
            artifact_types=("research-claim",),
            workflow_entrypoints=("operations/current_work.py",),
            content_sha256_tree={"domain.json": "a" * 64},
            pricing_metadata={"tier": "community", "usd": 0},
        )
        reparsed = DomainPackageManifest.from_dict(json.loads(json.dumps(good.to_dict())))
        self.assertEqual(reparsed, good)


if __name__ == "__main__":
    unittest.main(verbosity=2)
