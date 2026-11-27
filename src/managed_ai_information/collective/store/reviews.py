"""Judgments on an exact proposal and settlement of its captured or staged notices."""

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
        fields(
            r, {"actor", "command", "op", "proposal", "notice", "basis", "disposition", "account"}
        )
        proposal, context, target = self.proposals.current(r.get("proposal"), r["actor"])
        self.revisions.verify(context["basis"], current=True, keep_bodies=False)

        no = integer(r.get("notice"), "notice", 1)
        row = self.db.get_notice(no)
        require(
            row is not None and row["receiver"] == r["actor"],
            "wrong-recipient",
            "Notice is addressed elsewhere",
        )
        require(
            row["target"] == target["id"],
            "wrong-receiving-target",
            "Review belongs at the nominated target",
        )
        require(
            row["state"] == "pending" and row["required"] and row["kind"] == "proposed-impact",
            "notice-changed",
            "A separate review requires a pending mandatory offer",
        )

        self.validate_disposition(r)
        basis = r.get("basis")
        self.revisions.verify(basis, current=True, keep_bodies=False)
        if not isinstance(basis, list):
            raise ValueError("Invalid basis")

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
        self.db.put_review(proposal["id"], no, encoded(judgment))
        return {"review": judgment, "state": "pending"}

    def changed(self, proposal: str, *, installed_target: str | None = None) -> int | None:
        for notice_id, raw_judgment in sorted(self.db.reviews_for_proposal(proposal).items()):
            judgment = loads_strict(raw_judgment)
            for item in judgment.get("basis", ()):
                src_id = item["id"]
                if (
                    src_id != installed_target
                    and self.db.max_version_revision(src_id) > item["revision"]
                ):
                    return notice_id
        return None

    def missing(self, target: dict[str, Any], proposal: str, direct: Any) -> int | None:
        direct_set = set(direct)
        reviewed = set(self.db.reviews_for_proposal(proposal))
        for row in self.db.data["notices"]:
            if (
                row["target"] == target["id"]
                and row["receiver"] == target["owner"]
                and row["required"] == 1
                and row["state"] == "pending"
                and row["id"] not in direct_set
                and row["id"] not in reviewed
            ):
                return int(row["id"])
        return None

    def next(self, ident: str) -> dict[str, Any]:
        """Find one missing or changed judgment for this exact proposal."""
        proposal, context, target = self.proposals.current(ident)
        self.revisions.verify(context["basis"], current=True, keep_bodies=False)
        missing_id = self.missing(target, proposal["id"], [])
        if missing_id is not None:
            return {"notice": missing_id, "reason": "unreviewed"}
        changed_id = self.changed(proposal["id"])
        return (
            {"notice": changed_id, "reason": "changed-source"}
            if changed_id is not None
            else {"notice": None, "reason": None}
        )

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

        missing_id = self.missing(target, proposal["id"], treated)
        require(
            missing_id is None,
            "reconsideration-required",
            "A required impact lacks a judgment for this proposal",
        )
        require(
            self.changed(proposal["id"]) is None,
            "basis-changed",
            "A staged review source changed",
        )

        for no in treated:
            require(
                self.db.get_review(proposal["id"], no) is None,
                "duplicate-treatment",
                "This notice already has a staged judgment for this proposal",
            )
            actual = self.db.get_notice(no)
            require(
                actual is not None
                and actual["receiver"] == target["owner"]
                and actual["receipt"] is None,
                "notice-changed",
                "Notice was already treated or is addressed elsewhere",
            )
            require(
                actual["kind"] != "proposed-impact" or actual["target"] == target["id"],
                "wrong-receiving-target",
                "An offered impact must be treated at its nominated receiving target",
            )

        staged = len(self.db.reviews_for_proposal(proposal["id"]))
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
        artifact_pin = pin(record)
        for no, item in treated.items():
            receipt = {
                "actor": target["owner"],
                "disposition": item["disposition"],
                "account": item["account"],
                "artifact": artifact_pin,
                "context": context["id"],
            }
            row = self.db.get_notice(no)
            if row is not None:
                row["state"] = "received"
                row["receipt"] = encoded(receipt)

        if staged:
            updated = 0
            for notice_id, raw_judgment in self.db.reviews_for_proposal(proposal["id"]).items():
                row = self.db.get_notice(notice_id)
                if (
                    row is not None
                    and row["state"] == "pending"
                    and row["target"] == target["id"]
                    and row["receiver"] == target["owner"]
                ):
                    judgment = loads_strict(raw_judgment)
                    judgment["artifact"] = artifact_pin
                    judgment["context"] = context["id"]
                    row["state"] = "received"
                    row["receipt"] = encoded(judgment)
                    updated += 1
            require(
                updated == staged,
                "notice-changed",
                "Reviewed duties changed before publication",
            )
