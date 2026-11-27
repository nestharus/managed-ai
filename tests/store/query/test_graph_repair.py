"""Unit and CLI tests for managed-ai-information graph repair and reversible rollback."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from managed_ai_information.core.storage import exclusive  # noqa: E402
from managed_ai_information.store.cli.command_parser import is_readonly_action  # noqa: E402
from managed_ai_information.store.cli.command_parser import parser as cli_parser  # noqa: E402
from managed_ai_information.store.query import graph_query  # noqa: E402
from managed_ai_information.store.query.graph_profile import (  # noqa: E402
    GRAPH_PROFILE_CONFIG_FORMAT,
)
from managed_ai_information.store.query.graph_repair import (  # noqa: E402
    LINK_SPEC_FORMAT,
    REPAIR_PLAN_FORMAT,
    REPAIR_RECEIPT_FORMAT,
    repair_graph,
    rollback_graph_repair,
)
from managed_ai_information.store.system import System  # noqa: E402


def _run_cli(store_root: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(_SRC)
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "managed_ai_information.store.manage_store",
            "--root",
            str(store_root),
            *extra_args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def _seed_store(store_root: Path, *, reindex: bool = True) -> System:
    System.initialize(store_root, {"contribution": "Test mechanical graph-health repairs."})
    system = System(store_root)
    with exclusive(system.ex.root / "owner.lock"):
        # Active hub linked from goal, carrying a self-loop.
        system.put(
            {
                "id": "topic-hub",
                "kind": "topic",
                "content": {"summary": "Active hub."},
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
                "kind": "claim",
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
                "kind": "claim",
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
                "kind": "claim",
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
                "kind": "claim",
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
                "content": {"summary": "Stranded question from retired parent."},
                "relations": [],
                "basis": [],
                "active": True,
            },
            0,
        )
        # Non-question orphan whose parent retired (MUST be spared).
        system.put(
            {
                "id": "orphan-claim",
                "kind": "claim",
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
                "kind": "issue",
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
        # Zero-edge question with NO retired parent (MUST be spared).
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
        # Active 2-node island (MUST be spared from retirement; linkable via --link-spec).
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


class GraphRepairTests(unittest.TestCase):
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
            plan = repair_graph(system, apply=False, link_spec_path=link_path)
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
            applied = repair_graph(
                system,
                apply=True,
                receipt_path=receipt_path,
                link_spec_path=link_path,
                lint_fn=graph_query.lint,
            )
            self.assertEqual(applied["format"], REPAIR_RECEIPT_FORMAT)
            self.assertTrue(applied["applied"])
            self.assertEqual(applied["written_count"], 5)
            self.assertEqual(applied["lint_anomalies"], 0)
            self.assertTrue(receipt_path.exists())

            reloaded = System(store_root)
            hub = reloaded.record_store.show("topic-hub")
            self.assertEqual(hub["relations"], [{"relation": "serves", "target": "goal"}])
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
            c_ambig = reloaded.record_store.show("claim-ambiguous")
            self.assertIn({"relation": "bears-on", "target": "ambig-old"}, c_ambig["relations"])
            c_hist = reloaded.record_store.show("claim-historical")
            self.assertIn(
                {"relation": "bears-on", "target": "historical-dead"}, c_hist["relations"]
            )
            stranded = reloaded.record_store.show("stranded-q")
            self.assertFalse(stranded["active"])
            self.assertEqual(
                stranded["content"], {"summary": "Stranded question from retired parent."}
            )
            self.assertTrue(reloaded.record_store.show("orphan-claim")["active"])
            self.assertTrue(reloaded.record_store.show("unparented-q")["active"])
            island_q = reloaded.record_store.show("island-q")
            self.assertTrue(island_q["active"])
            self.assertIn(
                ("serves", "topic-hub"),
                [(r["relation"], r["target"]) for r in island_q["relations"]],
            )

            # 3. Idempotency: 2nd run writes 0 records.
            second = repair_graph(reloaded, apply=True, link_spec_path=link_path)
            self.assertFalse(second["applied"])
            self.assertEqual(second["written_count"], 0)
            self.assertEqual(second["explicit_links_already_present"], 1)

            # 4. Rollback preview (dry-run) + apply restores original state cleanly.
            rb_plan = rollback_graph_repair(reloaded, receipt_path, apply=False)
            self.assertTrue(rb_plan["dry_run"])
            self.assertEqual(len(rb_plan["rollback_ids"]), 5)
            rb_applied = rollback_graph_repair(
                reloaded, receipt_path, apply=True, lint_fn=graph_query.lint
            )
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
                rollback_graph_repair(restored, receipt_path, apply=False)

    def test_profile_config_and_strict_json_bool_rejection(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            store_root = base / "store"
            system = _seed_store(store_root)

            # Custom profile config exempts 'bears-on' from bypass repointing.
            cfg_path = base / "profile.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "format": GRAPH_PROFILE_CONFIG_FORMAT,
                        "root_kinds": ["goal"],
                        "bypass_exempt_predicates": [
                            "supersedes",
                            "superseded-by",
                            "consolidated-into",
                            "bears-on",
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            plan = repair_graph(system, apply=False, profile_config_path=cfg_path)
            self.assertEqual(plan["bypasses_repointed"], 0)
            self.assertEqual(plan["self_loops_removed"], 1)
            self.assertEqual(plan["stranded_questions_retired"], 1)
            self.assertEqual(plan["profile_config"], str(cfg_path))

            # Strict JSON bool rejection on link spec fields.
            bool_link = base / "bool_link.json"
            bool_link.write_text(
                json.dumps(
                    {
                        "format": LINK_SPEC_FORMAT,
                        "links": [{"from": True, "relation": "serves", "target": "topic-hub"}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "nonempty string"):
                repair_graph(system, apply=False, link_spec_path=bool_link)

            # Strict JSON bool rejection on rollback receipt after_revision & active flags.
            bad_receipt = base / "bool_receipt.json"
            bad_receipt.write_text(
                json.dumps(
                    {
                        "format": REPAIR_RECEIPT_FORMAT,
                        "instance_id": system.instance_id,
                        "batch_id": "bad-batch",
                        "records": [
                            {
                                "id": "topic-hub",
                                "after_revision": True,
                                "after_sha256": "0" * 64,
                                "previous_active": True,
                                "new_active": True,
                                "added_relations": [],
                                "removed_relations": [],
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "integer"):
                rollback_graph_repair(system, bad_receipt, apply=False)

            bad_active = base / "bad_active_receipt.json"
            bad_active.write_text(
                json.dumps(
                    {
                        "format": REPAIR_RECEIPT_FORMAT,
                        "instance_id": system.instance_id,
                        "batch_id": "bad-batch-2",
                        "records": [
                            {
                                "id": "topic-hub",
                                "after_revision": 1,
                                "after_sha256": "0" * 64,
                                "previous_active": "false",
                                "new_active": True,
                                "added_relations": [],
                                "removed_relations": [],
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "boolean"):
                rollback_graph_repair(system, bad_active, apply=False)

    def test_validation_guards_and_native_cli_roundtrip(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            store_root = base / "store"
            system = _seed_store(store_root)

            # Verify parser readonly classification for lock admission.
            p = cli_parser()
            dry_ns = p.parse_args(["--root", str(store_root), "graph", "repair"])
            apply_ns = p.parse_args(["--root", str(store_root), "graph", "repair", "--apply"])
            self.assertTrue(is_readonly_action(dry_ns))
            self.assertFalse(is_readonly_action(apply_ns))

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
                repair_graph(system, apply=False, link_spec_path=bad_link)

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
                repair_graph(system, apply=False, link_spec_path=inactive_link)

            # Reject directory masquerading as receipt output path BEFORE mutating store.
            before_revs = {r["id"]: r["revision"] for r in System(store_root).records()}
            dir_receipt = base / "not_a_file.json"
            dir_receipt.mkdir()
            with self.assertRaisesRegex(ValueError, "ordinary"):
                repair_graph(system, apply=True, receipt_path=dir_receipt)
            after_revs = {r["id"]: r["revision"] for r in System(store_root).records()}
            self.assertEqual(before_revs, after_revs)

            # Unindexed fallback produces identical repair plan.
            unindexed_root = base / "unindexed_store"
            unindexed_sys = _seed_store(unindexed_root, reindex=False)
            unindexed_plan = repair_graph(unindexed_sys, apply=False)
            self.assertFalse(unindexed_plan["indexed"])
            self.assertEqual(unindexed_plan["self_loops_removed"], 1)
            self.assertEqual(unindexed_plan["bypasses_repointed"], 2)
            self.assertEqual(unindexed_plan["stranded_questions_retired"], 1)

            # CLI --help exits 0 on stdout; dry-run CLI prints valid JSON.
            help_proc = _run_cli(store_root, "graph", "repair", "--help")
            self.assertEqual(help_proc.returncode, 0, help_proc.stderr)
            self.assertIn("--profile-config", help_proc.stdout)
            self.assertIn("--link-spec", help_proc.stdout)

            dry_proc = _run_cli(store_root, "graph", "repair")
            self.assertEqual(dry_proc.returncode, 0, dry_proc.stderr)
            parsed = json.loads(dry_proc.stdout)
            self.assertTrue(parsed["dry_run"])
            self.assertEqual(parsed["format"], REPAIR_PLAN_FORMAT)

            # CLI --apply + --receipt + --link-spec commits cleanly without lock deadlock.
            link_path = base / "cli_links.json"
            link_path.write_text(
                json.dumps(
                    {
                        "format": LINK_SPEC_FORMAT,
                        "links": [
                            {"from": "island-q", "relation": "serves", "target": "topic-hub"}
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            cli_receipt = base / "cli_receipt.json"
            apply_proc = _run_cli(
                store_root,
                "graph",
                "repair",
                "--apply",
                "--receipt",
                str(cli_receipt),
                "--link-spec",
                str(link_path),
            )
            self.assertEqual(apply_proc.returncode, 0, apply_proc.stderr)
            apply_doc = json.loads(apply_proc.stdout)
            self.assertTrue(apply_doc["applied"])
            self.assertEqual(apply_doc["written_count"], 5)
            self.assertEqual(apply_doc["lint_anomalies"], 0)
            self.assertTrue(cli_receipt.is_file())

            # CLI --rollback --apply restores original state cleanly.
            rb_proc = _run_cli(
                store_root,
                "graph",
                "repair",
                "--rollback",
                str(cli_receipt),
                "--apply",
            )
            self.assertEqual(rb_proc.returncode, 0, rb_proc.stderr)
            rb_doc = json.loads(rb_proc.stdout)
            self.assertTrue(rb_doc["applied"])
            self.assertEqual(rb_doc["written_count"], 5)
            self.assertEqual(rb_doc["lint_anomalies"], 0)

            # Invalid flag combination exits 2 with structured JSON error on stderr.
            bad_combo = _run_cli(
                store_root,
                "graph",
                "repair",
                "--rollback",
                str(base / "missing.json"),
                "--link-spec",
                str(bad_link),
            )
            self.assertEqual(bad_combo.returncode, 2)
            err_doc = json.loads(bad_combo.stderr)
            self.assertEqual(err_doc["status"], "unresolved")
            self.assertIn("--rollback", err_doc["error"])

            # Abbreviated flags rejected (allow_abbrev=False).
            abbrev_proc = _run_cli(store_root, "graph", "repair", "--app")
            self.assertEqual(abbrev_proc.returncode, 2)

    def test_retired_prefix_questions_and_hub_edge_delegation(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            store_root = base / "store"
            System.initialize(store_root, {"contribution": "Test opt-in repairs."})
            system = System(store_root)
            with exclusive(system.ex.root / "owner.lock"):
                system.put(
                    {
                        "id": "leaf-covered",
                        "kind": "claim",
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
                        "kind": "claim",
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
                        "kind": "topic-map",
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
                # Retired parent + 1 open_conditional stub (retire) + 1 open stub (spare).
                system.put(
                    {
                        "id": "study-demo-20261121-deadbeef",
                        "kind": "issue",
                        "content": {"summary": "Retired named study."},
                        "relations": [],
                        "basis": [],
                        "active": False,
                    },
                    0,
                )
                system.put(
                    {
                        "id": "study-demo-20261121-question-2-aaaa",
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
                        "id": "study-demo-20261121-question-1-bbbb",
                        "kind": "question",
                        "content": {"selection": "open", "summary": "Explicitly open question."},
                        "relations": [],
                        "basis": [],
                        "active": True,
                    },
                    0,
                )
                system.reindex()

            default_plan = repair_graph(system, apply=False)
            self.assertEqual(default_plan["records_to_patch"], 0)

            receipt_path = base / "optin-receipt.json"
            applied = repair_graph(
                system,
                apply=True,
                receipt_path=receipt_path,
                retire_retired_prefix_questions=True,
                delegate_redundant_hub_edges=True,
                lint_fn=graph_query.lint,
            )
            self.assertEqual(applied["retired_prefix_questions_retired"], 1)
            self.assertEqual(applied["hub_edges_delegated"], 1)
            self.assertEqual(applied["written_count"], 2)

            reloaded = System(store_root)
            self.assertFalse(
                reloaded.record_store.show("study-demo-20261121-question-2-aaaa")["active"]
            )
            self.assertTrue(
                reloaded.record_store.show("study-demo-20261121-question-1-bbbb")["active"]
            )
            hub_rels = {
                (r["relation"], r["target"])
                for r in reloaded.record_store.show("selector-hub")["relations"]
            }
            self.assertNotIn(("tracks-claim", "leaf-covered"), hub_rels)
            self.assertIn(("tracks-claim", "leaf-uncovered"), hub_rels)
            self.assertIn(("tracks-topic-map", "map-1"), hub_rels)

            rb = rollback_graph_repair(reloaded, receipt_path, apply=True, lint_fn=graph_query.lint)
            self.assertEqual(rb["written_count"], 2)
            restored = System(store_root)
            self.assertTrue(
                restored.record_store.show("study-demo-20261121-question-2-aaaa")["active"]
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
