"""Current whole-goal interpretation and its re-evaluation against record heads."""

import uuid
from pathlib import Path
from typing import Any

from managed_ai.core.ownership import relative_path
from managed_ai.core.storage import now, write_json
from managed_ai.store.protocol.inputs import integer, read, strings, text


class GoalUse:
    def __init__(self, records: Any, exchange: Any) -> None:
        self.records = records
        self.ex = exchange

    def _obligation_basis(self, heads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {k: r[k] for k in ("id", "revision", "sha256")}
            for r in heads
            if r["kind"] == "obligation"
        ]

    def _obligation_changes(
        self,
        interpretation: dict[str, Any] | None,
        heads: list[dict[str, Any]] | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        if interpretation is None:
            return "no-interpretation", []

        captured = interpretation.get("obligation_basis")
        if not isinstance(captured, list):
            return "unbound", []

        previous = {r["id"]: r for r in captured}
        if heads is None:
            heads = self.records.all()
        current = {r["id"]: r for r in self._obligation_basis(heads)}

        changes: list[dict[str, Any]] = []
        for ident in sorted(set(previous) | set(current)):
            old, new = previous.get(ident), current.get(ident)
            if old != new:
                change = "added" if old is None else "removed" if new is None else "revised"
                changes.append({"id": ident, "change": change, "captured": old, "current": new})

        return ("changed" if changes else "current"), changes

    def current_use(
        self,
        heads: list[dict[str, Any]] | None = None,
        *,
        goal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if goal is None:
            goal = self.records.show("goal")

        matches = []
        for path in sorted((self.records.work / "interpretations").glob("*.json")):
            row = read(path)
            if row["goal_revision"] == goal["revision"] and row["goal_sha256"] == goal["sha256"]:
                matches.append(row)

        selected = matches[-1] if matches else None
        changes = self.records.basis_changes({"basis": selected["evidence"]}) if selected else []
        obligation_binding, obligation_changes = self._obligation_changes(selected, heads)

        reasons = []
        if selected is None:
            reasons.append("no-interpretation-for-current-goal")
        if changes:
            reasons.append("evidence-changed")
        if obligation_binding in {"unbound", "changed"}:
            reasons.append("separate-obligations-" + obligation_binding)

        return {
            "goal_revision": goal["revision"],
            "interpretation": selected,
            "evidence_changes": changes,
            "obligation_binding": obligation_binding,
            "obligation_changes": obligation_changes,
            "reassessment_reasons": reasons,
            "needs_interpretation": bool(reasons),
            "scope": "Explicit whole-goal judgment; local item counts do not determine this.",
        }

    def interpret(
        self,
        revision: int,
        disposition: str,
        account: Path,
        evidence: list[str],
        condition: str | None = None,
    ) -> dict:
        goal = self.records.show("goal")
        if integer(revision, "goal revision", 1) != goal["revision"]:
            raise ValueError("interpretation belongs to another current goal revision")
        if disposition not in {"met", "continue", "wait"}:
            raise ValueError("unknown current-use disposition")
        if disposition == "wait":
            text(condition, "wait condition")

        body = account.read_text(encoding="utf-8")
        text(body, "public current-use account")
        captured = [self.ex.snapshot(relative_path(p)) for p in strings(evidence, "evidence")]
        heads = self.records.all()

        row = {
            "format": self.records.format,
            "id": uuid.uuid4().hex,
            "created_at": now(),
            "goal_source": self.records.pin(goal),
            "goal_revision": revision,
            "goal_sha256": goal["sha256"],
            "disposition": disposition,
            "account": body,
            "evidence": captured,
            "obligation_basis": self._obligation_basis(heads),
            "obligation_sources": [self.records.pin(r) for r in heads if r["kind"] == "obligation"],
            "condition": condition,
            "scope": "Caller-authored whole-goal interpretation.",
        }
        filename = row["created_at"].replace(":", "-") + "-" + row["id"] + ".json"
        write_json(self.records.work / "interpretations" / filename, row)
        return row
