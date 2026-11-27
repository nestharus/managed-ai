"""Dual-OS tests for scripts/consolidation/test_value_analyzer.py."""

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

from scripts.consolidation.test_value_analyzer import (  # noqa: E402
    TEST_VALUE_REPORT_FORMAT,
    analyze_test_value,
)

ANALYZER_CLI = _REPO / "scripts/consolidation/test_value_analyzer.py"


def _write_files(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    return root


def _make_sample_project(root: Path) -> Path:
    return _write_files(
        root,
        {
            "src/demo/__init__.py": "from .core_mod import compute\n__all__ = ['compute']\n",
            "src/demo/empty_sub/__init__.py": "",
            "src/demo/core_mod.py": (
                "def compute(x: int) -> int:\n"
                "    if x < 0:\n"
                "        raise ValueError('neg')\n"
                "    return x + 1\n"
            ),
            "src/demo/mocked_only.py": "def fetch_remote() -> str:\n    return 'ok'\n",
            "src/demo/untested_mod.py": "def orphan_logic(v: int) -> int:\n    return v * 3\n",
            "src/demo/indirect_mod.py": "def helper_val() -> int:\n    return 7\n",
            "src/demo/caller_mod.py": (
                "from demo.indirect_mod import helper_val\n"
                "def run_caller() -> int:\n"
                "    return helper_val() + 1\n"
            ),
            # 1. Real-boundary test suite importing through facade + testing exception contract
            "tests/test_core_real.py": (
                "import tempfile\n"
                "import unittest\n"
                "from demo import compute\n"
                "from demo.caller_mod import run_caller\n\n"
                "class CoreRealTests(unittest.TestCase):\n"
                "    def test_compute_and_caller(self) -> None:\n"
                "        with tempfile.TemporaryDirectory():\n"
                "            self.assertEqual(compute(2), 3)\n"
                "            self.assertEqual(run_caller(), 8)\n"
                "            with self.assertRaises(ValueError):\n"
                "                compute(-1)\n"
            ),
            # 2. Near-duplicate suite overlapping test_core_real (Jaccard == 1.0)
            "tests/test_core_duplicate.py": (
                "import unittest\n"
                "from demo import compute\n"
                "from demo.caller_mod import run_caller\n\n"
                "class CoreDupTests(unittest.TestCase):\n"
                "    def test_same_contract(self) -> None:\n"
                "        self.assertEqual(compute(4), 5)\n"
                "        self.assertEqual(run_caller(), 8)\n"
                "        with self.assertRaises(ValueError):\n"
                "            compute(-2)\n"
            ),
            # 3. Mock-heavy suite (sole coverage for demo.mocked_only)
            "tests/test_mocked_only.py": (
                "import unittest\n"
                "from unittest import mock\n"
                "from demo.mocked_only import fetch_remote\n\n"
                "class FakeTransport:\n"
                "    pass\n\n"
                "class MockedOnlyTests(unittest.TestCase):\n"
                "    @mock.patch('demo.mocked_only.fetch_remote', return_value='fake')\n"
                "    def test_faked(self, _m) -> None:\n"
                "        _t = FakeTransport()\n"
                "        self.assertEqual(fetch_remote(), 'fake')\n"
            ),
            # 4. Mixed suite (1 real method + 1 patched method < 50% threshold -> wait: 1/3 mocked)
            "tests/test_mixed_suite.py": (
                "import tempfile\n"
                "import unittest\n"
                "from unittest import mock\n"
                "from demo.core_mod import compute\n\n"
                "class MixedTests(unittest.TestCase):\n"
                "    def test_real_one(self) -> None:\n"
                "        with tempfile.TemporaryDirectory():\n"
                "            self.assertEqual(compute(1), 2)\n"
                "    def test_real_two(self) -> None:\n"
                "        self.assertEqual(compute(5), 6)\n"
                "    def test_one_patch(self) -> None:\n"
                "        with mock.patch('demo.core_mod.compute', return_value=9):\n"
                "            self.assertEqual(compute(0), 9)\n"
            ),
        },
    )


class TestValueAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="managed-ai-w17-test-value-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_boundary_classification_overlap_clustering_and_unknowns(self) -> None:
        repo = _make_sample_project(self.tmp / "sample")
        report = analyze_test_value(repo, package_rel="src/demo", tests_rel="tests")

        self.assertEqual(report["format"], TEST_VALUE_REPORT_FORMAT)
        summary = report["summary"]
        self.assertEqual(summary["test_suite_count"], 4)
        self.assertEqual(summary["test_method_count"], 6)
        self.assertEqual(
            summary["by_boundary_class"],
            {"real-boundary": 2, "mixed": 1, "mock-heavy": 1},
        )

        by_id = {s["suite_id"]: s for s in report["suites"]}
        # Facade import `from demo import compute` resolves both `demo` and `demo.core_mod`!
        self.assertIn("demo.core_mod", by_id["test-test_core_real"]["exercises_modules"])
        self.assertIn("raises:ValueError", by_id["test-test_core_real"]["asserts_contract"])
        self.assertEqual(by_id["test-test_core_real"]["boundary_class"], "real-boundary")
        self.assertEqual(by_id["test-test_mixed_suite"]["boundary_class"], "mixed")
        self.assertEqual(by_id["test-test_mocked_only"]["boundary_class"], "mock-heavy")
        self.assertIn("fake:FakeTransport", by_id["test-test_mocked_only"]["mocks_boundary"])

        # Overlap cluster pairs test_core_real & test_core_duplicate (Jaccard == 1.0)
        self.assertEqual(summary["overlap_cluster_count"], 1)
        cluster = report["overlap_clusters"][0]
        self.assertEqual(cluster["members"], ["test-test_core_duplicate", "test-test_core_real"])
        self.assertEqual(cluster["min_jaccard"], 1.0)
        self.assertEqual(
            by_id["test-test_core_real"]["overlap_cluster_id"],
            cluster["cluster_id"],
        )

        # Verification unknowns: untested_mod (unreachable), indirect_mod (transitive_only),
        # mocked_only (mock-only); empty_sub/__init__.py skipped as 0 executable statements!
        unks = {u["module"]: u for u in report["verification_unknowns"]}
        self.assertNotIn("demo.empty_sub", unks)
        self.assertNotIn("demo.core_mod", unks)
        self.assertEqual(unks["demo.untested_mod"]["reason"], "untested")
        self.assertFalse(unks["demo.untested_mod"]["transitive_only"])
        self.assertEqual(unks["demo.indirect_mod"]["reason"], "untested")
        self.assertTrue(unks["demo.indirect_mod"]["transitive_only"])
        self.assertEqual(unks["demo.mocked_only"]["reason"], "mock-only")
        self.assertEqual(unks["demo.mocked_only"]["mock_heavy_suites"], ["test-test_mocked_only"])
        self.assertEqual(summary["strictly_unreachable_module_count"], 1)
        self.assertEqual(summary["mock_only_module_count"], 1)

    def test_cli_summary_only_and_invalid_threshold_error(self) -> None:
        repo = _make_sample_project(self.tmp / "cli-sample")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        ok = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ANALYZER_CLI),
                "--repo",
                str(repo),
                "--package-dir",
                "src/demo",
                "--summary-only",
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
        self.assertEqual(payload["format"], TEST_VALUE_REPORT_FORMAT)
        self.assertEqual(payload["summary"]["test_suite_count"], 4)

        bad = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ANALYZER_CLI),
                "--repo",
                str(repo),
                "--package-dir",
                "src/demo",
                "--overlap-threshold",
                "1.5",
            ],
            cwd=_REPO,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(bad.returncode, 2)
        err = json.loads(bad.stderr)
        self.assertEqual(err["status"], "error")


if __name__ == "__main__":
    unittest.main()
