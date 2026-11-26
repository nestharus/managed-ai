"""Dual-OS tests for scripts/publish_library.py.

Covers publish, idempotent re-publish, audit, and CLI guards.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parents[2]
PUBLISH_SCRIPT = REPO / "scripts/publish_library.py"


class PublishLibraryScriptTest(unittest.TestCase):
    def test_publish_idempotent_and_audit_detects_drift(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            src_root = base / "sources"
            (src_root / "guides").mkdir(parents=True)
            doc = src_root / "guides/overview.md"
            doc.write_text("# Overview\n\nComplete canonical body.\n", encoding="utf-8")

            smap = base / "source-map.json"
            smap.write_text(
                json.dumps(
                    {
                        "format": "knowledge-source-map/1",
                        "families": ["guides"],
                        "additional": [],
                        "overrides": {},
                        "modules": [],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            out_lib = base / "published-lib"

            first = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(PUBLISH_SCRIPT),
                    "--root",
                    str(src_root),
                    "--source-map",
                    str(smap),
                    "publish",
                    "--edition",
                    "test-ed-1",
                    "--output",
                    str(out_lib),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            payload = json.loads(first.stdout)
            self.assertEqual(payload["status"], "published-data")
            self.assertTrue((out_lib / "catalogue.json").is_file())

            # Idempotent re-publish returns already-present-verified
            second = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(PUBLISH_SCRIPT),
                    "--root",
                    str(src_root),
                    "--source-map",
                    str(smap),
                    "publish",
                    "--edition",
                    "test-ed-1",
                    "--output",
                    str(out_lib),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(json.loads(second.stdout)["status"], "already-present-verified")

            # Audit matches clean sources
            clean_audit = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(PUBLISH_SCRIPT),
                    "--root",
                    str(src_root),
                    "--source-map",
                    str(smap),
                    "audit",
                    "--library",
                    str(out_lib),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(json.loads(clean_audit.stdout)["status"], "same")

            # Audit exits 1 with status=different after canonical source changes
            doc.write_text("# Overview\n\nUpdated body.\n", encoding="utf-8")
            drift_audit = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(PUBLISH_SCRIPT),
                    "--root",
                    str(src_root),
                    "--source-map",
                    str(smap),
                    "audit",
                    "--library",
                    str(out_lib),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(drift_audit.returncode, 1)
            self.assertEqual(json.loads(drift_audit.stdout)["status"], "different")

    def test_allow_abbrev_false_on_root_and_subcommands(self):
        for args in (
            ["--ro", "."],
            ["--root", ".", "publish", "--hel"],
            ["--root", ".", "audit", "--hel"],
        ):
            proc = subprocess.run(
                [sys.executable, "-B", str(PUBLISH_SCRIPT), *args],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0, args)


if __name__ == "__main__":
    unittest.main()
