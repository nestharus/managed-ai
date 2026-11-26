"""Transparent DEFLATE compaction of uncompressed exchange/objects (streaming O(1) RAM)."""

import zlib
from pathlib import Path
from typing import Any

from managed_ai_information.core.formats import COMPACT_PLAN_FORMAT, COMPACT_RECEIPT_FORMAT
from managed_ai_information.core.storage import atomic_bytes, encode, now, sha, sync_dir
from managed_ai_information.store.protocol.identities import digest, exact_fields, require
from managed_ai_information.store.protocol.inputs import integer
from managed_ai_information.store.records.deduplication import (
    _read_ordinary_unbounded,
    _verify_ordinary_file,
    _verify_plan,
    _write_receipt,
)


def plan_compact_objects(system: Any, *, min_savings_bytes: int = 64) -> dict[str, Any]:
    """Scan uncompressed exchange/objects and nominate blobs that compress strictly smaller."""
    integer(min_savings_bytes, "min_savings_bytes", 1)
    objects = system.ex.objects
    entries = objects.entries()
    compressed_keys = {key for key, _, is_comp, _ in entries if is_comp}

    candidates: list[dict] = []
    skipped_incompressible = 0
    already_compressed = len(compressed_keys)
    total_raw = 0
    total_comp = 0

    with objects.pack_session():
        for key, path, is_comp, disk_bytes in entries:
            if is_comp:
                continue

            raw = _read_ordinary_unbounded(path)
            if sha(raw) != key or len(raw) != disk_bytes:
                raise ValueError(f"corrupt object encountered during compaction plan: {key}")

            if key in compressed_keys:
                existing = objects.read(key, retained=True, expected_bytes=len(raw))
                require(existing == raw, f"conflicting raw and compressed object: {key}")

                deflate_path = objects.directory / (key + ".deflate")
                info = _verify_ordinary_file(
                    deflate_path, "ordinary compressed object file required"
                )
                comp_bytes = info.st_size
                if comp_bytes < len(raw) and len(raw) >= min_savings_bytes:
                    candidates.append(
                        {
                            "object": key,
                            "raw_bytes": len(raw),
                            "compressed_bytes": comp_bytes,
                            "saved_bytes": len(raw),
                            "already_deflated": True,
                        }
                    )
                    total_raw += len(raw)
                    total_comp += comp_bytes
                else:
                    skipped_incompressible += 1
                continue

            comp = zlib.compress(raw, level=6)
            saved = len(raw) - len(comp)
            if saved >= min_savings_bytes and len(comp) < len(raw):
                candidates.append(
                    {
                        "object": key,
                        "raw_bytes": len(raw),
                        "compressed_bytes": len(comp),
                        "saved_bytes": saved,
                        "already_deflated": False,
                    }
                )
                total_raw += len(raw)
                total_comp += len(comp)
            else:
                skipped_incompressible += 1

    reclaimable = sum(row["saved_bytes"] for row in candidates)
    body = {
        "format": COMPACT_PLAN_FORMAT,
        "instance_id": system.instance_id,
        "min_savings_bytes": min_savings_bytes,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "skipped_incompressible": skipped_incompressible,
        "already_compressed": already_compressed,
        "total_raw_bytes": total_raw,
        "total_compressed_bytes": total_comp,
        "reclaimable_bytes": reclaimable,
    }
    return dict(body, plan_sha256=sha(encode(body)))


