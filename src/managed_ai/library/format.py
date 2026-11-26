"""Catalogue format validation, strict JSON decoding, and bounded ordinary file reads.

Depends only on Python stdlib and managed_ai.platform (zero imports from core,
store, or deployment).
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from managed_ai.platform import (
    check_unfollowed_ancestors,
    check_unfollowed_descendant,
    is_ordinary_file_stat,
    is_reparse_or_symlink,
    strict_json_loads,
    validate_safe_segment,
)

from .config import DEFAULT_LIBRARY_CONFIG, LibraryConfig

FORMAT = DEFAULT_LIBRARY_CONFIG.library_format
SELECTION = DEFAULT_LIBRARY_CONFIG.selection_format
MAX_MANIFEST = DEFAULT_LIBRARY_CONFIG.limits.max_manifest_bytes
MAX_BODY = DEFAULT_LIBRARY_CONFIG.limits.max_body_bytes
MAX_BODIES = DEFAULT_LIBRARY_CONFIG.limits.max_bodies
MAX_MODULES = DEFAULT_LIBRARY_CONFIG.limits.max_modules
MAX_PACKAGE = DEFAULT_LIBRARY_CONFIG.limits.max_package_bytes
MAX_ARCHIVE = DEFAULT_LIBRARY_CONFIG.limits.max_archive_bytes
SHA = re.compile(r"[0-9a-f]{64}\Z")
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,239}\Z")


def encoded(value: Any) -> bytes:
    """Serialize value to canonical indented UTF-8 JSON bytes."""
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def digest(data: bytes) -> str:
    """Return lowercase SHA-256 hex digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def need(condition: Any, message: str) -> None:
    """Raise ValueError(message) when condition is falsy."""
    if not condition:
        raise ValueError(message)


def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object keys during parsing."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        need(key not in result, "duplicate JSON key: " + key)
        result[key] = value
    return result


def decoded(data: bytes | str) -> Any:
    """Parse UTF-8 JSON strictly, rejecting duplicate keys and nonfinite constants."""
    return strict_json_loads(data, "library JSON", require_object=False)


def ordinary(path: Path | str) -> Path:
    """Validate that path and all its ancestors contain no symlink or NTFS reparse point."""
    raw = Path(path).expanduser()
    try:
        return check_unfollowed_ancestors(raw, "library path", include_target=True)
    except ValueError as exc:
        raise ValueError("symlink path is unsupported: " + str(raw)) from exc


