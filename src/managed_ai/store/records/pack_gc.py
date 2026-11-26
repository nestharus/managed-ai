"""Immutable shard writer, logical .occ tombstone GC, and physical generation repacking."""

import os
import re
import struct
import zlib
from contextlib import suppress
from pathlib import Path
from typing import Any

from managed_ai.core.storage import (
    FRAME_HEADER_STRUCT,
    IDX_ENTRIES_OFFSET,
    IDX_ENTRY_STRUCT,
    IDX_HEADER_STRUCT,
    OCC_HEADER_STRUCT,
    OCC_LIVE_DEFLATE,
    OCC_LIVE_RAW,
    OCC_LIVE_ZSTD,
    OCC_SEALED,
    OCC_TOMBSTONE,
    PACK_CURRENT_FORMAT,
    PACK_FRAME_MAGIC,
    PACK_IDX_MAGIC,
    PACK_OCC_MAGIC,
    PACK_SLAB_MAGIC,
    SLAB_HEADER_STRUCT,
    PackShardHandle,
    atomic_bytes,
    encode,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    parse_pack_current,
    sha,
    sync_dir,
)
from managed_ai.store.protocol.identities import require

DEFAULT_REPACK_DEAD_RATIO = 0.30
_PACK_FILE_RE = re.compile(r"^pack-[0-9]{6}-[0-9]{4}\.(slab|idx|occ[1-9][0-9]*)$")
_VALID_OCC_BYTES = frozenset(
    (OCC_TOMBSTONE, OCC_LIVE_RAW, OCC_LIVE_DEFLATE, OCC_SEALED, OCC_LIVE_ZSTD)
)
_VALID_OCC_RAW_BYTES = b"\x00\x01\x02\x03\x04"


def _prune_unreferenced_pack_files(packs_dir: Path, active_shards: list[dict[str, Any]]) -> None:
    """Opportunistically unlink obsolete pack generations (ignoring Windows active-mmap locks)."""
    try:
        if not is_ordinary_dir_stat(packs_dir.lstat()):
            return
    except OSError:
        return

    keep: set[str] = {"CURRENT.json"}
    for shard in active_shards:
        keep.add(shard["slab"])
        keep.add(shard["idx"])
        keep.add(shard["occ"])

    try:
        with os.scandir(packs_dir) as items:
            names = [item.name for item in items]
    except OSError:
        return

    removed_any = False
    for name in names:
        if name in keep or not _PACK_FILE_RE.fullmatch(name):
            continue
        candidate = packs_dir / name
        try:
            info = candidate.lstat()
            if is_ordinary_file_stat(info):
                candidate.unlink()
                removed_any = True
        except OSError:
            # Expected on Windows when a concurrent reader holds an mmap on a prior generation.
            continue

    if removed_any:
        with suppress(OSError):
            sync_dir(packs_dir)


