"""Addressed notices, open announcements, subscription snapshots, and cursor views."""

from typing import Any

from .contract import (
    VIEW_LIMIT,
    account,
    body,
    encoded,
    fields,
    integer,
    loads_strict,
    name,
    owned,
    pin,
    require,
)


def decode_notice(row: Any) -> dict[str, Any]:
    value = dict(row)
    value["required"] = bool(value["required"])
    value["body"] = loads_strict(value["body"])
    value["acknowledgment"] = (
        loads_strict(value["acknowledgment"]) if value["acknowledgment"] else None
    )
    value["receipt"] = loads_strict(value["receipt"]) if value["receipt"] else None
    return value


class NoticeBoard:
    """Notice, event, watch, and bounded cursor-view manager."""

    def __init__(self, db: Any, revisions: Any, clock: Any) -> None:
        self.db = db
        self.revisions = revisions
        self.clock = clock

    def emit(self, kind: str, subject: str, actor: str, payload: Any) -> int:
        return int(self.db.emit_event(kind, subject, actor, encoded(payload)))

    def accepted_revision(self, record: dict[str, Any], actor: str) -> int:
        seq = self.emit("accepted-artifact", record["id"], actor, {"artifact": pin(record)})
        for watch in self.db.watches_for_subject(record["id"]):
            payload = {
                "event": seq,
                "artifact": pin(record),
                "reason": watch["reason"],
                "meaning": (
                    "A declared followed artifact changed; applicability needs interpretation."
                ),
            }
            self.db.add_notice(
                watch["actor"],
                record["id"],
                "source-change",
                0,
                encoded(payload),
                "pending",
            )
        return seq

    def offer(self, r: dict[str, Any]) -> dict[str, Any]:
        fields(r, {"actor", "command", "op", "target", "basis", "account", "body"})
        target = self.revisions.get(r.get("target"))
        pins = r.get("basis", [])
        self.revisions.verify(pins, current=False, keep_bodies=False)
        content = body(r.get("body"))
        why = account(r.get("account"))
        required = r["actor"] in target["reconsider_from"] or "*" in target["reconsider_from"]

        payload = {
            "from": r["actor"],
            "basis": pins,
            "account": why,
            "contribution": content,
            "target_at_offer": pin(target),
            "policy_at_offer": target["reconsider_from"],
        }
        notice = self.db.add_notice(
            target["owner"],
            target["id"],
            "proposed-impact",
            int(required),
            encoded(payload),
            "pending",
        )
        seq = self.emit(
            "offered-impact", target["id"], r["actor"], {"notice": notice, "required": required}
        )
        return {
            "notice": notice,
            "receiver": target["owner"],
            "required": required,
            "event": seq,
            "scope": (
                "An attributed proposed relationship; delivery does not establish uptake or truth."
            ),
        }

    def watch(self, r: dict[str, Any]) -> dict[str, Any]:
        fields(r, {"actor", "command", "op", "subject", "reason"})
        subject = name(r.get("subject"))
        reason = account(r.get("reason"))
        self.db.put_watch(r["actor"], subject, reason)
        snapshot = self.revisions.find(subject)
        cursor = self.db.max_event_seq()
        return {
            "subject": subject,
            "snapshot": snapshot,
            "cursor": cursor,
            "scope": (
                "Subscription and snapshot share this transaction; subsequent accepted changes "
                "create durable notices."
            ),
        }

    def announce(self, r: dict[str, Any]) -> dict[str, Any]:
        fields(r, {"actor", "command", "op", "artifact", "account"})
        (record,) = self.revisions.verify([r.get("artifact")], current=True)
        owned(record, r["actor"])
        note = account(r.get("account"))
        require(len(note.encode()) <= 2048, "account-bound", "An announcement must fit in 2 KiB")
        seq = self.emit(
            "open-encounter", record["id"], r["actor"], {"artifact": pin(record), "account": note}
        )
        return {
            "event": seq,
            "artifact": pin(record),
            "scope": (
                "Available for inspection in this store. No addressed notice or acceptance "
                "decision."
            ),
        }

    def acknowledge(self, r: dict[str, Any]) -> dict[str, Any]:
        fields(r, {"actor", "command", "op", "notice", "account"})
        no = integer(r.get("notice"), "notice", 1)
        why = account(r.get("account"))
        row = self.db.get_notice(no)
        require(
            row is not None and row["receiver"] == r["actor"],
            "wrong-recipient",
            "Notice is addressed elsewhere",
        )
        require(row["state"] == "pending", "notice-changed", "Notice already received")
        require(
            row["acknowledgment"] is None,
            "already-acknowledged",
            "Delivery was already acknowledged",
        )

        acknowledgment = {
            "actor": r["actor"],
            "account": why,
            "at": self.clock(),
            "scope": "Acknowledged delivery; no semantic uptake or target acceptance inferred.",
        }
        state = "pending" if row["required"] else "received"
        row["state"] = state
        row["acknowledgment"] = encoded(acknowledgment)
        return {"notice": no, "state": state, "acknowledgment": acknowledgment}

    def notice(self, ident: int) -> dict[str, Any]:
        row = self.db.get_notice(integer(ident, "notice", 1))
        require(row is not None, "missing-notice", "Notice is unavailable")
        return decode_notice(row)

    def encounters(
        self, *, after: int = 0, limit: int = 20, scan_limit: int = 256
    ) -> dict[str, Any]:
        """Return owner-selected accounts within a bounded traversal of the existing event log."""
        integer(after, "cursor")
        integer(limit, "limit", 1, 100)
        integer(scan_limit, "scan limit", 1, 2048)

        cursor = after
        examined = 0
        items: list[dict[str, Any]] = []
        for row in self.db.data["events"]:
            seq = int(row["seq"])
            if seq <= after:
                continue
            cursor = seq
            examined += 1
            if row["kind"] == "open-encounter":
                items.append({"event": cursor, "actor": row["actor"], **loads_strict(row["body"])})
                if len(items) == limit:
                    break
            if examined >= scan_limit:
                break

        more = any(int(ev["seq"]) > cursor for ev in self.db.data["events"])
        return {
            "items": items,
            "after": after,
            "next_after": cursor,
            "examined_events": examined,
            "more_events": more,
            "scope": (
                "Owner-selected accounts at their recorded revisions. Inspect the source and "
                "current receiving use before acting."
            ),
        }

    def view(
        self, kind: str, *, actor: str | None = None, after: int = 0, limit: int = 20
    ) -> dict[str, Any]:
        integer(after, "cursor")
        integer(limit, "limit", 1, 100)

        if kind == "inbox":
            name(actor, "actor")
            matching = [
                row
                for row in self.db.data["notices"]
                if row["receiver"] == actor and row["state"] == "pending" and int(row["id"]) > after
            ]
            total = len(matching)
            values = [decode_notice(row) for row in matching[:limit]]
            key = "id"
        elif kind == "changes":
            matching_events = [row for row in self.db.data["events"] if int(row["seq"]) > after]
            total = len(matching_events)
            values = [
                {**dict(row), "body": loads_strict(row["body"])} for row in matching_events[:limit]
            ]
            key = "seq"
        else:
            raise ValueError("Unknown view")

        selected: list[dict[str, Any]] = []
        size = 0
        for value in values:
            item_bytes = len(encoded(value).encode())
            if size + item_bytes > VIEW_LIMIT:
                break
            selected.append(value)
            size += item_bytes

        return {
            "items": selected,
            "after": after,
            "next_after": selected[-1][key] if selected else after,
            "remaining_count": total - len(selected),
            "complete": total == len(selected),
            "scope": (
                "A bounded exact view of installed records; unknown relevance, unregistered routes "
                "and semantic completeness are not inferred."
            ),
        }

    def select_for_capture(
        self,
        actor: str,
        target: str,
        nominated: list[int],
        staged: bool,
    ) -> list[dict[str, Any]]:
        required_rows: list[dict[str, Any]] = []
        if not staged:
            for row in self.db.data["notices"]:
                if (
                    row["receiver"] == actor
                    and row["target"] == target
                    and row["required"] == 1
                    and row["state"] == "pending"
                ):
                    required_rows.append(row)
                    if len(required_rows) >= 129:
                        break
        selected = {int(row["id"]): decode_notice(row) for row in required_rows}
        for no in nominated:
            row = self.db.get_notice(integer(no, "notice", 1))
            require(
                row is not None and row["receiver"] == actor,
                "wrong-recipient",
                "Notice is not addressed to this participant",
            )
            selected[no] = decode_notice(row)

        require(
            len(selected) <= 128,
            "context-bound",
            "This bounded adapter cannot capture more than 128 selected notices; the unresolved "
            "receiving duty remains pending",
        )
        return list(selected.values())

    def has_pending_required(self, target: dict[str, Any]) -> bool:
        return any(
            row["target"] == target["id"]
            and row["receiver"] == target["owner"]
            and row["required"] == 1
            and row["state"] == "pending"
            for row in self.db.data["notices"]
        )
