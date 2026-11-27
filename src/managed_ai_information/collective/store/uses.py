"""Continuing evidence enrollment and actual consumption at a receiving target."""

from typing import Any

from .contract import Refused, account, encoded, fields, loads_strict, name, owned, pin, require


class ReceivingUses:
    """Continuing-evidence enrollment and point-of-use admission manager."""

    def __init__(self, db: Any, revisions: Any, notices: Any, claims: Any, reviews: Any) -> None:
        self.db = db
        self.revisions = revisions
        self.notices = notices
        self.claims = claims
        self.reviews = reviews

    @staticmethod
    def ready(content: Any) -> bool:
        return (
            isinstance(content, dict)
            and content.get("format") == "g60-receiver-use/1"
            and content.get("disposition") == "ready"
        )

    def prepare_publication(
        self,
        target: dict[str, Any],
        context: dict[str, Any],
        proposal: dict[str, Any],
        why: str,
    ) -> None:
        if self.ready(proposal["body"]):
            self.renew_captured(target, context, proposal, why)
            self.require_current(target["id"])
            self.enroll(target["id"], proposal["id"], context["basis"])

    def changed(self, target: str) -> str | None:
        for source, raw_pin in sorted(self.db.use_sources_for_target(target).items()):
            prior = loads_strict(raw_pin)
            if self.db.max_version_revision(source) > prior["revision"]:
                return raw_pin
        return None

    def source(self, target: str, source: str | None = None) -> dict[str, Any] | None:
        """Expose one carried relation at its original and current source editions."""
        name(target)
        if source is None:
            raw_pin = self.changed(target)
        else:
            raw_pin = self.db.get_use_source(target, name(source))
        if raw_pin is None:
            return None

        prior = loads_strict(raw_pin)
        return {"source": prior, "current": self.revisions.source(prior["id"])}

    def require_current(self, target: str) -> None:
        stale = self.changed(target)
        if stale is not None:
            prior = loads_strict(stale)
            raise Refused(
                "basis-changed", "Requalify the receiving relationship for " + prior["id"]
            )

    def qualify(self, r: dict[str, Any]) -> dict[str, Any]:
        fields(
            r,
            {
                "actor",
                "command",
                "op",
                "target",
                "prior",
                "current",
                "disposition",
                "account",
                "token",
            },
        )
        (target,) = self.revisions.verify([r.get("target")], current=True)
        owned(target, r["actor"])
        self.claims.check(target["id"], r["actor"], r.get("token"))

        current = r.get("current")
        self.revisions.verify([current], current=True, keep_bodies=False)
        if not isinstance(current, dict):
            raise Refused("invalid-input", "Incomplete source pin")
        raw_prior = self.db.get_use_source(target["id"], current["id"])
        require(
            raw_prior is not None and loads_strict(raw_prior) == r.get("prior"),
            "use-relation-changed",
            "Return the exact prior source edition enrolled for this receiving target",
        )

        disposition = r.get("disposition")
        require(
            disposition in ("renew", "release"),
            "invalid-input",
            "Choose renew or release for this source",
        )
        why = account(r.get("account"))
        if disposition == "renew":
            self.db.put_use_source(target["id"], current["id"], encoded(current), overwrite=True)
        else:
            self.db.delete_use_source(target["id"], current["id"])

        decision = {
            "target": pin(target),
            "prior": r["prior"],
            "current": current,
            "disposition": disposition,
            "account": why,
        }
        event = self.notices.emit("qualified-use-source", target["id"], r["actor"], decision)
        return {"judgment": decision, "event": event}

    def renew_captured(
        self,
        target: dict[str, Any],
        context: dict[str, Any],
        proposal: dict[str, Any],
        why: str,
    ) -> None:
        for current in context["basis"]:
            raw_old = self.db.get_use_source(target["id"], current["id"])
            if raw_old is not None:
                old_pin = loads_strict(raw_old)
                if old_pin != current:
                    self.db.put_use_source(
                        target["id"], current["id"], encoded(current), overwrite=True
                    )
                    self.notices.emit(
                        "qualified-use-source",
                        target["id"],
                        target["owner"],
                        {
                            "target": pin(target),
                            "prior": old_pin,
                            "current": current,
                            "disposition": "renew",
                            "account": why,
                            "context": context["id"],
                            "proposal": proposal["id"],
                        },
                    )

    def enroll(self, target: str, proposal: str, basis: list[dict[str, Any]]) -> None:
        for item in basis:
            if item["id"] != target:
                self.db.put_use_source(target, item["id"], encoded(item), overwrite=False)
        for raw_judgment in self.db.reviews_for_proposal(proposal).values():
            judgment = loads_strict(raw_judgment)
            for item in judgment.get("basis", ()):
                if item["id"] != target:
                    self.db.put_use_source(target, item["id"], encoded(item), overwrite=False)

    def take(self, r: dict[str, Any]) -> dict[str, Any]:
        """Admit the exact reviewed edition at this receiver's local point of use."""
        fields(r, {"actor", "command", "op", "approval", "token"})
        (approval,) = self.revisions.verify([r.get("approval")], current=True)
        owned(approval, r["actor"])

        decision = approval["body"]
        require(
            self.ready(decision),
            "use-not-ready",
            "This receiver has no ready decision at that revision",
        )

        candidate_id = name(decision.get("candidate"), "candidate identity")
        observation_id = name(decision.get("observation"), "observation identity")
        outside = [item for item in approval["basis"] if item["id"] != approval["id"]]
        sources = {item["id"]: item for item in self.revisions.verify(outside, current=True)}

        require(
            candidate_id != observation_id
            and candidate_id in sources
            and observation_id in sources,
            "unbound-use",
            "Capture both the candidate and the receiving observation before approval",
        )
        require(
            sources[candidate_id]["owner"] != r["actor"]
            and sources[observation_id]["owner"] == r["actor"]
            and observation_id != approval["id"],
            "wrong-use-authority",
            "This use requires a separate producer and a receiver-owned observation",
        )

        self.require_current(approval["id"])
        qualified = any(
            ev["kind"] == "qualified-use-source"
            and ev["subject"] == approval["id"]
            and loads_strict(ev["body"]).get("target", {}).get("revision") == approval["revision"]
            for ev in self.db.data["events"]
        )
        require(
            not qualified,
            "basis-changed",
            "Issue a new decision after changing its receiving relationship",
        )

        if approval.get("staged_treatments"):
            require(
                self.reviews.changed(approval["proposal"], installed_target=approval["id"]) is None,
                "basis-changed",
                "A source used to treat an offer changed before receiving use",
            )

        require(
            not self.notices.has_pending_required(approval),
            "reconsideration-required",
            "Treat the new required observation before use",
        )

        lease = self.claims.check(approval["id"], r["actor"], r.get("token"))
        bindings = {
            "approval": pin(approval),
            "candidate": pin(sources[candidate_id]),
            "observation": pin(sources[observation_id]),
        }
        content = {"format": "g60-receiver-use/1", "disposition": "taken", **bindings}
        record = self.revisions.successor(
            approval,
            content,
            approval["basis"],
            account="Admitted the approved edition at this receiving target.",
        )
        self.revisions.save(record)
        self.notices.accepted_revision(record, r["actor"])
        self.db.delete_use_source(approval["id"])
        if lease:
            self.claims.finish(approval["id"])

        event = self.notices.emit("received-use", approval["id"], r["actor"], bindings)
        return {
            "event": event,
            "artifact": pin(record),
            **bindings,
            "scope": (
                "One local admission. Follow the pinned editions for the actual payload and "
                "instructions."
            ),
        }
