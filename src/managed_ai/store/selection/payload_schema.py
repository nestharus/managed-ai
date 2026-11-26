"""Pure zero-I/O structural JSON Schema validator for typed domain record payloads."""

import math
import re
from collections.abc import Callable, Iterator
from typing import Any

PAYLOAD_TYPES: dict[str, Callable[[Any], bool]] = {
    "object": lambda x: isinstance(x, dict),
    "array": lambda x: isinstance(x, list),
    "string": lambda x: isinstance(x, str),
    "integer": lambda x: type(x) is int,
    "number": lambda x: type(x) in (int, float) and math.isfinite(x),
    "boolean": lambda x: type(x) is bool,
    "null": lambda x: x is None,
}

_ALLOWED_SCHEMA_KEYWORDS = frozenset(
    [
        "$schema",
        "$id",
        "title",
        "description",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "pattern",
        "anyOf",
        "allOf",
        "oneOf",
        "$defs",
        "definitions",
        "$ref",
    ]
)


def same_literal(left: Any, right: Any) -> bool:
    """Compare JSON containers recursively, retaining the dialect's scalar equality."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_literal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            same_literal(a, b) for a, b in zip(left, right, strict=True)
        )
    return bool(left == right)


def _local_schema_target(document: Any, reference: Any) -> dict[str, Any] | bool:
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise ValueError(f"only local payload-schema references are supported: {reference!r}")
    target = document
    for token in reference[2:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or key not in target:
            raise ValueError(f"unresolved local payload-schema reference {reference!r}")
        target = target[key]
    if not isinstance(target, (dict, bool)):
        raise ValueError(f"local payload-schema reference {reference!r} does not name a schema")
    return target


def _validate_schema_type(schema: dict[str, Any]) -> None:
    if "type" not in schema:
        return
    declared = schema["type"]
    names = [declared] if isinstance(declared, str) else declared
    if (
        not isinstance(names, list)
        or not names
        or any(not isinstance(name, str) or name not in PAYLOAD_TYPES for name in names)
    ):
        raise ValueError(f"unsupported payload type declaration: {declared!r}")


def _validate_schema_bounds(schema: dict[str, Any]) -> None:
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError(f"schema keyword {key} must be a nonnegative integer")
    for low, high in (("minLength", "maxLength"), ("minItems", "maxItems")):
        if low in schema and high in schema and schema[low] > schema[high]:
            raise ValueError(f"schema {low} exceeds {high}")
    for key in ("minimum", "maximum"):
        if key in schema and (
            type(schema[key]) not in (int, float) or not math.isfinite(schema[key])
        ):
            raise ValueError(f"schema keyword {key} must be a finite number")
    if "minimum" in schema and "maximum" in schema and schema["minimum"] > schema["maximum"]:
        raise ValueError("schema minimum exceeds maximum")


def _validate_schema_scalars(schema: dict[str, Any]) -> None:
    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise ValueError("schema pattern must be a string")
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            raise ValueError(f"invalid schema pattern regex: {exc}") from None
    if "required" in schema:
        req = schema["required"]
        if not isinstance(req, list) or any(not isinstance(item, str) for item in req):
            raise ValueError("schema required must be a list of strings")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("schema enum must be a nonempty list")


def _schema_children(schema: Any, document: Any) -> Iterator[Any]:
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise ValueError("payload schema must be an object or Boolean")
    unknown = set(schema) - _ALLOWED_SCHEMA_KEYWORDS
    if unknown:
        raise ValueError("unsupported structural schema keywords: " + ", ".join(sorted(unknown)))

    _validate_schema_type(schema)
    _validate_schema_bounds(schema)
    _validate_schema_scalars(schema)

    for group in ("properties", "$defs", "definitions"):
        if group in schema:
            if not isinstance(schema[group], dict):
                raise ValueError(f"schema {group} must be an object")
            yield from schema[group].values()
    for group in ("anyOf", "allOf", "oneOf"):
        if group in schema:
            if not isinstance(schema[group], list) or not schema[group]:
                raise ValueError(f"schema {group} must be a nonempty list")
            yield from schema[group]
    for key in ("items", "additionalProperties"):
        if key in schema:
            yield schema[key]
    if "$ref" in schema:
        yield _local_schema_target(document, schema["$ref"])


def validate_schema(schema: Any) -> None:
    """Check every declaration once, including referenced schemas, without a payload."""
    pending = [schema]
    visited: set[int] = set()
    while pending:
        node = pending.pop()
        if id(node) not in visited:
            visited.add(id(node))
            pending.extend(_schema_children(node, schema))


def _validate_combinators(
    value: Any, schema: dict[str, Any], at: str, document: Any, references: frozenset[int]
) -> None:
    for group in ("allOf", "anyOf", "oneOf"):
        if group not in schema:
            continue
        valid = 0
        for child in schema[group]:
            try:
                validate_payload(value, child, at=at, document=document, _references=references)
                valid += 1
            except ValueError:
                pass
        expected = len(schema[group]) if group == "allOf" else 1
        if valid != expected if group != "anyOf" else valid < 1:
            raise ValueError(at + ": schema alternatives do not match")


def _validate_scalar_constraints(value: Any, schema: dict[str, Any], at: str) -> None:
    wanted = schema.get("type")
    if wanted is not None:
        wanted_list = [wanted] if isinstance(wanted, str) else wanted
        if not any(PAYLOAD_TYPES[k](value) for k in wanted_list):
            raise ValueError(at + ": wrong payload type")
    if "enum" in schema and not any(same_literal(value, item) for item in schema["enum"]):
        raise ValueError(at + ": value is outside schema enum")
    if "const" in schema and not same_literal(value, schema["const"]):
        raise ValueError(at + ": value differs from schema constant")


def _validate_object_payload(
    value: dict[str, Any], schema: dict[str, Any], at: str, document: Any
) -> None:
    missing = set(schema.get("required", [])) - set(value)
    if missing:
        raise ValueError(at + ": missing required fields " + ", ".join(sorted(missing)))
    props = schema.get("properties", {})
    default_schema = schema.get("additionalProperties", True)
    for key, item in value.items():
        validate_payload(item, props.get(key, default_schema), at=at + "." + key, document=document)


def _validate_array_payload(
    value: list[Any], schema: dict[str, Any], at: str, document: Any
) -> None:
    item_schema = schema.get("items", True)
    for index, item in enumerate(value):
        validate_payload(item, item_schema, at=at + "[" + str(index) + "]", document=document)
    if "minItems" in schema and len(value) < schema["minItems"]:
        raise ValueError(at + ": minItems violated")
    if "maxItems" in schema and len(value) > schema["maxItems"]:
        raise ValueError(at + ": maxItems violated")


def _validate_string_payload(value: str, schema: dict[str, Any], at: str) -> None:
    if "minLength" in schema and len(value) < schema["minLength"]:
        raise ValueError(at + ": minLength violated")
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        raise ValueError(at + ": maxLength violated")
    if "pattern" in schema and re.search(schema["pattern"], value) is None:
        raise ValueError(at + ": pattern differs")


def _validate_number_payload(value: int | float, schema: dict[str, Any], at: str) -> None:
    if not math.isfinite(value):
        raise ValueError(at + ": nonfinite numeric value prohibited")
    if ("minimum" in schema and value < schema["minimum"]) or (
        "maximum" in schema and value > schema["maximum"]
    ):
        raise ValueError(at + ": numeric bound violated")


def validate_payload(
    value: Any,
    schema: Any,
    *,
    at: str = "content",
    document: Any = None,
    _references: frozenset[int] = frozenset(),
) -> None:
    """Validate one JSON payload value against a verified structural schema."""
    document = schema if document is None else document
    if schema is True:
        return
    if schema is False:
        raise ValueError(at + ": prohibited by schema")

    if "$ref" in schema:
        target = _local_schema_target(document, schema["$ref"])
        if id(target) in _references:
            raise ValueError(at + ": cyclic schema reference at the same payload location")
        validate_payload(
            value, target, at=at, document=document, _references=_references | {id(target)}
        )

    _validate_scalar_constraints(value, schema, at)
    _validate_combinators(value, schema, at, document, _references)

    if isinstance(value, dict):
        _validate_object_payload(value, schema, at, document)
    elif isinstance(value, list):
        _validate_array_payload(value, schema, at, document)
    elif isinstance(value, str):
        _validate_string_payload(value, schema, at)
    elif type(value) in (int, float):
        _validate_number_payload(value, schema, at)
