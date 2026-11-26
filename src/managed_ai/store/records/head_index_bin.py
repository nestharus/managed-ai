"""Pure-stdlib binary mmap HeadIndex v3 with columnar bytemaps, Fwd/Rev CSR and HMAC footer."""
from __future__ import annotations

from array import array
import hmac
import json
import mmap
import os
from pathlib import Path
import re
import stat
import struct
from typing import Iterator

from managed_ai.core.storage import (
    _OPEN_RO_NOFOLLOW,
    atomic_bytes,
    has_ordinary_file,
    is_ordinary_file_stat,
    sync_dir,
)
from managed_ai.platform import strict_json_loads

HEAD_INDEX_FORMAT_V3 = 'managed-ai-head-index/3'
BINARY_INDEX_DOMAIN = b'g60/verified-head-index-bin/3\0'
_MAGIC = b'MAIHIDX3'
_VERSION = 3
_GEN_FILE_RE = re.compile(r'^head-index\.gen([1-9][0-9]*)\.bin$')
_HEX64_RE = re.compile(r'^[0-9a-f]{64}$')
_OPEN_QUESTION_STATES = frozenset({'', 'open', 'qualified_unissued'})

# 192-byte little-endian header: magic(8), version(2), flags(2), 7 x u32 counts/reserved(28), 19 x u64 offsets(152).
_HEADER_STRUCT = struct.Struct('<8sHHIIIIIII19Q')
_HEADER_SIZE = _HEADER_STRUCT.size
assert _HEADER_SIZE == 192

# 128-byte fixed-width record slot sorted lexicographically by UTF-8 record ID.
_SLOT_STRUCT = struct.Struct('<IHHIIHHHHIHHHHIQqQ32s32s')
_SLOT_SIZE = _SLOT_STRUCT.size
assert _SLOT_SIZE == 128
_SLOT_ID_STRUCT = struct.Struct('<IH')
_SLOT_FLAGS_OFFSET = struct.calcsize('<IHHIIHHH')
_SLOT_STAT_OFFSET = struct.calcsize('<IHHIIHHHHIHHHHI')
_SLOT_STAT_STRUCT = struct.Struct('<QqQ')
_SLOT_RAW_SHA_OFFSET = struct.calcsize('<IHHIIHHHHIHHHHIQqQ32s')
assert _SLOT_FLAGS_OFFSET == 22
assert _SLOT_STAT_OFFSET == 40
assert _SLOT_RAW_SHA_OFFSET == 96

_KIND_ENTRY_STRUCT = struct.Struct('<IIII')
_REL_ENTRY_STRUCT = struct.Struct('<II')
_FWD_EDGE_STRUCT = struct.Struct('<IiII')
_REV_EDGE_STRUCT = struct.Struct('<II')
_EXT_REV_EDGE_STRUCT = struct.Struct('<IIII')
_U32_STRUCT = struct.Struct('<I')
_U16_STRUCT = struct.Struct('<H')
_PAIR_U32_STRUCT = struct.Struct('<II')
_BASIS_HEAD_STRUCT = struct.Struct('<IIB')
_U64_STRUCT = struct.Struct('<Q')

# Bit flags stored in slot_flags (u16).
FLAG_ACTIVE = 0x0001
FLAG_TOKENS_TRUNCATED = 0x0002
FLAG_EXACT_REFS_TRUNCATED = 0x0004
FLAG_HAS_BASIS = 0x0008
FLAG_HAS_CONTENT_LINKS = 0x0010
FLAG_HAS_DUP_RELS = 0x0020
FLAG_HAS_QUESTION_NORM = 0x0040
FLAG_HAS_CONTENT_FORMAT = 0x0080
FLAG_OPEN_QUESTION = 0x0100
_PLATFORM_SHIFT = 9
_PLATFORM_MASK = 0x0600
_PLATFORM_POSIX = 0
_PLATFORM_NT = 1
_PLATFORM_CUSTOM = 2

# Compact 1-byte per-record columnar flags in flags_map.
MAP_FLAG_ACTIVE = 0x01
MAP_FLAG_OPEN_QUESTION = 0x02
MAP_FLAG_HAS_CONTENT_LINKS = 0x04
MAP_FLAG_HAS_DUP_RELS = 0x08
MAP_FLAG_HAS_BASIS = 0x10
MAP_FLAG_TOKENS_TRUNCATED = 0x20
MAP_FLAG_EXACT_REFS_TRUNCATED = 0x40
MAP_FLAG_HAS_QUESTION_NORM = 0x80


