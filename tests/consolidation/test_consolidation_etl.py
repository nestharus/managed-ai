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

        # 7. Companions package auto-links to pkg-companions when present
        comp_repo = _write_files(
            self.tmp / "comp-repo",
            {
                "src/demo/__init__.py": "",
                "src/demo/companions/__init__.py": "",
                "src/demo/companions/contracts.py": "VALUE = 1\n",
            },
        )
        comp_store = self._init_store("comp-store")
        comp_sys = System(comp_store)
        comp_sys.put(
            {
                "id": "managed-ai-architecture",
                "kind": "note",
                "active": True,
                "content": {"text": "root"},
                "relations": [],
            },
            0,
        )
        comp_res = ingest_repository(comp_repo, comp_store, package_rel="src/demo", dry_run=False)
        self.assertEqual(comp_res["lint_anomalies"], 0)
        comp_prof = System(comp_store).graph_profile(roots=["managed-ai-architecture"])
        orphans = set(comp_prof["discoverability"]["orphan_sample_ids"])
        self.assertNotIn("mod-companions", orphans)
        self.assertNotIn("facade-companions", orphans)
        self.assertNotIn("mod-companions-contracts", orphans)
        pkg_comp = System(comp_store).show("pkg-companions")
        self.assertEqual(pkg_comp["kind"], "software-component")
        self.assertEqual(
            pkg_comp["relations"],
            [{"relation": "maps-to", "target": "managed-ai-architecture"}],
        )

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

        # Verify cross-prompt link edge was captured cleanly and CRW graph has 0 structural SCCs
        sys_obj = System(store)
        install_rec = sys_obj.show("crw-dim-install-friction-risk")
        self.assertIn(
            {"relation": "bears-on", "target": "crw-dim-entity-identity-risk"},
            install_rec["relations"],
        )
        crw_prof = sys_obj.graph_profile(roots=["crw-catalog"])
        self.assertEqual(crw_prof["bottlenecks"]["structural_scc_count"], 0)

        # If sibling real CRW checkout exists locally, verify 12/40/54 ingest + coverage manifest!
        real_crw = next(
            (
                p / "code-review-workflows" / "trunk"
                for p in _REPO.parents[:3]
                if (
                    p
                    / "code-review-workflows"
                    / "trunk"
                    / "risk-axis-reviewers"
                    / "observer-dimensions.json"
                ).is_file()
            ),
            None,
        )
        if real_crw is not None:
            real_store = self._init_store("real-crw-store")
            real_res = ingest_crw(real_crw, real_store, dry_run=False)
            self.assertTrue(real_res["applied"])
            self.assertGreaterEqual(real_res["counts"]["families"], 12)
            self.assertGreaterEqual(real_res["counts"]["constituents"], 40)
            self.assertGreaterEqual(real_res["counts"]["controls"], 54)
            self.assertEqual(real_res["counts"]["mapped_controls"], real_res["counts"]["controls"])
            self.assertEqual(
                real_res["counts"]["covered_constituents"]
                + len(real_res["counts"]["zero_control_constituents"]),
                real_res["counts"]["constituents"],
            )
            self.assertGreaterEqual(real_res["counts"]["covered_constituents"], 34)
            self.assertEqual(real_res["lint_anomalies"], 0)
            real_sys = System(real_store)
            real_prof = real_sys.graph_profile(roots=["crw-catalog"])
            self.assertEqual(real_prof["bottlenecks"]["structural_scc_count"], 0)
            o01 = real_sys.show("crw-ctrl-o01")
            self.assertIn(
                {"relation": "supports", "target": "crw-fam-organization-and-communication"},
                o01["relations"],
            )
            self.assertIn(
                {"relation": "supports", "target": "crw-dim-relationship-choreography-risk"},
                o01["relations"],
            )

        # Synthetic CRW coverage-manifest.json: maps O01 -> family + constituent, flags uncovered
        cov_manifest = {
            "format": "crw-control-coverage/1",
            "controls": {
                "O01": {
                    "families": ["organization-and-communication"],
                    "constituents": ["entity-identity-risk"],
                    "escape_class": "boundary-test-escape",
                }
            },
        }
        manifest_path = (
            crw
            / "risk-axis-reviewers/evals/organization-boundary-controls-v1/coverage-manifest.json"
        )
        manifest_path.write_text(json.dumps(cov_manifest, indent=2) + "\n", encoding="utf-8")
        cov_res = ingest_crw(crw, store, dry_run=False)
        self.assertTrue(cov_res["applied"])
        self.assertEqual(cov_res["lint_anomalies"], 0)
        self.assertEqual(cov_res["counts"]["mapped_controls"], 1)
        self.assertEqual(cov_res["counts"]["covered_constituents"], 1)
        self.assertEqual(
            cov_res["counts"]["zero_control_constituents"],
            ["crw-dim-install-friction-risk"],
        )
        o01_syn = System(store).show("crw-ctrl-o01")
        self.assertIn(
            {"relation": "supports", "target": "crw-fam-organization-and-communication"},
            o01_syn["relations"],
        )
        self.assertIn(
            {"relation": "supports", "target": "crw-dim-entity-identity-risk"},
            o01_syn["relations"],
        )
        self.assertEqual(o01_syn["content"]["escape_class"], "boundary-test-escape")
        cat_syn = System(store).show("crw-catalog")
        self.assertEqual(cat_syn["content"]["unknowns"], ["crw-dim-install-friction-risk"])
        self.assertEqual(
            System(store).graph_profile(roots=["crw-catalog"])["bottlenecks"][
                "structural_scc_count"
            ],
            0,
        )
        # Idempotent re-run after manifest ingest writes 0 records
        cov_res2 = ingest_crw(crw, store, dry_run=False)
        self.assertFalse(cov_res2["applied"])
        self.assertEqual(cov_res2["written_count"], 0)

        # Unknown constituent slug in coverage-manifest.json fails closed
        bad_manifest = {
            "format": "crw-control-coverage/1",
            "controls": {
                "O01": {
                    "families": ["organization-and-communication"],
                    "constituents": ["does-not-exist-risk"],
                }
            },
        }
        manifest_path.write_text(json.dumps(bad_manifest) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            ingest_crw(crw, store, dry_run=True)

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

    def test_include_tests_ingest_idempotent_lint_clean_and_soft_retire(self) -> None:
        repo = _write_files(
            self.tmp / "tests-etl-repo",
            {
                "src/demo/__init__.py": "",
                "src/demo/covered.py": "def inc(n: int) -> int:\n    return n + 1\n",
                "src/demo/uncovered.py": "def dec(n: int) -> int:\n    return n - 1\n",
                "tests/test_covered.py": (
                    "import unittest\n"
                    "from demo.covered import inc\n\n"
                    "class CoveredTests(unittest.TestCase):\n"
                    "    def test_inc(self) -> None:\n"
                    "        self.assertEqual(inc(1), 2)\n"
                ),
            },
        )
        store = self._init_store("tests-etl-store")

        first = apply_consolidation(
            store, repo, mode="repo", package_rel="src/demo", include_tests=True
        )
        self.assertTrue(first["applied"])
        self.assertEqual(first["lint_anomalies"], 0)
        self.assertIn("test-test_covered", first["added"])
        self.assertIn("unknown-cov-uncovered", first["added"])

        sys_obj = System(store)
        suite_rec = sys_obj.show("test-test_covered")
        self.assertEqual(suite_rec["kind"], "test-suite")
        self.assertIn({"relation": "qualifies", "target": "mod-covered"}, suite_rec["relations"])
        self.assertIn(
            {"relation": "evidenced-by", "target": "profile-head"}, suite_rec["relations"]
        )
        unk_rec = sys_obj.show("unknown-cov-uncovered")
        self.assertEqual(unk_rec["kind"], "verification-unknown")
        self.assertIn({"relation": "bears-on", "target": "mod-uncovered"}, unk_rec["relations"])

        # 100% idempotent on plan & apply
        plan_again = plan_consolidation(
            store, repo, mode="repo", package_rel="src/demo", include_tests=True
        )
        self.assertEqual(plan_again["added"], [])
        self.assertEqual(plan_again["changed"], [])
        self.assertEqual(plan_again["retired"], [])

        # Adding a real test for uncovered.py soft-retires unknown-cov-uncovered!
        (repo / "tests/test_uncovered.py").write_text(
            "import unittest\n"
            "from demo.uncovered import dec\n\n"
            "class UncoveredTests(unittest.TestCase):\n"
            "    def test_dec(self) -> None:\n"
            "        self.assertEqual(dec(2), 1)\n",
            encoding="utf-8",
            newline="\n",
        )
        second = apply_consolidation(
            store, repo, mode="repo", package_rel="src/demo", include_tests=True
        )
        self.assertTrue(second["applied"])
        self.assertEqual(second["lint_anomalies"], 0)
        self.assertIn("test-test_uncovered", second["added"])
        self.assertIn("unknown-cov-uncovered", second["retired"])
        self.assertFalse(System(store).show("unknown-cov-uncovered")["active"])

        # Reject --include-tests with --mode crw
        with self.assertRaises(ValueError):
            plan_consolidation(store, repo, mode="crw", include_tests=True)

    def test_external_graph_edges_ingestion_and_validation(self) -> None:
        repo = _make_sample_repo(self.tmp / "ext-edges-repo")
        store = self._init_store("ext-edges-store")
        edges_payload = {
            "schema_version": 1,
            "repo_label": "demo-ext",
            "commit": "3ec6c76",
            "node_count": 3,
            "edge_count": 5,
            "nodes": [],
            "edges": [
                {
                    "kind": "declares_mod",
                    "from": "src/demo/__init__.py",
                    "to": "src/demo/app.py",
                    "weight": 1,
                    "label": None,
                },
                {
                    "kind": "imports",
                    "from": "src/demo/app.py",
                    "to": "src/demo/leaf.py",
                    "weight": 1,
                    "label": None,
                },
                {
                    "kind": "bypasses",
                    "from": "src/demo/app.py",
                    "to": "src/demo/leaf.py",
                    "weight": 1,
                    "label": "layer_skip",
                },
                {
                    "kind": "shares_write_target",
                    "from": "src/demo/app.py",
                    "to": "src/demo/leaf.py",
                    "weight": 2,
                    "label": "state.json",
                },
                {
                    "kind": "in_scc",
                    "from": "src/demo/missing.py",
                    "to": "src/demo/leaf.py",
                    "weight": 1,
                    "label": "scc-1",
                },
            ],
        }
        edges_file = self.tmp / "graph-edges.json"
        edges_file.write_text(json.dumps(edges_payload, indent=2) + "\n", encoding="utf-8")

        applied = apply_consolidation(
            store,
            repo,
            mode="repo",
            package_rel="src/demo",
            external_graph_edges=edges_file,
        )
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["lint_anomalies"], 0)

        sys_obj = System(store)
        init_rec = sys_obj.show("mod-demo")
        self.assertIn({"relation": "depends-on", "target": "mod-app"}, init_rec["relations"])
        app_rec = sys_obj.show("mod-app")
        self.assertIn({"relation": "depends-on", "target": "mod-leaf"}, app_rec["relations"])
        self.assertIn({"relation": "bears-on", "target": "mod-leaf"}, app_rec["relations"])
        sigs = app_rec["content"]["external_graph_signals"]
        self.assertEqual([s["kind"] for s in sigs], ["bypasses", "shares_write_target"])
        head = sys_obj.show("profile-head")
        ext_sum = head["content"]["external_graph_edges"]
        self.assertEqual(ext_sum["total_edges"], 5)
        self.assertEqual(ext_sum["mapped_edges"], 4)
        self.assertEqual(ext_sum["unmapped_edges"], 1)

        # Idempotent plan reports 0 pending changes
        plan_again = plan_consolidation(
            store,
            repo,
            mode="repo",
            package_rel="src/demo",
            external_graph_edges=edges_file,
        )
        self.assertEqual(plan_again["added"], [])
        self.assertEqual(plan_again["changed"], [])
        self.assertEqual(plan_again["retired"], [])

        # Fail closed on unsupported edge kind or --mode crw
        bad_edges = self.tmp / "bad-edges.json"
        bad_edges.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "edges": [
                        {
                            "kind": "unknown_kind",
                            "from": "src/demo/app.py",
                            "to": "src/demo/leaf.py",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "unsupported external graph edge kind"):
            ingest_repository(
                repo, store, package_rel="src/demo", external_graph_edges=bad_edges, dry_run=True
            )
        with self.assertRaises(ValueError):
            plan_consolidation(store, repo, mode="crw", external_graph_edges=edges_file)


if __name__ == "__main__":
    unittest.main()
