"""Wire-format schema, struct layouts, and binary builder for HeadIndex v3."""
from __future__ import annotations

from array import array
import hmac
import re
import struct

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
            item_bytes = max(0, int(item.get('bytes', 0)))
            parts.extend(_U64_STRUCT.pack(item_bytes))

    if slot_flags & FLAG_HAS_CONTENT_LINKS:
        items = sorted(graph.get('content_links', {}).items())
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


def _compute_slot_flags(
    entry: dict,
    graph: dict,
    kind: str,
    active: bool,
    state: str,
) -> tuple[int, int, int]:
    """Compute (slot_flags, map_flags, plat_code) bitfields for one record slot."""
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

    if entry.get('basis'):
        slot_flags |= FLAG_HAS_BASIS
        map_flags |= MAP_FLAG_HAS_BASIS

    if graph.get('content_links'):
        slot_flags |= FLAG_HAS_CONTENT_LINKS
        map_flags |= MAP_FLAG_HAS_CONTENT_LINKS

    if graph.get('duplicate_relations'):
        slot_flags |= FLAG_HAS_DUP_RELS
        map_flags |= MAP_FLAG_HAS_DUP_RELS

    if graph.get('question_norm') is not None:
        slot_flags |= FLAG_HAS_QUESTION_NORM
        map_flags |= MAP_FLAG_HAS_QUESTION_NORM

    if graph.get('content_format') is not None:
        slot_flags |= FLAG_HAS_CONTENT_FORMAT

    if kind == 'question' and active and state in _OPEN_QUESTION_STATES:
        slot_flags |= FLAG_OPEN_QUESTION
        map_flags |= MAP_FLAG_OPEN_QUESTION

    return slot_flags, map_flags, plat_code


