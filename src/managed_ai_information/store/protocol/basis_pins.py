"""Content-addressed basis reference identity helpers shared across record and composition."""

import re
from typing import Any

_SHARD_CONTENT_RE = re.compile(r"^([0-9a-f]{2})/([0-9a-f]{64})\.[A-Za-z0-9._-]+$")
_OBJECT_BASIS_RE = re.compile(r"^object:([0-9a-f]{64})$")


def content_addressed_sha(path: str, *, prefixes: tuple[str, ...] = ()) -> str | None:
    """Return the embedded 64-hex digest if path is an immutable content-addressed basis ref.

    Always recognizes generic `object:<64hex>`. Also recognizes domain-declared
    content-addressed workspace prefixes `<prefix><sha[:2]>/<sha>.<ext>`.
    """
    if not isinstance(path, str):
        return None

    obj_match = _OBJECT_BASIS_RE.fullmatch(path)
    if obj_match:
        return obj_match.group(1)

    for prefix in prefixes:
        norm = prefix if prefix.endswith("/") else prefix + "/"
        if path.startswith(norm):
            sub = path[len(norm) :]
            orig_match = _SHARD_CONTENT_RE.fullmatch(sub)
            if orig_match and orig_match.group(1) == orig_match.group(2)[:2]:
                return orig_match.group(2)

    return None


def is_content_addressed_basis(item: dict[str, Any], *, prefixes: tuple[str, ...] = ()) -> bool:
    """Return True when a captured basis pin matches its own content-addressed path digest."""
    if not isinstance(item, dict):
        return False

    expected = item.get("sha256")
    size = item.get("bytes")
    if not isinstance(expected, str) or type(size) is not int or size < 0:
        return False

    embedded = content_addressed_sha(item.get("path", ""), prefixes=prefixes)
    return embedded is not None and embedded == expected
