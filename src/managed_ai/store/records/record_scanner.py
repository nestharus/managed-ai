"""Parallel and indexed record head scanners extracted from RecordStore."""
from __future__ import annotations

import errno
import hashlib
import hmac
import os
from pathlib import Path
import stat

from managed_ai.core.formats import HEAD_INDEX_FORMAT
from managed_ai.core.ownership import packet_id
from managed_ai.core.storage import (
    _OPEN_RO_NOFOLLOW,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    sha,
)
from managed_ai.platform import PLATFORM
from managed_ai.store.protocol.inputs import parse_input_bytes
from managed_ai.store.records.head_index import build_entry
from managed_ai.store.records.head_index_bin import HeadIndexView
from managed_ai.store.records.head_receipts import MIN_BYTES

_SUMMARY_KEYS = ('format', 'id', 'kind', 'sha256', 'revision', 'basis', 'active')


def chunk_map(items: list, fn, *, max_workers: int = 8, min_parallel: int = 16) -> list:
    if len(items) < min_parallel:
        return fn(items)

    workers = min(max_workers, len(items))
    step = (len(items) + workers - 1) // workers
    chunks = [items[i:i + step] for i in range(0, len(items), step)]
    if len(chunks) <= 1:
        return fn(items)

    from concurrent.futures import ThreadPoolExecutor

    out: list = []
    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        for batch in pool.map(fn, chunks):
            out.extend(batch)
    return out


def validated_records_dir_win(store) -> tuple[Path, str]:
    records_dir = store.ex.path('records')
    try:
        dir_info = records_dir.lstat()
    except OSError:
        raise ValueError(f'ordinary records directory required: {records_dir}') from None

    if not is_ordinary_dir_stat(dir_info):
        raise ValueError(f'ordinary records directory required: {records_dir}')
    return records_dir, str(records_dir)


def read_verified_win(
    store,
    dir_str: str,
    ident: str,
    *,
    allow_cold: bool = True,
) -> tuple[dict, bytes, os.stat_result | None]:
    filename = packet_id(ident) + '.json'
    full_str = dir_str + os.sep + filename
    try:
        info = os.lstat(full_str)
    except FileNotFoundError:
        if allow_cold and store.cold_archive.has(ident):
            record, raw = store.cold_archive.read(ident)
            return record, raw, None
        raise ValueError(f'ordinary JSON file required: {full_str}') from None
    except OSError:
        raise ValueError(f'ordinary JSON file required: {full_str}') from None

    if not is_ordinary_file_stat(info):
        raise ValueError(f'ordinary JSON file required: {full_str}')

    with open(full_str, 'rb') as stream:
        raw = stream.read()

    record = parse_input_bytes(raw, Path(full_str))
    store._verify_record(ident, record, raw)
    return record, raw, info


def _read_dir_fd_raw(filename: str, full_path: Path, records_fd: int) -> tuple[bytes, os.stat_result]:
    try:
        fd = os.open(filename, _OPEN_RO_NOFOLLOW, dir_fd=records_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
            raise ValueError(f'ordinary JSON file required: {full_path}') from None
        raise

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'ordinary JSON file required: {full_path}')
        stream = os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise

    with stream:
        return stream.read(), info


