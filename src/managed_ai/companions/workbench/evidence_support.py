"""Detached, exact JSON evidence for the bounded callable agent runtime.

The engine owns control flow. These helpers only snapshot source material,
callback data, and the nonrecursive interaction history. They use no providers,
external packages, or object representations for unsupported callback values.
"""

import hashlib
import math
from collections.abc import Mapping, Sequence
from typing import Any


class JsonSnapshotError(ValueError):
    """A value cannot be represented by the finite JSON data model."""


def detached_json(value: Any) -> Any:
    """Copy a finite JSON value without retaining any mutable input aliases.

    JSON objects require string keys. Tuples, nonfinite numbers, cycles and
    arbitrary objects are rejected instead of being silently transformed. Shared
    containers are copied separately at each occurrence, as JSON has no aliases.
    """
    active: set[int] = set()

    def copy(item: Any) -> Any:
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise JsonSnapshotError("nonfinite float")
            return item
        if not isinstance(item, (dict, list)):
            item_type = type(item)
            raise JsonSnapshotError(
                "unsupported type: " + item_type.__module__ + "." + item_type.__qualname__
            )
        identity = id(item)
        if identity in active:
            raise JsonSnapshotError("cyclic container")
        active.add(identity)
        try:
            if isinstance(item, list):
                return [copy(child) for child in item]
            result: dict[str, Any] = {}
            for key, child in item.items():
                if not isinstance(key, str):
                    raise JsonSnapshotError("object key is not a string")
                result[key] = copy(child)
            return result
        finally:
            active.remove(identity)

    return copy(value)


def capture_return(value: Any) -> tuple[Any, bool]:
    """Return ``(snapshot, True)`` or ``(type_level_diagnostic, False)``.

    The separate boolean prevents a diagnostic from being confused with an
    actual callback action or observation. A diagnostic does not reproduce or
    call repr()/str() on the unsupported object.
    """
    try:
        return detached_json(value), True
    except (JsonSnapshotError, RecursionError) as exc:
        value_type = type(value)
        return {
            "non_json_value": {
                "type": value_type.__module__ + "." + value_type.__qualname__,
                "reason": str(exc),
            }
        }, False


def source_snapshots(
    sources: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Return independent ``(source_bodies, trace_source_records)`` dictionaries.

    Source bodies are immutable strings and are hashed from their exact UTF-8
    bytes. There is no trimming, newline conversion, or Unicode normalization.
    This function must run before the first callback.
    """
    if not isinstance(sources, Mapping):
        raise TypeError("sources must be a mapping of strings to strings")
    bodies = dict(sources)
    records: dict[str, dict[str, str]] = {}
    for source_id, body in bodies.items():
        if not isinstance(source_id, str) or not isinstance(body, str):
            raise TypeError("source IDs and bodies must be strings")
        records[source_id] = {
            "body": body,
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }
    return bodies, records


def interaction_history(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return detached interaction events, excluding all model requests."""
    retained = {"model_return", "tool_call", "tool_return", "callback_error"}
    result: list[dict[str, Any]] = detached_json(
        [event for event in events if event.get("kind") in retained]
    )
    return result