def _preverify_compact_candidates(
    objects: Any,
    candidates: list[dict[str, Any]],
) -> list[tuple[str, Path, Path, int, int, int, bool]]:
    """Pre-verify every candidate one object at a time without retaining raw/compressed buffers."""
    seen: set[str] = set()
    verified: list[tuple[str, Path, Path, int, int, int, bool]] = []

    for row in candidates:
        exact_fields(
            row,
            {"object", "raw_bytes", "compressed_bytes", "saved_bytes", "already_deflated"},
            set(),
            "compact candidate",
        )
        require(type(row["already_deflated"]) is bool, "already_deflated must be a boolean")
        key = digest(row["object"], "object sha256")
        require(key not in seen, "duplicate object in compact plan: " + key)
        seen.add(key)

        raw_bytes = integer(row["raw_bytes"], "raw_bytes")
        comp_bytes = integer(row["compressed_bytes"], "compressed_bytes")
        saved_bytes = integer(row["saved_bytes"], "saved_bytes", 1)

        raw_path = objects.directory / key
        deflate_path = objects.directory / (key + ".deflate")
        raw = _read_ordinary_unbounded(raw_path)
        require(
            len(raw) == raw_bytes and sha(raw) == key,
            "object changed since compaction plan: " + key,
        )

        if row["already_deflated"]:
            info = _verify_ordinary_file(deflate_path, "existing compressed object required")
            require(
                info.st_size == comp_bytes
                and info.st_size < raw_bytes
                and saved_bytes == raw_bytes,
                "dual-file compressed object changed since compaction plan: " + key,
            )
            with deflate_path.open("rb") as stream:
                roundtrip = objects._decompress_bounded(stream, raw_bytes, deflate_path)
            require(
                roundtrip == raw and sha(roundtrip) == key,
                "existing .deflate verification failed: " + key,
            )
        else:
            try:
                deflate_path.lstat()
            except FileNotFoundError:
                pass
            else:
                raise ValueError("object state changed since compaction plan: " + key)

            compressed = zlib.compress(raw, level=6)
            require(len(compressed) < raw_bytes, "object no longer compresses smaller: " + key)
            require(
                len(compressed) == comp_bytes and raw_bytes - len(compressed) == saved_bytes,
                "compressed size differs from compaction plan: " + key,
            )
            require(zlib.decompress(compressed) == raw, "round-trip decompression mismatch: " + key)

        verified.append(
            (
                key,
                raw_path,
                deflate_path,
                raw_bytes,
                comp_bytes,
                saved_bytes,
                row["already_deflated"],
            )
        )

    return verified


def execute_compact_objects(
    system: Any, *, plan: dict[str, Any], expected_plan: str, reason: str
) -> dict[str, Any]:
    """Atomically replace verified uncompressed objects with .deflate copies under owner.lock."""
    _verify_plan(
        plan,
        COMPACT_PLAN_FORMAT,
        {
            "format",
            "instance_id",
            "min_savings_bytes",
            "candidates",
            "candidate_count",
            "skipped_incompressible",
            "already_compressed",
            "total_raw_bytes",
            "total_compressed_bytes",
            "reclaimable_bytes",
            "plan_sha256",
        },
        expected_plan,
        system.instance_id,
        reason,
        "compact-objects",
    )

    objects = system.ex.objects
    candidates = plan["candidates"]
    require(
        isinstance(candidates, list) and len(candidates) > 0,
        "compact-objects plan has no candidates",
    )

    # Phase 1: Fail-closed verification streaming one object at a time (O(1) peak memory).
    verified = _preverify_compact_candidates(objects, candidates)

    # Phase 2: Stream compress + atomic write + verify + unlink one object at a time; sync_dir once.
    compacted = 0
    raw_bytes_total = 0
    comp_bytes_total = 0
    saved_bytes_total = 0
    mutated = False

    try:
        with objects.pack_session():
            for (
                key,
                raw_path,
                deflate_path,
                raw_bytes,
                comp_size,
                saved_bytes,
                already_deflated,
            ) in verified:
                raw = _read_ordinary_unbounded(raw_path)
                require(
                    len(raw) == raw_bytes and sha(raw) == key,
                    "object changed during compaction: " + key,
                )

                if not already_deflated:
                    compressed = zlib.compress(raw, level=6)
                    require(
                        len(compressed) == comp_size,
                        "compressed size drifted during compaction: " + key,
                    )
                    atomic_bytes(deflate_path, compressed)
                    with deflate_path.open("rb") as stream:
                        roundtrip = objects._decompress_bounded(stream, raw_bytes, deflate_path)
                    require(
                        roundtrip == raw and sha(roundtrip) == key,
                        "written .deflate verification failed: " + key,
                    )

                raw_path.unlink()
                mutated = True
                require(
                    objects.read(key, retained=True, expected_bytes=raw_bytes) == raw,
                    "post-compaction ObjectStore.read failed: " + key,
                )

                compacted += 1
                raw_bytes_total += raw_bytes
                comp_bytes_total += comp_size
                saved_bytes_total += saved_bytes
    finally:
        if mutated:
            sync_dir(objects.directory)

    receipt = {
        "format": COMPACT_RECEIPT_FORMAT,
        "instance_id": system.instance_id,
        "plan_sha256": expected_plan,
        "reason": reason,
        "executed_at": now(),
        "compacted_objects": compacted,
        "raw_bytes": raw_bytes_total,
        "compressed_bytes": comp_bytes_total,
        "saved_bytes": saved_bytes_total,
    }
    receipt_path = _write_receipt(system, f"compact-objects-{expected_plan[:16]}.json", receipt)
    return dict(receipt, receipt_path=str(receipt_path))
