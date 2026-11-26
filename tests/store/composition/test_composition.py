#!/usr/bin/env python3
"""Isolated substantive checks for the optional composition view."""

from __future__ import annotations

if __package__:
    pass
else:
    import sys as _sys
    from pathlib import Path as _Path

    _repo = str(_Path(__file__).resolve().parents[3])
    if _repo not in _sys.path:
        _sys.path.insert(0, _repo)
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from managed_ai_information.store.composition.composition import MAX_VIEW_BYTES, Composition


class OwnerPaths:
    def __init__(self, root):
        self.root = root

    def path(self, name):
        candidate = self.root / name
        if Path(name).is_absolute() or ".." in Path(name).parts or candidate.is_symlink():
            raise ValueError("ordinary workspace-relative path required")
        return candidate


class FixtureSystem:
    def __init__(self, root, content):
        self.ex = OwnerPaths(root)
        self.record = {
            "id": "design",
            "revision": 1,
            "sha256": "a" * 64,
            "content": content,
            "basis": [],
        }

    def show(self, ident):
        if ident != self.record["id"]:
            raise ValueError("unknown record")
        return copy.deepcopy(self.record)


def design():
    return {
        "format": "g60-composition/1",
        "purpose": "Compare useful contribution routes.",
        "elements": [{"id": "app", "account": "The same app artifact."}],
        "relations": [
            {
                "id": "meaning",
                "from": ["app"],
                "to": ["app"],
                "prediction": "A reader can qualify the source meaning.",
            },
            {
                "id": "flow",
                "from": ["app"],
                "to": ["app"],
                "prediction": "A receiver can find a useful sibling.",
            },
        ],
        "premises": [
            {"id": "source", "account": "Delivered source.", "disposition": "available"},
            {
                "id": "external",
                "account": "External result unavailable.",
                "disposition": "unavailable",
            },
        ],
        "opportunities": [
            {
                "id": "build",
                "at": ["relation:meaning"],
                "contribution": "Construct a dependent realization.",
                "requires": [["external"]],
                "reads": ["interface"],
                "writes": ["module"],
                "footprint": "declared",
            },
            {
                "id": "explain",
                "at": ["relation:flow"],
                "contribution": "Explain the available route.",
                "requires": [["external"], ["source"]],
                "reads": ["contract"],
                "writes": ["guide"],
                "footprint": "declared",
            },
            {
                "id": "review",
                "at": ["element:app"],
                "contribution": "Inspect the current producer.",
                "requires": [["source"]],
                "reads": ["module"],
                "writes": ["review"],
                "footprint": "declared",
            },
        ],
    }


def design_with_competing_focus_rows():
    body = design()
    body["elements"].append({"id": "later", "account": "A later receiving artifact."})
    relations = {row["id"]: row for row in body["relations"]}
    opportunities = {row["id"]: row for row in body["opportunities"]}
    relations["flow"]["to"].append("later")
    opportunities["explain"]["at"] = ["relation:meaning", "relation:flow"]
    opportunities["build"]["requires"] = [["source", "external"]]
    return body


class CompositionChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.system = FixtureSystem(Path(self.temp.name), design())
        self.view = Composition(self.system)

    def test_alternative_and_sibling_survive_local_blockage(self):
        result = self.view.inspect("design")
        offers = {r["id"]: r for r in result["opportunities"]}
        self.assertEqual(offers["build"]["availability"], "unavailable-under-declared-premises")
        self.assertEqual(offers["explain"]["availability"], "available-under-declared-premises")
        self.assertEqual(offers["review"]["availability"], "available-under-declared-premises")
        self.assertEqual(
            offers["explain"]["premise_alternatives"][0]["availability"], "unavailable"
        )

    def test_unresolved_reference_never_becomes_available_and_cycles_remain(self):
        self.system.record["content"]["opportunities"][0]["requires"] = [["not-supplied"]]
        self.system.record["content"]["relations"][0]["to"] = ["outside"]
        result = self.view.inspect("design")
        self.assertEqual(result["opportunities"][0]["availability"], "needs-qualification")
        self.assertIn("premise:not-supplied", [r["ref"] for r in result["unresolved_refs"]])
        self.assertIn("element:outside", [r["ref"] for r in result["unresolved_refs"]])
        # The other self-relation is a legitimate authored cycle, not a deadlock.
        self.assertEqual(result["relations"][1]["from"], result["relations"][1]["to"])

    def test_changed_missing_and_unbound_evidence_need_qualification(self):
        path = self.system.ex.path("source.txt")
        path.write_text("earlier")
        self.system.record["basis"] = [
            {"path": "source.txt", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        ]
        premise = self.system.record["content"]["premises"][0]
        premise["basis"] = ["source.txt"]
        self.assertEqual(self.view.inspect("design")["premises"][0]["availability"], "available")
        path.write_text("later")
        result = self.view.inspect("design")
        self.assertEqual(result["premises"][0]["availability"], "unassessed")
        self.assertEqual(result["basis"][0]["status"], "changed")
        path.unlink()
        self.assertEqual(self.view.inspect("design")["basis"][0]["status"], "missing")
        premise["basis"] = ["not-captured.txt"]
        self.assertEqual(
            self.view.inspect("design")["premises"][0]["support"]["sources"][0]["status"], "unbound"
        )

    def test_focus_keeps_sibling_visible_and_reports_omitted_identity(self):
        result = self.view.inspect("design", focus="relation:flow", limit=2)
        self.assertEqual(result["opportunities"][0]["id"], "explain")
        self.assertEqual(len(result["opportunities"]), 2)
        coverage = result["coverage"]["collections"]["opportunities"]
        self.assertEqual(coverage["total"], 3)
        self.assertEqual(coverage["omitted_refs"], ["opportunity:review"])

    def test_exact_focus_precedes_related_rows_at_tight_limit(self):
        # A selected opportunity can name several rows. The exact focus must
        # survive even when earlier related rows fill the one-row allowance.
        scenarios = (
            ("relations", "relation:flow"),
            ("elements", "element:later"),
            ("premises", "premise:external"),
            ("opportunities", "opportunity:review"),
        )
        self.system.record["content"] = design_with_competing_focus_rows()
        before = copy.deepcopy(self.system.record)
        for collection, focus in scenarios:
            with self.subTest(focus=focus):
                result = self.view.inspect("design", focus=focus, limit=1, pair_limit=0)
                self.assertEqual([row["ref"] for row in result[collection]], [focus])
                coverage = result["coverage"]["collections"][collection]
                self.assertEqual(coverage["shown"], 1)
                self.assertEqual(len(coverage["omitted_refs"]), coverage["total"] - 1)
                self.assertNotIn(focus, coverage["omitted_refs"])
                self.assertEqual(result["coverage"]["pairs"]["examined"], 0)
                self.assertEqual(self.system.record, before)

    def test_exact_opportunity_precedes_unresolved_location_reference(self):
        # Unsupported locations remain inspectable and qualified. An earlier
        # at reference to an opportunity must not crowd out that exact focus.
        body = {
            "format": "g60-composition/1",
            "purpose": "Inspect an exact typed contribution.",
            "elements": [{"id": "shared", "account": "An independently typed element."}],
            "relations": [
                {
                    "id": "shared",
                    "from": ["shared"],
                    "to": ["shared"],
                    "prediction": "A separately typed relation remains visible.",
                }
            ],
            "premises": [
                {
                    "id": "shared",
                    "account": "A separately typed premise.",
                    "disposition": "available",
                }
            ],
            "opportunities": [
                {
                    "id": "earlier",
                    "at": ["opportunity:shared"],
                    "contribution": "A contribution with an unsupported location reference.",
                },
                {
                    "id": "shared",
                    "at": ["element:shared"],
                    "contribution": "The requested actual opportunity.",
                    "requires": [["shared"]],
                },
            ],
        }
        self.system.record["content"] = body
        before = copy.deepcopy(self.system.record)
        result = self.view.inspect("design", focus="opportunity:shared", limit=1, pair_limit=0)
        self.assertEqual([row["ref"] for row in result["opportunities"]], ["opportunity:shared"])
        self.assertEqual(
            result["opportunities"][0]["availability"], "available-under-declared-premises"
        )
        self.assertEqual(
            result["coverage"]["collections"]["opportunities"]["omitted_refs"],
            ["opportunity:earlier"],
        )
        self.assertEqual(
            result["unresolved_refs"],
            [{"ref": "opportunity:shared", "declared_in": "opportunity:earlier"}],
        )
        wider = self.view.inspect("design", focus="opportunity:shared", limit=2, pair_limit=1)
        self.assertEqual(wider["opportunities"][1]["availability"], "needs-qualification")
        self.assertEqual(wider["interactions"][0]["left"], "opportunity:shared")
        self.assertEqual(self.system.record, before)

    def test_missing_focus_keeps_ordinary_detail_and_explicit_unknown(self):
        ordinary = self.view.inspect("design", limit=1, pair_limit=0)
        missing = self.view.inspect("design", focus="relation:missing", limit=1, pair_limit=0)
        for collection in ("elements", "relations", "premises", "opportunities"):
            self.assertEqual(missing[collection], ordinary[collection])
            self.assertEqual(
                missing["coverage"]["collections"][collection],
                ordinary["coverage"]["collections"][collection],
            )
        self.assertEqual(
            missing["unresolved_refs"],
            [{"ref": "relation:missing", "declared_in": "requested-focus"}],
        )
        self.assertEqual([row["id"] for row in ordinary["opportunities"]], ["build"])
        self.assertEqual(
            ordinary["coverage"]["collections"]["opportunities"]["omitted_refs"],
            ["opportunity:explain", "opportunity:review"],
        )

    def test_exact_focus_still_respects_the_returned_byte_bound(self):
        body = self.system.record["content"]
        body["elements"].append({"id": "large-focus", "account": "x" * 1100000})
        # The old related-first tie could silently omit this requested element.
        before = copy.deepcopy(self.system.record)
        with self.assertRaisesRegex(ValueError, "base view exceeds the 1 MiB"):
            self.view.inspect("design", focus="element:large-focus", limit=1, pair_limit=0)
        self.assertEqual(self.system.record, before)

    def test_same_artifact_edges_keep_exact_resource_interaction(self):
        result = self.view.inspect("design")
        pairs = {(p["left"], p["right"]): p for p in result["interactions"]}
        self.assertEqual(
            pairs[("opportunity:build", "opportunity:review")]["interactions"],
            [{"kind": "left-write-right-read", "resources": ["module"]}],
        )
        self.assertEqual(
            pairs[("opportunity:build", "opportunity:explain")]["status"], "no-declared-interaction"
        )
        self.assertIn(
            "not actual independence", pairs[("opportunity:build", "opportunity:explain")]["scope"]
        )

    def test_unknown_footprint_and_explicit_conflict_are_not_independence(self):
        offers = self.system.record["content"]["opportunities"]
        offers[0].pop("footprint")
        offers[1]["conflicts"] = ["review"]
        offers[1]["phase"] = "interpretation"
        result = self.view.inspect("design")
        self.assertEqual(result["interactions"][0]["status"], "unknown-footprint")
        self.assertEqual(result["interactions"][2]["status"], "declared-interaction")
        self.assertEqual(result["interactions"][2]["interactions"], [{"kind": "explicit-conflict"}])

    def test_pair_bound_reports_unexamined_pairs(self):
        result = self.view.inspect("design", pair_limit=1)
        self.assertEqual(len(result["interactions"]), 1)
        self.assertEqual(result["coverage"]["pairs"]["total"], 3)
        self.assertEqual(result["coverage"]["pairs"]["omitted"], 2)

    def test_zero_pair_budget_is_explicit_no_comparison(self):
        result = self.view.inspect("design", pair_limit=0)
        self.assertEqual(result["interactions"], [])
        self.assertEqual(result["coverage"]["pairs"]["omitted"], 3)
        self.assertEqual(result["coverage"]["pairs"]["saturated"], ["pair-limit"])

    def test_long_phase_is_referenced_and_expansion_has_a_byte_bound(self):
        self.system.record["content"]["opportunities"][0]["phase"] = "x" * 600000
        # A valid profile can still contain a phase which would amplify in pairs.
        for i in range(60):
            self.system.record["content"]["opportunities"].append(
                {
                    "id": "other-" + str(i),
                    "at": ["element:app"],
                    "contribution": "Another offered route.",
                    "reads": ["shared"],
                    "writes": ["shared"],
                    "footprint": "declared",
                }
            )
        result = self.view.inspect("design", limit=1, pair_limit=20000)
        encoded = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")
        self.assertLessEqual(len(encoded), MAX_VIEW_BYTES)
        (displayed,) = result["opportunities"]
        first_pair = result["interactions"][0]
        self.assertEqual(first_pair["left"], displayed["ref"])
        self.assertIn(
            first_pair["right"], result["coverage"]["collections"]["opportunities"]["omitted_refs"]
        )
        self.assertIn("phase_refs", first_pair)
        self.assertNotIn("phases", result["interactions"][0])
        self.assertGreater(result["coverage"]["pairs"]["omitted"], 0)
        self.assertIn("view-byte-bound", result["coverage"]["pairs"]["saturated"])
        self.system.record["content"]["opportunities"][0]["phase"] = "x" * 1100000
        with self.assertRaisesRegex(ValueError, "base view exceeds the 1 MiB"):
            self.view.inspect("design", limit=1, pair_limit=0)

    def test_schema_and_information_envelope_refuse_silent_interpretation(self):
        original = copy.deepcopy(self.system.record["content"])
        for mutation in (
            lambda b: b["premises"][0].update(disposition="done"),
            lambda b: b["opportunities"][0].update(requires=["source"]),
            lambda b: b["relations"][0].update(observation="supported"),
            lambda b: b["elements"].append(copy.deepcopy(b["elements"][0])),
        ):
            self.system.record["content"] = copy.deepcopy(original)
            mutation(self.system.record["content"])
            with self.assertRaises(ValueError):
                self.view.inspect("design")
        self.system.record["content"] = {"format": "g60-information/1", "payload": original}
        with self.assertRaisesRegex(ValueError, "not implicitly unwrapped"):
            self.view.inspect("design")

    def test_interaction_map_preserves_direction_and_explicit_edges(self):
        rows = self.system.record["content"]["opportunities"]
        rows[:] = [
            {
                "id": "a",
                "contribution": "A",
                "reads": ["hub"],
                "writes": ["hub"],
                "conflicts": ["b", "a", "outside"],
            },
            {
                "id": "b",
                "contribution": "B",
                "reads": ["hub"],
                "conflicts": ["a"],
                "footprint": "declared",
            },
            {"id": "c", "contribution": "C", "reads": ["read-only"]},
        ]
        answer = self.view.interaction_map("design")
        self.assertEqual(
            answer["groups"],
            [
                {"kind": "resource", "resource": "hub", "reads": ["a", "b"], "writes": ["a"]},
                {"kind": "explicit-conflict", "left": "a", "others": ["b"]},
            ],
        )
        coverage = answer["coverage"]
        self.assertEqual(
            (
                coverage["unknown_footprints"],
                coverage["self_conflict_refs"],
                coverage["unresolved_conflict_refs"],
            ),
            (2, 1, 1),
        )
        limited = self.view.interaction_map("design", limit=1)
        self.assertEqual(limited["coverage"]["omitted_by_limit"], 1)

    def test_interaction_map_keeps_small_witness_after_oversize_group(self):
        # One ID used twice in a group can amplify beyond the returned-view ceiling.
        long_id = "a" * 550000
        self.system.record["content"]["opportunities"] = [
            {"id": long_id, "contribution": "A", "reads": ["big"], "writes": ["big"]},
            {"id": "b", "contribution": "B", "writes": ["big", "small"]},
            {"id": "c", "contribution": "C", "reads": ["small"]},
        ]
        answer = self.view.interaction_map("design", limit=1)
        self.assertEqual([g["resource"] for g in answer["groups"]], ["small"])
        self.assertEqual(answer["coverage"]["omitted_by_bytes"], 1)
        self.assertLessEqual(len(json.dumps(answer, indent=2).encode()), MAX_VIEW_BYTES)

    def test_read_only_and_reported_completion_does_not_supply_premises(self):
        self.system.record["content"]["opportunities"][0]["disposition"] = "complete"
        before = copy.deepcopy(self.system.record)
        result = self.view.inspect("design")
        self.assertEqual(self.system.record, before)
        self.assertEqual(
            result["opportunities"][0]["availability"], "unavailable-under-declared-premises"
        )
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
