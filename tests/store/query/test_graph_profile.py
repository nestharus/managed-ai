"""Regression tests for native knowledge-graph health profiling and continuous alerting."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if not __package__:
    _SRC = Path(__file__).resolve().parents[3] / "src"
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))

from managed_ai_information.core.formats import BATCH_FORMAT
from managed_ai_information.store.query.graph_profile import (
    GRAPH_PROFILE_BASELINE_FORMAT,
    GRAPH_PROFILE_CONFIG_FORMAT,
    GRAPH_PROFILE_FORMAT,
    write_graph_baseline,
)
from managed_ai_information.store.records.record_batch import put_batch
from managed_ai_information.store.system import System

SRC_DIR = Path(__file__).resolve().parents[3] / "src"


def _put(
    system: System,
    ident: str,
    kind: str,
    *,
    relations: list[dict[str, str]] | None = None,
    active: bool = True,
    text: str = "body",
) -> None:
    rec = {
        "id": ident,
        "kind": kind,
        "active": active,
        "content": {"text": text},
        "relations": relations or [],
    }
    system.put(rec, 0)


class GraphProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = self.base / "store"
        System.initialize(
            self.store,
            {"contribution": "Test knowledge graph health profiling."},
            instance_id="gp-inst",
        )
        self.system = System(self.store)

    def _build_rich_graph(self) -> None:
        # Governing root 'goal' already exists at revision 1.
        # Chain from goal -> frontier-1 -> claim-a -> claim-b -> claim-c (hops 1..4)
        _put(
            self.system,
            "frontier-1",
            "frontier",
            relations=[{"relation": "maps-to", "target": "goal"}],
            text="frontier anchor " * 20,
        )
        _put(
            self.system,
            "claim-a",
            "claim",
            relations=[
                {"relation": "depends-on", "target": "frontier-1"},
                {"relation": "qualifies", "target": "claim-b"},
            ],
            text="claim a " * 40,
        )
        _put(
            self.system,
            "claim-b",
            "claim",
            relations=[
                {"relation": "evidenced-by", "target": "claim-a"},
                {"relation": "supports", "target": "frontier-1"},
            ],
            text="claim b " * 40,
        )
        _put(
            self.system,
            "claim-c",
            "claim",
            relations=[{"relation": "depends-on", "target": "claim-b"}],
            text="claim c " * 30,
        )
        # Orphan island of 2 active records disconnected from roots
        _put(
            self.system,
            "orphan-1",
            "claim",
            relations=[{"relation": "depends-on", "target": "orphan-2"}],
            text="orphan one",
        )
        _put(
            self.system,
            "orphan-2",
            "claim",
            relations=[{"relation": "depends-on", "target": "orphan-1"}],
            text="orphan two",
        )
        # Qualification desert: high fan-in (>= 3) with zero qualifying edges
        _put(
            self.system,
            "desert-hub",
            "synthesis",
            relations=[{"relation": "maps-to", "target": "goal"}],
            text="desert hub",
        )
        for idx in range(3):
            _put(
                self.system,
                f"desert-dep-{idx}",
                "note",
                relations=[{"relation": "depends-on", "target": "desert-hub"}],
                text=f"dependent {idx}",
            )
        # Unresolved contradicts pair vs resolved contradicts pair
        _put(
            self.system,
            "contra-u",
            "claim",
            relations=[
                {"relation": "maps-to", "target": "goal"},
                {"relation": "contradicts", "target": "contra-v"},
            ],
        )
        _put(
            self.system,
            "contra-v",
            "claim",
            relations=[{"relation": "maps-to", "target": "goal"}],
        )
        _put(
            self.system,
            "res-u",
            "claim",
            relations=[
                {"relation": "maps-to", "target": "goal"},
                {"relation": "contradicts", "target": "res-v"},
            ],
        )
        _put(
            self.system,
            "res-v",
            "claim",
            relations=[{"relation": "maps-to", "target": "goal"}],
        )
        _put(
            self.system,
            "res-mediator",
            "synthesis",
            relations=[
                {"relation": "qualifies", "target": "res-u"},
                {"relation": "scope-restricts", "target": "res-v"},
            ],
        )
        # Superseded inactive record: one legitimate 'supersedes' edge and one bypass 'depends-on'
        _put(self.system, "old-head", "claim", active=False, text="retired claim")
        _put(
            self.system,
            "new-head",
            "claim",
            relations=[
                {"relation": "maps-to", "target": "goal"},
                {"relation": "supersedes", "target": "old-head"},
            ],
        )
        _put(
            self.system,
            "bypass-caller",
            "claim",
            relations=[
                {"relation": "maps-to", "target": "goal"},
                {"relation": "depends-on", "target": "old-head"},
            ],
        )
        self.system.reindex()

    def test_mmap_and_unindexed_parity_all_metric_families(self) -> None:
        self._build_rich_graph()

        # Zero sealed object reads permitted during graph profiling
        with patch.object(
            self.system.ex.objects,
            "read",
            side_effect=AssertionError("graph profile must never read sealed objects"),
        ):
            indexed_report = self.system.graph_profile()

        self.assertEqual(indexed_report["format"], GRAPH_PROFILE_FORMAT)
        self.assertTrue(indexed_report["indexed"])

        # Discoverability & orphans
        disc = indexed_report["discoverability"]
        self.assertEqual(disc["root_selection"], "governing_kinds")
        self.assertIn("goal", disc["root_ids"])
        self.assertIn("frontier-1", disc["root_ids"])
        self.assertEqual(disc["orphan_count"], 2)
        self.assertEqual(disc["orphan_island_count"], 1)
        self.assertEqual(disc["orphan_islands"][0]["sample_ids"], ["orphan-1", "orphan-2"])
        self.assertGreater(disc["discoverability_2hop"], 0.5)
        self.assertLess(disc["discoverability_4hop"], 1.0)
        self.assertIsNotNone(disc["p50_hops"])
        self.assertIsNotNone(disc["p95_hops"])

        # Density, clustering, qualification deserts, unresolved contradicts, working set
        dens = indexed_report["density_and_qualification"]
        self.assertGreater(dens["structural_edge_count"], 0)
        self.assertGreater(dens["qualifying_edge_count"], 0)
        self.assertGreater(dens["predicate_mix_entropy"], 0.0)
        self.assertGreater(dens["clustering"]["avg_local_clustering"], 0.0)
        self.assertEqual(dens["per_predicate"]["depends-on"]["category"], "structural")
        self.assertEqual(dens["per_predicate"]["qualifies"]["category"], "qualifying")

        desert_ids = [d["id"] for d in dens["qualification_deserts"]]
        self.assertIn("desert-hub", desert_ids)
        self.assertNotIn("claim-a", desert_ids)

        contra = dens["contradicts"]
        self.assertEqual(contra["total_active_contradicts_edges"], 2)
        self.assertEqual(contra["unresolved_contradicts_edge_count"], 1)
        self.assertEqual(contra["unresolved_contradicts_cluster_count"], 1)
        self.assertEqual(
            contra["unresolved_contradicts_clusters"][0]["node_ids"], ["contra-u", "contra-v"]
        )

        ws = dens["working_set"]
        self.assertGreater(ws["hop1_bytes_p50"], 0)
        self.assertGreaterEqual(ws["hop2_bytes_p95"], ws["hop1_bytes_p95"])
        self.assertGreaterEqual(ws["signal_to_byte_ratio_mean"], 0.0)

        # Bottlenecks: structural SCC (orphan-1 <-> orphan-2) + superseded bypass (bypass-caller)
        bot = indexed_report["bottlenecks"]
        self.assertEqual(bot["structural_scc_count"], 1)
        self.assertEqual(bot["structural_sccs"][0]["node_ids"], ["orphan-1", "orphan-2"])
        self.assertGreater(bot["structural_scc_locked_bytes"], 0)

        self.assertEqual(bot["superseded_head_bypass_count"], 1)
        self.assertEqual(bot["actionable_bypass_count"], 1)
        self.assertEqual(bot["historical_inactive_ref_count"], 0)
        self.assertEqual(bot["superseded_head_bypasses"][0]["from"], "bypass-caller")
        self.assertEqual(bot["superseded_head_bypasses"][0]["to"], "old-head")
        self.assertTrue(bot["superseded_head_bypasses"][0]["has_active_successor"])
        self.assertEqual(bot["superseded_head_bypasses"][0]["active_successors"], ["new-head"])

        # Remove HeadIndex to verify exact parity with unindexed scan_records() fallback
        self.system.record_store.index.current_path.unlink(missing_ok=True)
        self.system.record_store.index.path.unlink(missing_ok=True)
        unindexed_report = self.system.graph_profile()
        self.assertFalse(unindexed_report["indexed"])

        indexed_norm = dict(indexed_report, indexed=False)
        self.assertEqual(unindexed_report, indexed_norm)

    def test_explicit_roots_and_invalid_root_rejection(self) -> None:
        self._build_rich_graph()
        report = self.system.graph_profile(roots=["orphan-1"])
        disc = report["discoverability"]
        self.assertEqual(disc["root_selection"], "explicit")
        self.assertEqual(disc["root_ids"], ["orphan-1"])
        self.assertEqual(disc["reachable_active_count"], 2)

        with self.assertRaises(ValueError):
            self.system.graph_profile(roots=["does-not-exist"])
        with self.assertRaises(ValueError):
            self.system.graph_profile(roots=["old-head"])

    def test_phantom_cochange_coupling_from_batch_receipts(self) -> None:
        _put(
            self.system,
            "co-a",
            "claim",
            relations=[{"relation": "maps-to", "target": "goal"}],
            text="v1",
        )
        _put(
            self.system,
            "co-b",
            "claim",
            relations=[{"relation": "maps-to", "target": "goal"}],
            text="v1",
        )
        _put(
            self.system,
            "co-linked",
            "claim",
            relations=[
                {"relation": "maps-to", "target": "goal"},
                {"relation": "qualifies", "target": "co-a"},
            ],
            text="v1",
        )

        # Two separate batches co-revising co-a, co-b, and co-linked
        for rev in (1, 2):
            ops = []
            for ident in ("co-a", "co-b", "co-linked"):
                cur, _ = self.system.record_store.read(ident)
                ops.append(
                    {
                        "id": ident,
                        "expected_revision": rev,
                        "expected_sha256": cur["sha256"],
                        "mode": "patch",
                        "replace_content_fields": {"text": f"v{rev + 1}"},
                    }
                )
            put_batch(
                self.system,
                {
                    "format": BATCH_FORMAT,
                    "id": f"cochange-batch-{rev}",
                    "operations": ops,
                },
            )
        self.system.reindex()

        without_flag = self.system.graph_profile(include_cochange=False)
        self.assertFalse(without_flag["bottlenecks"]["phantom_cochange"]["enabled"])
        self.assertEqual(without_flag["bottlenecks"]["phantom_cochange"]["phantom_pair_count"], 0)

        with_flag = self.system.graph_profile(include_cochange=True)
        co = with_flag["bottlenecks"]["phantom_cochange"]
        self.assertTrue(co["enabled"])
        self.assertEqual(co["batches_scanned"], 2)
        pair_set = {(p["a"], p["b"]) for p in co["phantom_pairs"]}
        # co-a and co-b have no explicit edge -> phantom pair!
        self.assertIn(("co-a", "co-b"), pair_set)
        # co-b and co-linked have no explicit edge -> phantom pair!
        self.assertIn(("co-b", "co-linked"), pair_set)
        # co-a and co-linked DO have an explicit 'qualifies' edge -> NOT phantom!
        self.assertNotIn(("co-a", "co-linked"), pair_set)

    def test_baseline_cli_check_alerts_and_symlink_rejection(self) -> None:
        # Healthy initial graph
        _put(
            self.system,
            "clean-claim",
            "claim",
            relations=[{"relation": "evidenced-by", "target": "goal"}],
        )
        self.system.reindex()

        baseline_file = self.base / "graph-baseline.json"
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(SRC_DIR)

        def _run_cli(*extra: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "managed_ai_information.store.manage_store",
                    "--root",
                    str(self.store),
                    "graph",
                    "profile",
                    *extra,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=30,
            )

        # Write baseline on clean graph + --check-alerts -> exit 0
        proc_write = _run_cli("--write-baseline", str(baseline_file), "--check-alerts")
        self.assertEqual(proc_write.returncode, 0, proc_write.stderr)
        out_write = json.loads(proc_write.stdout)
        self.assertTrue(out_write["alerts_passed"])
        self.assertEqual(out_write["alert_count"], 0)
        saved = json.loads(baseline_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["format"], GRAPH_PROFILE_BASELINE_FORMAT)

        # Now introduce a structural SCC + orphan regression
        _put(
            self.system,
            "reg-a",
            "claim",
            relations=[{"relation": "depends-on", "target": "reg-b"}],
        )
        _put(
            self.system,
            "reg-b",
            "claim",
            relations=[{"relation": "depends-on", "target": "reg-a"}],
        )
        self.system.reindex()

        baseline_before = baseline_file.read_bytes()
        proc_fail = _run_cli(
            "--baseline",
            str(baseline_file),
            "--write-baseline",
            str(baseline_file),
            "--check-alerts",
        )
        self.assertEqual(
            proc_fail.returncode, 1, f"stdout={proc_fail.stdout}\nstderr={proc_fail.stderr}"
        )
        out_fail = json.loads(proc_fail.stdout)
        self.assertIsNone(out_fail["baseline_written"])
        self.assertEqual(out_fail["baseline_write_skipped_reason"], "alerts_triggered")
        self.assertEqual(baseline_file.read_bytes(), baseline_before)
        self.assertFalse(out_fail["alerts_passed"])
        self.assertGreater(out_fail["alert_count"], 0)
        rules = {a["rule"] for a in out_fail["alerts"]}
        self.assertIn("structural_scc_regression", rules)
        self.assertIn("orphan_count_increase", rules)
        for alert in out_fail["alerts"]:
            self.assertEqual(
                set(alert.keys()),
                {"severity", "rule", "metric", "baseline", "current", "offending_ids"},
            )

        # Reject symlinked/junctioned baseline ancestor directory
        real_dir = self.base / "real-out"
        real_dir.mkdir()
        linked_dir = self.base / "linked-out"
        if os.name == "nt":
            mk = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked_dir), str(real_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(mk.returncode, 0, mk.stdout + mk.stderr)
            self.addCleanup(
                lambda p=linked_dir: subprocess.run(
                    ["cmd", "/c", "rmdir", str(p)], capture_output=True
                )
            )
        else:
            os.symlink(real_dir, linked_dir)

        with self.assertRaises(ValueError):
            write_graph_baseline(linked_dir / "bad.json", out_write)

    def test_domain_calibration_flags_config_and_navigational_separation(self) -> None:
        # Domain-neutral navigational hub ("topic-map") with high fan-in (3) and 0 qualifying
        # edges must NOT be flagged as a substantive qualification desert by default, while
        # domain-specific kinds ("research-topic-map") stay neutral until calibrated via config.
        _put(
            self.system,
            "neutral-map-1",
            "topic-map",
            relations=[{"relation": "maps-to", "target": "goal"}],
        )
        _put(
            self.system,
            "topic-map-1",
            "research-topic-map",
            relations=[{"relation": "maps-to", "target": "goal"}],
        )
        _put(
            self.system,
            "rclaim-1",
            "research-claim",
            relations=[
                {"relation": "maps-to", "target": "neutral-map-1"},
                {"relation": "maps-to", "target": "topic-map-1"},
                {"relation": "grounds-claim", "target": "rclaim-2"},
            ],
        )
        _put(
            self.system,
            "rclaim-2",
            "research-claim",
            relations=[
                {"relation": "maps-to", "target": "neutral-map-1"},
                {"relation": "maps-to", "target": "topic-map-1"},
            ],
        )
        _put(
            self.system,
            "rclaim-3",
            "research-claim",
            relations=[
                {"relation": "maps-to", "target": "neutral-map-1"},
                {"relation": "maps-to", "target": "topic-map-1"},
            ],
        )
        self.system.reindex()

        default_rep = self.system.graph_profile()
        dens_def = default_rep["density_and_qualification"]
        self.assertEqual(dens_def["navigational_active_count"], 1)
        self.assertEqual(dens_def["per_kind"]["topic-map"]["role"], "navigational")
        self.assertEqual(dens_def["per_kind"]["research-topic-map"]["role"], "other")
        self.assertNotIn("neutral-map-1", [d["id"] for d in dens_def["qualification_deserts"]])
        self.assertEqual(dens_def["per_predicate"]["grounds-claim"]["category"], "other")

        # Domain-calibrated config file + CLI override
        cfg_path = self.base / "research-profile-config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "format": GRAPH_PROFILE_CONFIG_FORMAT,
                    "claim_kinds": ["research-claim"],
                    "navigational_kinds": ["research-topic-map"],
                    "qualifying_predicates": ["grounds-claim", "qualifies"],
                    "structural_predicates": ["maps-to", "depends-on"],
                }
            ),
            encoding="utf-8",
        )
        cal_rep = self.system.graph_profile(profile_config_path=cfg_path)
        dens_cal = cal_rep["density_and_qualification"]
        self.assertEqual(dens_cal["per_kind"]["research-topic-map"]["role"], "navigational")
        self.assertNotIn("topic-map-1", [d["id"] for d in dens_cal["qualification_deserts"]])
        self.assertEqual(dens_cal["per_kind"]["research-claim"]["role"], "claim")
        self.assertEqual(dens_cal["per_predicate"]["grounds-claim"]["category"], "qualifying")
        self.assertGreater(dens_cal["qualification_density"], 0.0)
        self.assertEqual(dens_cal["qualification_target_count"], 3)
        self.assertIn(f"config:{cfg_path}", cal_rep["calibration"]["sources"])

        # CLI flags override config and reject overlapping structural/qualifying predicates
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(SRC_DIR)
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "managed_ai_information.store.manage_store",
                "--root",
                str(self.store),
                "graph",
                "profile",
                "--profile-config",
                str(cfg_path),
                "--claim-kinds",
                "research-claim",
                "--qualifying-predicates",
                "grounds-claim",
                "--structural-predicates",
                "maps-to",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cli_rep = json.loads(proc.stdout)
        self.assertIn("cli", cli_rep["calibration"]["sources"])
        self.assertEqual(
            cli_rep["density_and_qualification"]["per_predicate"]["grounds-claim"]["category"],
            "qualifying",
        )

        # Overlapping structural & qualifying predicates fail closed
        with self.assertRaises(ValueError):
            self.system.graph_profile(
                structural_predicates=["maps-to"],
                qualifying_predicates=["maps-to"],
            )

        # Unknown field in profile config fails closed
        bad_cfg = self.base / "bad-config.json"
        bad_cfg.write_text(
            json.dumps({"format": GRAPH_PROFILE_CONFIG_FORMAT, "unknown_key": []}),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            self.system.graph_profile(profile_config_path=bad_cfg)

        # Inverse qualified-by resolves contradicts cluster + historical vs actionable bypass split
        _put(
            self.system,
            "inv-a",
            "claim",
            relations=[
                {"relation": "maps-to", "target": "goal"},
                {"relation": "contradicts", "target": "inv-b"},
            ],
        )
        _put(self.system, "inv-b", "claim", relations=[{"relation": "maps-to", "target": "goal"}])
        _put(
            self.system,
            "inv-med",
            "synthesis",
            relations=[
                {"relation": "qualified-by", "target": "inv-a"},
                {"relation": "qualified-by", "target": "inv-b"},
            ],
        )
        _put(self.system, "hist-inactive", "note", active=False)
        _put(
            self.system,
            "hist-citer",
            "claim",
            relations=[
                {"relation": "maps-to", "target": "goal"},
                {"relation": "depends-on", "target": "hist-inactive"},
            ],
        )
        self.system.reindex()
        inv_rep = self.system.graph_profile()
        self.assertEqual(
            inv_rep["density_and_qualification"]["contradicts"][
                "unresolved_contradicts_cluster_count"
            ],
            0,
        )
        self.assertEqual(inv_rep["bottlenecks"]["superseded_head_bypass_count"], 1)
        self.assertEqual(inv_rep["bottlenecks"]["actionable_bypass_count"], 0)
        self.assertEqual(inv_rep["bottlenecks"]["historical_inactive_ref_count"], 1)
        # Historical inactive refs with 0 active successors must NOT fire bypass alerts
        alert_rules = {a["rule"] for a in inv_rep["alerts"]}
        self.assertNotIn("superseded_head_bypass_detected", alert_rules)

        # Verify domain-neutral calibration profile config loads & validates cleanly
        from managed_ai_information.store.query.graph_profile import load_profile_config

        sample_cfg = self.base / "domain_profile_config.json"
        sample_cfg.write_text(
            json.dumps(
                {
                    "format": GRAPH_PROFILE_CONFIG_FORMAT,
                    "root_kinds": ["goal", "field", "frontier"],
                    "claim_kinds": ["domain-claim", "claim", "synthesis"],
                    "navigational_kinds": ["question", "topic-map", "index"],
                    "structural_predicates": ["depends-on", "maps-to", "supersedes"],
                    "qualifying_predicates": ["qualified-by", "evidenced-by", "supports"],
                    "bypass_exempt_predicates": [
                        "supersedes",
                        "consolidated-into",
                        "grounded in original edition",
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        loaded_cfg = load_profile_config(sample_cfg)
        self.assertEqual(loaded_cfg["format"], GRAPH_PROFILE_CONFIG_FORMAT)
        sample_rep = self.system.graph_profile(profile_config_path=sample_cfg)
        self.assertIn("domain-claim", sample_rep["calibration"]["claim_kinds"])
        self.assertIn("qualified-by", sample_rep["calibration"]["qualifying_predicates"])

        # Inverse contradicted-by edge without mediator is flagged as an unresolved cluster
        _put(
            self.system,
            "cb-a",
            "claim",
            relations=[
                {"relation": "maps-to", "target": "goal"},
                {"relation": "contradicted-by", "target": "cb-b"},
            ],
        )
        _put(self.system, "cb-b", "claim", relations=[{"relation": "maps-to", "target": "goal"}])
        self.system.reindex()
        cb_rep = self.system.graph_profile()
        cb_contra = cb_rep["density_and_qualification"]["contradicts"]
        self.assertEqual(cb_contra["unresolved_contradicts_cluster_count"], 1)
        self.assertEqual(
            cb_contra["unresolved_contradicts_clusters"][0]["node_ids"], ["cb-a", "cb-b"]
        )

    def test_w17_symmetric_inverse_sccs_prefix_qualification_and_claim_deserts(self) -> None:
        # Build symmetric peer loop, inverse structural pair, prose-prefixed claim,
        # high-fan-in non-claim question, and evidenced-by mediated contradiction.
        _put(
            self.system,
            "gap-a",
            "gap",
            relations=[
                {"relation": "maps-to", "target": "goal"},
                {"relation": "related-gap", "target": "gap-b"},
                {"relation": "instantiated-by", "target": "frontier-1"},
            ],
        )
        _put(
            self.system,
            "gap-b",
            "gap",
            relations=[
                {"relation": "maps-to", "target": "goal"},
                {"relation": "depends-on", "target": "gap-a"},
            ],
        )
        _put(
            self.system,
            "frontier-1",
            "frontier",
            relations=[{"relation": "instantiates", "target": "gap-a"}],
        )
        _put(
            self.system,
            "claim-prose",
            "claim",
            relations=[
                {"relation": "maps-to", "target": "goal"},
                {"relation": "compares prior empirical bound", "target": "gap-a"},
            ],
        )
        _put(
            self.system,
            "question-hub",
            "question",
            relations=[{"relation": "maps-to", "target": "goal"}],
        )
        for idx in range(3):
            _put(
                self.system,
                f"cite-{idx}",
                "claim",
                relations=[
                    {"relation": "maps-to", "target": "claim-prose"},
                    {"relation": "maps-to", "target": "question-hub"},
                ],
            )
        _put(
            self.system,
            "med-claim",
            "claim",
            relations=[
                {"relation": "maps-to", "target": "goal"},
                {"relation": "contradicted-by", "target": "dir-old"},
                {"relation": "evidenced-by", "target": "dir-new"},
            ],
        )
        _put(
            self.system,
            "dir-old",
            "claim",
            relations=[{"relation": "maps-to", "target": "goal"}],
        )
        _put(
            self.system,
            "dir-new",
            "claim",
            relations=[
                {"relation": "maps-to", "target": "goal"},
                {"relation": "supersedes", "target": "dir-old"},
            ],
        )
        self.system.reindex()

        cfg_path = self.base / "w17-cal.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "format": GRAPH_PROFILE_CONFIG_FORMAT,
                    "claim_kinds": ["claim"],
                    "navigational_kinds": ["gap", "frontier"],
                    "structural_predicates": [
                        "depends-on",
                        "maps-to",
                        "instantiates",
                        "instantiated-by",
                        "related-gap",
                    ],
                    "symmetric_predicates": ["related-gap"],
                    "inverse_structural_predicates": ["instantiated-by"],
                    "qualifying_predicates": ["qualifies", "evidenced-by", "contradicted-by"],
                    "qualifying_predicate_prefixes": ["compares "],
                    "contradict_resolution_predicates": ["qualifies", "evidenced-by", "supersedes"],
                }
            ),
            encoding="utf-8",
        )
        rep = self.system.graph_profile(profile_config_path=cfg_path)
        self.assertEqual(rep["bottlenecks"]["structural_scc_count"], 0)
        dens = rep["density_and_qualification"]
        self.assertEqual(
            dens["per_predicate"]["compares prior empirical bound"]["category"], "qualifying"
        )
        # claim-prose is qualified via prefix; question-hub is non-claim so excluded from deserts.
        desert_ids = [d["id"] for d in dens["qualification_deserts"]]
        self.assertNotIn("claim-prose", desert_ids)
        self.assertNotIn("question-hub", desert_ids)
        self.assertEqual(dens["contradicts"]["unresolved_contradicts_cluster_count"], 0)


if __name__ == "__main__":
    unittest.main()
