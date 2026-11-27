"""Pointer, generation lifecycle, and auxiliary/entry decoders for HeadIndex v3."""

import contextlib
import mmap
import os
import stat
import struct
from pathlib import Path
from typing import Any, BinaryIO

from managed_ai_information.core.storage import (
    OPEN_RO_NOFOLLOW,
    has_ordinary_file,
    is_ordinary_file_stat,
    sync_dir,
)
from managed_ai_information.platform import strict_json_loads

from . import head_index_codec as _c


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
    except OSError, UnicodeDecodeError, ValueError:
        return None

    if doc.get("format") != _c.HEAD_INDEX_FORMAT_V3:
        return None

    gen = doc.get("generation")
    if type(gen) is not int or gen < 1:
        return None

    expected_name = f"head-index.gen{gen}.bin"
    if doc.get("file") != expected_name:
        return None

    tag = doc.get("hmac")
    if not isinstance(tag, str) or not _c._HEX64_RE.fullmatch(tag):
        return None

    if not isinstance(doc.get("record_format"), str) or not doc["record_format"]:
        return None

    return doc


def read_current_pointer(directory: Path) -> dict | None:
    """Read and validate the tiny atomic head-index.current.json pointer if present."""
    return _parse_pointer(directory / "head-index.current.json")


def active_binary_path(directory: Path) -> Path | None:
    """Return the active immutable head-index.gen<N>.bin path, or None if absent/invalid."""
    doc = read_current_pointer(directory)
    if doc is None:
        return None

    candidate = directory / doc["file"]
    return candidate if has_ordinary_file(candidate) else None


def _next_generation(directory: Path) -> int:
    highest = 0
    doc = read_current_pointer(directory)
    if doc is not None:
        highest = max(highest, doc["generation"])

    if not directory.is_dir():
        return highest + 1

    try:
        with os.scandir(directory) as items:
            names = [item.name for item in items]
    except OSError:
        return highest + 1

    for name in names:
        match = _c._GEN_FILE_RE.fullmatch(name)
        if match:
            highest = max(highest, int(match.group(1)))

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
        if name == keep_name or not _c._GEN_FILE_RE.fullmatch(name):
            continue
        candidate = directory / name

        try:
            info = candidate.lstat()
            if is_ordinary_file_stat(info):
                candidate.unlink()
                removed_any = True
        except OSError:
            continue

    if removed_any:
        with contextlib.suppress(OSError):
            sync_dir(directory)


def _open_readonly_ordinary(path: Path) -> tuple[BinaryIO, int] | None:
    if hasattr(os, "O_NOFOLLOW"):
        try:
            fd = os.open(path, OPEN_RO_NOFOLLOW)
        except OSError:
            return None

        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                os.close(fd)
                return None
            return os.fdopen(fd, "rb"), info.st_size
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
        return path.open("rb"), info.st_size
    except OSError:
        return None


def read_aux_basis(view: Any, mm: mmap.mmap, pos: int, *, skip: bool) -> tuple[list[dict], int]:
    count = _c._U32_STRUCT.unpack_from(mm, pos)[0]
    pos += 4
    basis: list[dict] = []

    for _ in range(count):
        p_off, p_len, has_sha = _c._BASIS_HEAD_STRUCT.unpack_from(mm, pos)
        pos += _c._BASIS_HEAD_STRUCT.size
        if has_sha:
            sha_hex: str | None = None if skip else mm[pos : pos + 32].hex()
            pos += 32
        else:
            sha_hex = None
        b_len = _c._U64_STRUCT.unpack_from(mm, pos)[0]
        pos += 8
        if not skip:
            basis.append({"path": view._str(p_off, p_len), "sha256": sha_hex, "bytes": b_len})

    return basis, pos


def read_aux_content_links(
    view: Any, mm: mmap.mmap, pos: int, *, skip: bool
) -> tuple[dict[str, list[str]], int]:
    field_count = _c._U32_STRUCT.unpack_from(mm, pos)[0]
    pos += 4
    clinks: dict[str, list[str]] = {}

    for _ in range(field_count):
        k_off, k_len = _c._PAIR_U32_STRUCT.unpack_from(mm, pos)
        pos += 8
        v_count = _c._U32_STRUCT.unpack_from(mm, pos)[0]
        pos += 4
        if skip:
            pos += v_count * 8
        else:
            vals: list[str] = []
            for _ in range(v_count):
                v_off, v_len = _c._PAIR_U32_STRUCT.unpack_from(mm, pos)
                pos += 8
                vals.append(view._str(v_off, v_len))
            clinks[view._str(k_off, k_len)] = vals

    return clinks, pos


def read_aux_dup_rels(
    view: Any, mm: mmap.mmap, pos: int, *, skip: bool
) -> tuple[list[list[str]] | None, int]:
    dup_count = _c._U32_STRUCT.unpack_from(mm, pos)[0]
    pos += 4
    if skip:
        return None, pos + dup_count * 12

    labels = view.rel_labels()
    dups: list[list[str]] = []
    for _ in range(dup_count):
        rid, t_off, t_len = struct.unpack_from("<III", mm, pos)
        pos += 12
        dups.append([labels[rid], view._str(t_off, t_len)])

    return dups, pos


