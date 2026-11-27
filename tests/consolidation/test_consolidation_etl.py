"""Dual-OS tests for scripts/consolidation/{ingest_repo,ingest_crw,consolidate}.py."""

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

from scripts.consolidation._common import file_digest_entry, write_digest_basis  # noqa: E402
from scripts.consolidation.consolidate import (  # noqa: E402
    apply_consolidation,
    consolidation_status,
    plan_consolidation,
    plan_obligations,
    reconcile_obligations,
    sync_digests,
)
from scripts.consolidation.ingest_crw import ingest_crw  # noqa: E402
from scripts.consolidation.ingest_repo import ingest_repository  # noqa: E402

from managed_ai_information.store.system import System  # noqa: E402

CONSOLIDATE_CLI = _REPO / "scripts/consolidation/consolidate.py"


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
            "src/demo/__init__.py": '"""Demo facade."""\n\n__all__ = ["run_app"]\n',
            "src/demo/leaf.py": "\n".join(
                [
                    "def compute(x: int) -> int:",
                    "    return x + 1",
                    "",
                ]
            ),
            "src/demo/app.py": "\n".join(
                [
                    "from demo.leaf import compute",
                    "",
                    "def run_app(x: int) -> int:",
                    "    return compute(x) * 2",
                    "",
                ]
            ),
        },
    )


def _make_sample_crw(root: Path) -> Path:
    observer = [
        {
            "id": "delivery/install-friction-risk.prompt.md",
            "family": "product-delivery-and-adoption-experience",
            "concern": "install friction",
            "cue": "Verify cold-start install commands.",
            "sources": ["delivery/install-friction-risk.prompt.md"],
        },
        {
            "id": "organization/entity-identity-risk.prompt.md",
            "family": "organization-and-communication",
            "concern": "entity identity",
            "cue": "Verify entity identity boundaries.",
            "sources": ["organization/entity-identity-risk.prompt.md"],
        },
    ]
    cases = [
        {
            "id": "O01",
            "purpose": "Check package boundary propagation.",
            "contract": "Changes propagate cleanly.",
            "expected": "defect",
        },
        {
            "id": "O48",
            "purpose": "Check CLI ergonomics and dry-run safety.",
            "contract": "Help exits 0 on stdout.",
            "expected": "clean",
        },
    ]
    return _write_files(
        root,
        {
            "risk-axis-reviewers/observer-dimensions.json": json.dumps(observer, indent=2) + "\n",
            "risk-axis-reviewers/review-dimensions.md": "# Review dimensions\n",
            "risk-axis-reviewers/semantic-review-contract.md": (
                "# Shared Semantic Review Contract\n"
            ),
            "risk-axis-reviewers/families/product-delivery-and-adoption-experience.prompt.md": (
                "# Product Delivery and Adoption Experience\n"
            ),
            "risk-axis-reviewers/families/organization-and-communication.prompt.md": (
                "# Organization and Communication\n"
            ),
            "risk-axis-reviewers/delivery/install-friction-risk.prompt.md": (
                "# Install Friction Risk\n"
                "See [entity](../organization/entity-identity-risk.prompt.md).\n"
            ),
            "risk-axis-reviewers/organization/entity-identity-risk.prompt.md": (
                "# Entity Identity Risk\n"
            ),
            "risk-axis-reviewers/evals/organization-boundary-controls-v1/cases.json": (
                json.dumps(cases, indent=2) + "\n"
            ),
            "risk-axis-reviewers/evals/organization-boundary-controls-v1/O01/README.md": "# O01\n",
            "risk-axis-reviewers/evals/organization-boundary-controls-v1/O48/assignment.md": (
                "# O48 assignment\n"
            ),
            "docs/review-escapes.md": "# Review Escapes\n",
            "docs/model-calibration.md": "# Model Calibration\n",
            "tools/semantic-profiler/README.md": "# semantic-profiler\n",
        },
    )


class ConsolidationETLTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="managed-ai-w14-consolidation-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _init_store(self, name: str) -> Path:
        store = self.tmp / name
        System.initialize(
            store,
            {"contribution": f"Test consolidation store {name}."},
            instance_id=name,
        )
        return store

    def test_repo_ingest_idempotent_staleness_and_soft_retire(self) -> None:
        repo = _make_sample_repo(self.tmp / "sample-repo")
        store = self._init_store("repo-store")

        # 1. Dry-run writes 0 records
        dry = ingest_repository(repo, store, package_rel="src/demo", dry_run=True)
        self.assertFalse(dry["applied"])
        self.assertEqual(dry["written_count"], 0)
        self.assertGreaterEqual(len(dry["added"]), 4)

        # 2. Apply ingests modules, facade, profile-head with 0 lint anomalies
        first = ingest_repository(repo, store, package_rel="src/demo", dry_run=False)
        self.assertTrue(first["applied"])
        self.assertEqual(first["lint_anomalies"], 0)
        self.assertGreaterEqual(first["written_count"], 4)

        # 3. Truthful dry-run on already-synced store reports 0 added and 0 updated (C68B-3)
        dry_synced = ingest_repository(repo, store, package_rel="src/demo", dry_run=True)
        self.assertFalse(dry_synced["applied"])
        self.assertEqual(dry_synced["added"], [])
        self.assertEqual(dry_synced["updated"], [])
        self.assertEqual(dry_synced["unchanged_count"], first["desired_record_count"])

        # Re-running apply on unchanged repo writes 0 records (100% idempotent)
        second = ingest_repository(repo, store, package_rel="src/demo", dry_run=False)
        self.assertFalse(second["applied"])
        self.assertEqual(second["written_count"], 0)
        self.assertEqual(second["unchanged_count"], first["desired_record_count"])

        # 4. Modify source file -> dry-run detects drift without mutating disk pin (C68B-3)
        leaf_pin_path = store / "work/sources/repo-digests/mod-leaf.json"
        pin_before = leaf_pin_path.read_bytes()
        (repo / "src/demo/leaf.py").write_text(
            "\n".join(["def compute(x: int) -> int:", "    return x + 2", ""]),
            encoding="utf-8",
            newline="\n",
        )
        dry_drift = ingest_repository(repo, store, package_rel="src/demo", dry_run=True)
        self.assertIn("mod-leaf", dry_drift["updated"])
        self.assertEqual(leaf_pin_path.read_bytes(), pin_before)

        # sync-digests + plan detects native basis_changes staleness
        synced = sync_digests(store, repo)
        self.assertIn("mod-leaf", synced["updated"])

        plan = plan_consolidation(store, repo, mode="repo", package_rel="src/demo")
        self.assertIn("mod-leaf", plan["stale_basis"])
        self.assertIn("mod-leaf", plan["changed"])

        # 5. Delete app.py -> sync-digests immediately surfaces deleted file in stale_basis (C68B-4)
        (repo / "src/demo/app.py").unlink()
        synced_del = sync_digests(store, repo)
        self.assertIn("mod-app", synced_del["updated"])
        self.assertIn("mod-app", consolidation_status(store)["stale_basis_records"])

        # apply reconciles leaf.py and soft-retires mod-app (active=False)
        applied = apply_consolidation(store, repo, mode="repo", package_rel="src/demo")
        self.assertTrue(applied["applied"])
        self.assertIn("mod-app", applied["retired"])
        self.assertEqual(applied["lint_anomalies"], 0)

        sys_obj = System(store)
        retired_rec = sys_obj.show("mod-app")
        self.assertFalse(retired_rec["active"])

        # 6. Status reports 0 stale records after apply
        status = consolidation_status(store)
        self.assertEqual(status["stale_basis_records"], [])

    def test_crw_ingest_synthetic_and_live_if_present(self) -> None:
        crw = _make_sample_crw(self.tmp / "sample-crw")
        store = self._init_store("crw-store")

        first = ingest_crw(crw, store, dry_run=False)
        self.assertTrue(first["applied"])
        self.assertEqual(first["counts"]["families"], 2)
        self.assertEqual(first["counts"]["constituents"], 2)
        self.assertEqual(first["counts"]["controls"], 2)
        self.assertEqual(first["lint_anomalies"], 0)

        # Idempotent re-run writes 0 records
        second = ingest_crw(crw, store, dry_run=False)
        self.assertFalse(second["applied"])
        self.assertEqual(second["written_count"], 0)

        # Verify cross-prompt link edge was captured cleanly
        sys_obj = System(store)
        install_rec = sys_obj.show("crw-dim-install-friction-risk")
        self.assertIn(
            {"relation": "bears-on", "target": "crw-dim-entity-identity-risk"},
            install_rec["relations"],
        )

        # If sibling real CRW checkout exists locally, verify full 12/40/48 ingest!
        real_crw = _REPO.parent.parent / "code-review-workflows" / "trunk"
        if (real_crw / "risk-axis-reviewers" / "observer-dimensions.json").is_file():
            real_store = self._init_store("real-crw-store")
            real_res = ingest_crw(real_crw, real_store, dry_run=False)
            self.assertTrue(real_res["applied"])
            self.assertGreaterEqual(real_res["counts"]["families"], 12)
            self.assertGreaterEqual(real_res["counts"]["constituents"], 40)
            self.assertGreaterEqual(real_res["counts"]["controls"], 48)
            self.assertEqual(real_res["lint_anomalies"], 0)

    def test_obligation_lifecycle_verified_closure_and_regression_detection(self) -> None:
        repo = _write_files(
            self.tmp / "obl-repo",
            {
                "src/demo/__init__.py": "",
                "src/demo/busy.py": "\n".join(
                    [
                        "def noisy(a: int, b: int, c: int) -> int:",
                        "    if a > 0:",
                        "        if b > 0:",
                        "            if c > 0 and a > b:",
                        "                return a + b + c",
                        "    return 0",
                        "",
                    ]
                ),
            },
        )
        baseline_path = self.tmp / "strict-baseline.json"
        baseline_path.write_text(
            json.dumps(
                {
                    "format": "managed-ai-semantic-baseline/1",
                    "package_dir": "src/demo",
                    "budgets": {
                        "max_file_lines": 50,
                        "max_cc": 3,
                        "max_depth": 2,
                        "max_working_set_tokens": 5000,
                    },
                    "allowances": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        store = self._init_store("obl-store")

        # 1. Create open obligations for budget violations
        planned = plan_obligations(
            store, repo, baseline_path=baseline_path, package_rel="src/demo", apply=True
        )
        self.assertTrue(planned["applied"])
        self.assertGreaterEqual(planned["violation_count"], 2)

        # 2. Reconcile BEFORE fixing code -> stays open (never auto-closes!)
        before_fix = reconcile_obligations(store, repo, package_rel="src/demo", apply=True)
        self.assertFalse(before_fix["applied"])
        self.assertEqual(before_fix["fulfilled"], [])
        self.assertGreaterEqual(len(before_fix["still_open"]), 2)

        # 3. Refactor busy.py below ceiling -> reconcile verifies and marks fulfilled!
        (repo / "src/demo/busy.py").write_text(
            "\n".join(["def noisy(a: int, b: int, c: int) -> int:", "    return a + b + c", ""]),
            encoding="utf-8",
            newline="\n",
        )
        after_fix = reconcile_obligations(store, repo, package_rel="src/demo", apply=True)
        self.assertTrue(after_fix["applied"])
        self.assertGreaterEqual(len(after_fix["fulfilled"]), 2)
        self.assertEqual(after_fix["still_open"], [])

        # Verify CLI status subcommand works cleanly with ZERO PYTHONPATH
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        cli = subprocess.run(
            [sys.executable, "-B", str(CONSOLIDATE_CLI), "status", "--store", str(store)],
            cwd=_REPO,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        status_doc = json.loads(cli.stdout)
        self.assertEqual(status_doc["open_obligations"], [])
        self.assertGreaterEqual(len(status_doc["fulfilled_obligations"]), 2)

        # 4. Re-introduce regression -> reconcile flags it in regressed!
        (repo / "src/demo/busy.py").write_text(
            "\n".join(
                [
                    "def noisy(a: int, b: int, c: int) -> int:",
                    "    if a > 0:",
                    "        if b > 0:",
                    "            if c > 0 and a > b:",
                    "                return a + b + c",
                    "    return 0",
                    "",
                ]
            ),
            encoding="utf-8",
            newline="\n",
        )
        reg = reconcile_obligations(store, repo, package_rel="src/demo", apply=False)
        self.assertGreaterEqual(len(reg["regressed"]), 2)

    def test_c68b_guards_path_traversal_symlink_missing_target_and_no_baseline_rel(self) -> None:
        # 1. C68B-1: path traversal & symlink/junction guards
        repo = _make_sample_repo(self.tmp / "guard-repo")
        store = self._init_store("guard-store")
        sys_obj = System(store)
        with self.assertRaises(ValueError):
            file_digest_entry(repo, "../outside.py")
        with self.assertRaises(ValueError):
            write_digest_basis(sys_obj, "../escape", source_repo="r", files=[])

        # Symlink/junction digest directory rejected
        real_dir = self.tmp / "real-digests"
        real_dir.mkdir()
        sources_dir = store / "work/sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        link_digests = sources_dir / "repo-digests"
        if os.name == "nt":
            mk = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_digests), str(real_dir)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(mk.returncode, 0)
            self.addCleanup(
                lambda: subprocess.run(
                    ["cmd", "/c", "rmdir", str(link_digests)], capture_output=True
                )
            )
        else:
            link_digests.symlink_to(real_dir)
        with self.assertRaises(ValueError):
            write_digest_basis(sys_obj, "mod-leaf", source_repo="r", files=[])

        # 2. C68B-2: missing target module NEVER falsely closes open obligation!
        missing_repo = _write_files(
            self.tmp / "missing-obl-repo",
            {
                "src/demo/__init__.py": "",
                "src/demo/a.py": "\n".join(
                    [
                        "from demo.b import fb",
                        "",
                        "def fa(x: int) -> int:",
                        "    return fb(x) if x > 0 else 0",
                        "",
                    ]
                ),
                "src/demo/b.py": "\n".join(
                    [
                        "from demo.a import fa",
                        "",
                        "def fb(x: int) -> int:",
                        "    return fa(x - 1) if x > 0 else 0",
                        "",
                    ]
                ),
            },
        )
        obl_store = self._init_store("rel-obl-store")
        # No-baseline plan_obligations MUST catch the SCC relationship breach!
        rel_plan = plan_obligations(obl_store, missing_repo, package_rel="src/demo", apply=True)
        self.assertGreaterEqual(rel_plan["violation_count"], 1)
        self.assertTrue(
            any("module_scc_count" in oid for oid in rel_plan["planned_obligations"]),
            rel_plan["planned_obligations"],
        )

        # Rename/delete target module -> reconcile MUST report missing_targets, NOT fulfilled!
        base_file = self.tmp / "tight.json"
        base_file.write_text(
            json.dumps(
                {
                    "format": "managed-ai-semantic-baseline/1",
                    "package_dir": "src/demo",
                    "budgets": {
                        "max_file_lines": 1,
                        "max_cc": 10,
                        "max_depth": 5,
                        "max_working_set_tokens": 5000,
                    },
                    "allowances": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        miss_store = self._init_store("miss-target-store")
        plan_obligations(
            miss_store, missing_repo, baseline_path=base_file, package_rel="src/demo", apply=True
        )
        (missing_repo / "src/demo/b.py").unlink()
        rec_res = reconcile_obligations(
            miss_store, missing_repo, package_rel="src/demo", apply=True
        )
        self.assertEqual(rec_res["fulfilled"], [])
        self.assertTrue(
            any(m["path"] == "src/demo/b.py" for m in rec_res["missing_targets"]),
            rec_res,
        )


if __name__ == "__main__":
    unittest.main()
