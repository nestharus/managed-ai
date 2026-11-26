"""Dual-OS tests for scripts/ci/semantic_profile.py and scripts/ci/semantic-baseline.json."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.ci.semantic_profile import (  # noqa: E402
    BASELINE_FORMAT,
    PROFILE_FORMAT,
    build_baseline,
    check_budget,
    load_baseline,
    profile_repository,
)

SCRIPT = _REPO / "scripts/ci/semantic_profile.py"
BASELINE = _REPO / "scripts/ci/semantic-baseline.json"


def _run_cli(*args: str, cwd: Path = _REPO) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _write_pkg(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    return root


class SemanticProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="managed-ai-w12-semantic-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_repository_baseline_passes_cli_budget_gate(self) -> None:
        proc = _run_cli("--check-budget", "--baseline", str(BASELINE))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["format"], PROFILE_FORMAT)
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["violation_count"], 0)
        self.assertGreater(payload["summary"]["module_count"], 50)
        self.assertGreater(payload["summary"]["total_lines"], 10000)

    def test_working_set_closure_and_coupling_metrics(self) -> None:
        repo = _write_pkg(
            self.tmp / "closure-repo",
            {
                "src/pkg/__init__.py": '"""Package root."""\n',
                "src/pkg/leaf.py": "\n".join(
                    [
                        "def leaf_value(x: int) -> int:",
                        "    return x + 1",
                        "",
                    ]
                ),
                "src/pkg/mid.py": "\n".join(
                    [
                        "from .leaf import leaf_value",
                        "",
                        "def mid_value(x: int) -> int:",
                        "    return leaf_value(x) * 2",
                        "",
                    ]
                ),
                "src/pkg/top.py": "\n".join(
                    [
                        "import pkg.mid",
                        "",
                        "def top_value(x: int) -> int:",
                        "    return pkg.mid.mid_value(x)",
                        "",
                    ]
                ),
            },
        )
        profile = profile_repository(repo, package_rel="src/pkg", top_n=5)
        by_path = {m["path"]: m for m in profile["modules"]}

        leaf = by_path["src/pkg/leaf.py"]
        mid = by_path["src/pkg/mid.py"]
        top = by_path["src/pkg/top.py"]

        self.assertEqual(leaf["fan_in"], 1)
        self.assertEqual(leaf["fan_out"], 0)
        self.assertEqual(leaf["working_set_modules"], 1)
        self.assertEqual(leaf["working_set_tokens"], leaf["tokens"])

        self.assertEqual(mid["fan_in"], 1)
        self.assertEqual(mid["fan_out"], 1)
        self.assertEqual(mid["working_set_modules"], 2)
        self.assertEqual(mid["working_set_tokens"], mid["tokens"] + leaf["tokens"])

        self.assertEqual(top["fan_in"], 0)
        self.assertEqual(top["fan_out"], 1)
        self.assertEqual(top["working_set_modules"], 3)
        self.assertEqual(
            top["working_set_tokens"],
            top["tokens"] + mid["tokens"] + leaf["tokens"],
        )

    def test_cyclomatic_complexity_and_nesting_depth_calculation(self) -> None:
        repo = _write_pkg(
            self.tmp / "cc-repo",
            {
                "src/pkg/__init__.py": "",
                "src/pkg/logic.py": "\n".join(
                    [
                        "def complex_fn(items: list[int], flag: bool) -> int:",
                        "    def helper(v: int) -> int:",
                        "        if v > 0:",
                        "            return v",
                        "        return 0",
                        "    total = 0",
                        "    for item in items:",
                        "        if item > 0 and flag:",
                        "            try:",
                        "                if item > 10:",
                        "                    total += helper(item)",
                        "                elif item > 5:",
                        "                    total += 1",
                        "            except ValueError:",
                        "                total -= 1",
                        "    return total",
                        "",
                    ]
                ),
            },
        )
        profile = profile_repository(repo, package_rel="src/pkg", top_n=5)
        logic = {m["path"]: m for m in profile["modules"]}["src/pkg/logic.py"]
        # complex_fn: base(1) + for(1) + if(1) + and(1) + if(1) + elif(1) + except(1) = 7
        # nested helper() is measured separately (cc=2) and not double-counted in complex_fn.
        self.assertEqual(logic["max_cc"], 7)
        self.assertEqual(logic["max_cc_function"], "complex_fn")
        # Nesting depth: for(1) -> if(2) -> try(3) -> if/elif(4)
        self.assertEqual(logic["max_depth"], 4)
        self.assertEqual(logic["max_depth_function"], "complex_fn")

    def test_write_baseline_budget_regression_and_tightening(self) -> None:
        repo = _write_pkg(
            self.tmp / "budget-repo",
            {
                "src/pkg/__init__.py": "",
                "src/pkg/mod.py": "\n".join(
                    [
                        "def run(x: int) -> int:",
                        "    if x > 0:",
                        "        return x",
                        "    return 0",
                        "",
                    ]
                ),
            },
        )
        baseline_path = self.tmp / "baseline.json"
        write_proc = _run_cli(
            "--root",
            str(repo),
            "--package-dir",
            "src/pkg",
            "--write-baseline",
            str(baseline_path),
            "--budget-lines",
            "20",
            "--budget-cc",
            "3",
            "--budget-depth",
            "2",
            "--budget-working-set-tokens",
            "500",
        )
        self.assertEqual(write_proc.returncode, 0, write_proc.stderr)
        written = load_baseline(baseline_path)
        self.assertEqual(written["format"], BASELINE_FORMAT)
        self.assertEqual(written["allowances"], {})

        # Pass while within budget
        ok_proc = _run_cli("--root", str(repo), "--check-budget", "--baseline", str(baseline_path))
        self.assertEqual(ok_proc.returncode, 0, ok_proc.stderr)

        # Introduce a complexity + depth regression -> must fail rc=1
        (repo / "src/pkg/mod.py").write_text(
            "\n".join(
                [
                    "def run(x: int, y: int) -> int:",
                    "    if x > 0:",
                    "        if y > 0:",
                    "            if x > y and y > 1:",
                    "                return x",
                    "    return 0",
                    "",
                ]
            ),
            encoding="utf-8",
            newline="\n",
        )
        fail_proc = _run_cli(
            "--root", str(repo), "--check-budget", "--baseline", str(baseline_path)
        )
        self.assertEqual(fail_proc.returncode, 1)
        report = json.loads(fail_proc.stderr)
        self.assertEqual(report["status"], "failed")
        metrics = {v["metric"] for v in report["violations"]}
        self.assertIn("max_cc", metrics)
        self.assertIn("max_depth", metrics)

        # Baseline with allowance passes and reports tightenable when refactored back down
        profile_big = profile_repository(repo, package_rel="src/pkg")
        baseline_allow = build_baseline(
            profile_big,
            budgets={
                "max_file_lines": 20,
                "max_cc": 3,
                "max_depth": 2,
                "max_working_set_tokens": 500,
            },
        )
        self.assertIn("src/pkg/mod.py", baseline_allow["allowances"])
        (repo / "src/pkg/mod.py").write_text(
            "\n".join(["def run(x: int) -> int:", "    return x", ""]),
            encoding="utf-8",
            newline="\n",
        )
        profile_small = profile_repository(repo, package_rel="src/pkg")
        tight = check_budget(profile_small, baseline_allow)
        self.assertEqual(tight["status"], "passed")
        self.assertGreaterEqual(tight["tightenable_count"], 2)

    def test_error_boundaries_and_strict_json_guards(self) -> None:
        # Missing --baseline with --check-budget -> exit 2
        p1 = _run_cli("--check-budget")
        self.assertEqual(p1.returncode, 2)
        self.assertEqual(json.loads(p1.stderr)["status"], "error")

        # Conflicting --check-budget and --write-baseline -> exit 2
        p2 = _run_cli(
            "--check-budget",
            "--baseline",
            str(BASELINE),
            "--write-baseline",
            str(self.tmp / "out.json"),
        )
        self.assertEqual(p2.returncode, 2)
        self.assertEqual(json.loads(p2.stderr)["status"], "error")

        # Duplicate JSON key in baseline -> exit 2 (strict_json_loads)
        dup_path = self.tmp / "dup.json"
        dup_path.write_text(
            '{"format":"managed-ai-semantic-baseline/1","format":"managed-ai-semantic-baseline/1"}\n',
            encoding="utf-8",
        )
        p3 = _run_cli("--check-budget", "--baseline", str(dup_path))
        self.assertEqual(p3.returncode, 2)
        self.assertIn("duplicate", json.loads(p3.stderr)["error"].lower())

        # Symlink / junction root rejected -> exit 2
        real_dir = _write_pkg(self.tmp / "real-root", {"src/pkg/__init__.py": ""})
        link_dir = self.tmp / "link-root"
        if os.name == "nt":
            mk = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_dir), str(real_dir)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(mk.returncode, 0)
            self.addCleanup(
                lambda: subprocess.run(["cmd", "/c", "rmdir", str(link_dir)], capture_output=True)
            )
        else:
            link_dir.symlink_to(real_dir)
        p4 = _run_cli("--root", str(link_dir), "--package-dir", "src/pkg")
        self.assertEqual(p4.returncode, 2)
        self.assertEqual(json.loads(p4.stderr)["status"], "error")


if __name__ == "__main__":
    unittest.main()
