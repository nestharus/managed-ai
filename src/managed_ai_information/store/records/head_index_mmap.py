"""Zero-copy read-only mmap view, CSR graph queries, and generation helpers for HeadIndex v3."""

import contextlib
import hmac
import mmap
import struct
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO

from . import head_index_codec as _c
from . import head_index_decode as _d
from .head_index_decode import (
    _next_generation as _next_generation,
)
from .head_index_decode import (
    _prune_old_generations as _prune_old_generations,
)
from .head_index_decode import (
    active_binary_path as active_binary_path,
)
from .head_index_decode import (
    read_current_pointer as read_current_pointer,
)

_SLOT_ID_STRUCT = _c._SLOT_ID_STRUCT
_SLOT_SIZE = _c._SLOT_SIZE
_SLOT_STRUCT = _c._SLOT_STRUCT


class HeadIndexView:
    """Zero-copy read-only mmap view over an HMAC-verified HeadIndex v3 (+ delta overlay)."""

    format = _c.HEAD_INDEX_FORMAT_V3

    def __init__(
        self,
        path: Path,
        stream: BinaryIO,
        mm: mmap.mmap,
        header_fields: tuple[Any, ...],
        hmac_hex: str,
        generation: int,
        delta_puts: dict[str, dict[str, Any]] | None = None,
        delta_dels: set[str] | None = None,
    ) -> None:
        self.path = path
        self._stream: BinaryIO | None = stream
        self._mm: mmap.mmap | None = mm
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

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        mm = getattr(self, "_mm", None)
        stream = getattr(self, "_stream", None)
        self._mm = None
        self._stream = None

        if mm is not None:
            with contextlib.suppress(BufferError, ValueError, OSError):
                mm.close()

        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()

    def __del__(self) -> None:
        self.close()

    def _check_open(self) -> mmap.mmap:
        mm = self._mm
        if self._closed or mm is None:
            raise ValueError("HeadIndexView is closed")
        return mm

    def _str_bytes(self, off: int, length: int) -> bytes:
        mm = self._check_open()
        start = self._string_pool_off + off
        return mm[start : start + length]

    def _str(self, off: int, length: int) -> str:
        if length == 0:
            return ""
        return self._str_bytes(off, length).decode("utf-8")

    def kind_names(self) -> list[str]:
        if self._kind_cache is None:
            mm = self._check_open()
            names: list[str] = []
            base = self._kind_table_off

            for idx in range(self.kind_count):
                s_off, s_len, _, _ = _c._KIND_ENTRY_STRUCT.unpack_from(mm, base + idx * 16)
                names.append(self._str(s_off, s_len))
            self._kind_cache = names

        return self._kind_cache

    def kind_counts(self) -> dict[str, tuple[int, int]]:
        """Return {kind_name: (total_count, active_count)} in O(K) from the binary header table."""
        mm = self._check_open()
        out: dict[str, tuple[int, int]] = {}
        base = self._kind_table_off

        for idx in range(self.kind_count):
            s_off, s_len, total_c, active_c = _c._KIND_ENTRY_STRUCT.unpack_from(mm, base + idx * 16)
            out[self._str(s_off, s_len)] = (total_c, active_c)

        return out

    def active_count_base(self) -> int:
        """Count active records in base generation via C-speed columnar bytemap scan."""
        mm = self._check_open()
        start = self._active_map_off
        return mm[start : start + self.record_count].count(1)

    def rel_labels(self) -> list[str]:
        if self._rel_cache is None:
            mm = self._check_open()
            labels: list[str] = []
            base = self._rel_table_off

            for idx in range(self.rel_label_count):
                s_off, s_len = _c._REL_ENTRY_STRUCT.unpack_from(mm, base + idx * 8)
                labels.append(self._str(s_off, s_len))
            self._rel_cache = labels

        return self._rel_cache

    def slot_id_bytes(self, slot: int) -> bytes:
        mm = self._check_open()
        id_off, id_len = _SLOT_ID_STRUCT.unpack_from(mm, self._slot_table_off + slot * _SLOT_SIZE)
        start = self._string_pool_off + id_off
        return mm[start : start + id_len]

    def slot_id(self, slot: int) -> str:
        return self.slot_id_bytes(slot).decode("utf-8")

    def lower_bound(self, ident: str) -> int:
        """O(log N) lower bound in sorted 128B slots: first slot with ID >= ident."""
        target = ident.encode("utf-8")
        lo = 0
        hi = self.record_count
        mm = self._check_open()
        slot_base = self._slot_table_off
        str_base = self._string_pool_off

        while lo < hi:
            mid = (lo + hi) >> 1
            id_off, id_len = _SLOT_ID_STRUCT.unpack_from(mm, slot_base + mid * _SLOT_SIZE)
            start = str_base + id_off
            if mm[start : start + id_len] < target:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def slot_for_id(self, ident: str) -> int:
        """O(log N) binary search over sorted 128B slots directly in mmap (-1 if absent)."""
        target = ident.encode("utf-8")
        lo = 0
        hi = self.record_count
        mm = self._check_open()
        slot_base = self._slot_table_off
        str_base = self._string_pool_off

        while lo < hi:
            mid = (lo + hi) >> 1
            id_off, id_len = _SLOT_ID_STRUCT.unpack_from(mm, slot_base + mid * _SLOT_SIZE)
            start = str_base + id_off
            cur = mm[start : start + id_len]
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
        """Unpack (size, mtime_ns, ino, slot_flags) without decoding the full 128B slot."""
        mm = self._check_open()
        base = self._slot_table_off + slot * _SLOT_SIZE
        size_val, mtime_ns, ino_val = _c._SLOT_STAT_STRUCT.unpack_from(
            mm, base + _c._SLOT_STAT_OFFSET
        )
        slot_flags = _c._U16_STRUCT.unpack_from(mm, base + _c._SLOT_FLAGS_OFFSET)[0]
        return size_val, mtime_ns, ino_val, slot_flags

    def slot_platform(self, slot: int, slot_flags: int) -> str:
        """Decode platform key ('posix', 'nt', or custom aux string) from slot_flags."""
        plat_code = (slot_flags & _c._PLATFORM_MASK) >> _c._PLATFORM_SHIFT
        if plat_code == _c._PLATFORM_POSIX:
            return "posix"
        if plat_code == _c._PLATFORM_NT:
            return "nt"
        return _d.decode_aux(self, slot, slot_flags, only_platform=True)["platform"]

    def slot_raw_sha_bin(self, slot: int) -> bytes:
        """Read 32-byte raw_sha256 digest slice directly from slot (slow path only)."""
        mm = self._check_open()
        start = self._slot_table_off + slot * _SLOT_SIZE + _c._SLOT_RAW_SHA_OFFSET
        return mm[start : start + 32]

    def slot_stat_meta(self, slot: int) -> tuple[int, int, int, str, bytes]:
        """Return (size, mtime_ns, ino, platform, raw_sha256_bin) without full decode."""
        size_val, mtime_ns, ino_val, slot_flags = self.slot_stat_fast(slot)
        plat = self.slot_platform(slot, slot_flags)
        return size_val, mtime_ns, ino_val, plat, self.slot_raw_sha_bin(slot)

    def slot_pin(self, slot: int) -> dict:
        """Return compact graph node pin {id, kind, revision, sha256, active} directly from slot."""
        tup = self.slot_tuple(slot)
        kinds = self.kind_names()
        return {
            "id": self._str(tup[0], tup[1]),
            "kind": kinds[tup[7]],
            "revision": tup[9],
            "sha256": tup[18].hex(),
            "active": bool(tup[8] & _c.FLAG_ACTIVE),
        }

    def slot_kind_name(self, slot: int) -> str:
        mm = self._check_open()
        kid = _c._U16_STRUCT.unpack_from(mm, self._kind_map_off + slot * 2)[0]
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
            slot
            for slot in range(self.record_count)
            if mm[base + slot * 2] == lo_byte and mm[base + slot * 2 + 1] == hi_byte
        ]

    def slot_active(self, slot: int) -> bool:
        mm = self._check_open()
        return mm[self._active_map_off + slot] != 0

    def slot_map_flags(self, slot: int) -> int:
        mm = self._check_open()
        return mm[self._flags_map_off + slot]

    def slot_summary_fields(self, slot: int) -> dict[str, Any]:
        """Return lightweight status/scope summary fields from slot + basis only."""
        tup = self.slot_tuple(slot)
        slot_flags = tup[8]
        kinds = self.kind_names()
        basis = self.slot_basis(slot, slot_flags) if (slot_flags & _c.FLAG_HAS_BASIS) else []
        return {
            "format": self.record_format,
            "id": self._str(tup[0], tup[1]),
            "kind": kinds[tup[7]],
            "sha256": tup[18].hex(),
            "revision": tup[9],
            "basis": basis,
            "active": bool(slot_flags & _c.FLAG_ACTIVE),
        }

    def slot_basis(self, slot: int, slot_flags: int | None = None) -> list[dict]:
        if slot_flags is None:
            slot_flags = self.slot_tuple(slot)[8]
        if not (slot_flags & _c.FLAG_HAS_BASIS):
            return []

        mm = self._check_open()
        a_start, a_end = _c._PAIR_U32_STRUCT.unpack_from(mm, self._aux_offsets_off + slot * 4)
        if a_start >= a_end:
            return []

        basis, _ = _d.read_aux_basis(self, mm, self._aux_pool_off + a_start, skip=False)
        return basis

    def slot_graph_aux(self, slot: int, slot_flags: int | None = None) -> dict[str, Any]:
        """Decode graph-relevant aux fields (content_links, duplicate_relations, question_norm)."""
        if slot_flags is None:
            slot_flags = self.slot_tuple(slot)[8]
        return _d.decode_aux(self, slot, slot_flags, only_platform=False)

    def fwd_edges_raw(self, slot: int) -> list[tuple[int, int, int, int]]:
        """Return forward CSR tuples (rel_id, target_slot, str_off, str_len) in order."""
        mm = self._check_open()
        e_start, e_end = _c._PAIR_U32_STRUCT.unpack_from(mm, self._fwd_csr_offsets_off + slot * 4)
        if e_start >= e_end:
            return []

        base = self._fwd_csr_edges_off
        return [
            _c._FWD_EDGE_STRUCT.unpack_from(mm, base + idx * 16) for idx in range(e_start, e_end)
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
        e_start, e_end = _c._PAIR_U32_STRUCT.unpack_from(mm, self._rev_csr_offsets_off + slot * 4)
        if e_start >= e_end:
            return []

        base = self._rev_csr_edges_off
        return [
            _c._REV_EDGE_STRUCT.unpack_from(mm, base + idx * 8) for idx in range(e_start, e_end)
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
        return _d.ext_rev_edges(self, target)

    def open_question_slots(self) -> list[int]:
        """Fast scan over the columnar flags_map to collect open-question slot indices."""
        mm = self._check_open()
        f_off = self._flags_map_off
        return [
            slot
            for slot in range(self.record_count)
            if mm[f_off + slot] & _c.MAP_FLAG_OPEN_QUESTION
        ]

    def decode_slot_entry(self, slot: int) -> dict:
        """Materialize one full HeadIndex v2-compatible entry dict for slot."""
        return _d.decode_slot_entry(self, slot)

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
            raw_sha = self.delta_puts[ident].get("raw_sha256")
            return raw_sha if isinstance(raw_sha, str) else None
        if ident in self.delta_dels:
            return None
        slot = self.slot_for_id(ident)
        if slot < 0:
            return None
        return self.slot_raw_sha_bin(slot).hex()

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


BinaryHeadIndexView = HeadIndexView


def open_binary_view(
    directory: Path,
    key: bytes,
    record_format: str,
) -> HeadIndexView | None:
    """Open, zero-copy HMAC-verify, and return HeadIndexView, or None if missing/tampered."""
    pointer = read_current_pointer(directory)
    if pointer is None or pointer.get("record_format") != record_format:
        return None

    bin_path = directory / pointer["file"]
    opened = _d._open_readonly_ordinary(bin_path)
    if opened is None:
        return None

    stream, file_size = opened
    if file_size < _c._HEADER_SIZE + 32:
        stream.close()
        return None

    mm: mmap.mmap | None = None
    try:
        mm = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
        fields = _c._HEADER_STRUCT.unpack_from(mm, 0)
        if not _c._validate_header_bounds(fields, file_size):
            mm.close()
            stream.close()
            return None

        hmac_off = fields[-1]
        mv = memoryview(mm)
        try:
            mac = hmac.new(key, _c.BINARY_INDEX_DOMAIN, "sha256")
            mac.update(mv[:hmac_off])
            computed = mac.digest()
            footer = bytes(mv[hmac_off : hmac_off + 32])
        finally:
            mv.release()

        if not hmac.compare_digest(computed, footer):
            mm.close()
            stream.close()
            return None

        hmac_hex = footer.hex()
        if not hmac.compare_digest(hmac_hex, pointer["hmac"]):
            mm.close()
            stream.close()
            return None

        view = HeadIndexView(bin_path, stream, mm, fields, hmac_hex, pointer["generation"])
        if view.record_format != record_format:
            view.close()
            return None
        return view
    except OSError, ValueError, struct.error, UnicodeDecodeError:
        if mm is not None:
            with contextlib.suppress(BufferError, ValueError, OSError):
                mm.close()
        with contextlib.suppress(OSError):
            stream.close()
        return None
