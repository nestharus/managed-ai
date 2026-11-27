"""Dual-OS tests for store/query/impact_analysis.py and `managed-ai-information impact` CLI."""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from managed_ai_information.store.cli.command_parser import is_readonly_action  # noqa: E402
from managed_ai_information.store.cli.command_parser import (  # noqa: E402
    parser as store_cli_parser,
)
from managed_ai_information.store.ingest import apply_desired_records  # noqa: E402
from managed_ai_information.store.query.impact_analysis import (  # noqa: E402
    IMPACT_REPORT_FORMAT,
    analyze_impact,
    extract_public_symbols,
    resolve_git_diff_targets,
)
from managed_ai_information.store.system import System  # noqa: E402

MANAGE_STORE_CLI = _REPO / "src/managed_ai_information/store/manage_store.py"


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


def _seed_software_store(store_root: Path) -> None:
    records = [
        {
            "id": "comp-demo",
            "kind": "software-component",
            "content": {"name": "demo"},
            "relations": [],
            "basis": [],
            "active": True,
        },
        {
            "id": "mod-limits",
            "kind": "code-module",
            "content": {"module": "demo.limits", "path": "src/demo/limits.py"},
            "relations": [{"relation": "maps-to", "target": "comp-demo"}],
            "basis": [],
            "active": True,
        },
        {
            "id": "mod-revision",
            "kind": "code-module",
            "content": {"module": "demo.revision", "path": "src/demo/revision.py"},
            "relations": [
                {"relation": "depends-on", "target": "mod-limits"},
                {"relation": "maps-to", "target": "comp-demo"},
            ],
            "basis": [],
            "active": True,
        },
        {
            "id": "mod-service",
            "kind": "code-module",
            "content": {"module": "demo.service", "path": "src/demo/service.py"},
            "relations": [
                {"relation": "depends-on", "target": "mod-revision"},
                {"relation": "maps-to", "target": "comp-demo"},
            ],
            "basis": [],
            "active": True,
        },
        {
            "id": "mod-entry",
            "kind": "code-module",
            "content": {"module": "demo.entry", "path": "src/demo/entry.py"},
            "relations": [
                {"relation": "depends-on", "target": "mod-service"},
                {"relation": "maps-to", "target": "comp-demo"},
            ],
            "basis": [],
            "active": True,
        },
        {
            "id": "test-test_revision",
            "kind": "test-suite",
            "content": {
                "path": "tests/test_revision.py",
                "boundary_class": "mixed",
                "assertion_count": 2,
            },
            "relations": [{"relation": "verifies", "target": "mod-revision"}],
            "basis": [],
            "active": True,
        },
    ]
    with System(store_root) as sys_obj:
        apply_desired_records(
            sys_obj,
            records,
            {r["id"]: {} for r in records},
            batch_prefix="seed-sw",
        )


def _seed_crw_store(store_root: Path) -> None:
    records = [
        {
            "id": "crw-dim-boundary-contract-risk",
            "kind": "crw-constituent",
            "content": {
                "slug": "boundary-contract-risk",
                "family": "organization-and-communication",
                "concern": "boundary contract and revision propagation",
                "cue": "Trace public symbol contracts and revision boundaries.",
                "prompt_source_id": (
                    "code-workflows/risk-axis-reviewers/organization/"
                    "boundary-contract-risk.prompt.md"
                ),
            },
            "relations": [],
            "basis": [],
            "active": True,
        },
        {
            "id": "crw-ctrl-o01",
            "kind": "crw-control",
            "content": {
                "control_id": "O01",
                "escape_class": "boundary-propagation-escape",
                "purpose": "Verify boundary propagation across revision callers.",
            },
            "relations": [{"relation": "supports", "target": "crw-dim-boundary-contract-risk"}],
            "basis": [],
            "active": True,
        },
    ]
    with System(store_root) as sys_obj:
        apply_desired_records(
            sys_obj,
            records,
            {r["id"]: {} for r in records},
            batch_prefix="seed-crw",
        )


class ImpactAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="managed-ai-w20-impact-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _init_store(self, name: str) -> Path:
        store = self.tmp / name
        System.initialize(store, {"contribution": f"Impact test store {name}."}, instance_id=name)
        return store

    def test_symbol_callers_blast_radius_crw_risks_and_bool_guards(self) -> None:
        repo = _make_sample_repo(self.tmp / "repo")
        sw_store = self._init_store("sw")
        crw_store = self._init_store("crw")
        _seed_software_store(sw_store)
        _seed_crw_store(crw_store)

        syms = extract_public_symbols(repo / "src/demo/revision.py", "src/demo/revision.py")
        sym_names = {row["name"] for row in syms}
        self.assertIn("MAX_VIEW_BYTES", sym_names)
        self.assertIn("revise", sym_names)

        with System(sw_store) as sys_sw, System(crw_store) as sys_crw:
            report = analyze_impact(
                repo,
                sys_sw,
                ["src/demo/revision.py"],
                package_rel="src/demo",
                crw_system=sys_crw,
            )
            # Strict JSON bool rejection on CRW limits
            with self.assertRaises(ValueError):
                analyze_impact(
                    repo,
                    sys_sw,
                    ["src/demo/revision.py"],
                    package_rel="src/demo",
                    max_crw_constituents=True,  # type: ignore[arg-type]
                )
            with self.assertRaises(ValueError):
                analyze_impact(
                    repo,
                    sys_sw,
                    ["src/demo/revision.py"],
                    package_rel="src/demo",
                    max_crw_controls=-1,
                )

        self.assertEqual(report["format"], IMPACT_REPORT_FORMAT)
        summary = report["summary"]
        self.assertEqual(summary["target_count"], 1)
        self.assertGreaterEqual(summary["public_symbol_count"], 2)
        self.assertGreaterEqual(summary["patch_site_count"], 2)

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

        hop1_ids = {row["id"] for row in report["blast_radius"]["hop_1_dependents"]}
        hop2_ids = {row["id"] for row in report["blast_radius"]["hop_2_dependents"]}
        self.assertIn("mod-service", hop1_ids)
        self.assertIn("mod-entry", hop2_ids)
        suite_ids = {row["id"] for row in report["blast_radius"]["exercising_test_suites"]}
        self.assertIn("test-test_revision", suite_ids)

        self.assertTrue(report["crw_risks"]["enabled"])
        self.assertIn(
            "crw-dim-boundary-contract-risk", {r["id"] for r in report["crw_risks"]["constituents"]}
        )
        self.assertIn("crw-ctrl-o01", {r["id"] for r in report["crw_risks"]["controls"]})

    def test_cli_impact_targets_git_diff_and_deleted_file_handling(self) -> None:
        repo = _make_sample_repo(self.tmp / "cli-repo")
        sw_store = self._init_store("cli-sw")
        crw_store = self._init_store("cli-crw")
        _seed_software_store(sw_store)
        _seed_crw_store(crw_store)

        # impact action is read-only and supports bare --git-diff (defaults to HEAD)
        cli_parser = store_cli_parser()
        parsed_targets = cli_parser.parse_args(
            [
                "--root",
                str(sw_store),
                "impact",
                "--repo",
                str(repo),
                "--targets",
                "src/demo/revision.py",
            ]
        )
        self.assertTrue(is_readonly_action(parsed_targets))
        parsed_bare_diff = cli_parser.parse_args(
            ["--root", str(sw_store), "impact", "--repo", str(repo), "--git-diff"]
        )
        self.assertEqual(parsed_bare_diff.git_diff, "HEAD")

        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        # 1. CLI --targets + --crw-store end-to-end
        ok = subprocess.run(
            [
                sys.executable,
                "-B",
                str(MANAGE_STORE_CLI),
                "--root",
                str(sw_store),
                "impact",
                "--repo",
                str(repo),
                "--package-dir",
                "src/demo",
                "--targets",
                "src/demo/revision.py",
                "--crw-store",
                str(crw_store),
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)
        doc = json.loads(ok.stdout)
        self.assertEqual(doc["format"], IMPACT_REPORT_FORMAT)
        self.assertIn("mod-service", {r["id"] for r in doc["blast_radius"]["hop_1_dependents"]})
        self.assertIn("crw-ctrl-o01", {r["id"] for r in doc["crw_risks"]["controls"]})

        # 2. Missing targets and --git-diff returns exit 2 cleanly
        bad = subprocess.run(
            [
                sys.executable,
                "-B",
                str(MANAGE_STORE_CLI),
                "--root",
                str(sw_store),
                "impact",
                "--repo",
                str(repo),
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(bad.returncode, 2)

        with self.assertRaisesRegex(ValueError, "invalid --git-diff"):
            resolve_git_diff_targets(repo, "--exec=bad")

        # 3. Bare --git-diff (defaults to HEAD) with modified AND deleted file in real git repo
        git_bin = shutil.which("git")
        if git_bin is not None:
            for args in (
                [git_bin, "-C", str(repo), "init"],
                [git_bin, "-C", str(repo), "config", "user.email", "test@example.com"],
                [git_bin, "-C", str(repo), "config", "user.name", "Test"],
                [git_bin, "-C", str(repo), "add", "."],
                [git_bin, "-C", str(repo), "commit", "-m", "initial"],
            ):
                p = subprocess.run(args, capture_output=True, text=True, check=False)
                self.assertEqual(p.returncode, 0, p.stderr)

            # Delete revision.py and modify limits.py in working tree
            (repo / "src/demo/revision.py").unlink()
            (repo / "src/demo/limits.py").write_text("MAX_VIEW_BYTES = 2048\n", encoding="utf-8")

            diff_res = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(MANAGE_STORE_CLI),
                    "--root",
                    str(sw_store),
                    "impact",
                    "--repo",
                    str(repo),
                    "--package-dir",
                    "src/demo",
                    "--git-diff",
                ],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(diff_res.returncode, 0, diff_res.stderr)
            diff_doc = json.loads(diff_res.stdout)
            by_path = {row["path"]: row for row in diff_doc["per_target"]}
            self.assertTrue(by_path["src/demo/revision.py"]["deleted"])
            self.assertFalse(by_path["src/demo/limits.py"]["deleted"])
            self.assertIn(
                "mod-service", {r["id"] for r in diff_doc["blast_radius"]["hop_1_dependents"]}
            )


if __name__ == "__main__":
    unittest.main()
