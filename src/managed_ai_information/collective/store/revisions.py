"""Versioned target bodies and their exact source pins, within a caller's transaction."""

from typing import Any

from .contract import (
    CONTEXT_DETAIL,
    body,
    digest,
    encoded,
    fields,
    integer,
    loads_strict,
    name,
    pin,
    require,
)


class Revisions:
    """Artifact and versioned-body storage manager."""

    def __init__(self, db: Any, clock: Any) -> None:
        self.db = db
        self.clock = clock

    def _raw(self, ident: str, revision: int | None = None) -> str:
        name(ident)
        if revision is None:
            raw = self.db.artifact_raw(ident)
        else:
            rev = integer(revision, "revision", 1)
            raw = self.db.version_raw(ident, rev)
        require(raw is not None, "missing-artifact", "Artifact or version is unavailable: " + ident)
        return str(raw)

    def get(
        self,
        ident: str,
        revision: int | None = None,
        *,
        byte_limit: int | None = None,
    ) -> dict[str, Any]:
        raw = self._raw(ident, revision)
        if byte_limit is not None:
            require(len(raw.encode("utf-8")) <= byte_limit, "context-bound", CONTEXT_DETAIL)
        return loads_strict(raw)

    def source(
        self,
        ident: str,
        revision: int | None = None,
        *,
        with_body: bool = False,
    ) -> dict[str, Any]:
        """Read a bounded content slice or just its pin without expanding a revision's audit
        trail.
        """
        record = loads_strict(self._raw(ident, revision))
        value: dict[str, Any] = {
            "id": ident,
            "revision": record["revision"],
            "sha256": record["sha256"],
        }
        if with_body:
            value.update(owner=record["owner"], body=record["body"])
        return value

    def verify(
        self,
        pins: Any,
        *,
        current: bool,
        keep_bodies: bool = True,
    ) -> list[dict[str, Any]]:
        require(
            isinstance(pins, list) and len(pins) <= 128,
            "invalid-input",
            "Basis must contain at most 128 pins",
        )
        seen: set[str] = set()
        complete: list[dict[str, Any]] = []

        for value in pins:
            fields(value, {"id", "revision", "sha256"})
            require(
                set(value) == {"id", "revision", "sha256"}, "invalid-input", "Incomplete source pin"
            )
            name(value["id"], "source identity")
            integer(value["revision"], "source revision", 1)
            require(value["id"] not in seen, "invalid-input", "Duplicate basis identity")
            seen.add(value["id"])

            revision = None if current else value["revision"]
            reader = self.get if keep_bodies else self.source
            actual = reader(value["id"], revision)
            require(
                pin(actual) == value,
                "basis-changed" if current else "unknown-pin",
                "The nominated basis does not match: " + value["id"],
            )
            if keep_bodies:
                complete.append(actual)

        return complete

    def save(self, record: dict[str, Any]) -> None:
        raw = encoded(record)
        self.db.save_artifact(record["id"], record["revision"], raw)

    def successor(
        self,
        target: dict[str, Any],
        content: Any,
        basis: list[dict[str, Any]],
        **account_fields: Any,
    ) -> dict[str, Any]:
        return {
            "id": target["id"],
            "revision": target["revision"] + 1,
            "owner": target["owner"],
            "body": content,
            "sha256": digest(content),
            "basis": basis,
            "reconsider_from": target["reconsider_from"],
            "accepted_at": self.clock(),
            "previous": pin(target),
            **account_fields,
        }

    def find(self, ident: str) -> dict[str, Any] | None:
        raw = self.db.artifact_raw(ident)
        return loads_strict(raw) if raw is not None else None

    def first(self, r: dict[str, Any]) -> dict[str, Any]:
        fields(r, {"actor", "command", "op", "id", "body", "reconsider_from"})
        ident = name(r.get("id"))
        require(
            self.db.artifact_raw(ident) is None,
            "already-exists",
            "Artifact identity already exists",
        )

        policy = r.get("reconsider_from", [])
        require(
            isinstance(policy, list) and len(policy) <= 128,
            "invalid-input",
            "reconsider_from must be a unique participant list",
        )
        for participant in policy:
            name(participant, "participant")
        require(len(set(policy)) == len(policy), "invalid-input", "Duplicate participant")

        content = body(r.get("body"))
        return {
            "id": ident,
            "revision": 1,
            "owner": r["actor"],
            "body": content,
            "sha256": digest(content),
            "basis": [],
            "reconsider_from": policy,
            "accepted_at": self.clock(),
            "previous": None,
        }
