"""Judgments on an exact proposal and settlement of its captured or staged notices."""
from __future__ import annotations

from typing import Any

from .contract import account, encoded, fields, integer, loads_strict, pin, require


class ImpactReviews:
    """Staged and direct mandatory-impact review settlement."""

    def __init__(self, db: Any, revisions: Any, proposals: Any) -> None:
        self.db = db
        self.revisions = revisions
        self.proposals = proposals

    @staticmethod
    def validate_disposition(treatment: dict[str, Any]) -> None:
        require(
            treatment.get("disposition") in ("incorporated", "retained-adequate", "not-applicable"),
            "invalid-input",
            "Unknown receiving disposition",
        )
        account(treatment.get("account"))

    def review(self, r: dict[str, Any]) -> dict[str, Any]:
        fields(r, {"actor", "command", "op", "proposal", "notice", "basis", "disposition", "account"})
        proposal, context, target = self.proposals.current(r.get("proposal"), r["actor"])
        self.revisions.verify(context["basis"], current=True, keep_bodies=False)

        no = integer(r.get("notice"), "notice", 1)
        row = self.db.execute("SELECT * FROM notices WHERE id=?", (no,)).fetchone()
        require(row is not None and row["receiver"] == r["actor"], "wrong-recipient", "Notice is addressed elsewhere")
        require(row["target"] == target["id"], "wrong-receiving-target", "Review belongs at the nominated target")
        require(
            row["state"] == "pending" and row["required"] and row["kind"] == "proposed-impact",
            "notice-changed",
            "A separate review requires a pending mandatory offer",
        )

        self.validate_disposition(r)
        basis = r.get("basis")
        self.revisions.verify(basis, current=True, keep_bodies=False)

        cited = loads_strict(row["body"])["basis"]
        require(
            {p["id"] for p in basis} == {p["id"] for p in cited},
            "incomplete-basis",
            "Return a current pin for every source cited by the immutable offer",
        )

        judgment = {
            "notice": no,
            "actor": r["actor"],
            "proposal": proposal["id"],
            "target": pin(target),
            "basis": basis,
            "disposition": r["disposition"],
            "account": r["account"],
        }
        self.db.execute(
            "INSERT INTO impact_reviews VALUES (?,?,?) ON CONFLICT(proposal,notice) "
            "DO UPDATE SET judgment=excluded.judgment",
            (proposal["id"], no, encoded(judgment)),
        )
        return {"review": judgment, "state": "pending"}

    def changed(self, proposal: str, *, installed_target: str | None = None) -> Any:
        return self.db.execute(
            """
            SELECT r.notice FROM impact_reviews AS r, json_each(r.judgment, '$.basis') AS p
            WHERE r.proposal=? AND json_extract(p.value,'$.id') IS NOT ? AND EXISTS (
                SELECT 1 FROM versions AS v WHERE v.id=json_extract(p.value,'$.id')
                    AND v.revision>json_extract(p.value,'$.revision'))
            ORDER BY r.notice LIMIT 1""",
            (proposal, installed_target),
        ).fetchone()

    def missing(self, target: dict[str, Any], proposal: str, direct: Any) -> Any:
        return self.db.execute(
            """
            SELECT n.id FROM notices AS n
            WHERE n.target=? AND n.receiver=? AND n.required=1 AND n.state='pending'
                AND n.id NOT IN (SELECT value FROM json_each(?))
                AND NOT EXISTS (SELECT 1 FROM impact_reviews AS r WHERE r.proposal=? AND r.notice=n.id)
            ORDER BY n.id LIMIT 1""",
            (target["id"], target["owner"], encoded(list(direct)), proposal),
        ).fetchone()

    def next(self, ident: str) -> dict[str, Any]:
        """Find one missing or changed judgment for this exact proposal."""
        proposal, context, target = self.proposals.current(ident)
        self.revisions.verify(context["basis"], current=True, keep_bodies=False)
        row = self.missing(target, proposal["id"], [])
        if row:
            return {"notice": row[0], "reason": "unreviewed"}
        row = self.changed(proposal["id"])
        return {"notice": row[0], "reason": "changed-source"} if row else {"notice": None, "reason": None}

    def prepare(
        self,
        target: dict[str, Any],
        context: dict[str, Any],
        proposal: dict[str, Any],
        treatments: Any,
    ) -> tuple[dict[int, dict[str, Any]], int]:
        require(
            isinstance(treatments, list) and len(treatments) <= 128,
            "invalid-input",
            "Invalid treatments",
        )
        selected = {item["id"]: item for item in context["notices"]}
        treated: dict[int, dict[str, Any]] = {}

        for item in treatments:
            fields(item, {"notice", "disposition", "account"})
            no = integer(item.get("notice"), "notice", 1)
            require(
                no in selected and no not in treated,
                "uncaptured-notice",
                "Treatment must name one captured notice once",
            )
            self.validate_disposition(item)
            treated[no] = item

        missing = self.missing(target, proposal["id"], treated)
        require(
            not missing,
            "reconsideration-required",
            "A required impact lacks a judgment for this proposal",
        )
        require(not self.changed(proposal["id"]), "basis-changed", "A staged review source changed")

        for no in treated:
            require(
                not self.db.execute(
                    "SELECT 1 FROM impact_reviews WHERE proposal=? AND notice=?",
                    (proposal["id"], no),
                ).fetchone(),
                "duplicate-treatment",
                "This notice already has a staged judgment for this proposal",
            )
            actual = self.db.execute("SELECT * FROM notices WHERE id=?", (no,)).fetchone()
            require(
                actual["receiver"] == target["owner"] and actual["receipt"] is None,
                "notice-changed",
                "Notice was already treated or is addressed elsewhere",
            )
            require(
                actual["kind"] != "proposed-impact" or actual["target"] == target["id"],
                "wrong-receiving-target",
                "An offered impact must be treated at its nominated receiving target",
            )

        staged = self.db.execute(
            "SELECT count(*) FROM impact_reviews WHERE proposal=?",
            (proposal["id"],),
        ).fetchone()[0]
        return treated, staged

    def settle(
        self,
        target: dict[str, Any],
        context: dict[str, Any],
        proposal: dict[str, Any],
        record: dict[str, Any],
        treated: dict[int, dict[str, Any]],
        staged: int,
    ) -> None:
        for no, item in treated.items():
            receipt = {
                "actor": target["owner"],
                "disposition": item["disposition"],
                "account": item["account"],
                "artifact": pin(record),
                "context": context["id"],
            }
            self.db.execute(
                "UPDATE notices SET state=?,receipt=? WHERE id=?",
                ("received", encoded(receipt), no),
            )

        if staged:
            updated = self.db.execute(
                """UPDATE notices SET state='received',
                    receipt=json_set(r.judgment,'$.artifact',json(?),'$.context',?)
                    FROM impact_reviews AS r WHERE r.proposal=? AND notices.id=r.notice
                    AND notices.state='pending' AND notices.target=? AND notices.receiver=?""",
                (encoded(pin(record)), context["id"], proposal["id"], target["id"], target["owner"]),
            )
            require(
                updated.rowcount == staged,
                "notice-changed",
                "Reviewed duties changed before publication",
            )
