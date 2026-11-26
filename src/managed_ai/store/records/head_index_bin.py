"""Public facade and atomic generation publisher for binary mmap HeadIndex v3."""
from __future__ import annotations

import json
from pathlib import Path

from managed_ai.core.storage import atomic_bytes, has_ordinary_file, sync_dir

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

    _HEADER_SIZE,
    _HEADER_STRUCT,
    _PLATFORM_CUSTOM,
    _PLATFORM_MASK,
    _PLATFORM_NT,
    _PLATFORM_POSIX,
    _PLATFORM_SHIFT,

    _SLOT_FLAGS_OFFSET,
    _SLOT_ID_STRUCT,
    _SLOT_RAW_SHA_OFFSET,
    _SLOT_SIZE,
    _SLOT_STAT_OFFSET,
    _SLOT_STAT_STRUCT,
    _SLOT_STRUCT,
    _U16_STRUCT,

    _validate_header_bounds,
    build_binary_image,
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
    entries: dict[str, dict],
) -> dict:
    """Atomically publish a new immutable head-index.gen<N>.bin generation and flip current pointer."""
    directory.mkdir(parents=True, exist_ok=True)
    generation = _next_generation(directory)
    filename = f'head-index.gen{generation}.bin'
    bin_path = directory / filename
    pointer_path = directory / 'head-index.current.json'

    image, tag_hex = build_binary_image(key, record_format, entries)
    atomic_bytes(bin_path, image)

    pointer = {
        'format': HEAD_INDEX_FORMAT_V3,
        'generation': generation,
        'file': filename,
        'record_format': record_format,
        'record_count': len(entries),
        'hmac': tag_hex,
    }

    pointer_raw = (
        json.dumps(pointer, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n'
    ).encode('utf-8')
    atomic_bytes(pointer_path, pointer_raw)

    # Clear delta sidecar and stale legacy JSON index after pointer flip.
    for obsolete in (directory / 'head-index.delta.jsonl', directory / 'head-index.json'):
        try:
            if has_ordinary_file(obsolete):
                obsolete.unlink(missing_ok=True)
        except OSError:
            pass

    sync_dir(directory)
    _prune_old_generations(directory, filename)

    return {
        'format': HEAD_INDEX_FORMAT_V3,
        'generation': generation,
        'indexed_records': len(entries),
        'path': str(bin_path),
        'pointer_path': str(pointer_path),
        'hmac': tag_hex,
    }


__all__ = [
    'BINARY_INDEX_DOMAIN',
    'BinaryHeadIndexView',

    'FLAG_ACTIVE',
    'FLAG_EXACT_REFS_TRUNCATED',
    'FLAG_HAS_BASIS',
    'FLAG_HAS_CONTENT_FORMAT',
    'FLAG_HAS_CONTENT_LINKS',
    'FLAG_HAS_DUP_RELS',
    'FLAG_HAS_QUESTION_NORM',
    'FLAG_OPEN_QUESTION',
    'FLAG_TOKENS_TRUNCATED',

    'HEAD_INDEX_FORMAT_V3',
    'HeadIndexView',

    'MAP_FLAG_ACTIVE',
    'MAP_FLAG_EXACT_REFS_TRUNCATED',
    'MAP_FLAG_HAS_BASIS',
    'MAP_FLAG_HAS_CONTENT_LINKS',
    'MAP_FLAG_HAS_DUP_RELS',
    'MAP_FLAG_HAS_QUESTION_NORM',
    'MAP_FLAG_OPEN_QUESTION',
    'MAP_FLAG_TOKENS_TRUNCATED',

    'active_binary_path',
    'build_binary_image',
    'open_binary_view',
    'read_current_pointer',
    'write_binary_index',
]