def write_pack_current(
    packs_dir: Path,
    generation: int,
    occ_generation: int,
    shards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Atomically flip packs/CURRENT.json and opportunistically prune obsolete generations."""
    packs_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "format": PACK_CURRENT_FORMAT,
        "generation": generation,
        "occ_generation": occ_generation,
        "shards": shards,
    }
    atomic_bytes(packs_dir / "CURRENT.json", encode(doc))
    sync_dir(packs_dir)
    _prune_unreferenced_pack_files(packs_dir, shards)
    return doc


def _build_shard_buffers(
    generation: int,
    shard_id: int,
    items: list[tuple[bytes, int, int, bytes]],
) -> tuple[bytes, bytes, bytes, list[tuple[bytes, int, int, int, int]], int]:
    """Assemble aligned .slab, 256-fanout .idx, and .occ byte images for one immutable shard."""
    slab_buf = bytearray(
        SLAB_HEADER_STRUCT.pack(PACK_SLAB_MAGIC, 1, 0, shard_id, len(items), generation)
    )
    index_rows: list[tuple[bytes, int, int, int, int]] = []
    live_bytes = 0

    for key_bytes, raw_len, codec, stored_bytes in items:
        if len(key_bytes) != 32:
            raise ValueError("pack shard key must be 32 raw bytes")
        if codec not in (OCC_LIVE_RAW, OCC_LIVE_DEFLATE, OCC_LIVE_ZSTD, OCC_SEALED):
            raise ValueError(f"unsupported pack frame codec: {codec}")

        frame_offset = len(slab_buf)
        crc = zlib.crc32(stored_bytes) & 0xFFFFFFFF
        slab_buf.extend(
            FRAME_HEADER_STRUCT.pack(
                codec,
                0,
                PACK_FRAME_MAGIC,
                len(stored_bytes),
                raw_len,
                key_bytes,
                crc,
            )
        )
        slab_buf.extend(stored_bytes)
        pad = (-len(stored_bytes)) & 15
        if pad:
            slab_buf.extend(b"\x00" * pad)

        index_rows.append((key_bytes, frame_offset, len(stored_bytes), raw_len, codec))
        live_bytes += len(stored_bytes) + FRAME_HEADER_STRUCT.size

    index_rows.sort(key=lambda row: row[0])
    for pos in range(1, len(index_rows)):
        if index_rows[pos][0] == index_rows[pos - 1][0]:
            raise ValueError(f"duplicate object key in pack shard: {index_rows[pos][0].hex()}")

    fanout = [0] * 256
    for key_bytes, _, _, _, _ in index_rows:
        fanout[key_bytes[0]] += 1

    running = 0
    for byte_val in range(256):
        running += fanout[byte_val]
        fanout[byte_val] = running

    idx_buf = bytearray(
        IDX_HEADER_STRUCT.pack(PACK_IDX_MAGIC, 1, 0, shard_id, len(index_rows), generation)
    )
    idx_buf.extend(struct.pack("<256I", *fanout))
    occ_buf = bytearray(OCC_HEADER_STRUCT.pack(PACK_OCC_MAGIC, 1, 0, len(index_rows)))

    for key_bytes, frame_offset, stored_len, raw_len, codec in index_rows:
        idx_buf.extend(IDX_ENTRY_STRUCT.pack(key_bytes, frame_offset, stored_len, raw_len))
        occ_buf.append(codec)

    return bytes(slab_buf), bytes(idx_buf), bytes(occ_buf), index_rows, live_bytes


def _verify_written_shard(
    packs_dir: Path,
    descriptor: dict[str, Any],
    index_rows: list[tuple[bytes, int, int, int, int]],
) -> None:
    """Verify 100% lookup + SHA-256 readback of non-sealed frames before flipping CURRENT.json."""
    handle = PackShardHandle(packs_dir, descriptor)
    try:
        for key_bytes, frame_offset, stored_len, raw_len, codec in index_rows:
            hit = handle.lookup(key_bytes)
            if hit is None:
                raise ValueError(f"written pack shard lookup failed: {key_bytes.hex()}")
            _slot, h_occ, h_off, h_stored, h_raw = hit
            require(
                h_occ == codec
                and h_off == frame_offset
                and h_stored == stored_len
                and h_raw == raw_len,
                f"written pack shard index mismatch: {key_bytes.hex()}",
            )
            if codec != OCC_SEALED:
                handle.read_frame(
                    key_bytes.hex(),
                    key_bytes,
                    h_occ,
                    h_off,
                    h_stored,
                    h_raw,
                    retained=True,
                    expected_bytes=raw_len,
                )
    finally:
        handle.close()


def write_shard(
    packs_dir: Path,
    generation: int,
    shard_id: int,
    items: list[tuple[bytes, int, int, bytes]],
    *,
    occ_generation: int = 1,
) -> dict[str, Any]:
    """Write one immutable (.slab, .idx, .occ) shard triplet and verify 100% SHA-256 readback."""
    require(len(items) > 0, "cannot write an empty pack shard")
    packs_dir.mkdir(parents=True, exist_ok=True)

    slab_name = f"pack-{generation:06d}-{shard_id:04d}.slab"
    idx_name = f"pack-{generation:06d}-{shard_id:04d}.idx"
    occ_name = f"pack-{generation:06d}-{shard_id:04d}.occ{occ_generation}"

    slab_bytes, idx_bytes, occ_bytes, index_rows, live_bytes = _build_shard_buffers(
        generation, shard_id, items
    )

    atomic_bytes(packs_dir / slab_name, slab_bytes)
    atomic_bytes(packs_dir / idx_name, idx_bytes)
    atomic_bytes(packs_dir / occ_name, occ_bytes)

    descriptor = {
        "shard": shard_id,
        "slab": slab_name,
        "idx": idx_name,
        "occ": occ_name,
        "object_count": len(index_rows),
        "live_count": len(index_rows),
        "slab_bytes": len(slab_bytes),
        "live_bytes": live_bytes,
        "dead_bytes": 0,
        "slab_sha256": sha(slab_bytes),
        "idx_sha256": sha(idx_bytes),
        "occ_sha256": sha(occ_bytes),
    }
    _verify_written_shard(packs_dir, descriptor, index_rows)
    return descriptor


def _find_shard_tombstone_hits(
    handle: PackShardHandle,
    key_bytes_map: dict[bytes, str],
) -> dict[int, tuple[str, int]]:
    hit_slots: dict[int, tuple[str, int]] = {}
    for k_bytes, k_hex in key_bytes_map.items():
        found = handle.lookup(k_bytes)
        if found is None:
            continue
        slot_idx, occ_val, _slab_off, stored_len, _raw_len = found
        if occ_val != OCC_SEALED:
            hit_slots[slot_idx] = (k_hex, stored_len + FRAME_HEADER_STRUCT.size)
    return hit_slots


def _scan_updated_occupancy(
    handle: PackShardHandle,
    occ_bytes: bytearray,
) -> tuple[int, int, int, list[tuple[bytes, int, int, int, int]]]:
    idx_mm = handle._idx_mm
    if idx_mm is None:
        raise RuntimeError("PackShardHandle idx is not open")
    live_count = 0
    live_bytes = 0
    dead_bytes = 0
    live_slot_meta: list[tuple[bytes, int, int, int, int]] = []

    for slot in range(handle.object_count):
        sha_bin, slab_off, stored_len, raw_len = IDX_ENTRY_STRUCT.unpack_from(
            idx_mm,
            IDX_ENTRIES_OFFSET + slot * IDX_ENTRY_STRUCT.size,
        )
        entry_disk = stored_len + FRAME_HEADER_STRUCT.size
        occ_val = occ_bytes[slot]
        if occ_val not in _VALID_OCC_BYTES:
            raise ValueError(
                f"corrupt pack occ state byte ({occ_val}) during GC: {handle.occ_path}"
            )
        if occ_val == OCC_TOMBSTONE:
            dead_bytes += entry_disk
        else:
            live_count += 1
            live_bytes += entry_disk
            live_slot_meta.append((sha_bin, slab_off, stored_len, raw_len, occ_val))

    return live_count, live_bytes, dead_bytes, live_slot_meta


def _extract_verified_live_frames(
    handle: PackShardHandle,
    live_slot_meta: list[tuple[bytes, int, int, int, int]],
) -> list[tuple[bytes, int, int, bytes]]:
    slab_mm = handle._slab_mm
    if slab_mm is None:
        raise RuntimeError("PackShardHandle slab is not open")
    live_items: list[tuple[bytes, int, int, bytes]] = []

    for sha_bin, slab_off, stored_len, raw_len, occ_val in live_slot_meta:
        frame_end = slab_off + FRAME_HEADER_STRUCT.size + stored_len
        require(
            slab_off >= SLAB_HEADER_STRUCT.size
            and stored_len >= 0
            and frame_end <= handle._slab_size,
            f"corrupt pack frame bounds during physical shard repack: {sha_bin.hex()}",
        )
        codec, _flags, magic2, hdr_stored, hdr_raw, hdr_sha, hdr_crc = (
            FRAME_HEADER_STRUCT.unpack_from(
                slab_mm,
                slab_off,
            )
        )
        require(
            magic2 == PACK_FRAME_MAGIC
            and codec == occ_val
            and hdr_stored == stored_len
            and hdr_raw == raw_len
            and hdr_sha == sha_bin,
            f"corrupt frame during physical shard repack: {sha_bin.hex()}",
        )
        p_start = slab_off + FRAME_HEADER_STRUCT.size
        payload = slab_mm[p_start : p_start + stored_len]
        require(
            (zlib.crc32(payload) & 0xFFFFFFFF) == hdr_crc,
            f"frame crc32 mismatch during physical shard repack: {sha_bin.hex()}",
        )
        live_items.append((sha_bin, raw_len, occ_val, payload))

    return live_items


def _gc_one_shard(
    packs_dir: Path,
    shard_desc: dict[str, Any],
    key_bytes_map: dict[bytes, str],
    *,
    next_gen: int,
    next_occ_gen: int,
    repack_threshold: float,
) -> dict[str, Any]:
    """Apply logical tombstone or physical repack GC to one shard handle."""
    handle = PackShardHandle(packs_dir, shard_desc)
    try:
        hit_slots = _find_shard_tombstone_hits(handle, key_bytes_map)
        if not hit_slots:
            return {"mutated": False, "descriptor": dict(shard_desc)}

        occ_mm = handle._occ_mm
        if occ_mm is None:
            raise RuntimeError("PackShardHandle occ is not open")
        raw_occ_image = occ_mm[:]
        require(
            sha(raw_occ_image) == shard_desc["occ_sha256"],
            f"pack occ sha256 mismatch before GC: {handle.occ_path}",
        )
        occ_bytes = bytearray(
            raw_occ_image[OCC_HEADER_STRUCT.size : OCC_HEADER_STRUCT.size + handle.object_count]
        )
        if bytes(occ_bytes).translate(None, _VALID_OCC_RAW_BYTES):
            raise ValueError(f"corrupt pack occ state byte before GC: {handle.occ_path}")
        tombstoned: list[str] = []
        logical_reclaimed = 0

        for slot_idx, (k_hex, frame_disk_bytes) in hit_slots.items():
            occ_bytes[slot_idx] = OCC_TOMBSTONE
            tombstoned.append(k_hex)
            logical_reclaimed += frame_disk_bytes

        live_count, live_bytes, dead_bytes, live_slot_meta = _scan_updated_occupancy(
            handle, occ_bytes
        )
        total_payload_bytes = live_bytes + dead_bytes
        dead_ratio = (
            1.0
            if live_count == 0
            else (dead_bytes / total_payload_bytes if total_payload_bytes else 0.0)
        )

        if dead_ratio <= repack_threshold:
            stem = shard_desc["slab"][:-5]
            new_occ_name = f"{stem}.occ{next_occ_gen}"
            occ_image = OCC_HEADER_STRUCT.pack(PACK_OCC_MAGIC, 1, 0, handle.object_count) + bytes(
                occ_bytes
            )
            atomic_bytes(packs_dir / new_occ_name, occ_image)
            updated = {
                **shard_desc,
                "occ": new_occ_name,
                "live_count": live_count,
                "live_bytes": live_bytes,
                "dead_bytes": dead_bytes,
                "occ_sha256": sha(occ_image),
            }
            return {
                "mutated": True,
                "repacked": False,
                "dropped": False,
                "descriptor": updated,
                "tombstoned": tombstoned,
                "logical_reclaimed": logical_reclaimed,
                "physical_reclaimed": 0,
            }

        if live_count == 0:
            return {
                "mutated": True,
                "repacked": True,
                "dropped": True,
                "descriptor": None,
                "tombstoned": tombstoned,
                "logical_reclaimed": logical_reclaimed,
                "physical_reclaimed": shard_desc["slab_bytes"],
            }

        live_items = _extract_verified_live_frames(handle, live_slot_meta)
        new_desc = write_shard(
            packs_dir, next_gen, shard_desc["shard"], live_items, occ_generation=1
        )
        return {
            "mutated": True,
            "repacked": True,
            "dropped": False,
            "descriptor": new_desc,
            "tombstoned": tombstoned,
            "logical_reclaimed": logical_reclaimed,
            "physical_reclaimed": max(0, shard_desc["slab_bytes"] - new_desc["slab_bytes"]),
        }
    finally:
        handle.close()


def gc_packed_objects(
    packs_dir: Path,
    unreachable_keys: set[str],
    *,
    repack_threshold: float = DEFAULT_REPACK_DEAD_RATIO,
) -> dict[str, Any]:
    """Logical .occ tombstone GC over packed shards, repacking shards over dead-ratio threshold."""
    current = parse_pack_current(packs_dir)

    if current is None or not current["shards"] or not unreachable_keys:
        return {
            "tombstoned_keys": [],
            "tombstoned_count": 0,
            "logical_reclaimed_bytes": 0,
            "physical_reclaimed_bytes": 0,
            "repacked_shards": [],
            "dropped_shards": [],
            "generation": current["generation"] if current is not None else 0,
            "occ_generation": current["occ_generation"] if current is not None else 0,
        }

    next_gen = current["generation"] + 1
    next_occ_gen = current["occ_generation"] + 1
    key_bytes_map = {bytes.fromhex(k): k for k in unreachable_keys}

    updated_shards: list[dict] = []
    tombstoned_keys: list[str] = []
    logical_reclaimed = 0
    physical_reclaimed = 0
    repacked_shards: list[int] = []
    dropped_shards: list[int] = []
    any_mutated = False
    any_repacked = False

    for shard_desc in current["shards"]:
        outcome = _gc_one_shard(
            packs_dir,
            shard_desc,
            key_bytes_map,
            next_gen=next_gen,
            next_occ_gen=next_occ_gen,
            repack_threshold=repack_threshold,
        )
        if not outcome["mutated"]:
            updated_shards.append(outcome["descriptor"])
            continue

        any_mutated = True
        tombstoned_keys.extend(outcome["tombstoned"])
        logical_reclaimed += outcome["logical_reclaimed"]
        physical_reclaimed += outcome["physical_reclaimed"]

        if outcome["dropped"]:
            any_repacked = True
            dropped_shards.append(shard_desc["shard"])
        elif outcome["repacked"]:
            any_repacked = True
            repacked_shards.append(shard_desc["shard"])
            updated_shards.append(outcome["descriptor"])
        else:
            updated_shards.append(outcome["descriptor"])

    new_gen = next_gen if any_repacked else current["generation"]
    if any_mutated:
        write_pack_current(packs_dir, new_gen, next_occ_gen, updated_shards)

    unique_keys = sorted(set(tombstoned_keys))
    return {
        "tombstoned_keys": unique_keys,
        "tombstoned_count": len(unique_keys),
        "logical_reclaimed_bytes": logical_reclaimed,
        "physical_reclaimed_bytes": physical_reclaimed,
        "repacked_shards": repacked_shards,
        "dropped_shards": dropped_shards,
        "generation": new_gen if any_mutated else current["generation"],
        "occ_generation": next_occ_gen if any_mutated else current["occ_generation"],
    }


__all__ = [
    "DEFAULT_REPACK_DEAD_RATIO",
    "gc_packed_objects",
    "write_pack_current",
    "write_shard",
]
