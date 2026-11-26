"""Reserved append-only shard slabs (.slab + 256-fanout .idx + .occ bytemap) for ObjectStore & ColdArchive."""
from __future__ import annotations

import os
from pathlib import Path
import re
import struct
import zlib

from managed_ai.core.storage import (
    DEFAULT_SHARD_TARGET_BYTES,
    FRAME_HEADER_STRUCT,
    IDX_ENTRY_STRUCT,
    IDX_HEADER_STRUCT,
    OCC_HEADER_STRUCT,
    OCC_LIVE_DEFLATE,
    OCC_LIVE_RAW,
    OCC_SEALED,
    OCC_TOMBSTONE,
    PACK_CURRENT_FORMAT,
    PACK_FRAME_MAGIC,
    PACK_IDX_MAGIC,
    PACK_OCC_MAGIC,
    PACK_SLAB_MAGIC,
    PackSession,
    PackShardHandle,
    SLAB_HEADER_STRUCT,
    atomic_bytes,
    encode,
    is_ordinary_file_stat,
    now,
    parse_pack_current,
    sha,
    sync_dir,
)
from managed_ai.store.protocol.identities import digest, require
from managed_ai.store.protocol.inputs import integer, text
from managed_ai.store.records.deduplication import (
    _read_ordinary_unbounded,
    _write_receipt,
)

PACK_OBJECTS_PLAN_FORMAT = 'managed-ai-pack-objects-plan/1'
PACK_OBJECTS_RECEIPT_FORMAT = 'managed-ai-pack-objects-receipt/1'
DEFAULT_REPACK_DEAD_RATIO = 0.30
_MIN_SHARD_TARGET_BYTES = 4096
_PACK_FILE_RE = re.compile(r'^pack-[0-9]{6}-[0-9]{4}\.(slab|idx|occ[1-9][0-9]*)$')
_NTFS_CLUSTER = 4096


def _cluster_alloc(size: int) -> int:
    if size <= 0:
        return 0
    return ((size + _NTFS_CLUSTER - 1) // _NTFS_CLUSTER) * _NTFS_CLUSTER


def _prune_unreferenced_pack_files(packs_dir: Path, active_shards: list[dict]) -> None:
    """Opportunistically unlink obsolete pack generations (ignoring Windows active-mmap locks)."""

    if not packs_dir.is_dir():
        return
    keep: set[str] = {'CURRENT.json'}

    for shard in active_shards:
        keep.add(shard['slab'])
        keep.add(shard['idx'])
        keep.add(shard['occ'])

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
        try:
            sync_dir(packs_dir)
        except OSError:
            pass


def write_pack_current(
    packs_dir: Path,
    generation: int,
    occ_generation: int,
    shards: list[dict],
) -> dict:
    """Atomically flip packs/CURRENT.json and opportunistically prune obsolete generations."""
    packs_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        'format': PACK_CURRENT_FORMAT,
        'generation': generation,
        'occ_generation': occ_generation,
        'shards': shards,
    }
    atomic_bytes(packs_dir / 'CURRENT.json', encode(doc))
    sync_dir(packs_dir)
    _prune_unreferenced_pack_files(packs_dir, shards)

    return doc