def read_scoped_one(store, ident: str, needs_body, indexed, records_fd: int | None, view: HeadIndexView | None = None):
    filename = packet_id(ident) + '.json'
    full_path = store.work / 'records' / filename

    if view is not None:
        entry = view.delta_puts.get(ident)
        slot = -1
        if entry is None and ident not in view.delta_dels:
            slot = view.slot_for_id(ident)

        if entry is not None:
            if not needs_body(ident, entry['kind']):
                if hasattr(os, 'O_NOFOLLOW') and records_fd is not None:
                    raw, _ = _read_dir_fd_raw(filename, full_path, records_fd)
                else:
                    try:
                        info = full_path.lstat()
                    except OSError:
                        raise ValueError(f'ordinary JSON file required: {full_path}') from None
                    if not is_ordinary_file_stat(info):
                        raise ValueError(f'ordinary JSON file required: {full_path}')
                    raw = full_path.read_bytes()

                if hmac.compare_digest(sha(raw), entry['raw_sha256']):
                    return {k: entry[k] for k in _SUMMARY_KEYS}

                record = parse_input_bytes(raw, full_path)
                store._verify_record(ident, record, raw)
                return record
        elif slot >= 0:
            kind = view.slot_kind_name(slot)
            if not needs_body(ident, kind):
                if hasattr(os, 'O_NOFOLLOW') and records_fd is not None:
                    raw, _ = _read_dir_fd_raw(filename, full_path, records_fd)
                else:
                    try:
                        info = full_path.lstat()
                    except OSError:
                        raise ValueError(f'ordinary JSON file required: {full_path}') from None
                    if not is_ordinary_file_stat(info):
                        raise ValueError(f'ordinary JSON file required: {full_path}')
                    raw = full_path.read_bytes()

                raw_sha_bin = view.slot_tuple(slot)[19]
                if hmac.compare_digest(hashlib.sha256(raw).digest(), raw_sha_bin):
                    return view.slot_summary_fields(slot)

                record = parse_input_bytes(raw, full_path)
                store._verify_record(ident, record, raw)
                return record

    if indexed is not None and ident in indexed:
        entry = indexed[ident]
        if not needs_body(ident, entry['kind']):
            if hasattr(os, 'O_NOFOLLOW') and records_fd is not None:
                raw, _ = _read_dir_fd_raw(filename, full_path, records_fd)
            else:
                try:
                    info = full_path.lstat()
                except OSError:
                    raise ValueError(f'ordinary JSON file required: {full_path}') from None
                if not is_ordinary_file_stat(info):
                    raise ValueError(f'ordinary JSON file required: {full_path}')
                raw = full_path.read_bytes()

            if hmac.compare_digest(sha(raw), entry['raw_sha256']):
                return {k: entry[k] for k in _SUMMARY_KEYS}

            record = parse_input_bytes(raw, full_path)
            store._verify_record(ident, record, raw)
            return record

    if hasattr(os, 'O_NOFOLLOW') and records_fd is not None:
        raw, info = _read_dir_fd_raw(filename, full_path, records_fd)
        if info.st_size >= MIN_BYTES and not store.receipts._xattr_unsupported:
            projection = store.receipts.projection(full_path, ident, store.format, size=info.st_size)
            if projection is not None:
                summary, raw_digest = projection
                if not needs_body(ident, summary['kind']) and hmac.compare_digest(sha(raw), raw_digest):
                    return summary

        record = parse_input_bytes(raw, full_path)
        store._verify_record(ident, record, raw)
        return record

    path = store.path(ident)
    projection = store.receipts.projection(path, ident, store.format)
    if projection is not None:
        summary, raw_digest = projection
        if not needs_body(ident, summary['kind']) and store.receipts.matches(path, raw_digest):
            return summary

    return store.show(ident)


