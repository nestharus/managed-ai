"""Bounded content-addressed chunking and recovery for large view record inventories."""

import json
import re
from typing import Any

from managed_ai_information.core.formats import RECORD_ID_CHUNK_FORMAT
from managed_ai_information.core.storage import MAX_BYTES, encode, strict_json_loads

OUTSIDE_VIEW_INLINE_LIMIT = 512 * 1024
RECORD_ID_CHUNK_MAX_BYTES = 512 * 1024

_SHA_RE = re.compile(r"^[0-9a-f]{64}\Z")
_CHUNK_WRAPPER_OVERHEAD = len(encode({"format": RECORD_ID_CHUNK_FORMAT, "records": [""]})) - 2
_CHUNK_ITEM_SEP_BYTES = len(",\n    ")


def _validate_nonneg_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _validate_record_id_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a list of strings")
    return value


def _partition_record_ids(records: list[str], chunk_max_bytes: int) -> list[list[str]]:
    if chunk_max_bytes <= _CHUNK_WRAPPER_OVERHEAD or chunk_max_bytes > MAX_BYTES:
        raise ValueError("invalid record-id chunk byte cap")
    chunks: list[list[str]] = []
    current: list[str] = []
    current_bytes = _CHUNK_WRAPPER_OVERHEAD
    for ident in records:
        item_bytes = len(json.dumps(ident, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if _CHUNK_WRAPPER_OVERHEAD + item_bytes > chunk_max_bytes:
            raise ValueError("single record identity exceeds chunk byte limit")
        extra = item_bytes + (_CHUNK_ITEM_SEP_BYTES if current else 0)
        if current and current_bytes + extra > chunk_max_bytes:
            chunks.append(current)
            current = [ident]
            current_bytes = _CHUNK_WRAPPER_OVERHEAD + item_bytes
        else:
            current.append(ident)
            current_bytes += extra
    if current:
        chunks.append(current)
    return chunks


def _store_record_id_chunk(objects: Any, chunk: list[str], chunk_max_bytes: int) -> list[str]:
    chunk_bytes = encode({"format": RECORD_ID_CHUNK_FORMAT, "records": chunk})
    if len(chunk_bytes) <= chunk_max_bytes:
        return [objects.put(chunk_bytes, compress=True)]
    if len(chunk) <= 1:
        raise ValueError("single record identity exceeds chunk byte limit")
    mid = len(chunk) // 2
    return _store_record_id_chunk(objects, chunk[:mid], chunk_max_bytes) + _store_record_id_chunk(
        objects, chunk[mid:], chunk_max_bytes
    )


def compact_outside_view_records(
    objects: Any,
    view_doc: dict[str, Any],
    *,
    inline_limit: int = OUTSIDE_VIEW_INLINE_LIMIT,
    chunk_max_bytes: int = RECORD_ID_CHUNK_MAX_BYTES,
) -> dict[str, Any]:
    """Record outside_view_record_count and spill large outside_view_records into bounded chunks."""
    out = dict(view_doc)
    outside = _validate_record_id_list(out.get("outside_view_records", []), "outside_view_records")
    out["outside_view_record_count"] = len(outside)
    if not outside:
        return out
    if len(encode(outside)) <= inline_limit and len(encode(out)) <= MAX_BYTES:
        return out

    chunk_hashes: list[str] = []
    for chunk in _partition_record_ids(outside, chunk_max_bytes):
        chunk_hashes.extend(_store_record_id_chunk(objects, chunk, chunk_max_bytes))
    out.pop("outside_view_records", None)
    out["outside_view_record_objects"] = chunk_hashes
    return out


def outside_view_record_count(view: dict[str, Any]) -> int:
    """Return outside_view_records count in O(1) without hydrating chunk objects."""
    if "outside_view_record_count" in view:
        count = _validate_nonneg_int(view["outside_view_record_count"], "outside_view_record_count")
        if "outside_view_records" in view:
            inline = _validate_record_id_list(view["outside_view_records"], "outside_view_records")
            if len(inline) != count:
                raise ValueError("outside_view_record_count disagrees with outside_view_records")
        return count
    if "outside_view_record_objects" in view:
        raise ValueError("chunked view missing outside_view_record_count")
    return len(
        _validate_record_id_list(view.get("outside_view_records", []), "outside_view_records")
    )


def validate_outside_view_chunks_metadata(view: dict[str, Any]) -> tuple[list[str], int] | None:
    """Validate chunk metadata on a captured view; return (chunk_hashes, expected_count) or None."""
    if "outside_view_record_objects" not in view:
        outside_view_record_count(view)
        return None
    if "outside_view_records" in view:
        raise ValueError(
            "captured view cannot define both outside_view_records and outside_view_record_objects"
        )
    expected_count = outside_view_record_count(view)
    chunk_hashes = view["outside_view_record_objects"]
    if not isinstance(chunk_hashes, list) or not all(
        isinstance(item, str) and _SHA_RE.match(item) for item in chunk_hashes
    ):
        raise ValueError("outside_view_record_objects must be a list of SHA-256 digests")
    return chunk_hashes, expected_count


def hydrate_outside_view_records(objects: Any, view: dict[str, Any]) -> dict[str, Any]:
    """Reassemble outside_view_records from bounded chunk objects when spilled."""
    meta = validate_outside_view_chunks_metadata(view)
    if meta is None:
        return view
    chunk_hashes, expected_count = meta
    reassembled: list[str] = []
    for chunk_sha in chunk_hashes:
        chunk_doc = strict_json_loads(objects.read(chunk_sha), "outside_view_record_chunk")
        if not isinstance(chunk_doc, dict) or set(chunk_doc) != {"format", "records"}:
            raise ValueError("outside_view_record chunk must contain only format and records")
        if chunk_doc["format"] != RECORD_ID_CHUNK_FORMAT:
            raise ValueError(
                f"unsupported outside_view_record chunk format: {chunk_doc['format']!r}"
            )
        reassembled.extend(_validate_record_id_list(chunk_doc["records"], "chunk records"))
    if len(reassembled) != expected_count:
        raise ValueError(
            f"outside_view_record_count mismatch: expected {expected_count}, got {len(reassembled)}"
        )
    out = dict(view)
    out["outside_view_records"] = reassembled
    return out
