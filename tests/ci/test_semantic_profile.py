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

    def test_write_baseline_rejects_symlink_or_junction_parent_and_leaf(self) -> None:
        repo = _write_pkg(self.tmp / "write-guard-repo", {"src/pkg/__init__.py": ""})
        real_out_dir = self.tmp / "real-out"
        real_out_dir.mkdir()
        link_out_dir = self.tmp / "link-out"
        if os.name == "nt":
            mk = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_out_dir), str(real_out_dir)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(mk.returncode, 0)
            self.addCleanup(
                lambda: subprocess.run(
                    ["cmd", "/c", "rmdir", str(link_out_dir)], capture_output=True
                )
            )
        else:
            link_out_dir.symlink_to(real_out_dir)

        # 1. Symlink/junction parent directory must be refused BEFORE resolving!
        through_link = link_out_dir / "baseline.json"
        p_parent = _run_cli(
            "--root",
            str(repo),
            "--package-dir",
            "src/pkg",
            "--write-baseline",
            str(through_link),
        )
        self.assertEqual(p_parent.returncode, 2)
        self.assertEqual(json.loads(p_parent.stderr)["status"], "error")
        self.assertFalse((real_out_dir / "baseline.json").exists())

        # 2. Non-ordinary leaf (junction/symlink/directory) must be refused BEFORE resolving!
        p_leaf_dir = _run_cli(
            "--root",
            str(repo),
            "--package-dir",
            "src/pkg",
            "--write-baseline",
            str(link_out_dir),
        )
        self.assertEqual(p_leaf_dir.returncode, 2)
        self.assertEqual(json.loads(p_leaf_dir.stderr)["status"], "error")

        # 3. File symlink leaf (when OS/privileges permit creating file symlinks)
        real_file = real_out_dir / "real-baseline.json"
        real_file.write_text('{"sentinel": true}\n', encoding="utf-8")
        link_file = real_out_dir / "link-baseline.json"
        try:
            link_file.symlink_to(real_file)
        except OSError:
            return
        p_leaf_file = _run_cli(
            "--root",
            str(repo),
            "--package-dir",
            "src/pkg",
            "--write-baseline",
            str(link_file),
        )
        self.assertEqual(p_leaf_file.returncode, 2)
        self.assertEqual(json.loads(p_leaf_file.stderr)["status"], "error")
        self.assertEqual(real_file.read_text(encoding="utf-8"), '{"sentinel": true}\n')

    def test_relationship_bottlenecks_sccs_bypasses_privates_and_nexus(self) -> None:
        repo = _write_pkg(
            self.tmp / "rel-repo",
            {
                "src/pkg/__init__.py": "",
                "src/pkg/alpha/__init__.py": "",
                "src/pkg/alpha/_internal.py": "HIDDEN = 7\n",
                "src/pkg/alpha/core.py": "\n".join(
                    [
                        "from pkg.beta.worker import run_worker",
                        "",
                        "def _secret() -> int:",
                        "    return 1",
                        "",
                        "def run_core() -> int:",
                        "    return run_worker() + _secret()",
                        "",
                    ]
                ),
                "src/pkg/beta/__init__.py": "",
                "src/pkg/beta/worker.py": "\n".join(
                    [
                        "from pkg.alpha import _internal",
                        "from pkg.alpha.core import _secret, run_core",
                        "from pkg.gamma.leaf import helper",
                        "",
                        "def run_worker() -> int:",
                        "    val = helper() + _secret() + _internal.HIDDEN",
                        "    return val + (0 if run_core else 1)",
                        "",
                    ]
                ),
                "src/pkg/gamma/__init__.py": "",
                "src/pkg/gamma/leaf.py": "\n".join(
                    [
                        "def helper() -> int:",
                        "    return 42",
                        "",
                    ]
                ),
                "src/pkg/top.py": "\n".join(
                    [
                        "from pkg.beta.worker import run_worker",
                        "from pkg.gamma.leaf import helper",
                        "",
                        "def entry() -> int:",
                        "    return run_worker() + helper()",
                        "",
                    ]
                ),
            },
        )
        profile = profile_repository(repo, package_rel="src/pkg", top_n=10)
        rel = profile["relationships"]
        rel_sum = rel["summary"]

        # 1. Tarjan SCC: alpha.core <-> beta.worker forms a module & package SCC
        self.assertEqual(rel_sum["module_scc_count"], 1)
        self.assertGreater(rel_sum["module_scc_tokens_locked"], 0)
        self.assertEqual(rel_sum["package_scc_count"], 1)
        self.assertEqual(
            rel["sccs"]["module"][0]["members"],
            ["pkg.alpha.core", "pkg.beta.worker"],
        )

        # 2. Cross-package _private imports: both symbol (_secret) and submodule (_internal)
        self.assertEqual(rel_sum["cross_package_private_imports"], 2)
        priv_syms = {row["symbol"] for row in rel["private_imports"]}
        self.assertEqual(priv_syms, {"_secret", "_internal"})
        self.assertTrue(all(row["scope"] == "cross_package" for row in rel["private_imports"]))

        # 3. Shortcut triangle: top -> beta.worker -> gamma.leaf AND top -> gamma.leaf
        self.assertGreaterEqual(rel_sum["shortcut_pair_count"], 1)
        self.assertGreaterEqual(rel_sum["cross_subpackage_shortcut_count"], 1)

        # 4. Spaghetti nexus & blast radius on beta.worker (fan_in=2, fan_out=3 -> nexus=6)
        by_mod = {m["module"]: m for m in profile["modules"]}
        self.assertEqual(by_mod["pkg.beta.worker"]["nexus_score"], 6)
        self.assertGreater(by_mod["pkg.beta.worker"]["blast_radius"], 0)

        # 5. Relationship budget breach fails check_budget and reports tightenable after fix
        strict_baseline = build_baseline(
            profile,
            budgets={
                "max_file_lines": 200,
                "max_cc": 10,
                "max_depth": 5,
                "max_working_set_tokens": 5000,
            },
            relationship_budgets={
                "max_module_sccs": 0,
                "max_subpackage_sccs": 0,
                "max_package_sccs": 0,
                "max_cross_package_private_imports": 0,
                "max_cross_subpackage_shortcuts": 0,
                "max_non_primitive_nexus": 10,
            },
        )
        # Baseline auto-captures actuals (SCC, private imports, shortcuts) so current passes:
        self.assertEqual(check_budget(profile, strict_baseline)["status"], "passed")
        self.assertGreaterEqual(
            strict_baseline["relationship_budgets"]["max_cross_subpackage_shortcuts"], 1
        )

        # Tightening relationship_budgets to 0 causes failure on SCC, private import, & shortcuts:
        zero_baseline = dict(strict_baseline)
        zero_baseline["relationship_budgets"] = {
            "max_module_sccs": 0,
            "max_subpackage_sccs": 0,
            "max_package_sccs": 0,
            "max_cross_package_private_imports": 0,
            "max_cross_subpackage_shortcuts": 0,
            "max_non_primitive_nexus": 10,
        }
        failed = check_budget(profile, zero_baseline)
        self.assertEqual(failed["status"], "failed")
        failed_metrics = {v["metric"] for v in failed["violations"]}
        self.assertIn("module_scc_count", failed_metrics)
        self.assertIn("cross_package_private_imports", failed_metrics)
        self.assertIn("cross_subpackage_shortcut_count", failed_metrics)

        # Removing the shortcut edge from top.py reports max_cross_subpackage_shortcuts tightenable:
        (repo / "src/pkg/top.py").write_text(
            "from pkg.beta.worker import run_worker\n\n"
            "def entry() -> int:\n"
            "    return run_worker()\n",
            encoding="utf-8",
            newline="\n",
        )
        reprofiled = profile_repository(repo, package_rel="src/pkg", top_n=10)
        self.assertEqual(
            reprofiled["relationships"]["summary"]["cross_subpackage_shortcut_count"], 0
        )
        tight_res = check_budget(reprofiled, strict_baseline)
        self.assertEqual(tight_res["status"], "passed")
        tight_metrics = {t["metric"] for t in tight_res["tightenable"]}
        self.assertIn("cross_subpackage_shortcut_count", tight_metrics)

        # Legacy 5-key relationship_budgets baselines (without max_cross_subpackage_shortcuts)
        # still load cleanly while unknown keys are rejected fail-closed.
        legacy_path = self.tmp / "legacy-rel-baseline.json"
        legacy_doc = dict(strict_baseline)
        legacy_doc["relationship_budgets"] = {
            k: v
            for k, v in strict_baseline["relationship_budgets"].items()
            if k != "max_cross_subpackage_shortcuts"
        }
        legacy_path.write_text(json.dumps(legacy_doc, indent=2) + "\n", encoding="utf-8")
        loaded_legacy = load_baseline(legacy_path)
        self.assertNotIn("max_cross_subpackage_shortcuts", loaded_legacy["relationship_budgets"])
        self.assertEqual(check_budget(profile, loaded_legacy)["status"], "passed")

    def test_phantom_cochange_graceful_without_git_and_detects_in_git_repo(self) -> None:
        repo = _write_pkg(
            self.tmp / "nogit-repo",
            {
                "src/pkg/__init__.py": "",
                "src/pkg/a.py": "VAL = 1\n",
                "src/pkg/b.py": "VAL = 2\n",
            },
        )
        # Without .git -> gracefully returns available=False, rc=0
        no_git = profile_repository(repo, package_rel="src/pkg", git_cochange=True)
        self.assertFalse(no_git["relationships"]["phantom_cochange"]["available"])

        git_bin = os.environ.get("GIT_BIN") or __import__("shutil").which("git")
        if git_bin is None:
            return
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        subprocess.run([git_bin, "init"], cwd=repo, env=env, check=True, capture_output=True)
        subprocess.run([git_bin, "config", "user.name", "test"], cwd=repo, env=env, check=True)
        subprocess.run(
            [git_bin, "config", "user.email", "test@example.com"], cwd=repo, env=env, check=True
        )
        for step in range(3):
            (repo / "src/pkg/a.py").write_text(f"VAL = {step}\n", encoding="utf-8")
            (repo / "src/pkg/b.py").write_text(f"VAL = {step + 10}\n", encoding="utf-8")
            subprocess.run([git_bin, "add", "."], cwd=repo, env=env, check=True)
            subprocess.run(
                [git_bin, "commit", "-m", f"step {step}"],
                cwd=repo,
                env=env,
                check=True,
                capture_output=True,
            )

        with_git = profile_repository(
            repo, package_rel="src/pkg", git_cochange=True, cochange_window=20, cochange_min=3
        )
        phantom = with_git["relationships"]["phantom_cochange"]
        self.assertTrue(phantom["available"])
        self.assertEqual(phantom["pair_count"], 1)
        self.assertEqual(phantom["pairs"][0]["cochange_count"], 3)
        self.assertEqual(
            {phantom["pairs"][0]["file_a"], phantom["pairs"][0]["file_b"]},
            {"src/pkg/a.py", "src/pkg/b.py"},
        )


if __name__ == "__main__":
    unittest.main()