def scoped_heads(store, needs_body, *, max_workers: int = 16):
    """Yield cryptographically verified scope/status fields and full records for nominated roles."""
    idents = store.ids()
    if not idents:
        return

    view = store.index.open_view(store.format)
    indexed = None if view is not None else store.index.load(store.format)
    try:
        if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
            with store.ex.open_workspace_dir('records') as records_fd:
                results = chunk_map(
                    idents,
                    lambda chunk: [read_scoped_one(store, i, needs_body, indexed, records_fd, view) for i in chunk],
                    max_workers=min(max_workers, 8),
                )
            yield from results
            return

        _, dir_str = validated_records_dir_win(store)

        def scoped_win_chunk(chunk):
            out = []
            for ident in chunk:
                if view is not None:
                    entry = view.delta_puts.get(ident)
                    slot = -1
                    if entry is None and ident not in view.delta_dels:
                        slot = view.slot_for_id(ident)

                    if entry is not None and not needs_body(ident, entry['kind']):
                        full_str = dir_str + os.sep + ident + '.json'
                        try:
                            info = os.lstat(full_str)
                        except OSError:
                            raise ValueError(f'ordinary JSON file required: {full_str}') from None
                        if not is_ordinary_file_stat(info):
                            raise ValueError(f'ordinary JSON file required: {full_str}')
                        with open(full_str, 'rb') as stream:
                            raw = stream.read()
                        if hmac.compare_digest(sha(raw), entry['raw_sha256']):
                            out.append({k: entry[k] for k in _SUMMARY_KEYS})
                            continue
                        record = parse_input_bytes(raw, Path(full_str))
                        store._verify_record(ident, record, raw)
                        out.append(record)
                        continue

                    if slot >= 0 and not needs_body(ident, view.slot_kind_name(slot)):
                        full_str = dir_str + os.sep + ident + '.json'
                        try:
                            info = os.lstat(full_str)
                        except OSError:
                            raise ValueError(f'ordinary JSON file required: {full_str}') from None
                        if not is_ordinary_file_stat(info):
                            raise ValueError(f'ordinary JSON file required: {full_str}')
                        with open(full_str, 'rb') as stream:
                            raw = stream.read()
                        raw_sha_bin = view.slot_tuple(slot)[19]
                        if hmac.compare_digest(hashlib.sha256(raw).digest(), raw_sha_bin):
                            out.append(view.slot_summary_fields(slot))
                            continue
                        record = parse_input_bytes(raw, Path(full_str))
                        store._verify_record(ident, record, raw)
                        out.append(record)
                        continue

                entry = indexed.get(ident) if indexed is not None else None
                if entry is not None and not needs_body(ident, entry['kind']):
                    full_str = dir_str + os.sep + ident + '.json'
                    try:
                        info = os.lstat(full_str)
                    except OSError:
                        raise ValueError(f'ordinary JSON file required: {full_str}') from None
                    if not is_ordinary_file_stat(info):
                        raise ValueError(f'ordinary JSON file required: {full_str}')

                    with open(full_str, 'rb') as stream:
                        raw = stream.read()
                    if hmac.compare_digest(sha(raw), entry['raw_sha256']):
                        out.append({k: entry[k] for k in _SUMMARY_KEYS})
                        continue

                    record = parse_input_bytes(raw, Path(full_str))
                    store._verify_record(ident, record, raw)
                    out.append(record)
                    continue

                record, _, _ = read_verified_win(store, dir_str, ident, allow_cold=False)
                out.append(record)
            return out

        yield from chunk_map(idents, scoped_win_chunk, max_workers=min(max_workers, 8))
    finally:
        if view is not None:
            view.close()


class VerifiedIndexView:
    """Context-managed HeadIndexView whose hot record filesystem metadata has been verified."""

    def __init__(
        self,
        view: HeadIndexView,
        *,
        live_ids: list[str],
        overrides: dict[str, dict],
        removed: set[str],
    ) -> None:
        self.view = view
        self.live_ids = live_ids
        self.overrides = overrides
        self.removed = removed
        self.is_exact_base = (not view.has_deltas) and (not overrides) and (not removed)

    def __enter__(self) -> VerifiedIndexView:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self.view.close()

    def all_entries(self) -> dict[str, dict]:
        if self.is_exact_base:
            base = self.view.all_entries()
            return {ident: base[ident] for ident in self.live_ids}

        out: dict[str, dict] = {}
        dels = self.view.delta_dels | self.removed
        puts = self.view.delta_puts
        overrides = self.overrides
        for ident in self.live_ids:
            if ident in overrides:
                out[ident] = overrides[ident]
            elif ident in puts:
                out[ident] = puts[ident]
            elif ident not in dels:
                slot = self.view.slot_for_id(ident)
                if slot >= 0:
                    out[ident] = self.view.decode_slot_entry(slot)
        return out


def _advance_slot_cursor(
    mm,
    slot_base: int,
    str_base: int,
    rec_count: int,
    cursor: int,
    target: bytes,
) -> tuple[int, int]:
    """O(1) amortized sorted merge-walk over sorted 128B mmap slots; returns (matched_slot_or_neg1, next_cursor)."""
    from managed_ai.store.records.head_index_bin import _SLOT_ID_STRUCT, _SLOT_SIZE

    unpack_id = _SLOT_ID_STRUCT.unpack_from
    while cursor < rec_count:
        id_off, id_len = unpack_id(mm, slot_base + cursor * _SLOT_SIZE)
        start = str_base + id_off
        cur = mm[start:start + id_len]
        if cur < target:
            cursor += 1
        elif cur == target:
            return cursor, cursor + 1
        else:
            return -1, cursor
    return -1, cursor


