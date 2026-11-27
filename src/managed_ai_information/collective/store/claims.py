"""Optional owner leases. Issuance, expiration, generation fencing, and release."""

import uuid
from typing import Any

from .contract import fields, integer, name, owned, require


class Claims:
    """Owner lease manager within one collective store transaction."""

    def __init__(self, db: Any, revisions: Any, clock: Any) -> None:
        self.db = db
        self.revisions = revisions
        self.clock = clock

    def claim(self, r: dict[str, Any]) -> dict[str, Any]:
        fields(r, {"actor", "command", "op", "target", "revision", "seconds"})
        target = self.revisions.get(r.get("target"))
        owned(target, r["actor"])

        require(
            target["revision"] == integer(r.get("revision"), "revision", 1),
            "target-changed",
            "Target revision changed",
        )
        seconds = integer(r.get("seconds", 60), "claim seconds", 1, 300)
        old = self.db.get_claim(target["id"])
        require(
            not old or not old["active"] or old["expires"] <= self.clock(),
            "already-claimed",
            "This target has an active claim",
        )

        generation = int(old["generation"]) + 1 if old else 1
        token = uuid.uuid4().hex
        expiry = self.clock() + seconds
        self.db.put_claim(target["id"], r["actor"], generation, token, expiry, active=1)
        return {"target": target["id"], "generation": generation, "token": token, "expires": expiry}

    def check(self, target: str, actor: str, token: str | None) -> Any:
        lease = self.db.get_claim(target)
        if token is not None or (lease and lease["active"]):
            require(
                lease is not None
                and lease["active"]
                and lease["token"] == token
                and lease["actor"] == actor
                and lease["expires"] > self.clock(),
                "claim-stale",
                "The claim is absent, expired or superseded",
            )
        return lease

    def release(self, r: dict[str, Any]) -> dict[str, Any]:
        fields(r, {"actor", "command", "op", "target", "token"})
        target = self.revisions.get(r.get("target"))
        owned(target, r["actor"])
        self.check(target["id"], r["actor"], name(r.get("token")))
        self.finish(target["id"])
        return {"released": target["id"]}

    def finish(self, target: str) -> None:
        self.db.set_claim_inactive(target)
