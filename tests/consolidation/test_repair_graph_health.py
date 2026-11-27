"""Unit and CLI tests for scripts/consolidation/repair_graph_health.py."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO = Path(__file__).resolve().parents[2]
for _entry in (str(_REPO), str(_REPO / "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from scripts.consolidation.repair_graph_health import (  # noqa: E402
    LINK_SPEC_FORMAT,
    REPAIR_PLAN_FORMAT,
    REPAIR_RECEIPT_FORMAT,
    repair_graph_health,
    rollback_graph_repair,
)

from managed_ai_information.core.storage import exclusive  # noqa: E402
from managed_ai_information.store.query import graph_query  # noqa: E402
from managed_ai_information.store.system import System  # noqa: E402

REPAIR_CLI = _REPO / "scripts/consolidation/repair_graph_health.py"


def _seed_store(store_root: Path, *, reindex: bool = True) -> System:
    System.initialize(store_root, {"contribution": "Test mechanical graph-health repairs."})
    system = System(store_root)
    with exclusive(system.ex.root / "owner.lock"):
        # Active hub linked from goal, carrying a self-loop.
        system.put(
            {
                "id": "topic-hub",
                "kind": "topic",
                "content": {"summary": "Active research hub."},
                "relations": [
                    {"relation": "serves", "target": "goal"},
                    {"relation": "revises native navigation from", "target": "topic-hub"},
                ],
                "basis": [],
                "active": True,
            },
            0,
        )
        # Unambiguous active successor chain: old-q --consolidated-into--> new-q.
        system.put(
            {
                "id": "new-q",
                "kind": "question",
                "content": {"summary": "Canonical active question."},
                "relations": [{"relation": "serves", "target": "topic-hub"}],
                "basis": [],
                "active": True,
            },
            0,
        )
        system.put(
            {
                "id": "old-q",
                "kind": "question",
                "content": {"summary": "Superseded inactive question."},
                "relations": [{"relation": "consolidated-into", "target": "new-q"}],
                "basis": [],
                "active": False,
            },
            0,
        )
        # Active claim pointing to old-q (unambiguous bypass -> should re-point to new-q).
        system.put(
            {
                "id": "claim-unambiguous",
                "kind": "research-claim",
                "content": {"summary": "Claim citing superseded question."},
                "relations": [
                    {"relation": "serves", "target": "topic-hub"},
                    {"relation": "bears-on", "target": "old-q"},
                ],
                "basis": [],
                "active": True,
            },
            0,
        )
        # Active claim already having BOTH old-q AND new-q (should dedup without duplicate edge).
        system.put(
            {
                "id": "claim-dedup",
                "kind": "research-claim",
                "content": {"summary": "Claim citing both old and new question."},
                "relations": [
                    {"relation": "serves", "target": "topic-hub"},
                    {"relation": "bears-on", "target": "old-q"},
                    {"relation": "bears-on", "target": "new-q"},
                ],
                "basis": [],
                "active": True,
            },
            0,
        )
        # Ambiguous multi-branch successor: ambig-old <--supersedes-- {branch-a, branch-b}.
        system.put(
            {
                "id": "ambig-old",
                "kind": "question",
                "content": {"summary": "Inactive split question."},
                "relations": [],
                "basis": [],
                "active": False,
            },
            0,
        )
        for bid in ("branch-a", "branch-b"):
            system.put(
                {
                    "id": bid,
                    "kind": "question",
                    "content": {"summary": f"Active branch {bid}."},
                    "relations": [
                        {"relation": "serves", "target": "topic-hub"},
                        {"relation": "supersedes", "target": "ambig-old"},
                    ],
                    "basis": [],
                    "active": True,
                },
                0,
            )
        system.put(
            {
                "id": "claim-ambiguous",
                "kind": "research-claim",
                "content": {"summary": "Claim citing ambiguous split."},
                "relations": [
                    {"relation": "serves", "target": "topic-hub"},
                    {"relation": "bears-on", "target": "ambig-old"},
                ],
                "basis": [],
                "active": True,
            },
            0,
        )
        # Historical inactive target with 0 active successors (must NOT be touched).
        system.put(
            {
                "id": "historical-dead",
                "kind": "question",
                "content": {"summary": "Closed historical question."},
                "relations": [],
                "basis": [],
                "active": False,
            },
            0,
        )
        system.put(
            {
                "id": "claim-historical",
                "kind": "research-claim",
                "content": {"summary": "Claim citing historical closed question."},
                "relations": [
                    {"relation": "serves", "target": "topic-hub"},
                    {"relation": "bears-on", "target": "historical-dead"},
                ],
                "basis": [],
                "active": True,
            },
            0,
        )
        # Stranded retired-parent question stub (MUST be soft-retired, content preserved).
        system.put(
            {
                "id": "stranded-q",
                "kind": "question",
                "content": {"summary": "Stranded question from retired issue."},
                "relations": [],
                "basis": [],
                "active": True,
            },
            0,
        )
        # Non-question orphan whose parent retired (MUST be spared!).
        system.put(
            {
                "id": "orphan-claim",
                "kind": "research-claim",
                "content": {"summary": "Orphan claim must never be auto-retired."},
                "relations": [],
                "basis": [],
                "active": True,
            },
            0,
        )
        # Retired parent pointing to both stranded-q and orphan-claim.
        system.put(
            {
                "id": "retired-issue",
                "kind": "research-issue",
                "content": {"summary": "Retired issue."},
                "relations": [
                    {"relation": "leaves open", "target": "stranded-q"},
                    {"relation": "tracks-claim", "target": "orphan-claim"},
                ],
                "basis": [],
                "active": False,
            },
            0,
        )
        # Zero-edge question with NO retired parent (MUST be spared!).
        system.put(
            {
                "id": "unparented-q",
                "kind": "question",
                "content": {"summary": "Zero-edge question with no retired parent."},
                "relations": [],
                "basis": [],
                "active": True,
            },
            0,
        )
        # Active 2-node island (MUST be spared from retirement; linkable via --link-file).
        system.put(
            {
                "id": "island-obs",
                "kind": "observation",
                "content": {"summary": "Active observation."},
                "relations": [],
                "basis": [],
                "active": True,
            },
            0,
        )
        system.put(
            {
                "id": "island-q",
                "kind": "question",
                "content": {"summary": "Active island question."},
                "relations": [{"relation": "interprets-observation", "target": "island-obs"}],
                "basis": [],
                "active": True,
            },
            0,
        )
        if reindex:
            system.reindex()
    return system


class RepairGraphHealthTests(unittest.TestCase):
    def test_dry_run_zero_mutation_apply_idempotency_and_rollback(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            store_root = base / "store"
            system = _seed_store(store_root)
            link_path = base / "links.json"
            link_path.write_text(
                json.dumps(
                    {
                        "format": LINK_SPEC_FORMAT,
                        "links": [
                            {
                                "from": "island-q",
                                "relation": "serves",
                                "target": "topic-hub",
                                "warrant": "Link reviewed observation island to active hub.",
                            }
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            receipt_path = base / "receipt.json"

            # 1. Dry-run produces full plan with ZERO mutations and ZERO receipt write.
            before_revs = {r["id"]: r["revision"] for r in system.records()}
            plan = repair_graph_health(store_root, apply=False, link_file=link_path)
            self.assertEqual(plan["format"], REPAIR_PLAN_FORMAT)
            self.assertTrue(plan["dry_run"])
            self.assertFalse(plan["applied"])
            self.assertEqual(plan["written_count"], 0)
            self.assertEqual(plan["self_loops_removed"], 1)
            self.assertEqual(plan["bypasses_repointed"], 2)
            self.assertEqual(plan["bypasses_skipped_historical"], 1)
            self.assertEqual(plan["bypasses_skipped_ambiguous_count"], 1)
            self.assertEqual(plan["stranded_questions_retired"], 1)
            self.assertEqual(plan["explicit_links_added"], 1)
            self.assertFalse(receipt_path.exists())
            after_dry_revs = {r["id"]: r["revision"] for r in System(store_root).records()}
            self.assertEqual(before_revs, after_dry_revs)

            # 2. Apply commits atomically under owner.lock, writes receipt, lint=0.
            applied = repair_graph_health(
                store_root, apply=True, receipt_path=receipt_path, link_file=link_path
            )
            self.assertEqual(applied["format"], REPAIR_RECEIPT_FORMAT)
            self.assertTrue(applied["applied"])
            self.assertEqual(applied["written_count"], 5)
            self.assertEqual(applied["lint_anomalies"], 0)
            self.assertTrue(receipt_path.exists())

            reloaded = System(store_root)
            # Self-loop removed, non-self preserved.
            hub = reloaded.record_store.show("topic-hub")
            self.assertEqual(hub["relations"], [{"relation": "serves", "target": "goal"}])
            # Unambiguous bypass re-pointed; dedup bypass did not duplicate.
            c_unamb = reloaded.record_store.show("claim-unambiguous")
            self.assertEqual(
                [(r["relation"], r["target"]) for r in c_unamb["relations"]],
                [("serves", "topic-hub"), ("bears-on", "new-q")],
            )
            c_dedup = reloaded.record_store.show("claim-dedup")
            self.assertEqual(
                [(r["relation"], r["target"]) for r in c_dedup["relations"]],
                [("serves", "topic-hub"), ("bears-on", "new-q")],
            )
            # Ambiguous & historical bypasses untouched.
            c_ambig = reloaded.record_store.show("claim-ambiguous")
            self.assertIn({"relation": "bears-on", "target": "ambig-old"}, c_ambig["relations"])
            c_hist = reloaded.record_store.show("claim-historical")
            self.assertIn(
                {"relation": "bears-on", "target": "historical-dead"}, c_hist["relations"]
            )
            # Stranded retired-parent question soft-retired with ZERO content loss.
            stranded = reloaded.record_store.show("stranded-q")
            self.assertFalse(stranded["active"])
            self.assertEqual(
                stranded["content"], {"summary": "Stranded question from retired issue."}
            )
            # Non-question orphan, unparented question, and active island all spared.
            self.assertTrue(reloaded.record_store.show("orphan-claim")["active"])
            self.assertTrue(reloaded.record_store.show("unparented-q")["active"])
            island_q = reloaded.record_store.show("island-q")
            self.assertTrue(island_q["active"])
            self.assertIn(
                (
                    "serves",
                    "topic-hub",
                ),
                [(r["relation"], r["target"]) for r in island_q["relations"]],
            )

            # 3. Idempotency: 2nd run writes 0 records.
            second = repair_graph_health(store_root, apply=True, link_file=link_path)
            self.assertFalse(second["applied"])
            self.assertEqual(second["written_count"], 0)
            self.assertEqual(second["explicit_links_already_present"], 1)

            # 4. Rollback preview (dry-run) + apply restores original state cleanly.
            rb_plan = rollback_graph_repair(store_root, receipt_path, apply=False)
            self.assertTrue(rb_plan["dry_run"])
            self.assertEqual(len(rb_plan["rollback_ids"]), 5)
            rb_applied = rollback_graph_repair(store_root, receipt_path, apply=True)
            self.assertTrue(rb_applied["applied"])
            self.assertEqual(rb_applied["written_count"], 5)
            self.assertEqual(rb_applied["lint_anomalies"], 0)

            restored = System(store_root)
            self.assertTrue(restored.record_store.show("stranded-q")["active"])
            self.assertIn(
                {"relation": "revises native navigation from", "target": "topic-hub"},
                restored.record_store.show("topic-hub")["relations"],
            )
            self.assertIn(
                {"relation": "bears-on", "target": "old-q"},
                restored.record_store.show("claim-unambiguous")["relations"],
            )
            self.assertEqual(
                sum(int(v) for v in graph_query.lint(restored).get("summary", {}).values()), 0
            )
            # Re-running rollback on already-rolled-back heads fails closed cleanly.
            with self.assertRaisesRegex(ValueError, "cannot rollback"):
                rollback_graph_repair(store_root, receipt_path, apply=False)

    def test_validation_guards_and_cli_help(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            store_root = base / "store"
            _seed_store(store_root)

            # Reject self-loop in link spec.
            bad_link = base / "bad_link.json"
            bad_link.write_text(
                json.dumps(
                    {
                        "format": LINK_SPEC_FORMAT,
                        "links": [
                            {"from": "topic-hub", "relation": "serves", "target": "topic-hub"}
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "self-loop"):
                repair_graph_health(store_root, apply=False, link_file=bad_link)

            # Reject inactive target in link spec.
            inactive_link = base / "inactive_link.json"
            inactive_link.write_text(
                json.dumps(
                    {
                        "format": LINK_SPEC_FORMAT,
                        "links": [{"from": "topic-hub", "relation": "serves", "target": "old-q"}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "active"):
                repair_graph_health(store_root, apply=False, link_file=inactive_link)

            # Reject directory masquerading as receipt output path BEFORE mutating store.
            before_revs = {r["id"]: r["revision"] for r in System(store_root).records()}
            dir_receipt = base / "not_a_file.json"
            dir_receipt.mkdir()
            with self.assertRaisesRegex(ValueError, "ordinary"):
                repair_graph_health(store_root, apply=True, receipt_path=dir_receipt)
            after_revs = {r["id"]: r["revision"] for r in System(store_root).records()}
            self.assertEqual(before_revs, after_revs)

            # Unindexed fallback produces identical repair plan.
            unindexed_root = base / "unindexed_store"
            _seed_store(unindexed_root, reindex=False)
            unindexed_plan = repair_graph_health(unindexed_root, apply=False)
            self.assertFalse(unindexed_plan["indexed"])
            self.assertEqual(unindexed_plan["self_loops_removed"], 1)
            self.assertEqual(unindexed_plan["bypasses_repointed"], 2)
            self.assertEqual(unindexed_plan["stranded_questions_retired"], 1)

            # CLI --help exits 0 on stdout; dry-run CLI prints valid JSON.
            help_proc = subprocess.run(
                [sys.executable, "-B", str(REPAIR_CLI), "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(help_proc.returncode, 0)
            self.assertIn("repair_graph_health.py", help_proc.stdout)

            dry_proc = subprocess.run(
                [sys.executable, "-B", str(REPAIR_CLI), "--store", str(store_root)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(dry_proc.returncode, 0, dry_proc.stderr)
            parsed = json.loads(dry_proc.stdout)
            self.assertTrue(parsed["dry_run"])
            self.assertEqual(parsed["format"], REPAIR_PLAN_FORMAT)

            # C69B-O1/O2: structured JSON stderr + exit 2 on invalid flags/errors.
            bad_combo = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(REPAIR_CLI),
                    "--store",
                    str(store_root),
                    "--rollback",
                    str(base / "missing.json"),
                    "--link-file",
                    str(bad_link),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(bad_combo.returncode, 2)
            err_doc = json.loads(bad_combo.stderr)
            self.assertFalse(err_doc["ok"])
            self.assertIn("--rollback", err_doc["error"])

            abbrev_proc = subprocess.run(
                [sys.executable, "-B", str(REPAIR_CLI), "--sto", str(store_root)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(abbrev_proc.returncode, 2)

    def test_w17_retired_prefix_questions_and_hub_edge_delegation(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            store_root = base / "store"
            System.initialize(store_root, {"contribution": "Test Wave 17 opt-in repairs."})
            system = System(store_root)
            with exclusive(system.ex.root / "owner.lock"):
                # Hub -> topic-map -> leaf-covered, plus direct hub -> leaf-covered (redundant)
                # and hub -> leaf-uncovered (must stay!).
                system.put(
                    {
                        "id": "leaf-covered",
                        "kind": "research-claim",
                        "content": {"summary": "Covered by topic-map."},
                        "relations": [],
                        "basis": [],
                        "active": True,
                    },
                    0,
                )
                system.put(
                    {
                        "id": "leaf-uncovered",
                        "kind": "research-claim",
                        "content": {"summary": "Only tracked by hub."},
                        "relations": [],
                        "basis": [],
                        "active": True,
                    },
                    0,
                )
                system.put(
                    {
                        "id": "map-1",
                        "kind": "research-topic-map",
                        "content": {"summary": "Active topic map."},
                        "relations": [{"relation": "maps-to", "target": "leaf-covered"}],
                        "basis": [],
                        "active": True,
                    },
                    0,
                )
                system.put(
                    {
                        "id": "selector-hub",
                        "kind": "index",
                        "content": {"summary": "Hub tracking maps and leaves."},
                        "relations": [
                            {"relation": "serves", "target": "goal"},
                            {"relation": "tracks-topic-map", "target": "map-1"},
                            {"relation": "tracks-claim", "target": "leaf-covered"},
                            {"relation": "tracks-claim", "target": "leaf-uncovered"},
                        ],
                        "basis": [],
                        "active": True,
                    },
                    0,
                )
                # Retired study issue + 1 open_conditional stub (retire) + 1 open stub (spare!).
                system.put(
                    {
                        "id": "g60-named-demo-study-20261121-deadbeef",
                        "kind": "research-issue",
                        "content": {"summary": "Retired named study."},
                        "relations": [],
                        "basis": [],
                        "active": False,
                    },
                    0,
                )
                system.put(
                    {
                        "id": "g60-named-demo-study-20261121-question-2-aaaa",
                        "kind": "question",
                        "content": {
                            "selection": "open_conditional",
                            "summary": "Conditional stub.",
                        },
                        "relations": [],
                        "basis": [],
                        "active": True,
                    },
                    0,
                )
                system.put(
                    {
                        "id": "g60-named-demo-study-20261121-question-1-bbbb",
                        "kind": "question",
                        "content": {"selection": "open", "summary": "Explicitly open question."},
                        "relations": [],
                        "basis": [],
                        "active": True,
                    },
                    0,
                )
                system.reindex()

            # Default run (opt-ins False) touches 0 records.
            default_plan = repair_graph_health(store_root, apply=False)
            self.assertEqual(default_plan["records_to_patch"], 0)

            # Opt-in flags retire ONLY open_conditional stub and delegate ONLY covered hub edge.
            receipt_path = base / "w17-receipt.json"
            applied = repair_graph_health(
                store_root,
                apply=True,
                receipt_path=receipt_path,
                retire_retired_prefix_questions=True,
                delegate_redundant_hub_edges=True,
            )
            self.assertEqual(applied["retired_prefix_questions_retired"], 1)
            self.assertEqual(applied["hub_edges_delegated"], 1)
            self.assertEqual(applied["written_count"], 2)

            reloaded = System(store_root)
            self.assertFalse(
                reloaded.record_store.show("g60-named-demo-study-20261121-question-2-aaaa")[
                    "active"
                ]
            )
            self.assertTrue(
                reloaded.record_store.show("g60-named-demo-study-20261121-question-1-bbbb")[
                    "active"
                ]
            )
            hub_rels = {
                (r["relation"], r["target"])
                for r in reloaded.record_store.show("selector-hub")["relations"]
            }
            self.assertNotIn(("tracks-claim", "leaf-covered"), hub_rels)
            self.assertIn(("tracks-claim", "leaf-uncovered"), hub_rels)
            self.assertIn(("tracks-topic-map", "map-1"), hub_rels)

            # Rollback restores both cleanly.
            rb = rollback_graph_repair(store_root, receipt_path, apply=True)
            self.assertEqual(rb["written_count"], 2)
            restored = System(store_root)
            self.assertTrue(
                restored.record_store.show("g60-named-demo-study-20261121-question-2-aaaa")[
                    "active"
                ]
            )
            self.assertIn(
                ("tracks-claim", "leaf-covered"),
                {
                    (r["relation"], r["target"])
                    for r in restored.record_store.show("selector-hub")["relations"]
                },
            )


if __name__ == "__main__":
    unittest.main()
