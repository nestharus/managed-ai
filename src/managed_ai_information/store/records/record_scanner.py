"""Parallel and indexed record head scanners extracted from RecordStore."""

import errno
import hashlib
import hmac
import mmap
import os
import stat
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from managed_ai_information.core.formats import HEAD_INDEX_FORMAT
from managed_ai_information.core.ownership import packet_id
from managed_ai_information.core.storage import (
    OPEN_RO_NOFOLLOW,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    sha,
)
from managed_ai_information.platform import PLATFORM
from managed_ai_information.store.protocol.inputs import parse_input_bytes
from managed_ai_information.store.records import head_index_bin as _bin
from managed_ai_information.store.records.head_index import build_entry
from managed_ai_information.store.records.head_index_bin import HeadIndexView
from managed_ai_information.store.records.head_receipts import MIN_BYTES

_SUMMARY_KEYS = ("format", "id", "kind", "sha256", "revision", "basis", "active")
_PLAT_CODES = {"posix": _bin._PLATFORM_POSIX, "nt": _bin._PLATFORM_NT}
_BAD_STAT_ERRNOS = (errno.ELOOP, errno.ENOENT, errno.ENOTDIR)
_BAD_OPEN_ERRNOS = (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO)


def _parse_and_verify(store: Any, ident: str, raw: bytes, path: Path) -> dict[str, Any]:
    record = parse_input_bytes(raw, path)
    store._verify_record(ident, record, raw)
    return record


class _ScanOps:
    """Per-OS bound file operations for one scan batch (zero extra syscalls)."""

    __slots__ = ("dir_str", "pkey", "plat_code", "records_dir", "records_fd", "store")

    def __init__(
        self,
        store: Any,
        records_fd: int | None = None,
        records_dir: Path | None = None,
        dir_str: str | None = None,
    ) -> None:
        self.store = store
        self.records_fd, self.records_dir, self.dir_str = records_fd, records_dir, dir_str
        self.pkey = PLATFORM.platform_key
        self.plat_code = _PLAT_CODES.get(self.pkey, -1)

    def stat(self, filename: str) -> os.stat_result:
        if self.records_fd is not None and self.records_dir is not None:
            full = self.records_dir / filename
            try:
                info = os.stat(filename, dir_fd=self.records_fd, follow_symlinks=False)
            except OSError as exc:
                if exc.errno in _BAD_STAT_ERRNOS:
                    raise ValueError(f"ordinary JSON file required: {full}") from None
                raise
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"ordinary JSON file required: {full}")
            return info
        full_str = f"{self.dir_str}{os.sep}{filename}"
        try:
            info = os.lstat(full_str)
        except OSError:
            raise ValueError(f"ordinary JSON file required: {full_str}") from None
        if not is_ordinary_file_stat(info):
            raise ValueError(f"ordinary JSON file required: {full_str}")
        return info

    def read_raw(self, filename: str) -> tuple[bytes, Path]:
        if self.records_fd is not None and self.records_dir is not None:
            full = self.records_dir / filename
            return _read_dir_fd_raw(filename, full, self.records_fd)[0], full
        full = Path(f"{self.dir_str}{os.sep}{filename}")
        return full.read_bytes(), full

    def verify_or_rebuild(
        self, ident: str, filename: str, info: os.stat_result, expected: bytes | str
    ) -> dict[str, Any] | None:
        raw, full_path = self.read_raw(filename)
        matched = (
            hmac.compare_digest(hashlib.sha256(raw).digest(), expected)
            if isinstance(expected, bytes)
            else hmac.compare_digest(sha(raw), expected)
        )
        if matched:
            return None
        return build_entry(_parse_and_verify(self.store, ident, raw, full_path), raw, info)

    def check_dict(
        self, ident: str, entry: dict[str, Any], filename: str, info: os.stat_result
    ) -> tuple[bool, dict[str, Any] | None]:
        if (
            entry.get("format") != self.store.format
            or entry.get("id") != ident
            or entry.get("size") != info.st_size
            or entry.get("mtime_ns") != info.st_mtime_ns
        ):
            return False, None
        if entry.get("ino") == info.st_ino and entry.get("platform", self.pkey) == self.pkey:
            return True, None
        return True, self.verify_or_rebuild(ident, filename, info, entry["raw_sha256"])

    def check_slot(
        self,
        view: HeadIndexView,
        mm: mmap.mmap,
        slot: int,
        ident: str,
        filename: str,
        info: os.stat_result,
    ) -> tuple[bool, dict[str, Any] | None]:
        off = view._slot_table_off + slot * _bin._SLOT_SIZE
        size_val, mtime_ns, ino_val = _bin._SLOT_STAT_STRUCT.unpack_from(
            mm, off + _bin._SLOT_STAT_OFFSET
        )
        if size_val != info.st_size or mtime_ns != info.st_mtime_ns:
            return False, None
        flags = _bin._U16_STRUCT.unpack_from(mm, off + _bin._SLOT_FLAGS_OFFSET)[0]
        code = (flags & _bin._PLATFORM_MASK) >> _bin._PLATFORM_SHIFT
        if ino_val == info.st_ino and (
            code == self.plat_code
            or (self.plat_code < 0 and view.slot_platform(slot, flags) == self.pkey)
        ):
            return True, None
        return True, self.verify_or_rebuild(ident, filename, info, view.slot_raw_sha_bin(slot))

    def fallback(self, ident: str, info: os.stat_result | None = None) -> dict[str, Any]:
        if self.records_fd is not None:
            record, raw = self.store.read(ident, dir_fd=self.records_fd)
            resolved = info or os.stat(
                f"{packet_id(ident)}.json", dir_fd=self.records_fd, follow_symlinks=False
            )
            return build_entry(record, raw, resolved)
        record, raw, cur = read_verified_win(
            self.store, self.dir_str or "", ident, allow_cold=False
        )
        if (final_info := cur or info) is None:
            raise RuntimeError(f"missing stat metadata for hot record: {ident}")
        return build_entry(record, raw, final_info)

    def read_record(self, ident: str, *, allow_cold: bool = False) -> dict[str, Any]:
        if self.records_fd is not None:
            return self.store.read(ident, dir_fd=self.records_fd)[0]
        return read_verified_win(self.store, self.dir_str or "", ident, allow_cold=allow_cold)[0]


