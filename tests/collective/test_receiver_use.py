"""Independent review followed by an optionally deferred local use."""

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from managed_ai.collective import Collective  # noqa: E402


class ReceiverUse(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        database = Path(self.tmp.name) / "room.sqlite"
        Collective.initialize(database)
        self.db = Collective(database)
        self.serial = 0
        self.candidate = self.create("drawing", "maker", {"part": "angle"})
        self.observation = self.create("jig", "bench", {"measured": 14})
        self.create("cut-list", "bench", {}, reconsider_from=["foreman"])
        self.ready = {
            "format": "g60-receiver-use/1",
            "disposition": "ready",
            "candidate": "drawing",
            "observation": "jig",
            "turns": 1,
        }
        self.approved = self.update("cut-list", "bench", self.ready, ["drawing", "jig"])

    def call(self, actor, op, **args):
        self.serial += 1
        return self.db.call({"actor": actor, "op": op, "command": str(self.serial), **args})

    def create(self, target, actor, data, **args):
        return self.call(actor, "create", id=target, body=data, **args)["artifact"]

    def update(self, target, actor, data, sources=(), **args):
        c = self.call(
            actor, "capture", id="ctx" + str(self.serial), target=target, sources=list(sources)
        )["context"]
        p = self.call(actor, "propose", id="prop" + str(self.serial), context=c["id"], body=data)[
            "proposal"
        ]
        return self.call(
            actor, "publish", proposal=p["id"], account="Checked for this target.", **args
        )["artifact"]

    def test_current_decision_once_and_exact_retry(self):
        requests = [
            {"actor": "bench", "command": c, "op": "take", "approval": self.approved}
            for c in ["left", "right"]
        ]
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(self.db.call, requests))
        first = next(i for i, r in enumerate(results) if r["status"] == "accepted")
        self.assertEqual("basis-changed", results[1 - first]["code"])
        took = results[first]
        self.assertEqual(took, self.db.call(requests[first]))
        self.assertEqual(self.candidate, took["candidate"])
        self.assertEqual(self.observation, took["observation"])
        self.assertEqual(self.approved, took["approval"])
        self.assertEqual("taken", self.db.show("cut-list")["body"]["disposition"])
        events = [
            r for r in self.db.view("changes", limit=100)["items"] if r["kind"] == "received-use"
        ]
        self.assertEqual([took["event"]], [r["seq"] for r in events])

    def test_scope_and_evidence_owner(self):
        self.assertEqual("wrong-owner", self.call("maker", "take", approval=self.approved)["code"])
        p = self.update("cut-list", "bench", self.ready, ["drawing"])
        self.assertEqual("unbound-use", self.call("bench", "take", approval=p)["code"])
        self.create("vendor-note", "maker", {"checked": True})
        p = self.update(
            "cut-list",
            "bench",
            dict(self.ready, observation="vendor-note"),
            ["drawing", "vendor-note"],
        )
        self.assertEqual("wrong-use-authority", self.call("bench", "take", approval=p)["code"])
        p = self.update("cut-list", "bench", dict(self.ready, disposition="cancelled"))
        self.assertEqual("use-not-ready", self.call("bench", "take", approval=p)["code"])

    def test_changed_binding_refuses_both_equal_body_revision_and_changed_drawing(self):
        self.update("jig", "bench", {"measured": 14})
        self.assertEqual(
            "basis-changed", self.call("bench", "take", approval=self.approved)["code"]
        )
        p = self.update("cut-list", "bench", self.ready, ["drawing", "jig"])
        self.update("drawing", "maker", {"part": "different"})
        self.assertEqual("basis-changed", self.call("bench", "take", approval=p)["code"])
        self.assertEqual("ready", self.db.show("cut-list")["body"]["disposition"])

    def test_ordinary_publication_cannot_emit_a_received_use_event(self):
        forged = self.update(
            "cut-list", "bench", dict(self.ready, disposition="taken"), ["drawing", "jig"]
        )
        self.assertEqual("use-not-ready", self.call("bench", "take", approval=forged)["code"])
        self.assertFalse(
            any(r["kind"] == "received-use" for r in self.db.view("changes", limit=100)["items"])
        )
        counterfeit = self.create("sales-copy", "maker", self.ready)
        self.assertEqual("unbound-use", self.call("maker", "take", approval=counterfeit)["code"])

    def test_pending_impact_and_lease(self):
        offered = self.call(
            "foreman",
            "offer",
            target="cut-list",
            basis=[self.observation],
            body="Close the stand.",
            account="The hall is shut.",
        )
        self.assertEqual(
            "reconsideration-required", self.call("bench", "take", approval=self.approved)["code"]
        )
        self.call("bench", "acknowledge", notice=offered["notice"], account="Arrived.")
        self.assertEqual(
            "reconsideration-required", self.call("bench", "take", approval=self.approved)["code"]
        )
        p = self.update(
            "cut-list",
            "bench",
            self.ready,
            ["drawing", "jig"],
            treatments=[
                {
                    "notice": offered["notice"],
                    "disposition": "not-applicable",
                    "account": "Other hall.",
                }
            ],
        )
        lease = self.call("bench", "claim", target="cut-list", revision=p["revision"])["token"]
        self.assertEqual("claim-stale", self.call("bench", "take", approval=p)["code"])
        self.assertEqual("accepted", self.call("bench", "take", approval=p, token=lease)["status"])


if __name__ == "__main__":
    unittest.main()
