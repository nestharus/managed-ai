"""Record validation, normalization, basis verification, and retained history pagination."""

import hashlib
import json
from collections.abc import Iterator
from typing import Any

from ..protocol.basis_pins import (
    content_addressed_sha as content_addressed_sha,
)
from ..protocol.basis_pins import (
    is_content_addressed_basis as is_content_addressed_basis,
)
from ..protocol.identities import (
    digest,
    exact_fields,
    identity,
    packet_id,
    record_identity,
    require,
    strict_json,
)
from ..protocol.inputs import (
    accounted_closure,
    fields,
    relative_path,
    strings,
    text,
)


def validate_goal_record(goal: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(goal, dict):
        raise ValueError("goal record must be an object")
    if "id" not in goal:
        goal = {"id": "goal", "kind": "goal", "content": goal}
    if goal.get("id") != "goal" or goal.get("kind") != "goal":
        raise ValueError("the initial record must have id=goal and kind=goal")
    if not isinstance(goal.get("content"), dict):
        raise ValueError("goal content must be an object")
    text(goal["content"].get("contribution"), "goal contribution")
    return goal


def merge_goal_obligations(content: dict[str, Any], previous: dict[str, Any] | None) -> None:
    rows = content.get("obligations", [])
    if not isinstance(rows, list):
        raise ValueError("goal obligations must be a list of {id, text, disposition?} objects")

    merged = {r["id"]: r for r in (previous or {}).get("content", {}).get("obligations", [])}
    seen = set()

    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each goal obligation must be an object")
        ident = packet_id(row.get("id", ""))
        if ident in seen:
            raise ValueError("duplicate goal obligation")
        seen.add(ident)
        text(row.get("text"), "obligation text")
        disposition = text(row.get("disposition", "open"), "obligation disposition")
        if disposition in {"fulfilled", "released"}:
            text(row.get("account"), "closed obligation account")
        merged[ident] = dict(row, disposition=disposition)

    content["obligations"] = list(merged.values())


def validate_record_relations(relations: Any) -> list[dict[str, Any]]:
    if not isinstance(relations, list):
        raise ValueError("relations must be a list of objects")
    for relation in relations:
        if not isinstance(relation, dict):
            raise ValueError("relation must be an object")
        text(relation.get("relation"), "relation label")
        text(relation.get("target"), "relation target")
    return relations


def capture_basis_item(store: Any, source: str, *, retain_basis: bool) -> dict[str, Any]:
    if source.startswith("object:"):
        key = digest(source[7:], "object basis sha256")
        raw = store.objects.read(key, retained=True)
        return {"path": source, "sha256": key, "bytes": len(raw)}

    rel = relative_path(source)
    path = store.ex.path(rel)
    if not path.exists():
        embedded = content_addressed_sha(rel, prefixes=store.content_addressed_basis_prefixes)
        if embedded is not None and store.objects.has(embedded):
            raw = store.objects.read(embedded, retained=True)
            return {"path": rel, "sha256": embedded, "bytes": len(raw)}

    return store.ex.snapshot(rel, optional=True) if retain_basis else store._preview_basis(rel)


def _resolve_basis(
    store: Any,
    record: dict[str, Any],
    previous: dict[str, Any] | None,
    prior_owed: bool,
    owed: bool,
    retain_basis: bool,
    preserve_basis: list[dict[str, Any]] | None,
    verify_preserved: bool,
) -> list[dict[str, Any]]:
    if preserve_basis is not None:
        if verify_preserved:
            with store.objects.pack_session():
                require(
                    all(
                        r["sha256"] is None or store.objects.has(r["sha256"])
                        for r in preserve_basis
                    ),
                    "preserved basis object is unavailable",
                )
        return preserve_basis
    if previous is not None and prior_owed and owed and "basis" not in record:
        return previous["basis"]
    return [
        capture_basis_item(store, item, retain_basis=retain_basis)
        for item in strings(record.get("basis", []), "record basis")
    ]


def _check_obligation_basis_change(
    content: Any, basis: list[dict[str, Any]], previous: dict[str, Any]
) -> None:
    current = {r["path"]: r["sha256"] for r in basis}
    if not any(
        r["path"] not in current or current[r["path"]] != r["sha256"] for r in previous["basis"]
    ):
        return
    change = content.get("basis_change") if isinstance(content, dict) else None
    if (
        not isinstance(change, dict)
        or type(change.get("from_revision")) is not int
        or change["from_revision"] != previous["revision"]
    ):
        raise ValueError("changed obligation basis requires an account for the previous revision")
    text(change.get("account"), "account for changed obligation basis")


def normalize_record(
    store: Any,
    record: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    retain_basis: bool = True,
    preserve_basis: list[dict[str, Any]] | None = None,
    verify_preserved_objects: bool = True,
) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    json.dumps(record, allow_nan=False)
    fields(record, {"id", "kind", "content", "relations", "basis", "active"}, "record")
    ident, kind = packet_id(record.get("id", "")), text(record.get("kind"), "record kind")
    if "content" not in record:
        raise ValueError("record content is required")
    active = record.get("active", True)
    if type(active) is not bool:
        raise ValueError("active must be Boolean navigation metadata")
    relations = validate_record_relations(record.get("relations", []))
    content = record["content"]
    if (
        kind == "obligation"
        and isinstance(content, dict)
        and content.get("disposition") in ("fulfilled", "released")
    ):
        text(content.get("account"), "closed obligation account")
    prior_owed = bool(
        previous and previous["kind"] == "obligation" and not accounted_closure(previous["content"])
    )
    owed = kind == "obligation" and not accounted_closure(content)
    if prior_owed and kind != "obligation":
        raise ValueError("account for the owed obligation before reclassification")
    if ident == "goal":
        store.goal_record(record)
        if not active:
            raise ValueError("the current goal cannot be hidden by navigation metadata")
        content = dict(content)
        merge_goal_obligations(content, previous)
    basis = _resolve_basis(
        store,
        record,
        previous,
        prior_owed,
        owed,
        retain_basis,
        preserve_basis,
        verify_preserved_objects,
    )
    if previous is not None and prior_owed and owed:
        _check_obligation_basis_change(content, basis, previous)
    return {
        "id": ident,
        "kind": kind,
        "content": content,
        "relations": relations,
        "basis": basis,
        "active": active,
    }


def verify_content_addressed_basis_item(
    store: Any,
    item: dict[str, Any],
    verified_objects: set[tuple[str, int]] | None,
) -> str | None:
    if not is_content_addressed_basis(
        item, prefixes=store.content_addressed_basis_prefixes
    ) or not store.objects.has(item["sha256"]):
        return None

    obj_key = (item["sha256"], item["bytes"])
    if verified_objects is not None and obj_key in verified_objects:
        return str(item["sha256"])

    try:
        store.objects.read(item["sha256"], retained=True, expected_bytes=item["bytes"])
    except FileNotFoundError:
        return None

    if verified_objects is not None:
        verified_objects.add(obj_key)
    return str(item["sha256"])


def compute_basis_changes(
    store: Any,
    record: dict[str, Any],
    *,
    resolved_hashes: dict[str, str | None] | None = None,
    verified_objects: set[tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    basis = record["basis"]
    if not basis:
        return []

    if resolved_hashes is not None:
        hashes = resolved_hashes
    else:
        workspace_paths = [item["path"] for item in basis if not item["path"].startswith("object:")]
        hashes = store.ex.hash_workspace_paths(workspace_paths) if workspace_paths else {}

    changes = []
    with store.objects.pack_session():
        for item in basis:
            path = item["path"]
            current = None if path.startswith("object:") else hashes.get(path)
            if current is None:
                current = verify_content_addressed_basis_item(store, item, verified_objects)
            if current != item["sha256"]:
                changes.append({"path": path, "captured": item["sha256"], "current": current})
    return changes


def read_historical_object(store: Any, ident: str, revision: int, key: Any) -> dict[str, Any]:
    digest(key, "retained record object")
    record = strict_json(store.objects.read(key, retained=True))
    require(
        record.get("format") == store.format
        and record.get("id") == ident
        and type(record.get("revision")) is int
        and record["revision"] == revision,
        "broken record lineage",
    )
    try:
        store._verify_record(ident, record)
    except ValueError:
        raise ValueError("retained record digest changed") from None
    return record


def iter_history_pages(
    store: Any,
    ident: str,
    *,
    limit: int = 8,
    cursor: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield descending verified pages. Reauthenticate the prefix once on external restart."""
    require(type(limit) is int and 1 <= limit <= 32, "history page limit must be 1..32")
    current, raw = store.read(ident)
    head = dict(record_identity(current), object=hashlib.sha256(raw).hexdigest())
    identity(head, object_pin=True)
    if cursor is not None:
        exact_fields(cursor, {"head", "next_revision", "next_object"}, set(), "history cursor")
        captured = identity(cursor["head"], object_pin=True)
        require(captured["id"] == ident, "history cursor record changed")
        target = cursor["next_revision"]
        require(
            type(target) is int and 1 <= target < captured["revision"], "invalid cursor revision"
        )
        digest(cursor["next_object"], "cursor next object")
        with store.objects.pack_session():
            if head != captured:
                current = read_historical_object(
                    store, ident, captured["revision"], captured["object"]
                )
                require(
                    record_identity(current) == record_identity(captured),
                    "history cursor head changed",
                )
            head = captured
            while current["revision"] > target:
                key = current["previous_object"]
                if current["revision"] == target + 1:
                    require(
                        key == cursor["next_object"], "history cursor is off the captured chain"
                    )
                current = read_historical_object(store, ident, current["revision"] - 1, key)

    page = []
    while True:
        rev, key = current["revision"], current["previous_object"]
        boundary = (
            key is None and rev > 1 and current.get("retention", {}).get("policy") == "current-use"
        )
        if rev == 1:
            require(key is None, "unexpected predecessor on first revision")
        elif not boundary:
            digest(key, "record predecessor")
        page.append(current)
        if len(page) == limit or rev == 1 or boundary:
            yield {
                "head": head,
                "records": page,
                "cursor": None
                if rev == 1 or boundary
                else {"head": head, "next_revision": rev - 1, "next_object": key},
                "complete": rev == 1,
                "retention_boundary": {
                    "policy": "current-use",
                    "retained_from_revision": rev,
                    "scope": (
                        "Earlier ordinary revisions were not retained by this chain; "
                        "separately selected evidence pins remain independent."
                    ),
                }
                if boundary
                else None,
            }
            if rev == 1 or boundary:
                return
            page = []
        current = read_historical_object(store, ident, rev - 1, key)