def _encode_one_slot(
    slot_idx: int,
    ident: str,
    entry: dict,
    *,
    id_to_slot: dict[str, int],
    pool: _StringPool,
    intern_kind,
    intern_rel,
    rel_to_id: dict[str, int],
    buffers: dict,
) -> None:
    """Encode a single record entry into fixed-width slot, columnar maps, CSR edges, and aux/search pools."""
    graph = entry.get('graph')
    if not isinstance(graph, dict):
        graph = {}

    active = bool(entry.get('active', True))
    kind = str(entry['kind'])
    kid = intern_kind(kind, active)

    id_off, id_len = pool.add(ident)
    title_off, title_len = pool.add(str(graph.get('title', '') or ''))
    state = str(graph.get('selection_state', '') or '')
    state_off, state_len = pool.add(state)

    if id_len > 0xFFFF or title_len > 0xFFFF or state_len > 0xFFFF:
        raise ValueError(f'head index string field exceeds u16 length for {ident}')

    slot_flags, map_flags, plat_code = _compute_slot_flags(entry, graph, kind, active, state)

    for dup_label, _ in graph.get('duplicate_relations') or ():
        intern_rel(dup_label)

    fwd_edges_buf = buffers['fwd_edges_buf']
    rev_buckets = buffers['rev_buckets']
    ext_rev_rows = buffers['ext_rev_rows']

    for label, target in graph.get('relations', ()):
        rid = intern_rel(label)
        t_slot = id_to_slot.get(target, -1)
        t_off, t_len = pool.add(target)
        fwd_edges_buf.extend(_FWD_EDGE_STRUCT.pack(rid, t_slot, t_off, t_len))
        if t_slot >= 0:
            rev_buckets[t_slot].append((label, ident, rid, slot_idx))
        else:
            ext_rev_rows.append((target.encode('utf-8'), label, ident, t_off, t_len, rid, slot_idx))

    buffers['fwd_offsets'].append(len(fwd_edges_buf) // _FWD_EDGE_STRUCT.size)
    buffers['aux_pool'].extend(_encode_aux(entry, graph, slot_flags, plat_code, rel_to_id, pool))
    buffers['aux_offsets'].append(len(buffers['aux_pool']))
    buffers['search_pool'].extend(_encode_search(entry, pool))
    buffers['search_offsets'].append(len(buffers['search_pool']))

    _SLOT_STRUCT.pack_into(
        buffers['slots_buf'],
        slot_idx * _SLOT_SIZE,
        id_off,
        id_len,
        title_len,
        title_off,
        state_off,
        state_len,
        min(len(entry.get('basis') or ()), 0xFFFF),
        kid,
        slot_flags,
        int(entry.get('revision', 0)),
        min(len(entry.get('exact_refs') or ()), 0xFFFF),
        min(len(entry.get('tokens') or ()), 0xFFFF),
        min(len(graph.get('content_links') or {}), 0xFFFF),
        min(len(graph.get('duplicate_relations') or ()), 0xFFFF),
        0,
        max(0, int(entry.get('size', 0))),
        int(entry.get('mtime_ns', 0)),
        int(entry.get('ino', 0)) & 0xFFFFFFFFFFFFFFFF,
        _hex64_to_bytes(entry['sha256'], 'record sha256'),
        _hex64_to_bytes(entry['raw_sha256'], 'record raw_sha256'),
    )
    buffers['active_map'][slot_idx] = 1 if active else 0
    buffers['flags_map'][slot_idx] = map_flags
    _U16_STRUCT.pack_into(buffers['kind_map_buf'], slot_idx * 2, kid)


def _encode_record_slots(
    sorted_ids: list[str],
    entries: dict[str, dict],
    id_to_slot: dict[str, int],
    pool: _StringPool,
) -> dict:
    """Encode fixed-width slots, columnar bytemaps, forward edges, aux/search pools, and reverse buckets."""
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

    buffers = {
        'slots_buf': bytearray(record_count * _SLOT_SIZE),
        'active_map': bytearray(record_count),
        'flags_map': bytearray(record_count),
        'kind_map_buf': bytearray(record_count * 2),
        'fwd_offsets': array('I', [0]),
        'fwd_edges_buf': bytearray(),
        'rev_buckets': [[] for _ in range(record_count)],
        'ext_rev_rows': [],
        'aux_offsets': array('I', [0]),
        'aux_pool': bytearray(),
        'search_offsets': array('I', [0]),
        'search_pool': bytearray(),
    }

    for slot_idx, ident in enumerate(sorted_ids):
        _encode_one_slot(
            slot_idx,
            ident,
            entries[ident],
            id_to_slot=id_to_slot,
            pool=pool,
            intern_kind=intern_kind,
            intern_rel=intern_rel,
            rel_to_id=rel_to_id,
            buffers=buffers,
        )

    return {
        'kinds': kinds,
        'kind_totals': kind_totals,
        'kind_actives': kind_actives,
        'rel_labels': rel_labels,
        **buffers,
    }


def _build_dictionary_tables(
    kinds: list[str],
    kind_totals: list[int],
    kind_actives: list[int],
    rel_labels: list[str],
    pool: _StringPool,
) -> tuple[bytearray, bytearray]:
    """Encode kind dictionary (with total/active counts) and relation label table."""
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

    return kind_table_buf, rel_table_buf


def _build_reverse_tables(
    rev_buckets: list[list[tuple[str, str, int, int]]],
    ext_rev_rows: list[tuple[bytes, str, str, int, int, int, int]],
) -> tuple[array, bytearray, bytearray]:
    """Build sorted reverse CSR (per target slot) and external-target reverse table."""
    rev_offsets = array('I', [0])
    rev_edges_buf = bytearray()

    for bucket in rev_buckets:
        if len(bucket) > 1:
            bucket.sort(key=lambda row: (row[0], row[1]))
        for _, _, rid, src_slot in bucket:
            rev_edges_buf.extend(_REV_EDGE_STRUCT.pack(rid, src_slot))
        rev_offsets.append(len(rev_edges_buf) // _REV_EDGE_STRUCT.size)

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

    return rev_offsets, rev_edges_buf, ext_rev_buf


def _assemble_signed_image(
    key: bytes,
    rf_off: int,
    rf_len: int,
    counts: tuple[int, int, int, int, int, int],
    sections: list[bytes | bytearray],
) -> tuple[bytes, str]:
    """Pack 8-byte aligned sections behind the 192B header and append the 32B HMAC-SHA256 footer."""
    offsets: list[int] = []
    cursor = _HEADER_SIZE

    for sec in sections:
        cursor += (-cursor) & 7
        offsets.append(cursor)
        cursor += len(sec)
    hmac_off = cursor

    record_count, kind_count, rel_count, fwd_count, rev_count, ext_rev_count = counts
    header = _HEADER_STRUCT.pack(
        _MAGIC,
        _VERSION,
        0,
        record_count,
        kind_count,
        rel_count,
        fwd_count,
        rev_count,
        ext_rev_count,
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

    encoded = _encode_record_slots(sorted_ids, entries, id_to_slot, pool)
    kind_table_buf, rel_table_buf = _build_dictionary_tables(
        encoded['kinds'],
        encoded['kind_totals'],
        encoded['kind_actives'],
        encoded['rel_labels'],
        pool,
    )
    rev_offsets, rev_edges_buf, ext_rev_buf = _build_reverse_tables(
        encoded['rev_buckets'],
        encoded['ext_rev_rows'],
    )

    aux_pool = encoded['aux_pool']
    search_pool = encoded['search_pool']
    if len(aux_pool) >= 0xFFFFFFFF or len(search_pool) >= 0xFFFFFFFF:
        raise ValueError('head index variable pool exceeds 4 GiB limit')

    sections: list[bytes | bytearray] = [
        encoded['slots_buf'],
        encoded['active_map'],
        encoded['flags_map'],
        encoded['kind_map_buf'],
        kind_table_buf,
        rel_table_buf,
        encoded['fwd_offsets'].tobytes(),
        encoded['fwd_edges_buf'],
        rev_offsets.tobytes(),
        rev_edges_buf,
        ext_rev_buf,
        encoded['aux_offsets'].tobytes(),
        aux_pool,
        encoded['search_offsets'].tobytes(),
        search_pool,
        pool.to_bytes(),
    ]
    counts = (
        len(sorted_ids),
        len(encoded['kinds']),
        len(encoded['rel_labels']),
        len(encoded['fwd_edges_buf']) // _FWD_EDGE_STRUCT.size,
        len(rev_edges_buf) // _REV_EDGE_STRUCT.size,
        len(encoded['ext_rev_rows']),
    )
    return _assemble_signed_image(key, rf_off, rf_len, counts, sections)


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