def write_shard(
    packs_dir: Path,
    generation: int,
    shard_id: int,
    items: list[tuple[bytes, int, int, bytes]],
    *,
    occ_generation: int = 1,
) -> dict:
    """Write one immutable (.slab, .idx, .occ) shard triplet and verify 100% SHA-256 readback."""
    require(len(items) > 0, 'cannot write an empty pack shard')
    packs_dir.mkdir(parents=True, exist_ok=True)

    slab_name = f'pack-{generation:06d}-{shard_id:04d}.slab'
    idx_name = f'pack-{generation:06d}-{shard_id:04d}.idx'
    occ_name = f'pack-{generation:06d}-{shard_id:04d}.occ{occ_generation}'

    # Build aligned slab frames sequentially.
    slab_buf = bytearray(SLAB_HEADER_STRUCT.pack(PACK_SLAB_MAGIC, 1, 0, shard_id, len(items), generation))
    index_rows: list[tuple[bytes, int, int, int, int]] = []
    live_bytes = 0

    for key_bytes, raw_len, codec, stored_bytes in items:
        if len(key_bytes) != 32:
            raise ValueError('pack shard key must be 32 raw bytes')

        if codec not in (OCC_LIVE_RAW, OCC_LIVE_DEFLATE, OCC_SEALED):
            raise ValueError(f'unsupported pack frame codec: {codec}')

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
            slab_buf.extend(b'\x00' * pad)

        index_rows.append((key_bytes, frame_offset, len(stored_bytes), raw_len, codec))
        live_bytes += len(stored_bytes) + FRAME_HEADER_STRUCT.size

    # Sort entries ascending by 32-byte key for 256-entry fanout binary search.
    index_rows.sort(key=lambda row: row[0])

    # Check strictly unique keys inside shard.
    for pos in range(1, len(index_rows)):
        if index_rows[pos][0] == index_rows[pos - 1][0]:
            raise ValueError(f'duplicate object key in pack shard: {index_rows[pos][0].hex()}')

    fanout = [0] * 256

    for key_bytes, _, _, _, _ in index_rows:
        fanout[key_bytes[0]] += 1
    running = 0

    for byte_val in range(256):
        running += fanout[byte_val]
        fanout[byte_val] = running

    idx_buf = bytearray(IDX_HEADER_STRUCT.pack(PACK_IDX_MAGIC, 1, 0, shard_id, len(index_rows), generation))
    idx_buf.extend(struct.pack('<256I', *fanout))
    occ_buf = bytearray(OCC_HEADER_STRUCT.pack(PACK_OCC_MAGIC, 1, 0, len(index_rows)))

    for key_bytes, frame_offset, stored_len, raw_len, codec in index_rows:
        idx_buf.extend(IDX_ENTRY_STRUCT.pack(key_bytes, frame_offset, stored_len, raw_len))
        occ_buf.append(codec)

    slab_bytes = bytes(slab_buf)
    idx_bytes = bytes(idx_buf)
    occ_bytes = bytes(occ_buf)

    atomic_bytes(packs_dir / slab_name, slab_bytes)
    atomic_bytes(packs_dir / idx_name, idx_bytes)
    atomic_bytes(packs_dir / occ_name, occ_bytes)

    descriptor = {
        'shard': shard_id,
        'slab': slab_name,
        'idx': idx_name,
        'occ': occ_name,
        'object_count': len(index_rows),
        'live_count': len(index_rows),
        'slab_bytes': len(slab_bytes),
        'live_bytes': live_bytes,
        'dead_bytes': 0,
        'slab_sha256': sha(slab_bytes),
        'idx_sha256': sha(idx_bytes),
        'occ_sha256': sha(occ_bytes),
    }

    # Verify 100% SHA-256 readback of every non-sealed frame BEFORE flipping CURRENT.json.
    handle = PackShardHandle(packs_dir, descriptor)

    try:
        for key_bytes, frame_offset, stored_len, raw_len, codec in index_rows:
            hit = handle.lookup(key_bytes)
            require(hit is not None, f'written pack shard lookup failed: {key_bytes.hex()}')
            _slot, h_occ, h_off, h_stored, h_raw = hit
            require(
                h_occ == codec and h_off == frame_offset and h_stored == stored_len and h_raw == raw_len,
                f'written pack shard index mismatch: {key_bytes.hex()}',
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

    return descriptor


def _collect_loose_pack_candidates(
    objects,
    *,
    compress: bool,
) -> tuple[list[dict], list[dict], set[str]]:
    """Inspect loose objects, validating SHA-256 and separating new candidates from already-packed duplicates."""
    entries = objects.entries()
    grouped: dict[str, dict[str, tuple[Path, int]]] = {}

    for key, path, is_comp, disk_bytes in entries:
        grouped.setdefault(key, {})['deflate' if is_comp else 'raw'] = (path, disk_bytes)

    existing_packed_keys: set[str] = set()

    if (objects.packs_dir / 'CURRENT.json').exists():
        with PackSession(objects.packs_dir) as session:
            for row in session.live_entries():
                existing_packed_keys.add(row['object'])

    to_pack: list[dict] = []
    duplicate_loose: list[dict] = []

    for key in sorted(grouped):
        variants = grouped[key]
        raw_entry = variants.get('raw')
        def_entry = variants.get('deflate')

        raw_bytes_data: bytes | None = None

        if raw_entry is not None:
            raw_path, _ = raw_entry
            raw_bytes_data = _read_ordinary_unbounded(raw_path)
            if sha(raw_bytes_data) != key:
                raise ValueError(f'corrupt loose object during pack planning: {key}')

        def_payload: bytes | None = None

        if def_entry is not None:
            def_path, _ = def_entry
            def_payload = _read_ordinary_unbounded(def_path)

            with def_path.open('rb') as stream:
                inflated = objects._decompress_bounded(stream, None, def_path)
            if sha(inflated) != key:
                raise ValueError(f'corrupt loose compressed object during pack planning: {key}')
            if raw_bytes_data is not None and inflated != raw_bytes_data:
                raise ValueError(f'conflicting loose raw and compressed object: {key}')
            raw_bytes_data = inflated

        assert raw_bytes_data is not None
        raw_len = len(raw_bytes_data)
        loose_paths = [pair[0] for pair in variants.values()]
        loose_disk_bytes = sum(pair[1] for pair in variants.values())
        loose_alloc_bytes = sum(_cluster_alloc(pair[1]) for pair in variants.values())

        if key in existing_packed_keys:
            duplicate_loose.append({
                'object': key,
                'raw_bytes': raw_len,
                'loose_disk_bytes': loose_disk_bytes,
                'loose_alloc_bytes': loose_alloc_bytes,
                'paths': loose_paths,
            })
            continue

        if def_payload is not None and len(def_payload) < raw_len:
            codec = OCC_LIVE_DEFLATE
            stored_len = len(def_payload)
        elif compress:
            comp = zlib.compress(raw_bytes_data, level=6)
            if len(comp) < raw_len:
                codec = OCC_LIVE_DEFLATE
                stored_len = len(comp)
            else:
                codec = OCC_LIVE_RAW
                stored_len = raw_len
        else:
            codec = OCC_LIVE_RAW
            stored_len = raw_len

        to_pack.append({
            'object': key,
            'raw_bytes': raw_len,
            'stored_bytes': stored_len,
            'codec': codec,
            'loose_disk_bytes': loose_disk_bytes,
            'loose_alloc_bytes': loose_alloc_bytes,
            'paths': loose_paths,
            'has_deflate': def_entry is not None,
        })

    return to_pack, duplicate_loose, existing_packed_keys


def _partition_shards(candidates: list[dict], shard_target_bytes: int) -> list[list[dict]]:
    """Group candidate objects into shards up to shard_target_bytes each."""
    shards: list[list[dict]] = []
    current: list[dict] = []
    current_bytes = SLAB_HEADER_STRUCT.size

    for row in candidates:
        pad = (-row['stored_bytes']) & 15
        frame_bytes = FRAME_HEADER_STRUCT.size + row['stored_bytes'] + pad

        if current and current_bytes + frame_bytes > shard_target_bytes:
            shards.append(current)
            current = []
            current_bytes = SLAB_HEADER_STRUCT.size
        current.append(row)
        current_bytes += frame_bytes

    if current:
        shards.append(current)

    return shards


def plan_pack_objects(
    system,
    *,
    shard_target_bytes: int = DEFAULT_SHARD_TARGET_BYTES,
    compress: bool = False,
    include_cold_archive: bool = False,
) -> dict:
    """Read-only dry-run plan for packing immutable loose exchange objects into reserved shard slabs."""
    integer(shard_target_bytes, 'shard_bytes', _MIN_SHARD_TARGET_BYTES)
    objects = system.ex.objects
    to_pack, duplicate_loose, existing_packed_keys = _collect_loose_pack_candidates(objects, compress=compress)
    planned_shards = _partition_shards(to_pack, shard_target_bytes)

    total_raw_bytes = sum(row['raw_bytes'] for row in to_pack)
    total_stored_bytes = sum(row['stored_bytes'] for row in to_pack)
    total_loose_disk = sum(row['loose_disk_bytes'] for row in to_pack) + sum(
        row['loose_disk_bytes'] for row in duplicate_loose
    )
    total_loose_alloc = sum(row['loose_alloc_bytes'] for row in to_pack) + sum(
        row['loose_alloc_bytes'] for row in duplicate_loose
    )
    estimated_pack_bytes = sum(
        SLAB_HEADER_STRUCT.size
        + sum(FRAME_HEADER_STRUCT.size + row['stored_bytes'] + ((-row['stored_bytes']) & 15) for row in shard)
        + 1056 + len(shard) * 48
        + 16 + len(shard)

        for shard in planned_shards
    )

    cold_loose = 0

    if include_cold_archive:
        archive = system.record_store.cold_archive
        cold_loose = sum(1 for ident in archive.ids() if archive.blob_path(ident).exists())

    body = {
        'format': PACK_OBJECTS_PLAN_FORMAT,
        'mode': 'dry-run',
        'instance_id': system.instance_id,
        'shard_target_bytes': shard_target_bytes,
        'compress': bool(compress),
        'include_cold_archive': bool(include_cold_archive),
        'existing_packed_objects': len(existing_packed_keys),
        'loose_pack_candidates': len(to_pack),
        'duplicate_loose_copies': len(duplicate_loose),
        'planned_new_shards': len(planned_shards),
        'candidate_raw_bytes': total_raw_bytes,
        'candidate_stored_bytes': total_stored_bytes,
        'loose_disk_bytes': total_loose_disk,
        'loose_estimated_cluster_bytes': total_loose_alloc,
        'estimated_pack_bytes': estimated_pack_bytes,
        'estimated_cluster_savings_bytes': max(0, total_loose_alloc - estimated_pack_bytes),
        'cold_archive_loose_candidates': cold_loose,
        'candidates': [
            {
                'object': row['object'],
                'raw_bytes': row['raw_bytes'],
                'stored_bytes': row['stored_bytes'],
                'compressed': row['codec'] == OCC_LIVE_DEFLATE,
            }

            for row in to_pack
        ],
    }

    return dict(body, plan_sha256=sha(encode(body)))


def execute_pack_objects(
    system,
    *,
    shard_target_bytes: int = DEFAULT_SHARD_TARGET_BYTES,
    compress: bool = False,
    include_cold_archive: bool = False,
    expected_plan: str | None = None,
    reason: str = 'Pack immutable loose exchange objects into reserved shard slabs.',
) -> dict:
    """Pack loose exchange objects into immutable reserved shard slabs, verify SHA-256 readback, then unlink loose."""
    reason = text(reason, 'pack-objects reason')
    integer(shard_target_bytes, 'shard_bytes', _MIN_SHARD_TARGET_BYTES)

    if expected_plan is not None:
        digest(expected_plan, 'expected plan sha256')

    live_plan = plan_pack_objects(
        system,
        shard_target_bytes=shard_target_bytes,
        compress=compress,
        include_cold_archive=include_cold_archive,
    )

    if expected_plan is not None:
        require(
            live_plan['plan_sha256'] == expected_plan,
            'pack-objects live state differs from expected plan_sha256',
        )

    objects = system.ex.objects
    to_pack, duplicate_loose, _ = _collect_loose_pack_candidates(objects, compress=compress)
    planned_shards = _partition_shards(to_pack, shard_target_bytes)

    current = parse_pack_current(objects.packs_dir)
    prev_gen = current['generation'] if current is not None else 0
    prev_occ_gen = current['occ_generation'] if current is not None else 1
    existing_shards = list(current['shards']) if current is not None else []
    next_shard_id = max((s['shard'] for s in existing_shards), default=-1) + 1
    next_gen = prev_gen + 1

    new_shard_descriptors: list[dict] = []
    packed_count = 0
    packed_raw_bytes = 0
    packed_stored_bytes = 0

    for offset, shard_rows in enumerate(planned_shards):
        shard_id = next_shard_id + offset
        shard_items: list[tuple[bytes, int, int, bytes]] = []

        for row in shard_rows:
            key = row['object']
            codec = row['codec']
            if codec == OCC_LIVE_DEFLATE:
                if row['has_deflate']:
                    stored = _read_ordinary_unbounded(objects.directory / (key + '.deflate'))
                else:
                    raw_data = _read_ordinary_unbounded(objects.directory / key)
                    stored = zlib.compress(raw_data, level=6)
            else:
                stored = _read_ordinary_unbounded(objects.directory / key)
            require(len(stored) == row['stored_bytes'], f'object size drifted during packing: {key}')
            shard_items.append((bytes.fromhex(key), row['raw_bytes'], codec, stored))

        descriptor = write_shard(objects.packs_dir, next_gen, shard_id, shard_items, occ_generation=1)
        new_shard_descriptors.append(descriptor)
        packed_count += len(shard_items)
        packed_raw_bytes += sum(row['raw_bytes'] for row in shard_rows)
        packed_stored_bytes += sum(row['stored_bytes'] for row in shard_rows)

    if new_shard_descriptors:
        all_shards = [*existing_shards, *new_shard_descriptors]
        write_pack_current(objects.packs_dir, next_gen, prev_occ_gen, all_shards)

        # Verify every newly packed object resolves cleanly via the live PackSession BEFORE unlinking loose files.
        with PackSession(objects.packs_dir) as session:
            for row in to_pack:
                got = session.read(row['object'], retained=True, expected_bytes=row['raw_bytes'])
                require(got is not None and sha(got) == row['object'], f'post-flip readback failed: {row["object"]}')

    # Verify already-packed duplicate loose copies match packed bytes BEFORE unlinking them.
    if duplicate_loose:
        with PackSession(objects.packs_dir) as session:
            for row in duplicate_loose:
                got = session.read(row['object'], retained=True, expected_bytes=row['raw_bytes'])
                require(got is not None and sha(got) == row['object'], f'packed duplicate mismatch: {row["object"]}')

    removed_loose_files = 0

    for row in to_pack:
        for loose_path in row['paths']:
            loose_path.unlink()
            removed_loose_files += 1

    for row in duplicate_loose:
        for loose_path in row['paths']:
            loose_path.unlink()
            removed_loose_files += 1

    if removed_loose_files > 0:
        sync_dir(objects.directory)

    cold_result = None

    if include_cold_archive:
        cold_result = pack_cold_archive_blobs(
            system.record_store.cold_archive,
            shard_target_bytes=shard_target_bytes,
        )

    receipt = {
        'format': PACK_OBJECTS_RECEIPT_FORMAT,
        'mode': 'applied',
        'instance_id': system.instance_id,
        'plan_sha256': live_plan['plan_sha256'],
        'reason': reason,
        'executed_at': now(),
        'generation': next_gen if new_shard_descriptors else prev_gen,
        'packed_objects': packed_count,
        'duplicate_loose_removed': len(duplicate_loose),
        'removed_loose_files': removed_loose_files,
        'new_shards': len(new_shard_descriptors),
        'total_shards': len(existing_shards) + len(new_shard_descriptors),
        'packed_raw_bytes': packed_raw_bytes,
        'packed_stored_bytes': packed_stored_bytes,
        'shards': new_shard_descriptors,
    }

    if cold_result is not None:
        receipt['cold_archive'] = cold_result

    if packed_count > 0 or duplicate_loose or (cold_result and cold_result['packed_records'] > 0):
        receipt_path = _write_receipt(system, f"pack-objects-{live_plan['plan_sha256'][:16]}.json", receipt)

        return dict(receipt, receipt_path=str(receipt_path))
    return receipt


def gc_packed_objects(
    packs_dir: Path,
    unreachable_keys: set[str],
    *,
    repack_threshold: float = DEFAULT_REPACK_DEAD_RATIO,
) -> dict:
    """Logical .occ tombstone GC over packed shards, physically repacking shards whose dead ratio > threshold."""
    current = parse_pack_current(packs_dir)

    if current is None or not current['shards'] or not unreachable_keys:
        return {
            'tombstoned_keys': [],
            'tombstoned_count': 0,
            'logical_reclaimed_bytes': 0,
            'physical_reclaimed_bytes': 0,
            'repacked_shards': [],
            'dropped_shards': [],
            'generation': current['generation'] if current is not None else 0,
            'occ_generation': current['occ_generation'] if current is not None else 0,
        }

    next_gen = current['generation'] + 1
    next_occ_gen = current['occ_generation'] + 1
    updated_shards: list[dict] = []
    tombstoned_keys: list[str] = []
    logical_reclaimed = 0
    physical_reclaimed = 0
    repacked_shards: list[int] = []
    dropped_shards: list[int] = []
    any_mutated = False
    any_repacked = False

    key_bytes_map = {bytes.fromhex(k): k for k in unreachable_keys}

    for shard_desc in current['shards']:
        handle = PackShardHandle(packs_dir, shard_desc)

        try:
            hit_slots: dict[int, tuple[str, int]] = {}

            for k_bytes, k_hex in key_bytes_map.items():
                found = handle.lookup(k_bytes)
                if found is not None:
                    slot_idx, occ_val, _slab_off, stored_len, _raw_len = found
                    if occ_val != OCC_SEALED:
                        hit_slots[slot_idx] = (k_hex, stored_len + FRAME_HEADER_STRUCT.size)

            if not hit_slots:
                updated_shards.append(dict(shard_desc))
                continue

            any_mutated = True
            assert handle._occ_mm is not None and handle._idx_mm is not None and handle._slab_mm is not None
            occ_bytes = bytearray(handle._occ_mm[OCC_HEADER_STRUCT.size:OCC_HEADER_STRUCT.size + handle.object_count])

            for slot_idx, (k_hex, frame_disk_bytes) in hit_slots.items():
                occ_bytes[slot_idx] = OCC_TOMBSTONE
                tombstoned_keys.append(k_hex)
                logical_reclaimed += frame_disk_bytes

            # Recompute exact live vs dead byte totals across the shard.
            live_count = 0
            live_bytes = 0
            dead_bytes = 0
            live_slot_meta: list[tuple[bytes, int, int, int, int]] = []

            for slot in range(handle.object_count):
                sha_bin, slab_off, stored_len, raw_len = IDX_ENTRY_STRUCT.unpack_from(
                    handle._idx_mm, 1056 + slot * 48,
                )
                entry_disk = stored_len + FRAME_HEADER_STRUCT.size
                occ_val = occ_bytes[slot]
                if occ_val == OCC_TOMBSTONE:
                    dead_bytes += entry_disk
                else:
                    live_count += 1
                    live_bytes += entry_disk
                    live_slot_meta.append((sha_bin, slab_off, stored_len, raw_len, occ_val))

            total_payload_bytes = live_bytes + dead_bytes
            dead_ratio = 1.0 if live_count == 0 else (dead_bytes / total_payload_bytes if total_payload_bytes else 0.0)

            if dead_ratio <= repack_threshold:
                # Logical GC only: write tiny new immutable .occ<next_occ_gen>, leave .slab & .idx untouched!
                stem = shard_desc['slab'][:-5]  # strip '.slab'
                new_occ_name = f'{stem}.occ{next_occ_gen}'
                occ_image = OCC_HEADER_STRUCT.pack(PACK_OCC_MAGIC, 1, 0, handle.object_count) + bytes(occ_bytes)
                atomic_bytes(packs_dir / new_occ_name, occ_image)
                updated_shards.append({
                    **shard_desc,
                    'occ': new_occ_name,
                    'live_count': live_count,
                    'live_bytes': live_bytes,
                    'dead_bytes': dead_bytes,
                    'occ_sha256': sha(occ_image),
                })
            elif live_count == 0:
                # Shard is 100% dead: drop it cleanly from CURRENT.json.
                any_repacked = True
                dropped_shards.append(shard_desc['shard'])
                physical_reclaimed += shard_desc['slab_bytes']
            else:
                # Dead bytes exceed threshold (>30%): physically repack surviving live slots into new immutable gen.
                any_repacked = True
                live_items: list[tuple[bytes, int, int, bytes]] = []
                for sha_bin, slab_off, stored_len, raw_len, occ_val in live_slot_meta:
                    codec, _flags, magic2, hdr_stored, hdr_raw, hdr_sha, hdr_crc = FRAME_HEADER_STRUCT.unpack_from(
                        handle._slab_mm, slab_off,
                    )
                    require(
                        magic2 == PACK_FRAME_MAGIC
                        and codec == occ_val
                        and hdr_stored == stored_len
                        and hdr_raw == raw_len
                        and hdr_sha == sha_bin,
                        f'corrupt frame during physical shard repack: {sha_bin.hex()}',
                    )
                    p_start = slab_off + FRAME_HEADER_STRUCT.size
                    payload = handle._slab_mm[p_start:p_start + stored_len]
                    require(
                        (zlib.crc32(payload) & 0xFFFFFFFF) == hdr_crc,
                        f'frame crc32 mismatch during physical shard repack: {sha_bin.hex()}',
                    )
                    live_items.append((sha_bin, raw_len, occ_val, payload))

                new_desc = write_shard(packs_dir, next_gen, shard_desc['shard'], live_items, occ_generation=1)
                updated_shards.append(new_desc)
                repacked_shards.append(shard_desc['shard'])
                physical_reclaimed += max(0, shard_desc['slab_bytes'] - new_desc['slab_bytes'])
        finally:
            handle.close()

    new_gen = next_gen if any_repacked else current['generation']
    if any_mutated:
        write_pack_current(packs_dir, new_gen, next_occ_gen, updated_shards)

    return {
        'tombstoned_keys': sorted(set(tombstoned_keys)),
        'tombstoned_count': len(set(tombstoned_keys)),
        'logical_reclaimed_bytes': logical_reclaimed,
        'physical_reclaimed_bytes': physical_reclaimed,
        'repacked_shards': repacked_shards,
        'dropped_shards': dropped_shards,
        'generation': new_gen if any_mutated else current['generation'],
        'occ_generation': next_occ_gen if any_mutated else current['occ_generation'],
    }


def pack_cold_archive_blobs(
    archive,
    *,
    shard_target_bytes: int = DEFAULT_SHARD_TARGET_BYTES,
) -> dict:
    """Pack loose exchange/cold-records/records/*.json.deflate blobs into reserved shard slabs."""
    integer(shard_target_bytes, 'shard_bytes', _MIN_SHARD_TARGET_BYTES)
    manifest_entries = archive.entries()
    if not manifest_entries:
        return {'packed_records': 0, 'new_shards': 0, 'removed_loose_blobs': 0}

    existing_packed: set[str] = set()
    if (archive.packs_dir / 'CURRENT.json').exists():
        with PackSession(archive.packs_dir) as session:
            existing_packed = {row['object'] for row in session.live_entries()}

    # Deduplicate by raw_sha256 (multiple archived records could theoretically share bytes, though IDs differ).
    candidates_by_sha: dict[str, tuple[int, bytes, list[str]]] = {}
    loose_to_unlink: list[Path] = []
    record_ids_to_verify: list[str] = []

    for ident in sorted(manifest_entries):
        blob = archive.blob_path(ident)
        if not blob.exists():
            continue
        entry = manifest_entries[ident]
        raw_sha = digest(entry['raw_sha256'], 'cold archive raw_sha256')
        raw_bytes = integer(entry['raw_bytes'], 'cold archive raw_bytes')

        # Verify loose blob thoroughly before packing.
        _record, raw = archive.read(ident)
        require(len(raw) == raw_bytes and sha(raw) == raw_sha, f'cold archive blob mismatch: {ident}')

        loose_to_unlink.append(blob)
        record_ids_to_verify.append(ident)
        if raw_sha in existing_packed:
            continue
        if raw_sha not in candidates_by_sha:
            deflated = _read_ordinary_unbounded(blob)
            candidates_by_sha[raw_sha] = (raw_bytes, deflated, [ident])
        else:
            candidates_by_sha[raw_sha][2].append(ident)

    if not loose_to_unlink:
        return {'packed_records': 0, 'new_shards': 0, 'removed_loose_blobs': 0}

    current = parse_pack_current(archive.packs_dir)
    prev_gen = current['generation'] if current is not None else 0
    prev_occ_gen = current['occ_generation'] if current is not None else 1
    existing_shards = list(current['shards']) if current is not None else []
    next_shard_id = max((s['shard'] for s in existing_shards), default=-1) + 1
    next_gen = prev_gen + 1

    shard_rows = [
        {
            'object': raw_sha,
            'raw_bytes': raw_bytes,
            'stored_bytes': len(deflated),
            'codec': OCC_LIVE_DEFLATE,
            'payload': deflated,
        }
        for raw_sha, (raw_bytes, deflated, _) in sorted(candidates_by_sha.items())
    ]
    partitioned = _partition_shards(shard_rows, shard_target_bytes)
    new_descriptors: list[dict] = []

    for offset, batch in enumerate(partitioned):
        shard_items = [
            (bytes.fromhex(row['object']), row['raw_bytes'], OCC_LIVE_DEFLATE, row['payload'])
            for row in batch
        ]
        new_descriptors.append(
            write_shard(archive.packs_dir, next_gen, next_shard_id + offset, shard_items, occ_generation=1)
        )

    if new_descriptors:
        write_pack_current(archive.packs_dir, next_gen, prev_occ_gen, [*existing_shards, *new_descriptors])

    # Verify every record reads back cleanly from the shard packs BEFORE removing loose blobs.
    with PackSession(archive.packs_dir) as session:
        for ident in record_ids_to_verify:
            entry = manifest_entries[ident]
            got = session.read(entry['raw_sha256'], retained=True, expected_bytes=entry['raw_bytes'])
            require(
                got is not None and sha(got) == entry['raw_sha256'],
                f'cold archive packed readback failed: {ident}',
            )

    for blob in loose_to_unlink:
        blob.unlink()
    if archive.blobs_dir.exists():
        sync_dir(archive.blobs_dir)

    return {
        'packed_records': len(record_ids_to_verify),
        'new_shards': len(new_descriptors),
        'removed_loose_blobs': len(loose_to_unlink),
    }


__all__ = [
    'DEFAULT_REPACK_DEAD_RATIO',
    'DEFAULT_SHARD_TARGET_BYTES',
    'PACK_OBJECTS_PLAN_FORMAT',
    'PACK_OBJECTS_RECEIPT_FORMAT',
    'execute_pack_objects',
    'gc_packed_objects',
    'pack_cold_archive_blobs',
    'plan_pack_objects',
    'write_pack_current',
    'write_shard',
]
