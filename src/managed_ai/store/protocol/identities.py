"""Exact identities and retained JSON shared by record and evidence owners."""

import re
from typing import Any

from managed_ai.core.ownership import packet_id
from managed_ai.platform import strict_json_loads


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def nonempty(value: Any, label: str) -> str:
    require(isinstance(value, str) and bool(value.strip()), label + " must be nonempty text")
    return value


def exact_fields(value: Any, required: set[str], optional: set[str], label: str) -> None:
    require(isinstance(value, dict), label + " must be an object")
    require(
        required <= set(value) and set(value) <= required | optional,
        label + " has missing or unknown fields",
    )


def digest(value: Any, label: str) -> str:
    require(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
        label + " must be a SHA-256 identity",
    )
    return value


def identity(value: Any, *, object_pin: bool = False) -> dict[str, Any]:
    names = {"id", "revision", "sha256"} | ({"object"} if object_pin else set())
    exact_fields(value, names, set(), "information identity")
    packet_id(nonempty(value["id"], "record id"))
    require(
        type(value["revision"]) is int and value["revision"] >= 1,
        "record revision must be a positive integer",
    )
    digest(value["sha256"], "record digest")
    if object_pin:
        digest(value["object"], "retained object")
    return value


def record_identity(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in ("id", "revision", "sha256")}


def strict_json(data: bytes) -> dict[str, Any]:
    return strict_json_loads(data, "retained information source", require_object=True)