def _verify_view_posix_chunk(store, view: HeadIndexView, records_fd: int, chunk: list[str]) -> list[tuple[str, dict]]:
    if not chunk:
        return []

    from managed_ai.store.records.head_index_bin import (
        _PLATFORM_MASK,
        _PLATFORM_NT,
        _PLATFORM_POSIX,
        _PLATFORM_SHIFT,
        _SLOT_FLAGS_OFFSET,
        _SLOT_SIZE,
        _SLOT_STAT_OFFSET,
        _SLOT_STAT_STRUCT,
        _U16_STRUCT,
    )

    platform_key = PLATFORM.platform_key
    expected_plat_code = _PLATFORM_POSIX if platform_key == 'posix' else (_PLATFORM_NT if platform_key == 'nt' else -1)
    delta_puts = view.delta_puts
    delta_dels = view.delta_dels
    overrides: list[tuple[str, dict]] = []

    mm = view._check_open()
    slot_base = view._slot_table_off
    str_base = view._string_pool_off
    rec_count = view.record_count
    cursor = view.lower_bound(chunk[0])
    unpack_stat = _SLOT_STAT_STRUCT.unpack_from
    unpack_u16 = _U16_STRUCT.unpack_from

    for ident in chunk:
        delta_entry = delta_puts.get(ident)
        slot = -1
        if delta_entry is None and ident not in delta_dels:
            slot, cursor = _advance_slot_cursor(
                mm, slot_base, str_base, rec_count, cursor, ident.encode('utf-8'),
            )

        filename = (ident if (delta_entry is not None or slot >= 0) else packet_id(ident)) + '.json'
        full_path = store.work / 'records' / filename
        try:
            info = os.stat(filename, dir_fd=records_fd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR):
                raise ValueError(f'ordinary JSON file required: {full_path}') from None
            raise

        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'ordinary JSON file required: {full_path}')

        if delta_entry is not None:
            if _entry_metadata_matches(store, ident, delta_entry, info):
                if delta_entry.get('ino') == info.st_ino and delta_entry.get('platform', platform_key) == platform_key:
                    continue
                raw, _ = _read_dir_fd_raw(filename, full_path, records_fd)
                if hmac.compare_digest(sha(raw), delta_entry['raw_sha256']):
                    continue
                record = parse_input_bytes(raw, full_path)
                store._verify_record(ident, record, raw)
                overrides.append((ident, build_entry(record, raw, info)))
                continue
        elif slot >= 0:
            off = slot_base + slot * _SLOT_SIZE
            size_val, mtime_ns, ino_val = unpack_stat(mm, off + _SLOT_STAT_OFFSET)
            if size_val == info.st_size and mtime_ns == info.st_mtime_ns:
                slot_flags = unpack_u16(mm, off + _SLOT_FLAGS_OFFSET)[0]
                plat_code = (slot_flags & _PLATFORM_MASK) >> _PLATFORM_SHIFT
                if ino_val == info.st_ino and (
                    plat_code == expected_plat_code
                    or (expected_plat_code < 0 and view.slot_platform(slot, slot_flags) == platform_key)
                ):
                    continue
                raw, _ = _read_dir_fd_raw(filename, full_path, records_fd)
                if hmac.compare_digest(hashlib.sha256(raw).digest(), view.slot_raw_sha_bin(slot)):
                    continue
                record = parse_input_bytes(raw, full_path)
                store._verify_record(ident, record, raw)
                overrides.append((ident, build_entry(record, raw, info)))
                continue

        record, raw = store.read(ident, dir_fd=records_fd)
        overrides.append((ident, build_entry(record, raw, info)))

    return overrides


