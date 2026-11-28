"""Unit and adversarial tests for parallel multi-graph federation and N-goal smoothing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from managed_ai_information.core.storage import exclusive, sha
from managed_ai_information.store.query.multi_graph import (
    MULTI_GRAPH_IMPACT_FORMAT,
    MULTI_GRAPH_NEIGHBORS_FORMAT,
    SMOOTHING_CYCLE_FORMAT,
    CrossGraphRef,
    MultiGraphFederation,
    SmoothingCycle,
    SmoothingTracker,
)
from managed_ai_information.store.records import record_transfer
from managed_ai_information.store.system import System


def _tree_digest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = sha(path.read_bytes())
    return result


class MultiGraphFederationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

        self.stores: dict[str, System] = {}
        for name in ("writing", "marketing", "product", "code"):
            store_root = self.base / f"store-{name}"
            System.initialize(
                store_root,
                {"contribution": f"Independent {name} domain store."},
                instance_id=f"inst-{name}",
            )
            sys_obj = System(store_root)
            self.addCleanup(sys_obj.close)
            self.stores[name] = sys_obj

        # Populate distinct domain graphs without sharing any storage directory
        with exclusive(self.stores["product"].ex.root / "owner.lock"):
            self.stores["product"].put(
                {
                    "id": "feature-sync",
                    "kind": "note",
                    "content": {"summary": "Parallel multi-graph sync capability"},
                    "relations": [
                        {"relation": "specified-by", "target": "product-spec"},
                        {"relation": "implemented-by", "target": "code:sync-engine"},
                    ],
                },
                0,
            )
            self.stores["product"].put(
                {
                    "id": "product-spec",
                    "kind": "note",
                    "content": {"summary": "Formal product specification"},
                    "relations": [],
                },
                0,
            )

        with exclusive(self.stores["writing"].ex.root / "owner.lock"):
            self.stores["writing"].put(
                {
                    "id": "launch-doc",
                    "kind": "note",
                    "content": {"summary": "User guide explaining multi-graph sync"},
                    "relations": [
                        {"relation": "explains", "target": "product:feature-sync"},
                        {"relation": "informs", "target": "graph:marketing:launch-campaign"},
                    ],
                },
                0,
            )
            self.stores["writing"].put(
                {
                    "id": "archived-draft",
                    "kind": "note",
                    "active": False,
                    "content": {"summary": "Inactive draft"},
                    "relations": [
                        {"relation": "draft-of", "target": "launch-doc"},
                    ],
                },
                0,
            )

        with exclusive(self.stores["marketing"].ex.root / "owner.lock"):
            self.stores["marketing"].put(
                {
                    "id": "launch-campaign",
                    "kind": "note",
                    "content": {"summary": "Launch messaging"},
                    "relations": [
                        {"relation": "promotes", "target": "product:feature-sync"},
                    ],
                },
                0,
            )

        with exclusive(self.stores["code"].ex.root / "owner.lock"):
            self.stores["code"].put(
                {
                    "id": "sync-engine",
                    "kind": "note",
                    "content": {"summary": "Core sync implementation"},
                    "relations": [
                        {"relation": "depends-on", "target": "storage-adapter"},
                    ],
                },
                0,
            )
            self.stores["code"].put(
                {
                    "id": "storage-adapter",
                    "kind": "note",
                    "content": {"summary": "Underlying storage adapter"},
                    "relations": [],
                },
                0,
            )

    def test_cross_graph_discovery_impact_and_native_source_observation(self) -> None:
        # Also test native cross-store transfer observation discovery
        transfer_bundle = self.base / "product-to-code.json"
        with exclusive(self.stores["product"].ex.root / "owner.lock"):
            record_transfer.export(self.stores["product"], "product-spec", transfer_bundle)
        with exclusive(self.stores["code"].ex.root / "owner.lock"):
            record_transfer.receive(
                self.stores["code"],
                transfer_bundle,
                "obs-product-spec",
                "Observed product spec for code alignment.",
                0,
            )

        fed = MultiGraphFederation(self.stores)
        explicit_ref = CrossGraphRef(
            source_graph="marketing",
            source_node_id="launch-campaign",
            relation="verified-against",
            target_graph="code",
            target_node_id="sync-engine",
            basis="audit:campaign-accuracy-v1",
        )
        fed.add_link(explicit_ref)

        refs = fed.discover_cross_graph_refs()
        ref_tuples = {
            (r.source_graph, r.source_node_id, r.relation, r.target_graph, r.target_node_id)
            for r in refs
        }
        self.assertIn(
            ("product", "feature-sync", "implemented-by", "code", "sync-engine"), ref_tuples
        )
        self.assertIn(("writing", "launch-doc", "explains", "product", "feature-sync"), ref_tuples)
        self.assertIn(
            ("writing", "launch-doc", "informs", "marketing", "launch-campaign"), ref_tuples
        )
        self.assertIn(
            ("marketing", "launch-campaign", "promotes", "product", "feature-sync"), ref_tuples
        )
        self.assertIn(
            ("marketing", "launch-campaign", "verified-against", "code", "sync-engine"), ref_tuples
        )
        self.assertIn(
            ("code", "obs-product-spec", "observes-source", "product", "product-spec"), ref_tuples
        )

        # Impact query from product:feature-sync finds writing, marketing, and code nodes
        impact = fed.impact("product", "feature-sync", max_hops=2, active_only=True)
        self.assertEqual(impact["format"], MULTI_GRAPH_IMPACT_FORMAT)
        self.assertTrue(impact["read_only_non_merging"])
        self.assertEqual(impact["origin"]["graph"], "product")
        self.assertEqual(impact["origin"]["id"], "feature-sync")
        self.assertEqual(impact["affected_graphs"], ["code", "marketing", "product", "writing"])
        self.assertEqual([n["id"] for n in impact["by_graph"]["writing"]], ["launch-doc"])
        self.assertEqual([n["id"] for n in impact["by_graph"]["marketing"]], ["launch-campaign"])
        self.assertEqual(
            [n["id"] for n in impact["by_graph"]["code"]],
            ["sync-engine", "obs-product-spec", "storage-adapter"],
        )
        self.assertGreaterEqual(impact["cross_graph_edge_count"], 3)

        # Filter to specific target graphs
        filtered = fed.neighborhood(
            "product",
            "feature-sync",
            max_hops=2,
            target_graphs=("writing", "marketing"),
            active_only=True,
        )
        self.assertEqual(filtered["format"], MULTI_GRAPH_NEIGHBORS_FORMAT)
        self.assertEqual({n["graph"] for n in filtered["neighbors"]}, {"writing", "marketing"})

    def test_n_graph_isolation_mutations_in_graph_a_never_pollute_graph_b(self) -> None:
        fed = MultiGraphFederation(self.stores)
        writing_before = _tree_digest(self.stores["writing"].root)
        marketing_before = _tree_digest(self.stores["marketing"].root)
        code_before = _tree_digest(self.stores["code"].root)

        # Run federation discovery & impact queries first
        _ = fed.impact("product", "feature-sync", max_hops=3)

        # Mutate ONLY graph A (product)
        with exclusive(self.stores["product"].ex.root / "owner.lock"):
            self.stores["product"].put(
                {
                    "id": "brand-new-product-node",
                    "kind": "note",
                    "content": {"summary": "Only belongs in product"},
                    "relations": [{"relation": "updates", "target": "feature-sync"}],
                },
                0,
            )
            self.stores["product"].reindex()

        # Run federation queries again after mutation
        impact_after = fed.impact("product", "feature-sync", max_hops=2)
        self.assertIn(
            "brand-new-product-node", [n["id"] for n in impact_after["by_graph"]["product"]]
        )

        # Graphs B, C, D remain byte-for-byte identical on disk and have zero product records
        self.assertEqual(_tree_digest(self.stores["writing"].root), writing_before)
        self.assertEqual(_tree_digest(self.stores["marketing"].root), marketing_before)
        self.assertEqual(_tree_digest(self.stores["code"].root), code_before)
        for other in ("writing", "marketing", "code"):
            self.assertFalse(self.stores[other].record_store.has_record("brand-new-product-node"))

    def test_cycle_safe_cross_graph_traversal(self) -> None:
        fed = MultiGraphFederation(self.stores)
        # Create explicit multi-graph cycle: product -> writing -> marketing -> code -> product
        fed.add_link(
            CrossGraphRef(
                "product", "feature-sync", "cycle-to", "writing", "launch-doc", "basis:c1"
            )
        )
        fed.add_link(
            CrossGraphRef(
                "writing", "launch-doc", "cycle-to", "marketing", "launch-campaign", "basis:c2"
            )
        )
        fed.add_link(
            CrossGraphRef(
                "marketing", "launch-campaign", "cycle-to", "code", "sync-engine", "basis:c3"
            )
        )
        fed.add_link(
            CrossGraphRef("code", "sync-engine", "cycle-to", "product", "feature-sync", "basis:c4")
        )

        nb = fed.neighborhood(
            "product", "feature-sync", direction="out", max_hops=6, relations=("cycle-to",)
        )
        self.assertEqual(nb["neighbor_count"], 3)
        self.assertEqual(
            [(n["graph"], n["id"], n["depth"]) for n in nb["neighbors"]],
            [
                ("writing", "launch-doc", 1),
                ("marketing", "launch-campaign", 2),
                ("code", "sync-engine", 3),
            ],
        )

    def test_cross_graph_ref_rejects_same_graph_and_empty_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "distinct graphs"):
            CrossGraphRef("code", "a", "depends-on", "code", "b", "basis:1")
        with self.assertRaises(ValueError):
            CrossGraphRef("code", "a", "depends-on", "product", "b", "   ")

    def test_smoothing_cycle_monotonicity_convergence_and_adversarial_guards(self) -> None:
        tracker = SmoothingTracker(enforce_budget=True)
        weights = {
            "writing:clarity": 1.0,
            "marketing:conversion": 1.5,
            "product:fit": 2.0,
            "code:reliability": 2.0,
        }

        c0 = SmoothingCycle(
            cycle_index=0,
            goal_scores={
                "writing:clarity": 0.60,
                "marketing:conversion": 0.55,
                "product:fit": 0.65,
                "code:reliability": 0.70,
            },
            cost_spent=10.0,
            cost_budget=100.0,
            graph_deltas={"writing": 0.0, "marketing": 0.0, "product": 0.0, "code": 0.0},
            touched_graphs=("writing", "marketing", "product", "code"),
            weights=weights,
        )
        c1 = SmoothingCycle(
            cycle_index=1,
            goal_scores={
                "writing:clarity": 0.75,
                "marketing:conversion": 0.72,
                "product:fit": 0.80,
                "code:reliability": 0.85,
            },
            cost_spent=35.0,
            cost_budget=100.0,
            graph_deltas={"writing": 0.15, "marketing": 0.17, "product": 0.15, "code": 0.15},
            touched_graphs=("writing", "marketing", "product", "code"),
            weights=weights,
        )
        c2 = SmoothingCycle(
            cycle_index=2,
            goal_scores={
                "writing:clarity": 0.755,
                "marketing:conversion": 0.725,
                "product:fit": 0.805,
                "code:reliability": 0.855,
            },
            cost_spent=48.0,
            cost_budget=100.0,
            graph_deltas={"writing": 0.005, "marketing": 0.005, "product": 0.005, "code": 0.005},
            touched_graphs=("writing", "marketing", "product", "code"),
            weights=weights,
        )

        r0 = tracker.record_cycle(c0)
        r1 = tracker.record_cycle(c1)
        r2 = tracker.record_cycle(c2)

        self.assertEqual(c0.format, SMOOTHING_CYCLE_FORMAT)
        self.assertEqual(SmoothingCycle.from_dict(c1.to_dict()), c1)
        self.assertEqual(r0["regressed_goals"], [])
        self.assertGreater(r1["aggregate_delta"], 0.0)
        self.assertGreater(r2["aggregate_delta"], 0.0)
        self.assertTrue(tracker.is_monotonically_improving(require_pareto=True))
        self.assertTrue(tracker.converged(min_delta=0.01, target_floor=0.70))
        self.assertFalse(tracker.converged(min_delta=0.01, target_floor=0.90))

        summary = tracker.summary(min_delta=0.01, target_floor=0.70)
        self.assertEqual(summary["cycle_count"], 3)
        self.assertTrue(summary["monotonically_improving"])
        self.assertTrue(summary["pareto_non_regressing"])
        self.assertTrue(summary["within_budget"])
        self.assertTrue(summary["converged"])

        # Single-goal regression breaks Pareto monotonicity even if aggregate rises
        c3_pareto_reg = SmoothingCycle(
            cycle_index=3,
            goal_scores={
                "writing:clarity": 0.70,
                "marketing:conversion": 0.90,
                "product:fit": 0.85,
                "code:reliability": 0.90,
            },
            cost_spent=60.0,
            cost_budget=100.0,
            graph_deltas={"writing": -0.055, "marketing": 0.175, "product": 0.045, "code": 0.045},
            touched_graphs=("writing", "marketing", "product", "code"),
            weights=weights,
        )
        r3 = tracker.record_cycle(c3_pareto_reg)
        self.assertEqual(r3["regressed_goals"], ["writing:clarity"])
        self.assertTrue(tracker.is_monotonically_improving(require_pareto=False))
        self.assertFalse(tracker.is_monotonically_improving(require_pareto=True))

        # Adversarial rejections
        with self.assertRaisesRegex(ValueError, "exceeds cost_budget"):
            tracker.record_cycle(
                SmoothingCycle(
                    cycle_index=4,
                    goal_scores=c3_pareto_reg.goal_scores,
                    cost_spent=120.0,
                    cost_budget=100.0,
                    graph_deltas={},
                    weights=weights,
                )
            )
        with self.assertRaisesRegex(ValueError, "strictly increase"):
            tracker.record_cycle(
                SmoothingCycle(
                    cycle_index=3,
                    goal_scores=c3_pareto_reg.goal_scores,
                    cost_spent=70.0,
                    cost_budget=100.0,
                    graph_deltas={},
                    weights=weights,
                )
            )
        with self.assertRaisesRegex(ValueError, "non-decreasing"):
            tracker.record_cycle(
                SmoothingCycle(
                    cycle_index=4,
                    goal_scores=c3_pareto_reg.goal_scores,
                    cost_spent=50.0,
                    cost_budget=100.0,
                    graph_deltas={},
                    weights=weights,
                )
            )
        with self.assertRaisesRegex(ValueError, "consistent"):
            tracker.record_cycle(
                SmoothingCycle(
                    cycle_index=4,
                    goal_scores={"only-one": 0.9},
                    cost_spent=70.0,
                    cost_budget=100.0,
                    graph_deltas={},
                )
            )
        with self.assertRaisesRegex(ValueError, "finite"):
            SmoothingCycle(
                cycle_index=0,
                goal_scores={"g": float("inf")},
                cost_spent=1.0,
                cost_budget=10.0,
                graph_deltas={},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
