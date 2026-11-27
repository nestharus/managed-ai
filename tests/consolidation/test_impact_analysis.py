"""Dual-OS tests for store/query/impact_analysis.py and scripts/consolidation/impact.py."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO = Path(__file__).resolve().parents[2]
for _entry in (str(_REPO), str(_REPO / "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from scripts.consolidation.ingest_crw import ingest_crw  # noqa: E402
from scripts.consolidation.ingest_repo import ingest_repository  # noqa: E402

from managed_ai_information.store.query.impact_analysis import (  # noqa: E402
    IMPACT_REPORT_FORMAT,
    analyze_impact,
    extract_public_symbols,
    resolve_git_diff_targets,
)
from managed_ai_information.store.system import System  # noqa: E402

IMPACT_CLI = _REPO / "scripts/consolidation/impact.py"


def _write_files(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    return root


def _make_sample_repo(root: Path) -> Path:
    return _write_files(
        root,
        {
            "src/demo/__init__.py": "from .revision import revise\n__all__ = ['revise']\n",
            "src/demo/limits.py": "MAX_VIEW_BYTES = 1024\n",
            "src/demo/revision.py": (
                "from demo.limits import MAX_VIEW_BYTES as MAX_VIEW_BYTES\n\n"
                "def revise(limit: int = MAX_VIEW_BYTES) -> int:\n"
                "    return limit\n"
            ),
            "src/demo/service.py": (
                "from demo.revision import revise\n\ndef handle() -> int:\n    return revise()\n"
            ),
            "src/demo/entry.py": (
                "from demo.service import handle\n\ndef run() -> int:\n    return handle()\n"
            ),
            "tests/test_revision.py": (
                "import tempfile\n"
                "import unittest\n"
                "from unittest.mock import patch\n"
                "from demo import revision\n\n"
                "class RevisionTests(unittest.TestCase):\n"
                "    def test_limit_patch_and_object_patch(self) -> None:\n"
                "        with tempfile.TemporaryDirectory():\n"
                "            with patch('demo.revision.MAX_VIEW_BYTES', 128):\n"
                "                self.assertEqual(revision.revise(128), 128)\n"
                "            with patch.object(revision, 'revise', return_value=7):\n"
                "                self.assertEqual(revision.revise(), 7)\n"
            ),
        },
    )


def _make_sample_crw(root: Path) -> Path:
    observer = [
        {
            "id": "organization/boundary-contract-risk.prompt.md",
            "family": "organization-and-communication",
            "concern": "boundary contract and revision propagation",
            "cue": "Trace public symbol contracts and revision boundaries.",
            "sources": ["organization/boundary-contract-risk.prompt.md"],
        }
    ]
    cases = [
        {
            "id": "O01",
            "purpose": "Verify boundary propagation across revision callers.",
            "contract": "Changes propagate cleanly.",
            "expected": "defect",
        }
    ]
    manifest = {
        "format": "crw-control-coverage/1",
        "controls": {
            "O01": {
                "families": ["organization-and-communication"],
                "constituents": ["boundary-contract-risk"],
                "escape_class": "boundary-propagation-escape",
            }
        },
    }
    return _write_files(
        root,
        {
            "risk-axis-reviewers/observer-dimensions.json": json.dumps(observer, indent=2) + "\n",
            "risk-axis-reviewers/review-dimensions.md": "# Dimensions\n",
            "risk-axis-reviewers/semantic-review-contract.md": "# Contract\n",
            "risk-axis-reviewers/families/organization-and-communication.prompt.md": "# Org\n",
            "risk-axis-reviewers/organization/boundary-contract-risk.prompt.md": "# Boundary\n",
            "risk-axis-reviewers/evals/organization-boundary-controls-v1/cases.json": (
                json.dumps(cases, indent=2) + "\n"
            ),
            "risk-axis-reviewers/evals/organization-boundary-controls-v1/coverage-manifest.json": (
                json.dumps(manifest, indent=2) + "\n"
            ),
            "risk-axis-reviewers/evals/organization-boundary-controls-v1/O01/README.md": "# O01\n",
            "docs/review-escapes.md": "# Escapes\n",
            "docs/model-calibration.md": "# Calibration\n",
            "tools/semantic-profiler/README.md": "# Profiler\n",
        },
    )


class ImpactAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="managed-ai-w19-impact-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _init_store(self, name: str) -> Path:
        store = self.tmp / name
        System.initialize(store, {"contribution": f"Impact test store {name}."}, instance_id=name)
        return store

    def test_symbol_callers_blast_radius_and_crw_risks(self) -> None:
        repo = _make_sample_repo(self.tmp / "repo")
        sw_store = self._init_store("sw")
        crw_repo = _make_sample_crw(self.tmp / "crw-repo")
        crw_store = self._init_store("crw")

        ing = ingest_repository(
            repo, sw_store, package_rel="src/demo", include_tests=True, dry_run=False
        )
        self.assertEqual(ing["lint_anomalies"], 0)
        crw_ing = ingest_crw(crw_repo, crw_store, dry_run=False)
        self.assertEqual(crw_ing["lint_anomalies"], 0)

        syms = extract_public_symbols(repo / "src/demo/revision.py", "src/demo/revision.py")
        sym_names = {row["name"] for row in syms}
        self.assertIn("MAX_VIEW_BYTES", sym_names)
        self.assertIn("revise", sym_names)

        report = analyze_impact(
            repo,
            System(sw_store),
            ["src/demo/revision.py"],
            package_rel="src/demo",
            crw_system=System(crw_store),
        )
        self.assertEqual(report["format"], IMPACT_REPORT_FORMAT)
        summary = report["summary"]
        self.assertEqual(summary["target_count"], 1)
        self.assertGreaterEqual(summary["public_symbol_count"], 2)
        self.assertGreaterEqual(summary["patch_site_count"], 2)

        # Verify exact unchanged callers incl. patch("demo.revision.MAX_VIEW_BYTES") & patch.object
        callers = report["unchanged_callers"]
        patch_hits = [
            c
            for c in callers
            if c["file"] == "tests/test_revision.py"
            and c["symbol"] == "MAX_VIEW_BYTES"
            and c["ref_kind"] == "patch"
        ]
        self.assertEqual(len(patch_hits), 1)
        self.assertEqual(
            patch_hits[0]["enclosing"], "RevisionTests.test_limit_patch_and_object_patch"
        )
        self.assertEqual(patch_hits[0]["line"], 9)

        patch_obj_hits = [
            c
            for c in callers
            if c["file"] == "tests/test_revision.py"
            and c["symbol"] == "revise"
            and c["ref_kind"] == "patch_object"
        ]
        self.assertEqual(len(patch_obj_hits), 1)

        src_callers = [
            c for c in callers if c["file"] == "src/demo/service.py" and c["symbol"] == "revise"
        ]
        self.assertGreaterEqual(len(src_callers), 1)

        # Verify 1-hop and 2-hop artifact graph blast radius
        hop1_ids = {row["id"] for row in report["blast_radius"]["hop_1_dependents"]}
        hop2_ids = {row["id"] for row in report["blast_radius"]["hop_2_dependents"]}
        self.assertIn("mod-service", hop1_ids)
        self.assertIn("mod-entry", hop2_ids)
        suite_ids = {row["id"] for row in report["blast_radius"]["exercising_test_suites"]}
        self.assertIn("test-test_revision", suite_ids)

        # Verify CRW risk constituents & O01 control attached
        self.assertTrue(report["crw_risks"]["enabled"])
        dim_ids = {row["id"] for row in report["crw_risks"]["constituents"]}
        ctrl_ids = {row["id"] for row in report["crw_risks"]["controls"]}
        self.assertIn("crw-dim-boundary-contract-risk", dim_ids)
        self.assertIn("crw-ctrl-o01", ctrl_ids)

    def test_cli_end_to_end_and_error_handling(self) -> None:
        repo = _make_sample_repo(self.tmp / "cli-repo")
        sw_store = self._init_store("cli-sw")
        ingest_repository(repo, sw_store, package_rel="src/demo", include_tests=True, dry_run=False)

        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        ok = subprocess.run(
            [
                sys.executable,
                "-B",
                str(IMPACT_CLI),
                "--repo",
                str(repo),
                "--store",
                str(sw_store),
                "--package-dir",
                "src/demo",
                "--targets",
                "src/demo/revision.py",
            ],
            cwd=_REPO,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)
        payload = json.loads(ok.stdout)
        self.assertEqual(payload["format"], IMPACT_REPORT_FORMAT)
        self.assertGreaterEqual(payload["summary"]["unchanged_caller_count"], 3)

        bad = subprocess.run(
            [sys.executable, "-B", str(IMPACT_CLI), "--repo", str(repo), "--store", str(sw_store)],
            cwd=_REPO,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(json.loads(bad.stderr)["status"], "error")

        with self.assertRaisesRegex(ValueError, "invalid --git-diff"):
            resolve_git_diff_targets(repo, "--exec=bad")

    def test_live_workspaces_catches_max_view_bytes_caller_if_present(self) -> None:
        sw_store = next(
            (
                p / "workspaces/software/stores/software"
                for p in _REPO.parents[:3]
                if (p / "workspaces/software/stores/software/system.json").is_file()
            ),
            None,
        )
        crw_store = next(
            (
                p / "workspaces/code-review-workflows/stores/crw"
                for p in _REPO.parents[:3]
                if (p / "workspaces/code-review-workflows/stores/crw/system.json").is_file()
            ),
            None,
        )
        if sw_store is None:
            self.skipTest("live workspaces/software store not present in this environment")

        report = analyze_impact(
            _REPO,
            System(sw_store),
            ["src/managed_ai_information/store/composition/composition_revision.py"],
            crw_system=System(crw_store) if crw_store is not None else None,
        )
        self.assertEqual(report["format"], IMPACT_REPORT_FORMAT)
        max_view_hits = [
            c
            for c in report["unchanged_callers"]
            if c["file"] == "tests/store/test_system.py"
            and c["symbol"] == "MAX_VIEW_BYTES"
            and c["ref_kind"] == "patch"
        ]
        self.assertGreaterEqual(len(max_view_hits), 1)
        self.assertGreaterEqual(report["summary"]["hop_1_dependent_count"], 1)
        if crw_store is not None:
            self.assertGreaterEqual(report["summary"]["crw_constituent_count"], 1)
            self.assertGreaterEqual(report["summary"]["crw_control_count"], 1)


if __name__ == "__main__":
    unittest.main()
