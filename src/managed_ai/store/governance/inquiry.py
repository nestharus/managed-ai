"""Issue or recover contact openings under the native owner lock."""

from typing import Any

from managed_ai.core.ownership import packet_id, relative_path
from managed_ai.core.storage import read_bytes
from managed_ai.store.governance.feedback import KIND, Feedback
from managed_ai.store.protocol.identities import (
    exact_fields,
    identity,
    nonempty,
    record_identity,
    require,
)
from managed_ai.store.protocol.inputs import strings


class InquiryConflict(ValueError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details: dict[str, Any] = {"code": "inquiry_conflict", **details}


class Inquiry:
    def __init__(self, system: Any) -> None:
        self.records = system.record_store
        self.feedback = Feedback(system)

    def existing(self, ident: str) -> dict[str, Any] | None:
        return self.records.show(ident) if self.records.path(ident).exists() else None

    @staticmethod
    def paths(source: Any) -> list[str]:
        return [relative_path(p) for p in strings(source, "inquiry basis")]

    def stages(self, ident: str) -> dict[str, dict[str, Any]]:
        return {
            stage: record_identity(record)
            for stage, key in [
                ("candidate", ident + "-inquiry"),
                ("purpose", ident + "-purpose"),
                ("opening", ident),
            ]
            if (record := self.existing(key)) is not None
        }

    def matches(
        self,
        record: dict[str, Any],
        kind: str,
        content: Any,
        paths: list[str],
    ) -> bool:
        retained = record["content"]
        if kind == "candidate" and isinstance(retained, dict) and "basis" in retained:
            retained = dict(retained, basis=self.paths(retained["basis"]))

        return bool(
            record["revision"] == 1
            and record["kind"] == kind
            and retained == content
            and record["active"]
            and not record["relations"]
            and [p["path"] for p in record["basis"]] == paths
        )

    def check_record(
        self,
        ident: str,
        kind: str,
        content: Any,
        paths: list[str],
    ) -> dict[str, Any] | None:
        record = self.existing(ident)
        if record:
            if not self.matches(record, kind, content, paths):
                raise InquiryConflict(
                    "Existing identity has different contents or revision",
                    retained=record_identity(record),
                )
            if self.records.basis_changes(record) or any(
                p["sha256"] is None for p in record["basis"]
            ):
                raise InquiryConflict(
                    "Retained source edition is missing or changed",
                    retained=record_identity(record),
                )
        return record

    def ensure(
        self,
        ident: str,
        kind: str,
        content: Any,
        paths: list[str],
    ) -> dict[str, Any]:
        record = self.check_record(ident, kind, content, paths)
        if record is None:
            for path in paths:
                read_bytes(self.records.ex.path(path))
            self.records.put({"id": ident, "kind": kind, "content": content, "basis": paths}, 0)
            record = self.records.show(ident)
        return record_identity(record)

    def open(self, order: dict[str, Any]) -> dict[str, Any]:
        required = {"id", "use", "trigger", "question", "forecast", "rival"}
        exact_fields(order, required, {"basis", "outside", "replaces"}, "inquiry order")
        for key in required:
            nonempty(order[key], key)

        ident = packet_id(order["id"])
        candidate_id = packet_id(ident + "-inquiry")
        purpose_id = packet_id(ident + "-purpose")

        try:
            self.feedback.opening_id(ident)
            self.feedback.predictions(order)
            paths = self.paths(order.get("basis", []))
            if "basis" in order:
                order = dict(order, basis=paths)

            outside = order.get("outside")
            if outside is not None:
                source = self.records.head(outside)
                require(
                    source["id"] not in (ident, candidate_id),
                    "outside cannot be the opening or candidate",
                )
                self.feedback.check_source(source)

            if "replaces" in order:
                old = self.records.head(identity(order["replaces"]))
                require(
                    old["kind"] == "candidate"
                    and isinstance(old["content"], dict)
                    and old["id"] == str(old["content"].get("id")) + "-inquiry"
                    and old["id"] != candidate_id,
                    "replaces must identify a different retained inquiry candidate",
                )

            candidate = self.check_record(candidate_id, "candidate", order, paths)
            purpose = self.check_record(purpose_id, "requirement", order["use"], [])
            first = self.existing(ident)

            if first is not None:
                if first["kind"] != KIND or candidate is None or purpose is None:
                    raise ValueError("Scope belongs to another opening")
                first = self.feedback.inspect(ident, 1)["record"]
                plan = first["content"]
                require(
                    record_identity(plan["candidate"]) == record_identity(candidate)
                    and [record_identity(r) for r in plan["references"]]
                    == [record_identity(purpose)]
                    and (record_identity(plan["outside"]) if plan["outside"] else None) == outside
                    and plan["scope"] == order["use"]
                    and all(plan[k] == order[k] for k in ("forecast", "rival")),
                    "Scope belongs to another opening",
                )
                return {"ticket": record_identity(first)}

            candidate = self.ensure(candidate_id, "candidate", order, paths)
            purpose = self.ensure(purpose_id, "requirement", order["use"], [])
            self.feedback.apply(
                "open",
                {
                    "id": ident,
                    "expected": 0,
                    "scope": order["use"],
                    "candidate": candidate,
                    "references": [purpose],
                    "outside": outside,
                    "forecast": order["forecast"],
                    "rival": order["rival"],
                },
            )
            return {"ticket": record_identity(self.records.show(ident))}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            stages = self.stages(ident)
            if stages:
                recovery = (
                    "Retry the original order with its original source bytes. "
                    "For changed input use a fresh id "
                    "and set replaces to the retained candidate pin."
                    if "candidate" in stages
                    else "Inspect the occupied identity and use a fresh inquiry id."
                )
                raise InquiryConflict(str(exc), stages=stages, recovery=recovery) from exc
            raise