def _read_stream_bounded(path: Path, info: Any, limit: int, chunk_size: int = 64 * 1024) -> bytes:
    need(is_ordinary_file_stat(info), "ordinary file required: " + str(path))
    need(info.st_size <= limit, "file exceeds byte bound: " + str(path))

    with path.open("rb") as stream:
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = stream.read(min(chunk_size, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)

    need(len(body) <= limit, "file grew beyond byte bound: " + str(path))
    return body


def read_bytes(path: Path | str, limit: int) -> bytes:
    """Read an ordinary file up to limit bytes, verifying all unfollowed ancestors."""
    checked = ordinary(path)
    try:
        info = checked.lstat()
    except OSError:
        raise ValueError("ordinary file required: " + str(checked)) from None

    return _read_stream_bounded(checked, info, limit)


def read_bytes_under(validated_root: Path | str, rel_path: str, limit: int) -> bytes:
    """Read an ordinary file below an already-validated root, checking every descendant segment."""
    try:
        leaf = check_unfollowed_descendant(
            validated_root,
            rel_path,
            expect_dir=False,
            allow_missing_leaf=False,
            role="library member",
        )
    except ValueError as exc:
        msg = str(exc)
        if "unsafe" in msg or "relative" in msg or "reserved" in msg or "trailing" in msg:
            raise ValueError("unsafe relative identity") from exc
        if "directory" in msg and is_reparse_or_symlink(
            Path(validated_root) / Path(rel_path).parent
        ):
            raise ValueError(
                "symlink path is unsupported: " + str(Path(validated_root) / rel_path)
            ) from exc
        raise ValueError("ordinary file required: " + str(Path(validated_root) / rel_path)) from exc

    try:
        info = leaf.lstat()
    except OSError:
        raise ValueError("ordinary file required: " + str(leaf)) from None

    return _read_stream_bounded(leaf, info, limit)


def relative(value: Any) -> str:
    """Validate one portable POSIX-style library identity or relative path."""
    need(isinstance(value, str) and IDENT.fullmatch(value), "invalid relative identity")
    need(
        not value.startswith("/")
        and "\\" not in value
        and not (len(value) >= 2 and value[1] == ":"),
        "unsafe relative identity",
    )

    for segment in value.split("/"):
        need(
            segment not in {"", ".", ".."} and not (len(segment) >= 2 and segment[1] == ":"),
            "unsafe relative identity",
        )
        try:
            validate_safe_segment(segment, "relative identity")
        except ValueError as exc:
            raise ValueError("unsafe relative identity") from exc

    return value


def text(
    value: Any, label: str, maximum: int = DEFAULT_LIBRARY_CONFIG.limits.max_text_bytes
) -> str:
    """Validate a nonempty UTF-8 string bounded in encoded bytes."""
    need(
        isinstance(value, str) and value.strip() and len(value.encode("utf-8")) <= maximum,
        "invalid " + label,
    )
    return value


def sha(value: Any) -> str:
    """Validate a lowercase 64-character SHA-256 hex digest."""
    need(isinstance(value, str) and SHA.fullmatch(value), "invalid SHA-256")
    return value


def integer(value: Any, label: str, low: int, high: int) -> int:
    """Validate an exact integer (rejecting booleans) within [low, high]."""
    need(type(value) is int and low <= value <= high, "invalid " + label)
    return value


def _validate_body_row(row: Any, by_id: dict[str, dict[str, Any]], config: LibraryConfig) -> None:
    limits = config.limits
    expected_keys = {
        "id",
        "title",
        "summary",
        "object",
        "sha256",
        "bytes",
        "source",
        "link_base",
        "references",
    }
    need(isinstance(row, dict) and set(row) == expected_keys, "invalid body fields")

    ident = relative(row["id"])
    need(ident not in by_id, "duplicate body identity: " + ident)
    text(row["title"], "title", limits.max_title_bytes)
    text(row["summary"], "summary", limits.max_summary_bytes)

    h = sha(row["sha256"])
    need(row["object"] == "objects/" + h + ".txt", "body object is not content-addressed")
    integer(row["bytes"], "body bytes", 0, limits.max_body_bytes)

    need(
        isinstance(row["source"], dict) and set(row["source"]) == {"path", "sha256"},
        "invalid body source",
    )
    relative(row["source"]["path"])
    relative(row["link_base"])
    need(row["source"]["sha256"] == h, "complete copied source must have the body digest")

    refs = row["references"]
    need(
        isinstance(refs, list) and len(refs) <= limits.max_references_per_body, "invalid references"
    )
    for ref in refs:
        need(
            isinstance(ref, dict) and set(ref) == {"target", "anchor", "kind"}, "invalid reference"
        )
        text(ref["target"], "reference target", limits.max_reference_target_bytes)
        need(
            isinstance(ref["anchor"], str)
            and len(ref["anchor"]) <= limits.max_reference_anchor_bytes,
            "invalid reference anchor",
        )
        need(
            isinstance(ref["kind"], str) and ref["kind"] in {"source", "url"},
            "invalid reference kind",
        )

    by_id[ident] = row


def _validate_module_row(
    row: Any,
    by_id: dict[str, dict[str, Any]],
    module_ids: set[str],
    config: LibraryConfig,
) -> None:
    limits = config.limits
    expected_keys = {
        "id",
        "title",
        "purpose",
        "members",
        "requires",
        "parents",
        "external_participation",
    }
    need(isinstance(row, dict) and set(row) == expected_keys, "invalid module fields")

    ident = relative(row["id"])
    need(ident not in module_ids, "duplicate module identity: " + ident)
    module_ids.add(ident)

    text(row["title"], "module title", limits.max_title_bytes)
    text(row["purpose"], "module purpose", limits.max_text_bytes)

    for key in ("members", "requires"):
        need(
            isinstance(row[key], list) and len(row[key]) <= limits.max_bodies,
            "invalid module " + key,
        )
        for member in row[key]:
            relative(member)
        need(len(row[key]) == len(set(row[key])), "duplicate module " + key)

    need(row["members"], "a module needs at least one complete member body")
    need(
        all(x in by_id for x in row["members"]), "module has an unavailable required body: " + ident
    )

    need(
        isinstance(row["parents"], list) and len(row["parents"]) <= limits.max_modules,
        "invalid parents",
    )
    for parent in row["parents"]:
        need(isinstance(parent, dict) and set(parent) == {"id", "role"}, "invalid parent relation")
        relative(parent["id"])
        text(parent["role"], "parent role", limits.max_parent_role_bytes)

    ext = row["external_participation"]
    need(
        isinstance(ext, list) and len(ext) <= limits.max_external_participation,
        "invalid external participation",
    )
    for item in ext:
        text(item, "external participation", limits.max_text_bytes)


def check_manifest(value: Any, config: LibraryConfig = DEFAULT_LIBRARY_CONFIG) -> dict[str, Any]:
    """Validate a complete library catalogue manifest against schema and byte bounds."""
    limits = config.limits
    need(
        isinstance(value, dict)
        and set(value) == {"format", "edition", "bodies", "modules", "provenance"},
        "invalid library fields",
    )
    need(
        value["format"] == config.library_format,
        "unsupported library format; explicit migration required",
    )
    text(value["edition"], "edition", limits.max_edition_bytes)
    need(isinstance(value["provenance"], dict), "provenance must be an object")

    bodies = value["bodies"]
    modules = value["modules"]
    need(isinstance(bodies, list) and 1 <= len(bodies) <= limits.max_bodies, "invalid body count")
    need(isinstance(modules, list) and len(modules) <= limits.max_modules, "invalid module count")

    by_id: dict[str, dict[str, Any]] = {}
    for row in bodies:
        _validate_body_row(row, by_id, config)

    need(
        sum(row["bytes"] for row in bodies) <= limits.max_package_bytes,
        "library exceeds declared total body byte bound",
    )

    module_ids: set[str] = set()
    for row in modules:
        _validate_module_row(row, by_id, module_ids, config)

    for row in modules:
        need(
            all(x in module_ids for x in row["requires"]),
            "module has an unavailable required module: " + row["id"],
        )

    return value
