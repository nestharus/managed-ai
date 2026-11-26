"""Public facade and atomic generation publisher for binary mmap HeadIndex v3."""

import contextlib
import json
from pathlib import Path
from typing import Any

from managed_ai_information.core.storage import atomic_bytes, has_ordinary_file, sync_dir

from .head_index_codec import (
    _HEADER_SIZE as _HEADER_SIZE,
)
from .head_index_codec import (
    _HEADER_STRUCT as _HEADER_STRUCT,
)
from .head_index_codec import (
    _PLATFORM_CUSTOM as _PLATFORM_CUSTOM,
)
from .head_index_codec import (
    _PLATFORM_MASK as _PLATFORM_MASK,
)
from .head_index_codec import (
    _PLATFORM_NT as _PLATFORM_NT,
)
from .head_index_codec import (
    _PLATFORM_POSIX as _PLATFORM_POSIX,
)
from .head_index_codec import (
    _PLATFORM_SHIFT as _PLATFORM_SHIFT,
)
from .head_index_codec import (
    _SLOT_FLAGS_OFFSET as _SLOT_FLAGS_OFFSET,
)
from .head_index_codec import (
    _SLOT_ID_STRUCT as _SLOT_ID_STRUCT,
)
from .head_index_codec import (
    _SLOT_RAW_SHA_OFFSET as _SLOT_RAW_SHA_OFFSET,
)
from .head_index_codec import (
    _SLOT_SIZE as _SLOT_SIZE,
)
from .head_index_codec import (
    _SLOT_STAT_OFFSET as _SLOT_STAT_OFFSET,
)
from .head_index_codec import (
    _SLOT_STAT_STRUCT as _SLOT_STAT_STRUCT,
)
from .head_index_codec import (
    _SLOT_STRUCT as _SLOT_STRUCT,
)
from .head_index_codec import (
    _U16_STRUCT as _U16_STRUCT,
)
from .head_index_codec import (
    BINARY_INDEX_DOMAIN,
    FLAG_ACTIVE,
    FLAG_EXACT_REFS_TRUNCATED,
    FLAG_HAS_BASIS,
    FLAG_HAS_CONTENT_FORMAT,
    FLAG_HAS_CONTENT_LINKS,
    FLAG_HAS_DUP_RELS,
    FLAG_HAS_QUESTION_NORM,
    FLAG_OPEN_QUESTION,
    FLAG_TOKENS_TRUNCATED,
    HEAD_INDEX_FORMAT_V3,
    MAP_FLAG_ACTIVE,
    MAP_FLAG_EXACT_REFS_TRUNCATED,
    MAP_FLAG_HAS_BASIS,
    MAP_FLAG_HAS_CONTENT_LINKS,
    MAP_FLAG_HAS_DUP_RELS,
    MAP_FLAG_HAS_QUESTION_NORM,
    MAP_FLAG_OPEN_QUESTION,
    MAP_FLAG_TOKENS_TRUNCATED,
    build_binary_image,
)
from .head_index_codec import (
    _validate_header_bounds as _validate_header_bounds,
)
from .head_index_mmap import (
    BinaryHeadIndexView,
    HeadIndexView,
    _next_generation,
    _prune_old_generations,
    active_binary_path,
    open_binary_view,
    read_current_pointer,
)


def write_binary_index(
    directory: Path,
    key: bytes,
    record_format: str,
    entries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Atomically publish a new head-index.gen<N>.bin generation and flip current pointer."""
    directory.mkdir(parents=True, exist_ok=True)
    generation = _next_generation(directory)
    filename = f"head-index.gen{generation}.bin"
    bin_path = directory / filename
    pointer_path = directory / "head-index.current.json"

    image, tag_hex = build_binary_image(key, record_format, entries)
    atomic_bytes(bin_path, image)

    pointer = {
        "format": HEAD_INDEX_FORMAT_V3,
        "generation": generation,
        "file": filename,
        "record_format": record_format,
        "record_count": len(entries),
        "hmac": tag_hex,
    }

    pointer_raw = (
        json.dumps(
            pointer,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    atomic_bytes(pointer_path, pointer_raw)

    # Clear delta sidecar and stale legacy JSON index after pointer flip.
    for obsolete in (directory / "head-index.delta.jsonl", directory / "head-index.json"):
        with contextlib.suppress(OSError):
            if has_ordinary_file(obsolete):
                obsolete.unlink(missing_ok=True)

    sync_dir(directory)
    _prune_old_generations(directory, filename)

    return {
        "format": HEAD_INDEX_FORMAT_V3,
        "generation": generation,
        "indexed_records": len(entries),
        "path": str(bin_path),
        "pointer_path": str(pointer_path),
        "hmac": tag_hex,
    }


__all__ = [
    "BINARY_INDEX_DOMAIN",
    "FLAG_ACTIVE",
    "FLAG_EXACT_REFS_TRUNCATED",
    "FLAG_HAS_BASIS",
    "FLAG_HAS_CONTENT_FORMAT",
    "FLAG_HAS_CONTENT_LINKS",
    "FLAG_HAS_DUP_RELS",
    "FLAG_HAS_QUESTION_NORM",
    "FLAG_OPEN_QUESTION",
    "FLAG_TOKENS_TRUNCATED",
    "HEAD_INDEX_FORMAT_V3",
    "MAP_FLAG_ACTIVE",
    "MAP_FLAG_EXACT_REFS_TRUNCATED",
    "MAP_FLAG_HAS_BASIS",
    "MAP_FLAG_HAS_CONTENT_LINKS",
    "MAP_FLAG_HAS_DUP_RELS",
    "MAP_FLAG_HAS_QUESTION_NORM",
    "MAP_FLAG_OPEN_QUESTION",
    "MAP_FLAG_TOKENS_TRUNCATED",
    "BinaryHeadIndexView",
    "HeadIndexView",
    "active_binary_path",
    "build_binary_image",
    "open_binary_view",
    "read_current_pointer",
    "write_binary_index",
]