@contextmanager
def _open_scan_ops(store: Any) -> Generator[_ScanOps]:
    if hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
        with store.ex.open_workspace_dir("records") as records_fd:
            yield _ScanOps(store, records_fd=records_fd, records_dir=store.work / "records")
        return
    _, dir_str = validated_records_dir_win(store)
    yield _ScanOps(store, dir_str=dir_str)


def chunk_map[T, R](
    items: list[T],
    fn: Callable[[list[T]], list[R]],
    *,
    max_workers: int = 8,
    min_parallel: int = 16,
) -> list[R]:
    if len(items) < min_parallel:
        return fn(items)
    workers = min(max_workers, len(items))
    step = (len(items) + workers - 1) // workers
    chunks = [items[i : i + step] for i in range(0, len(items), step)]
    if len(chunks) <= 1:
        return fn(items)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        return [item for batch in pool.map(fn, chunks) for item in batch]


def validated_records_dir_win(store: Any) -> tuple[Path, str]:
    records_dir = store.ex.path("records")
    try:
        if is_ordinary_dir_stat(records_dir.lstat()):
            return records_dir, str(records_dir)
    except OSError:
        pass
    raise ValueError(f"ordinary records directory required: {records_dir}")


def _read_dir_fd_raw(
    filename: str, full_path: Path, records_fd: int
) -> tuple[bytes, os.stat_result]:
    try:
        fd = os.open(filename, OPEN_RO_NOFOLLOW, dir_fd=records_fd)
    except OSError as exc:
        if exc.errno in _BAD_OPEN_ERRNOS:
            raise ValueError(f"ordinary JSON file required: {full_path}") from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"ordinary JSON file required: {full_path}")
        stream = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with stream:
        return stream.read(), info


def read_verified_win(
    store: Any, dir_str: str, ident: str, *, allow_cold: bool = True
) -> tuple[dict[str, Any], bytes, os.stat_result | None]:
    full_str = f"{dir_str}{os.sep}{packet_id(ident)}.json"
    try:
        info = os.lstat(full_str)
    except FileNotFoundError:
        if allow_cold and store.cold_archive.has(ident):
            return (*store.cold_archive.read(ident), None)
        raise ValueError(f"ordinary JSON file required: {full_str}") from None
    except OSError:
        raise ValueError(f"ordinary JSON file required: {full_str}") from None
    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary JSON file required: {full_str}")
    full_path = Path(full_str)
    raw = full_path.read_bytes()
    return _parse_and_verify(store, ident, raw, full_path), raw, info


