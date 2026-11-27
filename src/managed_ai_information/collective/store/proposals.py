"""Immutable source captures and their proposed editions before target publication."""

from typing import Any

from .contract import (
    CONTEXT_DETAIL,
    CONTEXT_LIMIT,
    body,
    digest,
    encoded,
    fields,
    loads_strict,
    name,
    owned,
    pin,
    require,
)


class Proposals:
    """Context capture and proposal storage manager."""

    def __init__(self, db: Any, revisions: Any, notices: Any, clock: Any) -> None:
        self.db = db
        self.revisions = revisions
        self.notices = notices
        self.clock = clock

    def capture(self, r: dict[str, Any]) -> dict[str, Any]:
        fields(r, {"actor", "command", "op", "id", "target", "sources", "notices", "staged"})
        ident = name(r.get("id"))
        target = self.revisions.get(r.get("target"))
        owned(target, r["actor"])

        require(
            self.db.get_context_raw(ident) is None,
            "already-exists",
            "Context identity already exists",
        )

        sources = r.get("sources", [])
        nominated = r.get("notices", [])
        require(
            isinstance(sources, list) and len(sources) <= 128,
            "invalid-input",
            "sources must be unique identities, at most 128",
        )
        for ident_ in sources:
            name(ident_, "source identity")
        require(len(set(sources)) == len(sources), "invalid-input", "Duplicate source identity")
        require(
            isinstance(nominated, list) and len(nominated) <= 128,
            "invalid-input",
            "Too many nominated notices",
        )

        staged = r.get("staged", False)
        require(type(staged) is bool, "invalid-input", "staged must be boolean")
        selected = self.notices.select_for_capture(r["actor"], target["id"], nominated, staged)

        ids = list(sources)
        seen_ids = set(sources)
        historical_keys: dict[tuple[str, int], None] = {}
        for notice in selected:
            for item in notice["body"].get("basis", []):
                if item["id"] not in seen_ids:
                    ids.append(item["id"])
                    seen_ids.add(item["id"])
                historical_keys[(item["id"], item["revision"])] = None

        require(len(ids) <= 128, "context-bound", "Selected notice/source bases exceed 128 sources")

        captured: dict[str, Any] = {
            "id": ident,
            "actor": r["actor"],
            "target": pin(target),
            "target_body": target["body"],
            "sources": [],
            "basis": [],
            "notices": selected,
            "historical_notice_sources": [],
            "captured_at": self.clock(),
        }
        if staged:
            captured["required_review"] = "separate"

        remaining = CONTEXT_LIMIT - len(encoded(captured).encode())
        require(remaining >= 0, "context-bound", CONTEXT_DETAIL)

        for source, revision in historical_keys:
            old = self.revisions.get(source, revision, byte_limit=remaining)
            remaining -= len(encoded(old).encode()) + bool(captured["historical_notice_sources"])
            require(remaining >= 0, "context-bound", CONTEXT_DETAIL)
            captured["historical_notice_sources"].append(old)

        for source in ids:
            record = self.revisions.get(name(source), byte_limit=remaining)
            source_pin = pin(record)
            remaining -= (
                len(encoded(record).encode())
                + len(encoded(source_pin).encode())
                + 2 * bool(captured["sources"])
            )
            require(remaining >= 0, "context-bound", CONTEXT_DETAIL)
            captured["sources"].append(record)
            captured["basis"].append(source_pin)

        require(len(encoded(captured).encode()) <= CONTEXT_LIMIT, "context-bound", CONTEXT_DETAIL)
        self.db.put_context(ident, r["actor"], encoded(captured))
        return {"context": captured, "sha256": digest(captured)}

    def propose(self, r: dict[str, Any]) -> dict[str, Any]:
        fields(r, {"actor", "command", "op", "id", "context", "body"})
        ident = name(r.get("id"))
        raw_ctx = self.db.get_context_raw(name(r.get("context")))
        require(raw_ctx is not None, "missing-context", "Context is unavailable")

        context = loads_strict(raw_ctx)
        require(
            context["actor"] == r["actor"], "wrong-owner", "Context belongs to another participant"
        )
        require(
            self.db.get_proposal_raw(ident) is None,
            "already-exists",
            "Proposal identity already exists",
        )

        content = body(r.get("body"))
        proposal = {
            "id": ident,
            "actor": r["actor"],
            "context": context["id"],
            "body": content,
            "sha256": digest(content),
            "received_at": self.clock(),
        }
        self.db.put_proposal(ident, r["actor"], encoded(proposal))
        return {
            "proposal": proposal,
            "scope": (
                "The actual supplied body is retained even if its captured basis is no longer "
                "current."
            ),
        }

    def current(
        self,
        ident: Any,
        actor: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        raw_prop = self.db.get_proposal_raw(name(ident))
        require(raw_prop is not None, "missing-proposal", "Proposal is unavailable")

        proposal = loads_strict(raw_prop)
        if actor is not None:
            require(
                proposal["actor"] == actor, "wrong-owner", "Proposal belongs to another participant"
            )

        raw_ctx = self.db.get_context_raw(proposal["context"])
        context = loads_strict(raw_ctx)
        target = self.revisions.get(context["target"]["id"])
        owned(target, proposal["actor"])
        require(
            pin(target) == context["target"],
            "target-changed",
            "The target has changed since this proposal was captured",
        )
        return proposal, context, target

    def retained(self, kind: str, ident: str) -> Any:
        key = name(ident)
        if kind == "context":
            raw = self.db.get_context_raw(key)
        elif kind == "proposal":
            raw = self.db.get_proposal_raw(key)
        else:
            raise ValueError("Unknown retained kind")
        if raw is None:
            raise ValueError("Retained body is unavailable")
        return loads_strict(raw)