def _verify_view_win_chunk(store, view: HeadIndexView, dir_str: str, chunk: list[str]) -> list[tuple[str, dict]]:
    if not chunk:
        return []

    from managed_ai.store.records.head_index_bin import (
        _PLATFORM_MASK,
        _PLATFORM_NT,
        _PLATFORM_POSIX,
        _PLATFORM_SHIFT,
        _SLOT_FLAGS_OFFSET,
        _SLOT_SIZE,
        _SLOT_STAT_OFFSET,
        _SLOT_STAT_STRUCT,
        _U16_STRUCT,
    )

    platform_key = PLATFORM.platform_key
    expected_plat_code = _PLATFORM_POSIX if platform_key == 'posix' else (_PLATFORM_NT if platform_key == 'nt' else -1)
    delta_puts = view.delta_puts
    delta_dels = view.delta_dels
    overrides: list[tuple[str, dict]] = []

    mm = view._check_open()
    slot_base = view._slot_table_off
    str_base = view._string_pool_off
    rec_count = view.record_count
    cursor = view.lower_bound(chunk[0])
    unpack_stat = _SLOT_STAT_STRUCT.unpack_from
    unpack_u16 = _U16_STRUCT.unpack_from

    for ident in chunk:
        delta_entry = delta_puts.get(ident)
        slot = -1
        if delta_entry is None and ident not in delta_dels:
            slot, cursor = _advance_slot_cursor(
                mm, slot_base, str_base, rec_count, cursor, ident.encode('utf-8'),
            )

        filename = (ident if (delta_entry is not None or slot >= 0) else packet_id(ident)) + '.json'
        full_str = dir_str + os.sep + filename
        try:
            info = os.lstat(full_str)
        except OSError:
            raise ValueError(f'ordinary JSON file required: {full_str}') from None

        if not is_ordinary_file_stat(info):
            raise ValueError(f'ordinary JSON file required: {full_str}')

        if delta_entry is not None:
            if _entry_metadata_matches(store, ident, delta_entry, info):
                if delta_entry.get('ino') == info.st_ino and delta_entry.get('platform', platform_key) == platform_key:
                    continue
                with open(full_str, 'rb') as stream:
                    raw = stream.read()
                if hmac.compare_digest(sha(raw), delta_entry['raw_sha256']):
                    continue
                record = parse_input_bytes(raw, Path(full_str))
                store._verify_record(ident, record, raw)
                overrides.append((ident, build_entry(record, raw, info)))
                continue
        elif slot >= 0:
            off = slot_base + slot * _SLOT_SIZE
            size_val, mtime_ns, ino_val = unpack_stat(mm, off + _SLOT_STAT_OFFSET)
            if size_val == info.st_size and mtime_ns == info.st_mtime_ns:
                slot_flags = unpack_u16(mm, off + _SLOT_FLAGS_OFFSET)[0]
                plat_code = (slot_flags & _PLATFORM_MASK) >> _PLATFORM_SHIFT
                if ino_val == info.st_ino and (
                    plat_code == expected_plat_code
                    or (expected_plat_code < 0 and view.slot_platform(slot, slot_flags) == platform_key)
                ):
                    continue
                with open(full_str, 'rb') as stream:
                    raw = stream.read()
                if hmac.compare_digest(hashlib.sha256(raw).digest(), view.slot_raw_sha_bin(slot)):
                    continue
                record = parse_input_bytes(raw, Path(full_str))
                store._verify_record(ident, record, raw)
                overrides.append((ident, build_entry(record, raw, info)))
                continue

        record, raw, cur_info = read_verified_win(store, dir_str, ident, allow_cold=False)
        overrides.append((ident, build_entry(record, raw, cur_info or info)))

    return overrides


def open_verified_index_view(store, *, max_workers: int = 16) -> VerifiedIndexView | None:
    """Open a binary v3 HeadIndexView and verify hot record freshness in zero-copy mmap."""
    view = store.index.open_view(store.format)
    if view is None:
        return None

    try:
        raw_idents = store.ids()
        # HeadIndex v3 slots are sorted by UTF-8 bare ID (head_index_bin.py:build_binary_image), whereas
        # RecordStore.ids() preserves legacy <id>.json filename order; sort for O(N) merge-walk while
        # keeping raw_idents on VerifiedIndexView.live_ids for exact unindexed status() order parity.
        sorted_idents = sorted(raw_idents, key=lambda ident: ident.encode('utf-8'))
        overrides: dict[str, dict] = {}
        if sorted_idents:
            if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
                with store.ex.open_workspace_dir('records') as records_fd:
                    pairs = chunk_map(
                        sorted_idents,
                        lambda chunk: _verify_view_posix_chunk(store, view, records_fd, chunk),
                        max_workers=min(max_workers, 8),
                    )
            else:
                _, dir_str = validated_records_dir_win(store)
                pairs = chunk_map(
                    sorted_idents,
                    lambda chunk: _verify_view_win_chunk(store, view, dir_str, chunk),
                    max_workers=min(max_workers, 8),
                )
            overrides = dict(pairs)

        if len(sorted_idents) == view.record_count and not view.has_deltas and not overrides:
            removed: set[str] = set()
        else:
            live_set = set(sorted_idents)
            removed = {ident for ident in view.iter_ids() if ident not in live_set}

        return VerifiedIndexView(view, live_ids=raw_idents, overrides=overrides, removed=removed)
    except BaseException:
        view.close()
        raise


