"""Parallel and indexed record head scanners extracted from RecordStore."""
from __future__ import annotations

import errno
import hmac
import os
from pathlib import Path
import stat

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


def read_scoped_one(store, ident: str, needs_body, indexed, records_fd: int | None):
    filename = packet_id(ident) + '.json'
    full_path = store.work / 'records' / filename

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

    indexed = store.index.load(store.format)
    if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
        with store.ex.open_workspace_dir('records') as records_fd:
            results = chunk_map(
                idents,
                lambda chunk: [read_scoped_one(store, i, needs_body, indexed, records_fd) for i in chunk],
                max_workers=min(max_workers, 8),
            )
        yield from results
        return

    _, dir_str = validated_records_dir_win(store)

    def scoped_win_chunk(chunk):
        out = []
        for ident in chunk:
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
    if (not idents and not prebuilt) or not store.index.path.exists():
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