def _scoped_candidate(
    ident: str,
    needs_body: Callable[[str, str], bool],
    view: HeadIndexView | None,
    indexed: dict[str, dict[str, Any]] | None,
) -> tuple[bytes | str, dict[str, Any] | int] | None:
    if view is not None:
        if (entry := view.delta_puts.get(ident)) is not None:
            return (entry["raw_sha256"], entry) if not needs_body(ident, entry["kind"]) else None
        if (
            ident not in view.delta_dels
            and (slot := view.slot_for_id(ident)) >= 0
            and not needs_body(ident, view.slot_kind_name(slot))
        ):
            return view.slot_tuple(slot)[19], slot
    if (
        indexed is not None
        and (entry := indexed.get(ident)) is not None
        and not needs_body(ident, entry["kind"])
    ):
        return entry["raw_sha256"], entry
    return None


def _verify_or_parse_scoped(
    store: Any,
    ident: str,
    raw: bytes,
    full_path: Path,
    candidate: tuple[bytes | str, dict[str, Any] | int],
    view: HeadIndexView | None,
) -> dict[str, Any]:
    expected, source = candidate
    if isinstance(expected, bytes):
        if (
            view is not None
            and isinstance(source, int)
            and hmac.compare_digest(hashlib.sha256(raw).digest(), expected)
        ):
            return view.slot_summary_fields(source)
    elif isinstance(source, dict) and hmac.compare_digest(sha(raw), expected):
        return {k: source[k] for k in _SUMMARY_KEYS}
    return _parse_and_verify(store, ident, raw, full_path)


def read_scoped_one(
    store: Any,
    ident: str,
    needs_body: Callable[[str, str], bool],
    indexed: dict[str, dict[str, Any]] | None,
    records_fd: int | None,
    view: HeadIndexView | None = None,
) -> dict[str, Any]:
    filename = f"{packet_id(ident)}.json"
    full_path = store.work / "records" / filename
    fd = records_fd if hasattr(os, "O_NOFOLLOW") else None
    candidate = _scoped_candidate(ident, needs_body, view, indexed)
    if candidate is not None:
        if fd is not None:
            raw, _ = _read_dir_fd_raw(filename, full_path, fd)
        else:
            _ScanOps(store, dir_str=str(full_path.parent)).stat(filename)
            raw = full_path.read_bytes()
        return _verify_or_parse_scoped(store, ident, raw, full_path, candidate, view)
    if fd is not None:
        raw, info = _read_dir_fd_raw(filename, full_path, fd)
        if info.st_size >= MIN_BYTES and not store.receipts._xattr_unsupported:
            proj = store.receipts.projection(full_path, ident, store.format, size=info.st_size)
            if (
                proj
                and not needs_body(ident, proj[0]["kind"])
                and hmac.compare_digest(sha(raw), proj[1])
            ):
                return proj[0]
        return _parse_and_verify(store, ident, raw, full_path)
    path = store.path(ident)
    proj = store.receipts.projection(path, ident, store.format)
    if proj and not needs_body(ident, proj[0]["kind"]) and store.receipts.matches(path, proj[1]):
        return proj[0]
    return store.show(ident)


def _scoped_chunk(
    ops: _ScanOps,
    needs_body: Callable[[str, str], bool],
    indexed: dict[str, dict[str, Any]] | None,
    view: HeadIndexView | None,
    chunk: list[str],
) -> list[dict[str, Any]]:
    if ops.records_fd is not None:
        return [
            read_scoped_one(ops.store, i, needs_body, indexed, ops.records_fd, view) for i in chunk
        ]
    out: list[dict[str, Any]] = []
    for ident in chunk:
        if (cand := _scoped_candidate(ident, needs_body, view, indexed)) is None:
            out.append(ops.read_record(ident, allow_cold=False))
            continue
        ops.stat(f"{ident}.json")
        raw, full_path = ops.read_raw(f"{ident}.json")
        out.append(_verify_or_parse_scoped(ops.store, ident, raw, full_path, cand, view))
    return out