def get_verified_entry(store, ident: str) -> dict | None:
    """O(log N) single-record stat + HMAC verified lookup via binary v3 mmap."""
    ident = packet_id(ident)
    view = store.index.open_view(store.format)
    if view is None:
        return None

    with view:
        path = store.path(ident)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise ValueError(f'ordinary JSON file required: {path}') from None

        if not is_ordinary_file_stat(info):
            raise ValueError(f'ordinary JSON file required: {path}')

        platform_key = PLATFORM.platform_key
        delta_entry = view.delta_puts.get(ident)
        if delta_entry is not None:
            if _entry_metadata_matches(store, ident, delta_entry, info):
                if delta_entry.get('ino') == info.st_ino and delta_entry.get('platform', platform_key) == platform_key:
                    return dict(delta_entry)
                raw = path.read_bytes()
                if hmac.compare_digest(sha(raw), delta_entry['raw_sha256']):
                    return dict(delta_entry)
        elif ident not in view.delta_dels:
            slot = view.slot_for_id(ident)
            if slot >= 0:
                size_val, mtime_ns, ino_val, plat, raw_sha_bin = view.slot_stat_meta(slot)
                if size_val == info.st_size and mtime_ns == info.st_mtime_ns:
                    if ino_val == info.st_ino and plat == platform_key:
                        return view.decode_slot_entry(slot)
                    raw = path.read_bytes()
                    if hmac.compare_digest(hashlib.sha256(raw).digest(), raw_sha_bin):
                        return view.decode_slot_entry(slot)

        record, raw = store.read(ident)
        return build_entry(record, raw, info)


def _entry_metadata_matches(store, ident: str, entry: dict | None, info: os.stat_result) -> bool:
    return (
        entry is not None
        and entry.get('format') == store.format
        and entry.get('id') == ident
        and entry.get('size') == info.st_size
        and entry.get('mtime_ns') == info.st_mtime_ns
    )


def _check_indexed_posix_chunk(store, indexed: dict[str, dict], records_fd: int, chunk: list[str]) -> list:
    platform_key = PLATFORM.platform_key
    out = []

    for ident in chunk:
        entry = indexed.get(ident)
        filename = (ident if entry is not None else packet_id(ident)) + '.json'
        full_path = store.work / 'records' / filename
        try:
            info = os.stat(filename, dir_fd=records_fd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR):
                raise ValueError(f'ordinary JSON file required: {full_path}') from None
            raise

        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f'ordinary JSON file required: {full_path}')

        if _entry_metadata_matches(store, ident, entry, info):
            if entry.get('ino') == info.st_ino and entry.get('platform', platform_key) == platform_key:
                out.append((ident, entry))
                continue

            # Cross-OS st_ino mismatch (e.g. Windows <-> WSL): verify raw SHA-256 against HeadIndex.
            raw, _ = _read_dir_fd_raw(filename, full_path, records_fd)
            if hmac.compare_digest(sha(raw), entry['raw_sha256']):
                out.append((ident, entry))
                continue

            record = parse_input_bytes(raw, full_path)
            store._verify_record(ident, record, raw)
            out.append((ident, build_entry(record, raw, info)))
            continue

        record, raw = store.read(ident, dir_fd=records_fd)
        out.append((ident, build_entry(record, raw, info)))

    return out


def _check_indexed_win_chunk(store, indexed: dict[str, dict], dir_str: str, chunk: list[str]) -> list:
    platform_key = PLATFORM.platform_key
    out = []

    for ident in chunk:
        entry = indexed.get(ident)
        filename = (ident if entry is not None else packet_id(ident)) + '.json'
        full_str = dir_str + os.sep + filename
        try:
            info = os.lstat(full_str)
        except OSError:
            raise ValueError(f'ordinary JSON file required: {full_str}') from None

        if not is_ordinary_file_stat(info):
            raise ValueError(f'ordinary JSON file required: {full_str}')

        if _entry_metadata_matches(store, ident, entry, info):
            if entry.get('ino') == info.st_ino and entry.get('platform', platform_key) == platform_key:
                out.append((ident, entry))
                continue

            with open(full_str, 'rb') as stream:
                raw = stream.read()
            if hmac.compare_digest(sha(raw), entry['raw_sha256']):
                out.append((ident, entry))
                continue

            record = parse_input_bytes(raw, Path(full_str))
            store._verify_record(ident, record, raw)
            out.append((ident, build_entry(record, raw, info)))
            continue

        record, raw, cur_info = read_verified_win(store, dir_str, ident, allow_cold=False)
        out.append((ident, build_entry(record, raw, cur_info or info)))

    return out