def decode_aux(view: Any, slot: int, slot_flags: int, *, only_platform: bool = False) -> dict:
    mm = view._check_open()
    a_start, a_end = _c._PAIR_U32_STRUCT.unpack_from(mm, view._aux_offsets_off + slot * 4)
    pos = view._aux_pool_off + a_start
    end_pos = view._aux_pool_off + a_end
    result: dict = {
        "basis": [],
        "content_links": {},
        "duplicate_relations": None,
        "question_norm": None,
        "content_format": None,
        "platform": None,
    }
    if pos >= end_pos:
        return result

    if slot_flags & _c.FLAG_HAS_BASIS:
        result["basis"], pos = read_aux_basis(view, mm, pos, skip=only_platform)

    if slot_flags & _c.FLAG_HAS_CONTENT_LINKS:
        result["content_links"], pos = read_aux_content_links(view, mm, pos, skip=only_platform)

    if slot_flags & _c.FLAG_HAS_DUP_RELS:
        dups, pos = read_aux_dup_rels(view, mm, pos, skip=only_platform)
        if not only_platform:
            result["duplicate_relations"] = dups

    if slot_flags & _c.FLAG_HAS_QUESTION_NORM:
        q_off, q_len = _c._PAIR_U32_STRUCT.unpack_from(mm, pos)
        pos += 8
        if not only_platform:
            result["question_norm"] = view._str(q_off, q_len)

    if slot_flags & _c.FLAG_HAS_CONTENT_FORMAT:
        c_off, c_len = _c._PAIR_U32_STRUCT.unpack_from(mm, pos)
        pos += 8
        if not only_platform:
            result["content_format"] = view._str(c_off, c_len)

    plat_code = (slot_flags & _c._PLATFORM_MASK) >> _c._PLATFORM_SHIFT
    if plat_code == _c._PLATFORM_CUSTOM and pos + 8 <= end_pos:
        pl_off, pl_len = _c._PAIR_U32_STRUCT.unpack_from(mm, pos)
        result["platform"] = view._str(pl_off, pl_len)

    return result


def decode_search(view: Any, slot: int) -> tuple[list[str], list[str]]:
    mm = view._check_open()
    s_start, s_end = _c._PAIR_U32_STRUCT.unpack_from(mm, view._search_offsets_off + slot * 4)
    if s_start >= s_end:
        return [], []

    pos = view._search_pool_off + s_start
    ref_count, tok_count = _c._PAIR_U32_STRUCT.unpack_from(mm, pos)
    pos += 8

    refs: list[str] = []
    for _ in range(ref_count):
        r_off, r_len = _c._PAIR_U32_STRUCT.unpack_from(mm, pos)
        pos += 8
        refs.append(view._str(r_off, r_len))

    toks: list[str] = []
    for _ in range(tok_count):
        t_off, t_len = _c._PAIR_U32_STRUCT.unpack_from(mm, pos)
        pos += 8
        toks.append(view._str(t_off, t_len))

    return refs, toks


def ext_rev_edges(view: Any, target: str) -> list[tuple[str, int]]:
    count = view.ext_rev_count
    if count == 0:
        return []

    mm = view._check_open()
    target_bytes = target.encode("utf-8")
    base = view._ext_rev_edges_off
    str_base = view._string_pool_off

    lo = 0
    hi = count
    while lo < hi:
        mid = (lo + hi) >> 1
        t_off, t_len, _, _ = _c._EXT_REV_EDGE_STRUCT.unpack_from(mm, base + mid * 16)
        if mm[str_base + t_off : str_base + t_off + t_len] < target_bytes:
            lo = mid + 1
        else:
            hi = mid

    labels = view.rel_labels()
    out: list[tuple[str, int]] = []
    idx = lo
    while idx < count:
        t_off, t_len, rid, src_slot = _c._EXT_REV_EDGE_STRUCT.unpack_from(mm, base + idx * 16)
        if mm[str_base + t_off : str_base + t_off + t_len] != target_bytes:
            break
        out.append((labels[rid], src_slot))
        idx += 1

    return out


def decode_slot_entry(view: Any, slot: int) -> dict:
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
    ) = view.slot_tuple(slot)

    ident = view._str(id_off, id_len)
    aux = decode_aux(view, slot, slot_flags)
    exact_refs, tokens = decode_search(view, slot)
    relations = [[label, target] for label, target, _ in view.fwd_edges(slot)]

    plat_code = (slot_flags & _c._PLATFORM_MASK) >> _c._PLATFORM_SHIFT
    if plat_code == _c._PLATFORM_POSIX:
        plat = "posix"
    elif plat_code == _c._PLATFORM_NT:
        plat = "nt"
    else:
        plat = aux["platform"] or ""

    graph: dict = {
        "relations": relations,
        "content_links": aux["content_links"],
        "title": view._str(title_off, title_len),
        "selection_state": view._str(state_off, state_len),
    }
    if aux["duplicate_relations"]:
        graph["duplicate_relations"] = aux["duplicate_relations"]
    if aux["question_norm"] is not None:
        graph["question_norm"] = aux["question_norm"]
    if aux["content_format"] is not None:
        graph["content_format"] = aux["content_format"]

    entry: dict = {
        "format": view.record_format,
        "id": ident,
        "kind": view.kind_names()[kid],
        "revision": revision,
        "sha256": sha_bin.hex(),
        "active": bool(slot_flags & _c.FLAG_ACTIVE),
        "basis": aux["basis"],
        "raw_sha256": raw_sha_bin.hex(),
        "size": size_val,
        "mtime_ns": mtime_ns,
        "ino": ino_val,
        "platform": plat,
        "exact_refs": exact_refs,
        "tokens": tokens,
        "graph": graph,
    }
    if slot_flags & _c.FLAG_TOKENS_TRUNCATED:
        entry["tokens_truncated"] = True
    if slot_flags & _c.FLAG_EXACT_REFS_TRUNCATED:
        entry["exact_refs_truncated"] = True
    return entry