def scoped_heads(
    store: Any, needs_body: Callable[[str, str], bool], *, max_workers: int = 16
) -> Iterator[dict[str, Any]]:
    """Yield cryptographically verified scope/status fields and full records for nominated roles."""
    if not (idents := store.ids()):
        return
    view = store.index.open_view(store.format)
    indexed = None if view is not None else store.index.load(store.format)
    try:
        with _open_scan_ops(store) as ops:
            results = chunk_map(
                idents,
                lambda c: _scoped_chunk(ops, needs_body, indexed, view, c),
                max_workers=min(max_workers, 8),
            )
        yield from results
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
        self.view, self.live_ids, self.overrides, self.removed = view, live_ids, overrides, removed
        self.is_exact_base = (not view.has_deltas) and (not overrides) and (not removed)

    def __enter__(self) -> VerifiedIndexView:
        return self

    def __exit__(self, *_args: object) -> None:
        self.view.close()

    def close(self) -> None:
        self.view.close()

    def all_entries(self) -> dict[str, dict]:
        if self.is_exact_base:
            base = self.view.all_entries()
            return {ident: base[ident] for ident in self.live_ids}
        out: dict[str, dict] = {}
        dels = self.view.delta_dels | self.removed
        puts, overrides = self.view.delta_puts, self.overrides
        for ident in self.live_ids:
            if (hit := overrides.get(ident)) is not None or (hit := puts.get(ident)) is not None:
                out[ident] = hit
            elif ident not in dels and (slot := self.view.slot_for_id(ident)) >= 0:
                out[ident] = self.view.decode_slot_entry(slot)
        return out


def _advance_slot_cursor(
    mm: mmap.mmap, slot_base: int, str_base: int, rec_count: int, cursor: int, target: bytes
) -> tuple[int, int]:
    """O(1) amortized merge-walk over 128B mmap slots; returns (slot_or_neg1, next_cursor)."""
    unpack_id = _bin._SLOT_ID_STRUCT.unpack_from
    while cursor < rec_count:
        id_off, id_len = unpack_id(mm, slot_base + cursor * _bin._SLOT_SIZE)
        cur = mm[str_base + id_off : str_base + id_off + id_len]
        if cur < target:
            cursor += 1
            continue
        return (cursor, cursor + 1) if cur == target else (-1, cursor)
    return -1, cursor


def _verify_view_chunk(
    view: HeadIndexView, ops: _ScanOps, chunk: list[str]
) -> list[tuple[str, dict[str, Any]]]:
    if not chunk:
        return []
    mm = view._check_open()
    slot_base, str_base, rec_count = view._slot_table_off, view._string_pool_off, view.record_count
    cursor = view.lower_bound(chunk[0])
    overrides: list[tuple[str, dict[str, Any]]] = []
    for ident in chunk:
        delta = view.delta_puts.get(ident)
        slot = -1
        if delta is None and ident not in view.delta_dels:
            slot, cursor = _advance_slot_cursor(
                mm, slot_base, str_base, rec_count, cursor, ident.encode("utf-8")
            )
        filename = f"{ident if (delta is not None or slot >= 0) else packet_id(ident)}.json"
        info = ops.stat(filename)
        if delta is not None:
            handled, rebuilt = ops.check_dict(ident, delta, filename, info)
        elif slot >= 0:
            handled, rebuilt = ops.check_slot(view, mm, slot, ident, filename, info)
        else:
            handled, rebuilt = False, None
        if not handled:
            overrides.append((ident, ops.fallback(ident, info)))
        elif rebuilt is not None:
            overrides.append((ident, rebuilt))
    return overrides


def open_verified_index_view(store: Any, *, max_workers: int = 16) -> VerifiedIndexView | None:
    """Open a binary v3 HeadIndexView and verify hot record freshness in zero-copy mmap."""
    if (view := store.index.open_view(store.format)) is None:
        return None
    try:
        raw_idents = store.ids()
        # Sort by UTF-8 bare ID for O(N) merge-walk while preserving raw_idents on live_ids.
        sorted_idents = sorted(raw_idents, key=lambda ident: ident.encode("utf-8"))
        overrides: dict[str, dict] = {}
        if sorted_idents:
            with _open_scan_ops(store) as ops:
                pairs = chunk_map(
                    sorted_idents,
                    lambda c: _verify_view_chunk(view, ops, c),
                    max_workers=min(max_workers, 8),
                )
            overrides = dict(pairs)
        removed: set[str] = set()
        if not (len(sorted_idents) == view.record_count and not view.has_deltas and not overrides):
            live_set = set(sorted_idents)
            removed = {ident for ident in view.iter_ids() if ident not in live_set}
        return VerifiedIndexView(view, live_ids=raw_idents, overrides=overrides, removed=removed)
    except BaseException:
        view.close()
        raise


