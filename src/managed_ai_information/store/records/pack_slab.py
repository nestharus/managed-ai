"""Planning and execution of reserved append-only shard slabs for ObjectStore & ColdArchive."""

import os
import zlib
from compression import zstd
from pathlib import Path
from typing import Any

from managed_ai_information.core.storage import (
    DEFAULT_SHARD_TARGET_BYTES,
    FRAME_HEADER_STRUCT,
    OCC_LIVE_DEFLATE,
    OCC_LIVE_RAW,
    OCC_LIVE_ZSTD,
    SLAB_HEADER_STRUCT,
    ObjectStore,
    PackSession,
    encode,
    has_pack_pointer,
    now,
    parse_pack_current,
    sha,
    sync_dir,
)
from managed_ai_information.store.protocol.identities import digest, require
from managed_ai_information.store.protocol.inputs import integer, text
from managed_ai_information.store.records.deduplication import (
    _read_ordinary_unbounded,
    _write_receipt,
)
from managed_ai_information.store.records.pack_gc import (
    DEFAULT_REPACK_DEAD_RATIO,
    gc_packed_objects,
    write_pack_current,
    write_shard,
)

PACK_OBJECTS_PLAN_FORMAT = "managed-ai-pack-objects-plan/1"
PACK_OBJECTS_RECEIPT_FORMAT = "managed-ai-pack-objects-receipt/1"
VALID_PACK_CODECS = ("zstd", "deflate")
DEFAULT_PACK_CODEC = "zstd"
_MIN_SHARD_TARGET_BYTES = 4096
_NTFS_CLUSTER = 4096
_ZSTD_LEVEL = 3