class _StringPool:
    """Deduplicated UTF-8 string pool returning (offset, byte_length)."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._intern: dict[bytes, tuple[int, int]] = {}

    def add(self, text: str) -> tuple[int, int]:
        raw = text.encode('utf-8')
        hit = self._intern.get(raw)

        if hit is not None:
            return hit

        off = len(self._buf)
        length = len(raw)

        if off + length >= 0xFFFFFFFF:
            raise ValueError('head index string pool exceeds 4 GiB limit')

        self._buf.extend(raw)
        pair = (off, length)
        self._intern[raw] = pair

        return pair

    def to_bytes(self) -> bytes:
        return bytes(self._buf)


def _hex64_to_bytes(value: object, label: str) -> bytes:
    if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
        raise ValueError(f'invalid 64-hex digest for {label}')

    return bytes.fromhex(value)


def _encode_aux(
    entry: dict,
    graph: dict,
    slot_flags: int,
    plat_code: int,
    rel_to_id: dict[str, int],
    pool: _StringPool,
) -> bytes:
    parts = bytearray()

    if slot_flags & FLAG_HAS_BASIS:
        basis = entry.get('basis', ())
        parts.extend(_U32_STRUCT.pack(len(basis)))

        for item in basis:
            p_off, p_len = pool.add(str(item.get('path', '')))
            item_sha = item.get('sha256')
            if item_sha is None:
                parts.extend(_BASIS_HEAD_STRUCT.pack(p_off, p_len, 0))
            else:
                parts.extend(_BASIS_HEAD_STRUCT.pack(p_off, p_len, 1))
                parts.extend(_hex64_to_bytes(item_sha, 'basis sha256'))
            item_bytes = int(item.get('bytes', 0))
            if item_bytes < 0:
                item_bytes = 0
            parts.extend(_U64_STRUCT.pack(item_bytes))

    if slot_flags & FLAG_HAS_CONTENT_LINKS:
        content_links = graph.get('content_links', {})
        items = sorted(content_links.items())
        parts.extend(_U32_STRUCT.pack(len(items)))

        for field_name, values in items:
            k_off, k_len = pool.add(field_name)
            parts.extend(_PAIR_U32_STRUCT.pack(k_off, k_len))
            parts.extend(_U32_STRUCT.pack(len(values)))

            for val in values:
                v_off, v_len = pool.add(val)
                parts.extend(_PAIR_U32_STRUCT.pack(v_off, v_len))

    if slot_flags & FLAG_HAS_DUP_RELS:
        dups = graph.get('duplicate_relations', ())
        parts.extend(_U32_STRUCT.pack(len(dups)))

        for dup_label, dup_target in dups:
            t_off, t_len = pool.add(dup_target)
            parts.extend(struct.pack('<III', rel_to_id[dup_label], t_off, t_len))

    if slot_flags & FLAG_HAS_QUESTION_NORM:
        q_off, q_len = pool.add(graph['question_norm'])
        parts.extend(_PAIR_U32_STRUCT.pack(q_off, q_len))

    if slot_flags & FLAG_HAS_CONTENT_FORMAT:
        c_off, c_len = pool.add(graph['content_format'])
        parts.extend(_PAIR_U32_STRUCT.pack(c_off, c_len))

    if plat_code == _PLATFORM_CUSTOM:
        pl_off, pl_len = pool.add(str(entry.get('platform', '')))
        parts.extend(_PAIR_U32_STRUCT.pack(pl_off, pl_len))

    return bytes(parts)


def _encode_search(entry: dict, pool: _StringPool) -> bytes:
    exact_refs = entry.get('exact_refs', ())
    tokens = entry.get('tokens', ())

    if not exact_refs and not tokens:
        return b''

    parts = bytearray()
    parts.extend(_PAIR_U32_STRUCT.pack(len(exact_refs), len(tokens)))

    for ref in exact_refs:
        r_off, r_len = pool.add(ref)
        parts.extend(_PAIR_U32_STRUCT.pack(r_off, r_len))

    for tok in tokens:
        t_off, t_len = pool.add(tok)
        parts.extend(_PAIR_U32_STRUCT.pack(t_off, t_len))

    return bytes(parts)


def build_binary_image(
    key: bytes,
    record_format: str,
    entries: dict[str, dict],
) -> tuple[bytes, str]:
    """Serialize verified head-index entries into a signed HeadIndex v3 binary image."""
    pool = _StringPool()
    rf_off, rf_len = pool.add(record_format)

    sorted_ids = sorted(entries.keys(), key=lambda ident: ident.encode('utf-8'))
    id_to_slot = {ident: idx for idx, ident in enumerate(sorted_ids)}
    record_count = len(sorted_ids)

    kinds: list[str] = []
    kind_to_id: dict[str, int] = {}
    kind_totals: list[int] = []
    kind_actives: list[int] = []

    rel_labels: list[str] = []
    rel_to_id: dict[str, int] = {}

    def intern_kind(kind: str, active: bool) -> int:
        kid = kind_to_id.get(kind)

        if kid is None:
            kid = len(kinds)
            if kid >= 65535:
                raise ValueError('head index kind dictionary exceeds u16 limit')
            kinds.append(kind)
            kind_to_id[kind] = kid
            kind_totals.append(0)
            kind_actives.append(0)
        kind_totals[kid] += 1

        if active:
            kind_actives[kid] += 1

        return kid

    def intern_rel(label: str) -> int:
        rid = rel_to_id.get(label)

        if rid is None:
            rid = len(rel_labels)
            rel_labels.append(label)
            rel_to_id[label] = rid

        return rid

    slots_buf = bytearray(record_count * _SLOT_SIZE)
    active_map = bytearray(record_count)
    flags_map = bytearray(record_count)
    kind_map_buf = bytearray(record_count * 2)

    fwd_offsets = array('I', [0])
    fwd_edges_buf = bytearray()
    rev_buckets: list[list[tuple[str, str, int, int]]] = [[] for _ in range(record_count)]
    ext_rev_rows: list[tuple[bytes, str, str, int, int, int, int]] = []

    aux_offsets = array('I', [0])
    aux_pool = bytearray()
    search_offsets = array('I', [0])
    search_pool = bytearray()

    for slot_idx, ident in enumerate(sorted_ids):
        entry = entries[ident]
        graph = entry.get('graph')

        if not isinstance(graph, dict):
            graph = {}

        active = bool(entry.get('active', True))
        kind = str(entry['kind'])
        kid = intern_kind(kind, active)

        id_off, id_len = pool.add(ident)
        title = str(graph.get('title', '') or '')
        title_off, title_len = pool.add(title)
        state = str(graph.get('selection_state', '') or '')
        state_off, state_len = pool.add(state)

        if id_len > 0xFFFF or title_len > 0xFFFF or state_len > 0xFFFF:
            raise ValueError(f'head index string field exceeds u16 length for {ident}')

        basis = entry.get('basis') or ()
        content_links = graph.get('content_links') or {}
        dup_rels = graph.get('duplicate_relations') or ()
        question_norm = graph.get('question_norm')
        content_format = graph.get('content_format')
        exact_refs = entry.get('exact_refs') or ()
        tokens = entry.get('tokens') or ()

        is_open_question = (
            kind == 'question'
            and active
            and state in _OPEN_QUESTION_STATES
        )

        platform_val = entry.get('platform', 'posix')

        if platform_val == 'posix':
            plat_code = _PLATFORM_POSIX
        elif platform_val == 'nt':
            plat_code = _PLATFORM_NT
        else:
            plat_code = _PLATFORM_CUSTOM

        slot_flags = plat_code << _PLATFORM_SHIFT
        map_flags = 0

        if active:
            slot_flags |= FLAG_ACTIVE
            map_flags |= MAP_FLAG_ACTIVE

        if entry.get('tokens_truncated'):
            slot_flags |= FLAG_TOKENS_TRUNCATED
            map_flags |= MAP_FLAG_TOKENS_TRUNCATED

        if entry.get('exact_refs_truncated'):
            slot_flags |= FLAG_EXACT_REFS_TRUNCATED
            map_flags |= MAP_FLAG_EXACT_REFS_TRUNCATED

        if basis:
            slot_flags |= FLAG_HAS_BASIS
            map_flags |= MAP_FLAG_HAS_BASIS

        if content_links:
            slot_flags |= FLAG_HAS_CONTENT_LINKS
            map_flags |= MAP_FLAG_HAS_CONTENT_LINKS

        if dup_rels:
            slot_flags |= FLAG_HAS_DUP_RELS
            map_flags |= MAP_FLAG_HAS_DUP_RELS

        if question_norm is not None:
            slot_flags |= FLAG_HAS_QUESTION_NORM
            map_flags |= MAP_FLAG_HAS_QUESTION_NORM

        if content_format is not None:
            slot_flags |= FLAG_HAS_CONTENT_FORMAT

        if is_open_question:
            slot_flags |= FLAG_OPEN_QUESTION
            map_flags |= MAP_FLAG_OPEN_QUESTION

        for dup_label, _ in dup_rels:
            intern_rel(dup_label)

        for label, target in graph.get('relations', ()):
            rid = intern_rel(label)
            t_slot = id_to_slot.get(target, -1)
            t_off, t_len = pool.add(target)
            fwd_edges_buf.extend(_FWD_EDGE_STRUCT.pack(rid, t_slot, t_off, t_len))
            if t_slot >= 0:
                rev_buckets[t_slot].append((label, ident, rid, slot_idx))
            else:
                ext_rev_rows.append((target.encode('utf-8'), label, ident, t_off, t_len, rid, slot_idx))

        fwd_offsets.append(len(fwd_edges_buf) // _FWD_EDGE_STRUCT.size)

        aux_bytes = _encode_aux(entry, graph, slot_flags, plat_code, rel_to_id, pool)
        aux_pool.extend(aux_bytes)
        aux_offsets.append(len(aux_pool))

        search_bytes = _encode_search(entry, pool)
        search_pool.extend(search_bytes)
        search_offsets.append(len(search_pool))

        sha_bin = _hex64_to_bytes(entry['sha256'], 'record sha256')
        raw_sha_bin = _hex64_to_bytes(entry['raw_sha256'], 'record raw_sha256')
        size_val = max(0, int(entry.get('size', 0)))
        mtime_val = int(entry.get('mtime_ns', 0))
        ino_val = int(entry.get('ino', 0)) & 0xFFFFFFFFFFFFFFFF
        rev_val = int(entry.get('revision', 0))

        _SLOT_STRUCT.pack_into(
            slots_buf,
            slot_idx * _SLOT_SIZE,
            id_off,
            id_len,
            title_len,
            title_off,
            state_off,
            state_len,
            min(len(basis), 0xFFFF),
            kid,
            slot_flags,
            rev_val,
            min(len(exact_refs), 0xFFFF),
            min(len(tokens), 0xFFFF),
            min(len(content_links), 0xFFFF),
            min(len(dup_rels), 0xFFFF),
            0,
            size_val,
            mtime_val,
            ino_val,
            sha_bin,
            raw_sha_bin,
        )
        active_map[slot_idx] = 1 if active else 0
        flags_map[slot_idx] = map_flags
        _U16_STRUCT.pack_into(kind_map_buf, slot_idx * 2, kid)

    # Build kind & relation label tables.
    kind_table_buf = bytearray(len(kinds) * _KIND_ENTRY_STRUCT.size)

    for kid, kname in enumerate(kinds):
        k_off, k_len = pool.add(kname)
        _KIND_ENTRY_STRUCT.pack_into(
            kind_table_buf,
            kid * _KIND_ENTRY_STRUCT.size,
            k_off,
            k_len,
            kind_totals[kid],
            kind_actives[kid],
        )

    rel_table_buf = bytearray(len(rel_labels) * _REL_ENTRY_STRUCT.size)

    for rid, rname in enumerate(rel_labels):
        r_off, r_len = pool.add(rname)
        _REL_ENTRY_STRUCT.pack_into(rel_table_buf, rid * _REL_ENTRY_STRUCT.size, r_off, r_len)

    # Build Reverse CSR (sorted by (label, src_id) per target slot).
    rev_offsets = array('I', [0])
    rev_edges_buf = bytearray()

    for bucket in rev_buckets:
        if len(bucket) > 1:
            bucket.sort(key=lambda row: (row[0], row[1]))

        for _, _, rid, src_slot in bucket:
            rev_edges_buf.extend(_REV_EDGE_STRUCT.pack(rid, src_slot))
        rev_offsets.append(len(rev_edges_buf) // _REV_EDGE_STRUCT.size)

    # Build External-Target Reverse Table (sorted by (target_utf8, label, src_id)).
    ext_rev_rows.sort(key=lambda row: (row[0], row[1], row[2]))
    ext_rev_buf = bytearray(len(ext_rev_rows) * _EXT_REV_EDGE_STRUCT.size)

    for idx, (_, _, _, t_off, t_len, rid, src_slot) in enumerate(ext_rev_rows):
        _EXT_REV_EDGE_STRUCT.pack_into(
            ext_rev_buf,
            idx * _EXT_REV_EDGE_STRUCT.size,
            t_off,
            t_len,
            rid,
            src_slot,
        )

    string_pool_bytes = pool.to_bytes()

    if len(aux_pool) >= 0xFFFFFFFF or len(search_pool) >= 0xFFFFFFFF:
        raise ValueError('head index variable pool exceeds 4 GiB limit')

    fwd_offsets_bytes = fwd_offsets.tobytes()
    rev_offsets_bytes = rev_offsets.tobytes()
    aux_offsets_bytes = aux_offsets.tobytes()
    search_offsets_bytes = search_offsets.tobytes()

    # Assemble sections with 8-byte alignment padding where appropriate.
    sections: list[bytes | bytearray] = [
        slots_buf,
        active_map,
        flags_map,
        kind_map_buf,
        kind_table_buf,
        rel_table_buf,
        fwd_offsets_bytes,
        fwd_edges_buf,
        rev_offsets_bytes,
        rev_edges_buf,
        ext_rev_buf,
        aux_offsets_bytes,
        aux_pool,
        search_offsets_bytes,
        search_pool,
        string_pool_bytes,
    ]
    offsets: list[int] = []
    cursor = _HEADER_SIZE

    for sec in sections:
        pad = (-cursor) & 7
        cursor += pad
        offsets.append(cursor)
        cursor += len(sec)
    hmac_off = cursor

    header = _HEADER_STRUCT.pack(
        _MAGIC,
        _VERSION,
        0,
        record_count,
        len(kinds),
        len(rel_labels),
        len(fwd_edges_buf) // _FWD_EDGE_STRUCT.size,
        len(rev_edges_buf) // _REV_EDGE_STRUCT.size,
        len(ext_rev_rows),
        0,
        rf_off,
        rf_len,
        *offsets,
        hmac_off,
    )

    out = bytearray(hmac_off + 32)
    out[0:_HEADER_SIZE] = header

    for off, sec in zip(offsets, sections):
        out[off:off + len(sec)] = sec

    mv = memoryview(out)

    try:
        mac = hmac.new(key, BINARY_INDEX_DOMAIN, 'sha256')
        mac.update(mv[:hmac_off])
        digest_bytes = mac.digest()
    finally:
        mv.release()

    out[hmac_off:hmac_off + 32] = digest_bytes

    return bytes(out), digest_bytes.hex()


def _parse_pointer(pointer_path: Path) -> dict | None:
    try:
        info = pointer_path.lstat()
    except OSError:
        return None

    if not is_ordinary_file_stat(info) or info.st_size == 0 or info.st_size > 4096:
        return None

    try:
        raw = pointer_path.read_bytes()
        doc = strict_json_loads(raw, pointer_path, require_object=True)
    except (OSError, UnicodeDecodeError, ValueError):
        return None

    if doc.get('format') != HEAD_INDEX_FORMAT_V3:
        return None
    gen = doc.get('generation')

    if type(gen) is not int or gen < 1:
        return None
    expected_name = f'head-index.gen{gen}.bin'

    if doc.get('file') != expected_name:
        return None
    tag = doc.get('hmac')

    if not isinstance(tag, str) or not _HEX64_RE.fullmatch(tag):
        return None

    if not isinstance(doc.get('record_format'), str) or not doc['record_format']:
        return None
    return doc


def read_current_pointer(directory: Path) -> dict | None:
    """Read and validate the tiny atomic head-index.current.json pointer if present."""

    return _parse_pointer(directory / 'head-index.current.json')


def active_binary_path(directory: Path) -> Path | None:
    """Return the active immutable head-index.gen<N>.bin path, or None if absent/invalid."""
    doc = read_current_pointer(directory)

    if doc is None:
        return None
    candidate = directory / doc['file']

    return candidate if has_ordinary_file(candidate) else None


def _next_generation(directory: Path) -> int:
    highest = 0
    doc = read_current_pointer(directory)

    if doc is not None:
        highest = max(highest, doc['generation'])

    if directory.is_dir():
        try:
            with os.scandir(directory) as items:
                for item in items:
                    match = _GEN_FILE_RE.fullmatch(item.name)
                    if match:
                        highest = max(highest, int(match.group(1)))
        except OSError:
            pass

    return highest + 1


def _prune_old_generations(directory: Path, keep_name: str) -> None:
    """Opportunistically unlink obsolete generations (ignoring Windows active-mmap locks)."""

    if not directory.is_dir():
        return
    removed_any = False

    try:
        with os.scandir(directory) as items:
            names = [item.name for item in items]
    except OSError:
        return

    for name in names:
        if name == keep_name or not _GEN_FILE_RE.fullmatch(name):
            continue
        candidate = directory / name

        try:
            info = candidate.lstat()
            if is_ordinary_file_stat(info):
                candidate.unlink()
                removed_any = True
        except OSError:
            # Expected on Windows when a concurrent reader still holds an mmap on gen<N-1>.
            continue

    if removed_any:
        try:
            sync_dir(directory)
        except OSError:
            pass


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


def _open_readonly_ordinary(path: Path):
    if hasattr(os, 'O_NOFOLLOW'):
        try:
            fd = os.open(path, _OPEN_RO_NOFOLLOW)
        except OSError:
            return None

        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                os.close(fd)
                return None
            return os.fdopen(fd, 'rb'), info.st_size
        except BaseException:
            os.close(fd)
            raise

    try:
        info = path.lstat()
    except OSError:
        return None

    if not is_ordinary_file_stat(info):
        return None

    try:
        return path.open('rb'), info.st_size
    except OSError:
        return None


class HeadIndexView:
    """Zero-copy read-only mmap view over an HMAC-verified HeadIndex v3 generation (+ optional delta overlay)."""

    format = HEAD_INDEX_FORMAT_V3

    def __init__(
        self,
        path: Path,
        stream,
        mm: mmap.mmap,
        header_fields: tuple,
        hmac_hex: str,
        generation: int,
        delta_puts: dict[str, dict] | None = None,
        delta_dels: set[str] | None = None,
    ) -> None:
        self.path = path
        self._stream = stream
        self._mm = mm
        self._closed = False
        self.hmac_hex = hmac_hex
        self.generation = generation

        (
            _magic,
            self.version,
            self.header_flags,
            self.record_count,
            self.kind_count,
            self.rel_label_count,
            self.fwd_edge_count,
            self.rev_edge_count,
            self.ext_rev_count,
            _reserved,
            self._rf_off,
            self._rf_len,
            self._slot_table_off,
            self._active_map_off,
            self._flags_map_off,
            self._kind_map_off,
            self._kind_table_off,
            self._rel_table_off,
            self._fwd_csr_offsets_off,
            self._fwd_csr_edges_off,
            self._rev_csr_offsets_off,
            self._rev_csr_edges_off,
            self._ext_rev_edges_off,
            self._aux_offsets_off,
            self._aux_pool_off,
            self._search_offsets_off,
            self._search_pool_off,
            self._string_pool_off,
            self._hmac_off,
        ) = header_fields

        self.record_format = self._str(self._rf_off, self._rf_len)
        self._kind_cache: list[str] | None = None
        self._rel_cache: list[str] | None = None
        self.delta_puts: dict[str, dict] = dict(delta_puts) if delta_puts else {}
        self.delta_dels: set[str] = set(delta_dels) if delta_dels else set()

    @property
    def has_deltas(self) -> bool:
        return bool(self.delta_puts or self.delta_dels)

    def __enter__(self) -> HeadIndexView:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        mm = getattr(self, '_mm', None)
        stream = getattr(self, '_stream', None)
        self._mm = None
        self._stream = None

        if mm is not None:
            try:
                mm.close()
            except (BufferError, ValueError, OSError):
                pass

        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def __del__(self) -> None:
        self.close()

    def _check_open(self) -> mmap.mmap:
        mm = self._mm

        if self._closed or mm is None:
            raise ValueError('HeadIndexView is closed')

        return mm

    def _str_bytes(self, off: int, length: int) -> bytes:
        mm = self._check_open()
        start = self._string_pool_off + off

        return mm[start:start + length]

    def _str(self, off: int, length: int) -> str:
        if length == 0:
            return ''
        return self._str_bytes(off, length).decode('utf-8')

    def kind_names(self) -> list[str]:
        if self._kind_cache is None:
            mm = self._check_open()
            names: list[str] = []
            base = self._kind_table_off

            for idx in range(self.kind_count):
                s_off, s_len, _, _ = _KIND_ENTRY_STRUCT.unpack_from(mm, base + idx * 16)
                names.append(self._str(s_off, s_len))
            self._kind_cache = names

        return self._kind_cache

    def kind_counts(self) -> dict[str, tuple[int, int]]:
        """Return {kind_name: (total_count, active_count)} in O(K) from the binary header table."""
        mm = self._check_open()
        out: dict[str, tuple[int, int]] = {}
        base = self._kind_table_off

        for idx in range(self.kind_count):
            s_off, s_len, total_c, active_c = _KIND_ENTRY_STRUCT.unpack_from(mm, base + idx * 16)
            out[self._str(s_off, s_len)] = (total_c, active_c)

        return out

    def active_count_base(self) -> int:
        """Count active records in base generation via C-speed columnar bytemap scan."""
        mm = self._check_open()
        start = self._active_map_off

        return mm[start:start + self.record_count].count(1)

    def rel_labels(self) -> list[str]:
        if self._rel_cache is None:
            mm = self._check_open()
            labels: list[str] = []
            base = self._rel_table_off

            for idx in range(self.rel_label_count):
                s_off, s_len = _REL_ENTRY_STRUCT.unpack_from(mm, base + idx * 8)
                labels.append(self._str(s_off, s_len))
            self._rel_cache = labels

        return self._rel_cache

    def slot_id_bytes(self, slot: int) -> bytes:
        mm = self._check_open()
        id_off, id_len = struct.unpack_from('<IH', mm, self._slot_table_off + slot * _SLOT_SIZE)
        start = self._string_pool_off + id_off

        return mm[start:start + id_len]

    def slot_id(self, slot: int) -> str:
        return self.slot_id_bytes(slot).decode('utf-8')

    def lower_bound(self, ident: str) -> int:
        """O(log N) lower bound in sorted 128B slots: first slot whose ID >= ident (or record_count)."""
        target = ident.encode('utf-8')
        lo = 0
        hi = self.record_count
        mm = self._check_open()
        slot_base = self._slot_table_off
        str_base = self._string_pool_off

        while lo < hi:
            mid = (lo + hi) >> 1
            id_off, id_len = _SLOT_ID_STRUCT.unpack_from(mm, slot_base + mid * _SLOT_SIZE)
            start = str_base + id_off
            if mm[start:start + id_len] < target:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def slot_for_id(self, ident: str) -> int:
        """O(log N) binary search over sorted 128B slots directly in mmap (-1 if absent)."""
        target = ident.encode('utf-8')
        lo = 0
        hi = self.record_count
        mm = self._check_open()
        slot_base = self._slot_table_off
        str_base = self._string_pool_off

        while lo < hi:
            mid = (lo + hi) >> 1
            id_off, id_len = _SLOT_ID_STRUCT.unpack_from(mm, slot_base + mid * _SLOT_SIZE)
            start = str_base + id_off
            cur = mm[start:start + id_len]
            if cur < target:
                lo = mid + 1
            elif cur > target:
                hi = mid
            else:
                return mid
        return -1

    def slot_tuple(self, slot: int) -> tuple:
        mm = self._check_open()

        return _SLOT_STRUCT.unpack_from(mm, self._slot_table_off + slot * _SLOT_SIZE)

    def slot_stat_fast(self, slot: int) -> tuple[int, int, int, int]:
        """Unpack only (size, mtime_ns, ino, slot_flags) without decoding the full 128B slot or digests."""
        mm = self._check_open()
        base = self._slot_table_off + slot * _SLOT_SIZE
        size_val, mtime_ns, ino_val = _SLOT_STAT_STRUCT.unpack_from(mm, base + _SLOT_STAT_OFFSET)
        slot_flags = _U16_STRUCT.unpack_from(mm, base + _SLOT_FLAGS_OFFSET)[0]
        return size_val, mtime_ns, ino_val, slot_flags

    def slot_platform(self, slot: int, slot_flags: int) -> str:
        """Decode platform key ('posix', 'nt', or custom aux string) from slot_flags."""
        plat_code = (slot_flags & _PLATFORM_MASK) >> _PLATFORM_SHIFT
        if plat_code == _PLATFORM_POSIX:
            return 'posix'
        if plat_code == _PLATFORM_NT:
            return 'nt'
        return self._decode_aux(slot, slot_flags, only_platform=True)['platform']

    def slot_raw_sha_bin(self, slot: int) -> bytes:
        """Read 32-byte raw_sha256 digest slice directly from slot (slow path only)."""
        mm = self._check_open()
        start = self._slot_table_off + slot * _SLOT_SIZE + _SLOT_RAW_SHA_OFFSET
        return mm[start:start + 32]

    def slot_stat_meta(self, slot: int) -> tuple[int, int, int, str, bytes]:
        """Return (size, mtime_ns, ino, platform, raw_sha256_bin) from slot without decoding full entry."""
        size_val, mtime_ns, ino_val, slot_flags = self.slot_stat_fast(slot)
        plat = self.slot_platform(slot, slot_flags)
        return size_val, mtime_ns, ino_val, plat, self.slot_raw_sha_bin(slot)

    def slot_pin(self, slot: int) -> dict:
        """Return compact graph node pin {id, kind, revision, sha256, active} directly from slot."""
        tup = self.slot_tuple(slot)
        kinds = self.kind_names()

        return {
            'id': self._str(tup[0], tup[1]),
            'kind': kinds[tup[7]],
            'revision': tup[9],
            'sha256': tup[18].hex(),
            'active': bool(tup[8] & FLAG_ACTIVE),
        }

    def slot_kind_name(self, slot: int) -> str:
        mm = self._check_open()
        kid = _U16_STRUCT.unpack_from(mm, self._kind_map_off + slot * 2)[0]

        return self.kind_names()[kid]

    def slots_for_kind(self, kind: str) -> list[int]:
        """Return all slot indices matching a given kind name by scanning the 2B kind_map."""
        kinds = self.kind_names()

        try:
            target_kid = kinds.index(kind)
        except ValueError:
            return []
        mm = self._check_open()
        base = self._kind_map_off
        lo_byte = target_kid & 0xFF
        hi_byte = (target_kid >> 8) & 0xFF

        return [
            slot for slot in range(self.record_count)
            if mm[base + slot * 2] == lo_byte and mm[base + slot * 2 + 1] == hi_byte
        ]

    def slot_active(self, slot: int) -> bool:
        mm = self._check_open()

        return mm[self._active_map_off + slot] != 0

    def slot_map_flags(self, slot: int) -> int:
        mm = self._check_open()

        return mm[self._flags_map_off + slot]

    def slot_summary_fields(self, slot: int) -> dict:
        """Return lightweight status/scope summary fields from slot + basis without decoding search/relations."""
        tup = self.slot_tuple(slot)
        slot_flags = tup[8]
        kinds = self.kind_names()
        basis = self.slot_basis(slot, slot_flags) if (slot_flags & FLAG_HAS_BASIS) else []
        return {
            'format': self.record_format,
            'id': self._str(tup[0], tup[1]),
            'kind': kinds[tup[7]],
            'sha256': tup[18].hex(),
            'revision': tup[9],
            'basis': basis,
            'active': bool(slot_flags & FLAG_ACTIVE),
        }

    def slot_basis(self, slot: int, slot_flags: int | None = None) -> list[dict]:
        if slot_flags is None:
            slot_flags = self.slot_tuple(slot)[8]
        if not (slot_flags & FLAG_HAS_BASIS):
            return []
        mm = self._check_open()
        a_start, a_end = _PAIR_U32_STRUCT.unpack_from(mm, self._aux_offsets_off + slot * 4)
        if a_start >= a_end:
            return []
        pos = self._aux_pool_off + a_start
        count = _U32_STRUCT.unpack_from(mm, pos)[0]
        pos += 4
        basis: list[dict] = []
        for _ in range(count):
            p_off, p_len, has_sha = _BASIS_HEAD_STRUCT.unpack_from(mm, pos)
            pos += _BASIS_HEAD_STRUCT.size
            if has_sha:
                sha_hex: str | None = mm[pos:pos + 32].hex()
                pos += 32
            else:
                sha_hex = None
            b_len = _U64_STRUCT.unpack_from(mm, pos)[0]
            pos += 8
            basis.append({'path': self._str(p_off, p_len), 'sha256': sha_hex, 'bytes': b_len})
        return basis

    def _decode_aux(self, slot: int, slot_flags: int, *, only_platform: bool = False) -> dict:
        mm = self._check_open()
        a_start, a_end = _PAIR_U32_STRUCT.unpack_from(mm, self._aux_offsets_off + slot * 4)
        pos = self._aux_pool_off + a_start
        end_pos = self._aux_pool_off + a_end
        result: dict = {
            'basis': [],
            'content_links': {},
            'duplicate_relations': None,
            'question_norm': None,
            'content_format': None,
            'platform': None,
        }
        if pos >= end_pos:
            return result

        if slot_flags & FLAG_HAS_BASIS:
            count = _U32_STRUCT.unpack_from(mm, pos)[0]
            pos += 4
            basis: list[dict] = []
            for _ in range(count):
                p_off, p_len, has_sha = _BASIS_HEAD_STRUCT.unpack_from(mm, pos)
                pos += _BASIS_HEAD_STRUCT.size
                if has_sha:
                    sha_hex: str | None = None if only_platform else mm[pos:pos + 32].hex()
                    pos += 32
                else:
                    sha_hex = None
                b_len = _U64_STRUCT.unpack_from(mm, pos)[0]
                pos += 8
                if not only_platform:
                    basis.append({'path': self._str(p_off, p_len), 'sha256': sha_hex, 'bytes': b_len})
            result['basis'] = basis

        if slot_flags & FLAG_HAS_CONTENT_LINKS:
            field_count = _U32_STRUCT.unpack_from(mm, pos)[0]
            pos += 4
            clinks: dict[str, list[str]] = {}
            for _ in range(field_count):
                k_off, k_len = _PAIR_U32_STRUCT.unpack_from(mm, pos)
                pos += 8
                v_count = _U32_STRUCT.unpack_from(mm, pos)[0]
                pos += 4
                if only_platform:
                    pos += v_count * 8
                else:
                    vals: list[str] = []
                    for _ in range(v_count):
                        v_off, v_len = _PAIR_U32_STRUCT.unpack_from(mm, pos)
                        pos += 8
                        vals.append(self._str(v_off, v_len))
                    clinks[self._str(k_off, k_len)] = vals
            result['content_links'] = clinks

        if slot_flags & FLAG_HAS_DUP_RELS:
            dup_count = _U32_STRUCT.unpack_from(mm, pos)[0]
            pos += 4
            if only_platform:
                pos += dup_count * 12
            else:
                labels = self.rel_labels()
                dups: list[list[str]] = []
                for _ in range(dup_count):
                    rid, t_off, t_len = struct.unpack_from('<III', mm, pos)
                    pos += 12
                    dups.append([labels[rid], self._str(t_off, t_len)])
                result['duplicate_relations'] = dups

        if slot_flags & FLAG_HAS_QUESTION_NORM:
            q_off, q_len = _PAIR_U32_STRUCT.unpack_from(mm, pos)
            pos += 8
            if not only_platform:
                result['question_norm'] = self._str(q_off, q_len)

        if slot_flags & FLAG_HAS_CONTENT_FORMAT:
            c_off, c_len = _PAIR_U32_STRUCT.unpack_from(mm, pos)
            pos += 8
            if not only_platform:
                result['content_format'] = self._str(c_off, c_len)

        plat_code = (slot_flags & _PLATFORM_MASK) >> _PLATFORM_SHIFT
        if plat_code == _PLATFORM_CUSTOM and pos + 8 <= end_pos:
            pl_off, pl_len = _PAIR_U32_STRUCT.unpack_from(mm, pos)
            result['platform'] = self._str(pl_off, pl_len)

        return result

    def slot_graph_aux(self, slot: int, slot_flags: int | None = None) -> dict:
        """Decode only graph-relevant aux fields (content_links, duplicate_relations, question_norm)."""
        if slot_flags is None:
            slot_flags = self.slot_tuple(slot)[8]
        return self._decode_aux(slot, slot_flags, only_platform=False)

    def _decode_search(self, slot: int) -> tuple[list[str], list[str]]:
        mm = self._check_open()
        s_start, s_end = _PAIR_U32_STRUCT.unpack_from(mm, self._search_offsets_off + slot * 4)
        if s_start >= s_end:
            return [], []
        pos = self._search_pool_off + s_start
        ref_count, tok_count = _PAIR_U32_STRUCT.unpack_from(mm, pos)
        pos += 8
        refs: list[str] = []
        for _ in range(ref_count):
            r_off, r_len = _PAIR_U32_STRUCT.unpack_from(mm, pos)
            pos += 8
            refs.append(self._str(r_off, r_len))
        toks: list[str] = []
        for _ in range(tok_count):
            t_off, t_len = _PAIR_U32_STRUCT.unpack_from(mm, pos)
            pos += 8
            toks.append(self._str(t_off, t_len))
        return refs, toks

    def fwd_edges_raw(self, slot: int) -> list[tuple[int, int, int, int]]:
        """Return forward CSR tuples (rel_id, target_slot, target_str_off, target_str_len) in authored order."""
        mm = self._check_open()
        e_start, e_end = _PAIR_U32_STRUCT.unpack_from(mm, self._fwd_csr_offsets_off + slot * 4)
        if e_start >= e_end:
            return []
        base = self._fwd_csr_edges_off
        return [
            _FWD_EDGE_STRUCT.unpack_from(mm, base + idx * 16)
            for idx in range(e_start, e_end)
        ]

    def fwd_edges(self, slot: int) -> list[tuple[str, str, int]]:
        """Return forward edges as (label, target_str, target_slot) in authored order."""
        raw = self.fwd_edges_raw(slot)
        if not raw:
            return []
        labels = self.rel_labels()
        return [(labels[rid], self._str(t_off, t_len), t_slot) for rid, t_slot, t_off, t_len in raw]

    def rev_edges_raw(self, slot: int) -> list[tuple[int, int]]:
        """Return reverse CSR tuples (rel_id, src_slot) pre-sorted by (label, src_id)."""
        mm = self._check_open()
        e_start, e_end = _PAIR_U32_STRUCT.unpack_from(mm, self._rev_csr_offsets_off + slot * 4)
        if e_start >= e_end:
            return []
        base = self._rev_csr_edges_off
        return [
            _REV_EDGE_STRUCT.unpack_from(mm, base + idx * 8)
            for idx in range(e_start, e_end)
        ]

    def rev_edges(self, slot: int) -> list[tuple[str, int]]:
        """Return reverse edges as (label, src_slot) pre-sorted by (label, src_id)."""
        raw = self.rev_edges_raw(slot)
        if not raw:
            return []
        labels = self.rel_labels()
        return [(labels[rid], src_slot) for rid, src_slot in raw]

    def ext_rev_edges(self, target: str) -> list[tuple[str, int]]:
        """O(log E_ext) binary search for inbound edges targeting a non-record external string."""
        count = self.ext_rev_count
        if count == 0:
            return []
        mm = self._check_open()
        target_bytes = target.encode('utf-8')
        base = self._ext_rev_edges_off
        str_base = self._string_pool_off

        lo = 0
        hi = count
        while lo < hi:
            mid = (lo + hi) >> 1
            t_off, t_len, _, _ = _EXT_REV_EDGE_STRUCT.unpack_from(mm, base + mid * 16)
            cur = mm[str_base + t_off:str_base + t_off + t_len]
            if cur < target_bytes:
                lo = mid + 1
            else:
                hi = mid

        labels = self.rel_labels()
        out: list[tuple[str, int]] = []
        idx = lo
        while idx < count:
            t_off, t_len, rid, src_slot = _EXT_REV_EDGE_STRUCT.unpack_from(mm, base + idx * 16)
            cur = mm[str_base + t_off:str_base + t_off + t_len]
            if cur != target_bytes:
                break
            out.append((labels[rid], src_slot))
            idx += 1
        return out

    def open_question_slots(self) -> list[int]:
        """Fast scan over the columnar flags_map to collect open-question slot indices."""
        mm = self._check_open()
        f_off = self._flags_map_off
        return [
            slot for slot in range(self.record_count)
            if mm[f_off + slot] & MAP_FLAG_OPEN_QUESTION
        ]

    def decode_slot_entry(self, slot: int) -> dict:
        """Materialize one full HeadIndex v2-compatible entry dict for slot."""
        tup = self.slot_tuple(slot)
        (
            id_off,
            id_len,
            title_len,
            title_off,
            state_off,
            state_len,
            _basis_count,
            kid,
            slot_flags,
            revision,
            _ref_count,
            _tok_count,
            _clink_count,
            _dup_count,
            _reserved,
            size_val,
            mtime_ns,
            ino_val,
            sha_bin,
            raw_sha_bin,
        ) = tup

        ident = self._str(id_off, id_len)
        kinds = self.kind_names()
        aux = self._decode_aux(slot, slot_flags)
        exact_refs, tokens = self._decode_search(slot)
        relations = [[label, target] for label, target, _ in self.fwd_edges(slot)]

        plat_code = (slot_flags & _PLATFORM_MASK) >> _PLATFORM_SHIFT
        if plat_code == _PLATFORM_POSIX:
            plat = 'posix'
        elif plat_code == _PLATFORM_NT:
            plat = 'nt'
        else:
            plat = aux['platform'] or ''

        graph: dict = {
            'relations': relations,
            'content_links': aux['content_links'],
            'title': self._str(title_off, title_len),
            'selection_state': self._str(state_off, state_len),
        }
        if aux['duplicate_relations']:
            graph['duplicate_relations'] = aux['duplicate_relations']
        if aux['question_norm'] is not None:
            graph['question_norm'] = aux['question_norm']
        if aux['content_format'] is not None:
            graph['content_format'] = aux['content_format']

        entry: dict = {
            'format': self.record_format,
            'id': ident,
            'kind': kinds[kid],
            'revision': revision,
            'sha256': sha_bin.hex(),
            'active': bool(slot_flags & FLAG_ACTIVE),
            'basis': aux['basis'],
            'raw_sha256': raw_sha_bin.hex(),
            'size': size_val,
            'mtime_ns': mtime_ns,
            'ino': ino_val,
            'platform': plat,
            'exact_refs': exact_refs,
            'tokens': tokens,
            'graph': graph,
        }
        if slot_flags & FLAG_TOKENS_TRUNCATED:
            entry['tokens_truncated'] = True
        if slot_flags & FLAG_EXACT_REFS_TRUNCATED:
            entry['exact_refs_truncated'] = True
        return entry

    def get_entry(self, ident: str) -> dict | None:
        """O(log N) single-record lookup with delta overlay."""
        if ident in self.delta_puts:
            return self.delta_puts[ident]
        if ident in self.delta_dels:
            return None
        slot = self.slot_for_id(ident)
        if slot < 0:
            return None
        return self.decode_slot_entry(slot)

    def get_raw_sha256(self, ident: str) -> str | None:
        """O(log N) lookup of raw_sha256 hex digest without decoding full entry."""
        if ident in self.delta_puts:
            raw_sha = self.delta_puts[ident].get('raw_sha256')
            return raw_sha if isinstance(raw_sha, str) else None
        if ident in self.delta_dels:
            return None
        slot = self.slot_for_id(ident)
        if slot < 0:
            return None
        return self.slot_tuple(slot)[19].hex()

    def iter_ids(self) -> Iterator[str]:
        seen: set[str] = set()
        for slot in range(self.record_count):
            ident = self.slot_id(slot)
            if ident in self.delta_dels:
                continue
            seen.add(ident)
            yield ident
        for ident in sorted(self.delta_puts):
            if ident not in seen:
                yield ident

    def all_entries(self) -> dict[str, dict]:
        """Materialize all entries as a HeadIndex v2-compatible dictionary with deltas applied."""
        out: dict[str, dict] = {}
        dels = self.delta_dels
        puts = self.delta_puts
        for slot in range(self.record_count):
            ident = self.slot_id(slot)
            if ident in dels or ident in puts:
                continue
            out[ident] = self.decode_slot_entry(slot)
        out.update(puts)
        return out


def _validate_header_bounds(fields: tuple, file_size: int) -> bool:
    (
        magic,
        version,
        _hflags,
        record_count,
        kind_count,
        rel_label_count,
        fwd_edge_count,
        rev_edge_count,
        ext_rev_count,
        _reserved,
        rf_off,
        rf_len,
        slot_table_off,
        active_map_off,
        flags_map_off,
        kind_map_off,
        kind_table_off,
        rel_table_off,
        fwd_csr_offsets_off,
        fwd_csr_edges_off,
        rev_csr_offsets_off,
        rev_csr_edges_off,
        ext_rev_edges_off,
        aux_offsets_off,
        aux_pool_off,
        search_offsets_off,
        search_pool_off,
        string_pool_off,
        hmac_off,
    ) = fields

    if magic != _MAGIC or version != _VERSION:
        return False
    if hmac_off + 32 != file_size or hmac_off < _HEADER_SIZE:
        return False

    checks = (
        (slot_table_off, record_count * _SLOT_SIZE),
        (active_map_off, record_count),
        (flags_map_off, record_count),
        (kind_map_off, record_count * 2),
        (kind_table_off, kind_count * _KIND_ENTRY_STRUCT.size),
        (rel_table_off, rel_label_count * _REL_ENTRY_STRUCT.size),
        (fwd_csr_offsets_off, (record_count + 1) * 4),
        (fwd_csr_edges_off, fwd_edge_count * _FWD_EDGE_STRUCT.size),
        (rev_csr_offsets_off, (record_count + 1) * 4),
        (rev_csr_edges_off, rev_edge_count * _REV_EDGE_STRUCT.size),
        (ext_rev_edges_off, ext_rev_count * _EXT_REV_EDGE_STRUCT.size),
        (aux_offsets_off, (record_count + 1) * 4),
        (search_offsets_off, (record_count + 1) * 4),
    )
    for start, length in checks:
        if start < _HEADER_SIZE or start + length > hmac_off:
            return False

    if (
        aux_pool_off < _HEADER_SIZE
        or search_pool_off < _HEADER_SIZE
        or aux_pool_off > search_offsets_off
        or search_pool_off > string_pool_off
    ):
        return False
    if string_pool_off > hmac_off or string_pool_off + rf_off + rf_len > hmac_off:
        return False
    return True


def open_binary_view(
    directory: Path,
    key: bytes,
    record_format: str,
) -> HeadIndexView | None:
    """Open, zero-copy HMAC-verify, and return a HeadIndexView, or None on missing/invalid/tampered index."""
    pointer = read_current_pointer(directory)
    if pointer is None:
        return None
    if pointer.get('record_format') != record_format:
        return None

    bin_path = directory / pointer['file']
    opened = _open_readonly_ordinary(bin_path)
    if opened is None:
        return None

    stream, file_size = opened
    if file_size < _HEADER_SIZE + 32:
        stream.close()
        return None

    mm: mmap.mmap | None = None
    try:
        mm = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        fields = _HEADER_STRUCT.unpack_from(mm, 0)
        if not _validate_header_bounds(fields, file_size):
            mm.close()
            stream.close()
            return None

        hmac_off = fields[-1]
        mv = memoryview(mm)
        try:
            mac = hmac.new(key, BINARY_INDEX_DOMAIN, 'sha256')
            mac.update(mv[:hmac_off])
            computed = mac.digest()
            footer = bytes(mv[hmac_off:hmac_off + 32])
        finally:
            mv.release()

        if not hmac.compare_digest(computed, footer):
            mm.close()
            stream.close()
            return None

        hmac_hex = footer.hex()
        if not hmac.compare_digest(hmac_hex, pointer['hmac']):
            mm.close()
            stream.close()
            return None

        view = HeadIndexView(bin_path, stream, mm, fields, hmac_hex, pointer['generation'])
        if view.record_format != record_format:
            view.close()
            return None
        return view
    except (OSError, ValueError, struct.error, UnicodeDecodeError):
        if mm is not None:
            try:
                mm.close()
            except (BufferError, ValueError, OSError):
                pass
        try:
            stream.close()
        except OSError:
            pass
        return None


__all__ = [
    'BINARY_INDEX_DOMAIN',
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