def get_verified_entry(store: Any, ident: str) -> dict[str, Any] | None:
    """O(log N) single-record stat + HMAC verified lookup via binary v3 mmap."""
    ident = packet_id(ident)
    if (view := store.index.open_view(store.format)) is None:
        return None
    with view:
        path = store.path(ident)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            raise ValueError(f"ordinary JSON file required: {path}") from None
        if not is_ordinary_file_stat(info):
            raise ValueError(f"ordinary JSON file required: {path}")
        ops = _ScanOps(store, dir_str=str(path.parent))
        delta = view.delta_puts.get(ident)
        if delta is not None:
            handled, rebuilt = ops.check_dict(ident, delta, path.name, info)
            if handled:
                return dict(delta) if rebuilt is None else rebuilt
        elif ident not in view.delta_dels and (slot := view.slot_for_id(ident)) >= 0:
            handled, rebuilt = ops.check_slot(
                view, view._check_open(), slot, ident, path.name, info
            )
            if handled:
                return view.decode_slot_entry(slot) if rebuilt is None else rebuilt
        record, raw = store.read(ident)
        return build_entry(record, raw, info)


def _check_indexed_chunk(
    indexed: dict[str, dict[str, Any]], ops: _ScanOps, chunk: list[str]
) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for ident in chunk:
        entry = indexed.get(ident)
        filename = f"{ident if entry is not None else packet_id(ident)}.json"
        info = ops.stat(filename)
        if entry is not None:
            handled, rebuilt = ops.check_dict(ident, entry, filename, info)
            if handled:
                out.append((ident, entry if rebuilt is None else rebuilt))
                continue
        out.append((ident, ops.fallback(ident, info)))
    return out


def indexed_summaries_with_format(
    store: Any, *, max_workers: int = 16
) -> tuple[dict[str, dict[str, Any]], str] | None:
    """Return (verified_entries, index_format) or None if unindexed (works on POSIX and Windows)."""
    if (verified := open_verified_index_view(store, max_workers=max_workers)) is not None:
        with verified:
            return verified.all_entries(), HEAD_INDEX_FORMAT
    if (loaded := store.index.load_with_format(store.format)) is None:
        return None
    indexed, index_format = loaded
    if not (idents := store.ids()):
        return {}, index_format
    with _open_scan_ops(store) as ops:
        pairs = chunk_map(
            idents, lambda c: _check_indexed_chunk(indexed, ops, c), max_workers=min(max_workers, 8)
        )
    return dict(pairs), index_format


def scan_records(
    store: Any, idents: Iterable[str] | None = None, *, max_workers: int = 16
) -> list[dict[str, Any]]:
    """Read and cryptographically verify nominated records (or all records) in parallel."""
    if not (target_ids := store.ids() if idents is None else list(idents)):
        return []
    with store.cold_archive.pack_session(), _open_scan_ops(store) as ops:
        return chunk_map(
            target_ids,
            lambda c: [ops.read_record(i, allow_cold=True) for i in c],
            max_workers=min(max_workers, 8),
        )


def reindex(store: Any, *, max_workers: int = 16) -> dict[str, Any]:
    """Rebuild the HMAC-verified store-local head index under an exclusive owner lock."""
    entries: dict[str, dict[str, Any]] = {}
    if idents := store.ids():
        with _open_scan_ops(store) as ops:
            entries = dict(
                chunk_map(
                    idents,
                    lambda c: [(i, ops.fallback(i)) for i in c],
                    max_workers=min(max_workers, 8),
                )
            )
    return store.index.save(store.format, entries, create_key=True)


def refresh_index_entries(
    store: Any, idents: list[str], *, prebuilt: dict[str, dict[str, Any]] | None = None
) -> bool:
    """Batch-update existing HMAC head index entries for freshly committed records."""
    if (not idents and not prebuilt) or not store.index.exists():
        return False
    updates: dict[str, dict[str, Any]] = dict(prebuilt) if prebuilt else {}
    if idents:
        with _open_scan_ops(store) as ops:
            updates.update((ident, ops.fallback(ident)) for ident in idents)
    return store.index.update_if_present(store.format, updates)
