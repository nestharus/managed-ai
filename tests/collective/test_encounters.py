"""Exercise owner-nominated contact in the existing collective store."""

import sys
import tempfile
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from managed_ai_information.collective import Collective, Refused, pin  # noqa: E402


class Encounters(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "room.sqlite"
        Collective.initialize(self.path)
        self.store = Collective(self.path)

    def cmd(self, actor, command, op, **kw):
        return self.store.call(dict(actor=actor, command=command, op=op, **kw))

    def create(self, who, label):
        self.cmd(who, "create-" + label, "create", id=label, body={"detail": label})
        return pin(self.store.show(label))

    def test_current_owned_and_idempotent(self):
        source = self.create("first", "paper")
        data = {"artifact": source, "account": "A shadow appears at a second angle."}
        wrong = self.cmd("other", "wrong", "announce", **data)
        self.assertEqual(wrong["code"], "wrong-owner")
        forged = {**source, "sha256": "0" * 64}
        self.assertEqual(
            self.cmd("first", "bad", "announce", artifact=forged, account="Test")["code"],
            "basis-changed",
        )
        self.assertEqual(
            self.cmd("first", "long", "announce", artifact=source, account="é" * 1025)["code"],
            "account-bound",
        )
        receipt = self.cmd("first", "first-post", "announce", **data)
        self.assertEqual(receipt, self.cmd("first", "first-post", "announce", **data))
        with self.assertRaises(Refused) as conflict:
            self.cmd("first", "first-post", "announce", artifact=source, account="Another account")
        self.assertEqual(conflict.exception.code, "command-conflict")
        self.assertEqual(self.store.view("inbox", actor="other")["items"], [])
        self.assertEqual(len(Collective(self.path).encounters()["items"]), 1)
        self.assertEqual(self.store.show("paper", source["revision"])["body"], {"detail": "paper"})

    def test_bounded_cursor_passes_noise_without_losing_contact(self):
        a = self.create("owner", "a")
        for n in range(5):
            self.create("owner", "n" + str(n))
        for n in range(4):
            self.cmd("owner", "a" + str(n), "announce", artifact=a, account="Observation " + str(n))
        page = self.store.encounters(limit=1, scan_limit=3)
        self.assertEqual(
            (page["items"], page["examined_events"], page["more_events"]), ([], 3, True)
        )
        cursor = page["next_after"]
        seen = []
        while True:
            page = self.store.encounters(after=cursor, limit=1, scan_limit=2)
            self.assertLessEqual(page["examined_events"], 2)
            seen.extend(x["account"] for x in page["items"])
            cursor = page["next_after"]
            if not page["more_events"]:
                break
        self.assertEqual(seen, ["Observation " + str(n) for n in range(4)])
        self.assertFalse(self.store.encounters(after=cursor)["items"])
        self.cmd("owner", "new", "announce", artifact=a, account="Later")
        self.assertEqual(self.store.encounters(after=cursor)["items"][0]["account"], "Later")

    def test_contact_can_become_scoped_offer_without_authority(self):
        a = self.create("writer", "letter")
        b = self.create("editor", "question")
        self.cmd(
            "editor", "open", "announce", artifact=b, account="How far can this picture travel?"
        )
        target = self.store.encounters()["items"][0]["artifact"]["id"]
        item = self.cmd(
            "writer",
            "proposal",
            "offer",
            target=target,
            basis=[a],
            account="Try the actual recipient.",
            body="Independent return",
        )
        self.assertEqual(item["status"], "accepted")
        self.assertFalse(item["required"])
        notices = self.store.view("inbox", actor="editor")["items"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(self.store.show(target)["revision"], 1)
        with self.assertRaises(Refused):
            self.store.encounters(limit=0)


if __name__ == "__main__":
    unittest.main()