def indexed_summaries_with_format(store, *, max_workers: int = 16) -> tuple[dict[str, dict], str] | None:
    """Return (verified_entries, index_format) or None if unindexed (works on POSIX and Windows)."""
    verified = open_verified_index_view(store, max_workers=max_workers)
    if verified is not None:
        with verified:
            return verified.all_entries(), HEAD_INDEX_FORMAT

    loaded = store.index.load_with_format(store.format)
    if loaded is None:
        return None

    indexed, index_format = loaded
    idents = store.ids()
    if not idents:
        return {}, index_format

    if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
        with store.ex.open_workspace_dir('records') as records_fd:
            pairs = chunk_map(
                idents,
                lambda chunk: _check_indexed_posix_chunk(store, indexed, records_fd, chunk),
                max_workers=min(max_workers, 8),
            )
        return dict(pairs), index_format

    _, dir_str = validated_records_dir_win(store)
    pairs = chunk_map(
        idents,
        lambda chunk: _check_indexed_win_chunk(store, indexed, dir_str, chunk),
        max_workers=min(max_workers, 8),
    )
    return dict(pairs), index_format


def scan_records(store, idents=None, *, max_workers: int = 16) -> list[dict]:
    """Read and cryptographically verify nominated records (or all records) in parallel."""
    target_ids = store.ids() if idents is None else list(idents)
    if not target_ids:
        return []

    if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
        with store.ex.open_workspace_dir('records') as records_fd:
            return chunk_map(
                target_ids,
                lambda chunk: [store.read(ident, dir_fd=records_fd)[0] for ident in chunk],
                max_workers=min(max_workers, 8),
            )

    _, dir_str = validated_records_dir_win(store)
    return chunk_map(
        target_ids,
        lambda chunk: [read_verified_win(store, dir_str, ident, allow_cold=True)[0] for ident in chunk],
        max_workers=min(max_workers, 8),
    )


def reindex(store, *, max_workers: int = 16) -> dict:
    """Rebuild the HMAC-verified store-local head index under an exclusive owner lock."""
    idents = store.ids()
    entries: dict[str, dict] = {}

    if idents:
        if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
            with store.ex.open_workspace_dir('records') as records_fd:
                def index_chunk_posix(chunk):
                    out = []
                    for ident in chunk:
                        filename = packet_id(ident) + '.json'
                        record, raw = store.read(ident, dir_fd=records_fd)
                        info = os.stat(filename, dir_fd=records_fd, follow_symlinks=False)
                        out.append((ident, build_entry(record, raw, info)))
                    return out

                pairs = chunk_map(idents, index_chunk_posix, max_workers=min(max_workers, 8))
                entries = dict(pairs)
        else:
            _, dir_str = validated_records_dir_win(store)

            def index_chunk_win(chunk):
                out = []
                for ident in chunk:
                    record, raw, info = read_verified_win(store, dir_str, ident, allow_cold=False)
                    out.append((ident, build_entry(record, raw, info)))
                return out

            pairs = chunk_map(idents, index_chunk_win, max_workers=min(max_workers, 8))
            entries = dict(pairs)

    return store.index.save(store.format, entries, create_key=True)


def refresh_index_entries(store, idents: list[str], *, prebuilt: dict[str, dict] | None = None) -> bool:
    """Batch-update existing HMAC head index entries for freshly committed records."""
    if (not idents and not prebuilt) or not store.index.exists():
        return False

    updates: dict[str, dict] = dict(prebuilt) if prebuilt else {}
    if idents:
        if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
            with store.ex.open_workspace_dir('records') as records_fd:
                for ident in idents:
                    filename = packet_id(ident) + '.json'
                    record, raw = store.read(ident, dir_fd=records_fd)
                    info = os.stat(filename, dir_fd=records_fd, follow_symlinks=False)
                    updates[ident] = build_entry(record, raw, info)
        else:
            _, dir_str = validated_records_dir_win(store)
            for ident in idents:
                record, raw, info = read_verified_win(store, dir_str, ident, allow_cold=False)
                updates[ident] = build_entry(record, raw, info)

    return store.index.update_if_present(store.format, updates)
