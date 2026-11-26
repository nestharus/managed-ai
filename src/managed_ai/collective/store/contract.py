"""Collective command values. Source identity, bounds and refusal vocabulary."""

import hashlib
import json
from collections.abc import Iterable
from typing import Any, NoReturn

BODY_LIMIT = 256 * 1024
CONTEXT_LIMIT = 2 * 1024 * 1024
VIEW_LIMIT = 1024 * 1024
CONTEXT_DETAIL = "Complete context exceeds 2 MiB; select a narrower receiving contribution"


class Refused(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code, self.detail = code, detail


def encoded(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _unique_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError("Nonfinite JSON constant: " + value)


def loads_strict(raw: bytes | bytearray | str) -> Any:
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    return json.loads(
        text, object_pairs_hook=_unique_json_pairs, parse_constant=_reject_json_constant
    )


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def require(condition: Any, code: str, detail: str) -> None:
    if not condition:
        raise Refused(code, detail)


def name(value: Any, label: str = "identity") -> str:
    require(
        isinstance(value, str) and 0 < len(value) <= 240 and not any(ord(c) < 32 for c in value),
        "invalid-input",
        label + " must be a nonempty bounded string",
    )
    return str(value)


def account(value: Any) -> str:
    require(
        isinstance(value, str) and value.strip() and len(value.encode()) <= 16 * 1024,
        "invalid-input",
        "An account must contain text, at most 16 KiB",
    )
    return str(value)


def body(value: Any) -> Any:
    require(len(encoded(value).encode()) <= BODY_LIMIT, "body-bound", "Body exceeds 256 KiB")
    return value


def integer(value: Any, label: str, lower: int = 0, upper: int = 2**63 - 1) -> int:
    require(
        type(value) is int and lower <= value <= upper, "invalid-input", label + " is out of range"
    )
    return int(value)


def fields(value: Any, allowed: Iterable[str]) -> None:
    require(
        isinstance(value, dict) and not set(value) - set(allowed),
        "invalid-input",
        "Unknown request fields",
    )


def pin(record: dict[str, Any]) -> dict[str, Any]:
    return {k: record[k] for k in ("id", "revision", "sha256")}


def owned(record: dict[str, Any], actor: str) -> None:
    require(record["owner"] == actor, "wrong-owner", "The target belongs to another participant")