def _cluster_alloc(size: int) -> int:
    if size <= 0:
        return 0
    return ((size + _NTFS_CLUSTER - 1) // _NTFS_CLUSTER) * _NTFS_CLUSTER


def _pack_workers(item_count: int) -> int:
    return min(8, os.cpu_count() or 4, max(1, item_count))


def _resolve_compression(compress: bool, codec: str | None) -> tuple[bool, str]:
    if codec is not None and codec not in VALID_PACK_CODECS:
        raise ValueError(f"unsupported pack codec: {codec!r}")
    effective_compress = bool(compress or codec is not None)
    effective_codec = codec if codec is not None else DEFAULT_PACK_CODEC
    return effective_compress, effective_codec


def _read_verified_loose_pair(
    objects: ObjectStore,
    key: str,
    variants: dict[str, tuple[Path, int]],
) -> tuple[bytes, bytes | None]:
    """Read and SHA-256 verify raw and/or .deflate loose variants for a single object key."""
    raw_entry = variants.get("raw")
    def_entry = variants.get("deflate")
    raw_bytes_data: bytes | None = None

    if raw_entry is not None:
        raw_path, _ = raw_entry
        raw_bytes_data = _read_ordinary_unbounded(raw_path)
        if sha(raw_bytes_data) != key:
            raise ValueError(f"corrupt loose object during pack planning: {key}")

    def_payload: bytes | None = None
    if def_entry is not None:
        def_path, _ = def_entry
        def_payload = _read_ordinary_unbounded(def_path)
        with def_path.open("rb") as stream:
            inflated = objects._decompress_bounded(stream, None, def_path)
        if sha(inflated) != key:
            raise ValueError(f"corrupt loose compressed object during pack planning: {key}")
        if raw_bytes_data is not None and inflated != raw_bytes_data:
            raise ValueError(f"conflicting loose raw and compressed object: {key}")
        raw_bytes_data = inflated

    if raw_bytes_data is None:
        raise RuntimeError(f"loose object has neither raw nor deflate variant: {key}")
    return raw_bytes_data, def_payload


def _select_pack_codec(
    raw_bytes_data: bytes,
    def_payload: bytes | None,
    *,
    compress: bool,
    codec: str = DEFAULT_PACK_CODEC,
) -> tuple[int, int, str]:
    """Choose (codec_occ, stored_len, source_mode) for packing one verified loose object."""
    raw_len = len(raw_bytes_data)

    if compress and codec == "zstd":
        zstd_comp = zstd.compress(raw_bytes_data, level=_ZSTD_LEVEL)
        best_len = raw_len
        best_occ = OCC_LIVE_RAW
        best_mode = "raw"
        if def_payload is not None and len(def_payload) < best_len:
            best_len = len(def_payload)
            best_occ = OCC_LIVE_DEFLATE
            best_mode = "reuse_deflate"
        if len(zstd_comp) < best_len or (
            def_payload is not None and len(zstd_comp) == best_len and len(zstd_comp) < raw_len
        ):
            return OCC_LIVE_ZSTD, len(zstd_comp), "recompress_zstd"
        return best_occ, best_len, best_mode

    if def_payload is not None and len(def_payload) < raw_len:
        return OCC_LIVE_DEFLATE, len(def_payload), "reuse_deflate"

    if compress:
        comp = zlib.compress(raw_bytes_data, level=6)
        if len(comp) < raw_len:
            return OCC_LIVE_DEFLATE, len(comp), "recompress_deflate"

    return OCC_LIVE_RAW, raw_len, "raw"


def _collect_loose_pack_candidates(
    objects: ObjectStore,
    *,
    compress: bool,
    codec: str = DEFAULT_PACK_CODEC,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    """Inspect loose objects, validating SHA-256 and separating new candidates from duplicates."""
    grouped: dict[str, dict[str, tuple[Path, int]]] = {}
    for key, path, is_comp, disk_bytes in objects.entries():
        grouped.setdefault(key, {})["deflate" if is_comp else "raw"] = (path, disk_bytes)

    existing_packed_keys: set[str] = set()
    if has_pack_pointer(objects.packs_dir):
        with PackSession(objects.packs_dir) as session:
            for row in session.live_entries():
                existing_packed_keys.add(row["object"])

    sorted_items = [(key, grouped[key]) for key in sorted(grouped)]

    def _inspect_one(
        item: tuple[str, dict[str, tuple[Path, int]]],
    ) -> tuple[bool, dict[str, Any]]:
        key, variants = item
        raw_bytes_data, def_payload = _read_verified_loose_pair(objects, key, variants)
        raw_len = len(raw_bytes_data)
        loose_paths = [pair[0] for pair in variants.values()]
        loose_disk_bytes = sum(pair[1] for pair in variants.values())
        loose_alloc_bytes = sum(_cluster_alloc(pair[1]) for pair in variants.values())

        if key in existing_packed_keys:
            return True, {
                "object": key,
                "raw_bytes": raw_len,
                "loose_disk_bytes": loose_disk_bytes,
                "loose_alloc_bytes": loose_alloc_bytes,
                "paths": loose_paths,
            }

        occ_codec, stored_len, source_mode = _select_pack_codec(
            raw_bytes_data,
            def_payload,
            compress=compress,
            codec=codec,
        )
        return False, {
            "object": key,
            "raw_bytes": raw_len,
            "stored_bytes": stored_len,
            "codec": occ_codec,
            "source_mode": source_mode,
            "loose_disk_bytes": loose_disk_bytes,
            "loose_alloc_bytes": loose_alloc_bytes,
            "paths": loose_paths,
            "has_raw": "raw" in variants,
            "has_deflate": "deflate" in variants,
        }

    workers = _pack_workers(len(sorted_items))
    if workers > 1 and compress:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            inspected = list(pool.map(_inspect_one, sorted_items))
    else:
        inspected = [_inspect_one(item) for item in sorted_items]

    to_pack: list[dict[str, Any]] = []
    duplicate_loose: list[dict[str, Any]] = []
    for is_dup, row in inspected:
        if is_dup:
            duplicate_loose.append(row)
        else:
            to_pack.append(row)

    return to_pack, duplicate_loose, existing_packed_keys


def _partition_shards(
    candidates: list[dict[str, Any]],
    shard_target_bytes: int,
) -> list[list[dict[str, Any]]]:
    """Group candidate objects into shards up to shard_target_bytes each."""
    shards: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = SLAB_HEADER_STRUCT.size

    for row in candidates:
        pad = (-row["stored_bytes"]) & 15
        frame_bytes = FRAME_HEADER_STRUCT.size + row["stored_bytes"] + pad
        if current and current_bytes + frame_bytes > shard_target_bytes:
            shards.append(current)
            current = []
            current_bytes = SLAB_HEADER_STRUCT.size
        current.append(row)
        current_bytes += frame_bytes

    if current:
        shards.append(current)
    return shards


def _plan_pack_objects_with_candidates(
    system: Any,
    *,
    shard_target_bytes: int = DEFAULT_SHARD_TARGET_BYTES,
    compress: bool = False,
    codec: str | None = None,
    include_cold_archive: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect loose pack candidates once and return (plan_dict, to_pack, duplicate_loose)."""
    integer(shard_target_bytes, "shard_bytes", _MIN_SHARD_TARGET_BYTES)
    effective_compress, effective_codec = _resolve_compression(compress, codec)
    objects: ObjectStore = system.ex.objects
    to_pack, duplicate_loose, existing_packed_keys = _collect_loose_pack_candidates(
        objects,
        compress=effective_compress,
        codec=effective_codec,
    )
    planned_shards = _partition_shards(to_pack, shard_target_bytes)

    total_raw_bytes = sum(row["raw_bytes"] for row in to_pack)
    total_stored_bytes = sum(row["stored_bytes"] for row in to_pack)
    total_loose_disk = sum(row["loose_disk_bytes"] for row in to_pack) + sum(
        row["loose_disk_bytes"] for row in duplicate_loose
    )
    total_loose_alloc = sum(row["loose_alloc_bytes"] for row in to_pack) + sum(
        row["loose_alloc_bytes"] for row in duplicate_loose
    )
    estimated_pack_bytes = sum(
        SLAB_HEADER_STRUCT.size
        + sum(
            FRAME_HEADER_STRUCT.size + row["stored_bytes"] + ((-row["stored_bytes"]) & 15)
            for row in shard
        )
        + 1056
        + len(shard) * 48
        + 16
        + len(shard)
        for shard in planned_shards
    )

    cold_loose = 0
    if include_cold_archive:
        archive = system.record_store.cold_archive
        cold_loose = sum(1 for ident in archive.ids() if archive.blob_path(ident).exists())

    body = {
        "format": PACK_OBJECTS_PLAN_FORMAT,
        "mode": "dry-run",
        "instance_id": system.instance_id,
        "shard_target_bytes": shard_target_bytes,
        "compress": effective_compress,
        "codec": effective_codec if effective_compress else "raw",
        "include_cold_archive": bool(include_cold_archive),
        "existing_packed_objects": len(existing_packed_keys),
        "loose_pack_candidates": len(to_pack),
        "duplicate_loose_copies": len(duplicate_loose),
        "planned_new_shards": len(planned_shards),
        "candidate_raw_bytes": total_raw_bytes,
        "candidate_stored_bytes": total_stored_bytes,
        "loose_disk_bytes": total_loose_disk,
        "loose_estimated_cluster_bytes": total_loose_alloc,
        "estimated_pack_bytes": estimated_pack_bytes,
        "estimated_cluster_savings_bytes": max(0, total_loose_alloc - estimated_pack_bytes),
        "cold_archive_loose_candidates": cold_loose,
        "candidates": [
            {
                "object": row["object"],
                "raw_bytes": row["raw_bytes"],
                "stored_bytes": row["stored_bytes"],
                "compressed": row["codec"] in (OCC_LIVE_DEFLATE, OCC_LIVE_ZSTD),
            }
            for row in to_pack
        ],
    }

    plan = dict(body, plan_sha256=sha(encode(body)))
    return plan, to_pack, duplicate_loose


def plan_pack_objects(
    system: Any,
    *,
    shard_target_bytes: int = DEFAULT_SHARD_TARGET_BYTES,
    compress: bool = False,
    codec: str | None = None,
    include_cold_archive: bool = False,
) -> dict[str, Any]:
    """Read-only dry-run plan for packing loose exchange objects into reserved shard slabs."""
    plan, _, _ = _plan_pack_objects_with_candidates(
        system,
        shard_target_bytes=shard_target_bytes,
        compress=compress,
        codec=codec,
        include_cold_archive=include_cold_archive,
    )
    return plan


def _load_stored_payload(objects: ObjectStore, row: dict[str, Any]) -> bytes:
    """Read or recompress one loose object payload matching its planned mode and length."""
    key = row["object"]
    source_mode = row["source_mode"]
    if source_mode == "reuse_deflate":
        stored = _read_ordinary_unbounded(objects.directory / (key + ".deflate"))
    elif source_mode == "recompress_zstd":
        raw_data = (
            _read_ordinary_unbounded(objects.directory / key)
            if row["has_raw"]
            else objects.read(key, retained=True, expected_bytes=row["raw_bytes"])
        )
        stored = zstd.compress(raw_data, level=_ZSTD_LEVEL)
    elif source_mode in ("recompress_deflate", "recompress"):
        raw_data = (
            _read_ordinary_unbounded(objects.directory / key)
            if row["has_raw"]
            else objects.read(key, retained=True, expected_bytes=row["raw_bytes"])
        )
        stored = zlib.compress(raw_data, level=6)
    else:
        stored = (
            _read_ordinary_unbounded(objects.directory / key)
            if row["has_raw"]
            else objects.read(key, retained=True, expected_bytes=row["raw_bytes"])
        )
    require(len(stored) == row["stored_bytes"], f"object size drifted during packing: {key}")
    return stored


def _write_new_object_shards(
    objects: ObjectStore,
    planned_shards: list[list[dict[str, Any]]],
    *,
    next_gen: int,
    next_shard_id: int,
) -> tuple[list[dict[str, Any]], int, int, int]:
    """Write planned new shard triplets and return (descriptors, count, raw_bytes, stored)."""
    new_descriptors: list[dict[str, Any]] = []
    packed_count = 0
    packed_raw_bytes = 0
    packed_stored_bytes = 0

    def _build_item(row: dict[str, Any]) -> tuple[bytes, int, int, bytes]:
        return (
            bytes.fromhex(row["object"]),
            row["raw_bytes"],
            row["codec"],
            _load_stored_payload(objects, row),
        )

    for offset, shard_rows in enumerate(planned_shards):
        shard_id = next_shard_id + offset
        workers = _pack_workers(len(shard_rows))
        needs_recompress = any(
            row["source_mode"] in ("recompress_zstd", "recompress_deflate", "recompress")
            for row in shard_rows
        )
        if workers > 1 and needs_recompress:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=workers) as pool:
                shard_items = list(pool.map(_build_item, shard_rows))
        else:
            shard_items = [_build_item(row) for row in shard_rows]

        descriptor = write_shard(
            objects.packs_dir,
            next_gen,
            shard_id,
            shard_items,
            occ_generation=1,
        )
        new_descriptors.append(descriptor)
        packed_count += len(shard_items)
        packed_raw_bytes += sum(row["raw_bytes"] for row in shard_rows)
        packed_stored_bytes += sum(row["stored_bytes"] for row in shard_rows)

    return new_descriptors, packed_count, packed_raw_bytes, packed_stored_bytes


def _verify_and_unlink_loose(
    objects: ObjectStore,
    to_pack: list[dict[str, Any]],
    duplicate_loose: list[dict[str, Any]],
) -> int:
    """Verify packed readback for newly packed and duplicate objects, then unlink loose files."""
    if to_pack or duplicate_loose:
        with PackSession(objects.packs_dir) as session:
            for row in to_pack:
                got = session.read(row["object"], retained=True, expected_bytes=row["raw_bytes"])
                require(
                    got is not None and sha(got) == row["object"],
                    f"post-flip readback failed: {row['object']}",
                )
            for row in duplicate_loose:
                got = session.read(row["object"], retained=True, expected_bytes=row["raw_bytes"])
                require(
                    got is not None and sha(got) == row["object"],
                    f"packed duplicate mismatch: {row['object']}",
                )

    removed_loose_files = 0
    for row in (*to_pack, *duplicate_loose):
        for loose_path in row["paths"]:
            loose_path.unlink()
            removed_loose_files += 1

    if removed_loose_files > 0:
        sync_dir(objects.directory)
    return removed_loose_files


def execute_pack_objects(
    system: Any,
    *,
    shard_target_bytes: int = DEFAULT_SHARD_TARGET_BYTES,
    compress: bool = False,
    codec: str | None = None,
    include_cold_archive: bool = False,
    expected_plan: str | None = None,
    reason: str = "Pack immutable loose exchange objects into reserved shard slabs.",
) -> dict[str, Any]:
    """Pack loose objects into reserved shard slabs, verify SHA-256 readback, then unlink loose."""
    reason = text(reason, "pack-objects reason")
    integer(shard_target_bytes, "shard_bytes", _MIN_SHARD_TARGET_BYTES)
    effective_compress, effective_codec = _resolve_compression(compress, codec)

    if expected_plan is not None:
        digest(expected_plan, "expected plan sha256")

    live_plan, to_pack, duplicate_loose = _plan_pack_objects_with_candidates(
        system,
        shard_target_bytes=shard_target_bytes,
        compress=effective_compress,
        codec=effective_codec if effective_compress else None,
        include_cold_archive=include_cold_archive,
    )

    if expected_plan is not None:
        require(
            live_plan["plan_sha256"] == expected_plan,
            "pack-objects live state differs from expected plan_sha256",
        )

    objects: ObjectStore = system.ex.objects
    planned_shards = _partition_shards(to_pack, shard_target_bytes)
    current = parse_pack_current(objects.packs_dir)
    prev_gen = current["generation"] if current is not None else 0
    prev_occ_gen = current["occ_generation"] if current is not None else 1
    existing_shards = list(current["shards"]) if current is not None else []
    next_shard_id = max((s["shard"] for s in existing_shards), default=-1) + 1
    next_gen = prev_gen + 1

    new_descriptors, packed_count, packed_raw_bytes, packed_stored_bytes = _write_new_object_shards(
        objects,
        planned_shards,
        next_gen=next_gen,
        next_shard_id=next_shard_id,
    )

    if new_descriptors:
        write_pack_current(
            objects.packs_dir,
            next_gen,
            prev_occ_gen,
            [*existing_shards, *new_descriptors],
        )

    removed_loose_files = _verify_and_unlink_loose(objects, to_pack, duplicate_loose)

    cold_result: dict[str, Any] | None = None
    if include_cold_archive:
        cold_result = pack_cold_archive_blobs(
            system.record_store.cold_archive,
            shard_target_bytes=shard_target_bytes,
        )

    receipt: dict[str, Any] = {
        "format": PACK_OBJECTS_RECEIPT_FORMAT,
        "mode": "applied",
        "instance_id": system.instance_id,
        "plan_sha256": live_plan["plan_sha256"],
        "codec": effective_codec if effective_compress else "raw",
        "reason": reason,
        "executed_at": now(),
        "generation": next_gen if new_descriptors else prev_gen,
        "packed_objects": packed_count,
        "duplicate_loose_removed": len(duplicate_loose),
        "removed_loose_files": removed_loose_files,
        "new_shards": len(new_descriptors),
        "total_shards": len(existing_shards) + len(new_descriptors),
        "packed_raw_bytes": packed_raw_bytes,
        "packed_stored_bytes": packed_stored_bytes,
        "shards": new_descriptors,
    }
    if cold_result is not None:
        receipt["cold_archive"] = cold_result

    if packed_count > 0 or duplicate_loose or (cold_result and cold_result["packed_records"] > 0):
        receipt_path = _write_receipt(
            system,
            f"pack-objects-{live_plan['plan_sha256'][:16]}.json",
            receipt,
        )
        return dict(receipt, receipt_path=str(receipt_path))
    return receipt


def _collect_cold_archive_pack_candidates(
    archive: Any,
    manifest_entries: dict[str, Any],
) -> tuple[dict[str, tuple[int, bytes, list[str]]], list[Path], list[str]]:
    """Verify loose cold-archive .json.deflate blobs and deduplicate them by raw_sha256."""
    existing_packed: set[str] = set()
    if has_pack_pointer(archive.packs_dir):
        with PackSession(archive.packs_dir) as session:
            existing_packed = {row["object"] for row in session.live_entries()}

    candidates_by_sha: dict[str, tuple[int, bytes, list[str]]] = {}
    loose_to_unlink: list[Path] = []
    record_ids_to_verify: list[str] = []

    for ident in sorted(manifest_entries):
        blob = archive.blob_path(ident)
        if not blob.exists():
            continue
        entry = manifest_entries[ident]
        raw_sha = digest(entry["raw_sha256"], "cold archive raw_sha256")
        raw_bytes = integer(entry["raw_bytes"], "cold archive raw_bytes")

        _record, raw = archive.read(ident)
        require(
            len(raw) == raw_bytes and sha(raw) == raw_sha,
            f"cold archive blob mismatch: {ident}",
        )

        loose_to_unlink.append(blob)
        record_ids_to_verify.append(ident)
        if raw_sha in existing_packed:
            continue
        if raw_sha not in candidates_by_sha:
            candidates_by_sha[raw_sha] = (raw_bytes, _read_ordinary_unbounded(blob), [ident])
        else:
            candidates_by_sha[raw_sha][2].append(ident)

    return candidates_by_sha, loose_to_unlink, record_ids_to_verify


def pack_cold_archive_blobs(
    archive: Any,
    *,
    shard_target_bytes: int = DEFAULT_SHARD_TARGET_BYTES,
) -> dict[str, Any]:
    """Pack loose exchange/cold-records/records/*.json.deflate blobs into reserved shard slabs."""
    integer(shard_target_bytes, "shard_bytes", _MIN_SHARD_TARGET_BYTES)
    manifest_entries = archive.entries()
    if not manifest_entries:
        return {"packed_records": 0, "new_shards": 0, "removed_loose_blobs": 0}

    candidates_by_sha, loose_to_unlink, record_ids_to_verify = (
        _collect_cold_archive_pack_candidates(archive, manifest_entries)
    )
    if not loose_to_unlink:
        return {"packed_records": 0, "new_shards": 0, "removed_loose_blobs": 0}

    current = parse_pack_current(archive.packs_dir)
    prev_gen = current["generation"] if current is not None else 0
    prev_occ_gen = current["occ_generation"] if current is not None else 1
    existing_shards = list(current["shards"]) if current is not None else []
    next_shard_id = max((s["shard"] for s in existing_shards), default=-1) + 1
    next_gen = prev_gen + 1

    shard_rows = [
        {
            "object": raw_sha,
            "raw_bytes": raw_bytes,
            "stored_bytes": len(deflated),
            "codec": OCC_LIVE_DEFLATE,
            "payload": deflated,
        }
        for raw_sha, (raw_bytes, deflated, _) in sorted(candidates_by_sha.items())
    ]
    partitioned = _partition_shards(shard_rows, shard_target_bytes)
    new_descriptors = [
        write_shard(
            archive.packs_dir,
            next_gen,
            next_shard_id + offset,
            [
                (bytes.fromhex(row["object"]), row["raw_bytes"], OCC_LIVE_DEFLATE, row["payload"])
                for row in batch
            ],
            occ_generation=1,
        )
        for offset, batch in enumerate(partitioned)
    ]

    if new_descriptors:
        write_pack_current(
            archive.packs_dir,
            next_gen,
            prev_occ_gen,
            [*existing_shards, *new_descriptors],
        )

    with PackSession(archive.packs_dir) as session:
        for ident in record_ids_to_verify:
            entry = manifest_entries[ident]
            got = session.read(
                entry["raw_sha256"], retained=True, expected_bytes=entry["raw_bytes"]
            )
            require(
                got is not None and sha(got) == entry["raw_sha256"],
                f"cold archive packed readback failed: {ident}",
            )

    for blob in loose_to_unlink:
        blob.unlink()
    if archive.blobs_dir.exists():
        sync_dir(archive.blobs_dir)

    return {
        "packed_records": len(record_ids_to_verify),
        "new_shards": len(new_descriptors),
        "removed_loose_blobs": len(loose_to_unlink),
    }


__all__ = [
    "DEFAULT_PACK_CODEC",
    "DEFAULT_REPACK_DEAD_RATIO",
    "DEFAULT_SHARD_TARGET_BYTES",
    "PACK_OBJECTS_PLAN_FORMAT",
    "PACK_OBJECTS_RECEIPT_FORMAT",
    "VALID_PACK_CODECS",
    "execute_pack_objects",
    "gc_packed_objects",
    "pack_cold_archive_blobs",
    "plan_pack_objects",
    "write_pack_current",
    "write_shard",
]
