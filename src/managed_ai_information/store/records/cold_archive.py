"""Cold-tier compressed record archive with verified on-demand readback and strict safety guards."""

import errno
import os
import stat
import threading
import zlib
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

from managed_ai_information.core.formats import ARCHIVE_PLAN_FORMAT as PLAN_FORMAT
from managed_ai_information.core.formats import ARCHIVE_RECEIPT_FORMAT as RECEIPT_FORMAT
from managed_ai_information.core.formats import COLD_ARCHIVE_MANIFEST_FORMAT as MANIFEST_FORMAT
from managed_ai_information.core.formats import COMPOSITION_FORMAT, RESTORE_RECEIPT_FORMAT
from managed_ai_information.core.ownership import packet_id
from managed_ai_information.core.storage import (
    _OPEN_RO_NOFOLLOW,
    DEFAULT_SHARD_TARGET_BYTES,
    PackSession,
    atomic_bytes,
    encode,
    has_pack_pointer,
    is_ordinary_file_stat,
    now,
    read_json,
    sha,
    sync_dir,
    write_json,
)
from managed_ai_information.store.governance.feedback import KIND as FEEDBACK_KIND
from managed_ai_information.store.protocol.identities import (
    digest,
    exact_fields,
    require,
    strict_json,
)
from managed_ai_information.store.protocol.inputs import strings, text
from managed_ai_information.store.records.head_index import (
    build_entry,
    extract_content_record_links,
)
from managed_ai_information.store.records.hot_refs import (
    extract_hot_record_id,
    scan_attention_refs,
    scan_interpretation_refs,
    scan_packet_refs,
    scan_uncommitted_turn_refs,
)

PROTECTED_IDS = frozenset({"goal"})
PROTECTED_KINDS = frozenset({"goal", "obligation", "received-duty-reference", FEEDBACK_KIND})


def _open_archived_blob(path: Path) -> BinaryIO:
    if hasattr(os, "O_NOFOLLOW"):
        try:
            fd = os.open(path, _OPEN_RO_NOFOLLOW)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                raise ValueError(f"ordinary archived record blob required: {path}") from None
            raise

        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f"ordinary archived record blob required: {path}")
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    try:
        info = path.lstat()
    except OSError:
        raise ValueError(f"ordinary archived record blob required: {path}") from None

    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary archived record blob required: {path}")
    return path.open("rb")


def _decompress_exact(path: Path, expected_bytes: int, expected_sha256: str) -> bytes:
    """Open an ordinary deflate blob and stream-decompress within exact raw byte bounds."""
    with _open_archived_blob(path) as stream:
        decompressor = zlib.decompressobj()
        chunks: list[bytes] = []
        total = 0

        try:
            while True:
                part = stream.read(65536)
                if not part:
                    break
                remaining = (expected_bytes + 1) - total
                out = decompressor.decompress(part, remaining)
                if out:
                    chunks.append(out)
                    total += len(out)
                    if total > expected_bytes:
                        raise ValueError(f"archived record exceeds declared size: {path}")
                if decompressor.unconsumed_tail:
                    raise ValueError(f"archived record exceeds declared size: {path}")
                if decompressor.eof:
                    if decompressor.unused_data or stream.read(1):
                        raise ValueError(f"trailing bytes after archived record: {path}")
                    break

            tail = decompressor.flush((expected_bytes + 1) - total)
            if tail:
                chunks.append(tail)
                total += len(tail)
                if total > expected_bytes:
                    raise ValueError(f"archived record exceeds declared size: {path}")
            if not decompressor.eof:
                raise ValueError(f"truncated archived record: {path}")
        except zlib.error as exc:
            raise ValueError(f"corrupt archived record {path}: {exc}") from None

    raw = b"".join(chunks)
    if len(raw) != expected_bytes or sha(raw) != expected_sha256:
        raise ValueError(f"archived record integrity changed: {path}")
    return raw


class ColdArchive:
    """Verified compressed cold record store under exchange/cold-records/ (loose + shard slabs)."""

    def __init__(self, exchange_root: Path, record_format: str) -> None:
        self.directory = exchange_root / "cold-records"
        self.blobs_dir = self.directory / "records"
        self.packs_dir = self.directory / "packs"
        self.manifest_path = self.directory / "manifest.json"
        self.record_format = record_format
        self._manifest_cache: tuple[tuple[int, int, int], dict[str, Any]] | None = None
        self._active_pack_session: PackSession | None = None
        self._pack_session_depth = 0
        self._pack_session_lock = threading.RLock()

    def _has_pack_pointer(self) -> bool:
        return has_pack_pointer(self.packs_dir)

    @contextmanager
    def pack_session(self) -> Generator[PackSession | None]:
        """Share one thread-safe read-only PackSession across batched cold-archive reads."""
        with self._pack_session_lock:
            if self._active_pack_session is not None:
                self._pack_session_depth += 1
                session: PackSession | None = self._active_pack_session
            elif not self._has_pack_pointer():
                session = None
            else:
                session = PackSession(self.packs_dir)
                self._active_pack_session = session
                self._pack_session_depth = 1

        if session is None:
            yield None
            return

        try:
            yield session
        finally:
            to_close: PackSession | None = None
            with self._pack_session_lock:
                self._pack_session_depth -= 1
                if self._pack_session_depth == 0:
                    to_close = self._active_pack_session
                    self._active_pack_session = None
            if to_close is not None:
                to_close.close()

    def _load_manifest(self) -> dict | None:
        try:
            info = self.manifest_path.lstat()
        except FileNotFoundError:
            self._manifest_cache = None
            return None

        stat_key: tuple[int, int, int] | None = None
        if is_ordinary_file_stat(info):
            stat_key = (info.st_mtime_ns, info.st_size, info.st_ino)
            if self._manifest_cache is not None and self._manifest_cache[0] == stat_key:
                return self._manifest_cache[1]
        else:
            self._manifest_cache = None

        doc = read_json(self.manifest_path)
        require(doc.get("format") == MANIFEST_FORMAT, "unsupported cold archive manifest format")
        require(
            doc.get("record_format") == self.record_format, "cold archive record format differs"
        )
        records = doc.get("records")
        require(isinstance(records, dict), "cold archive manifest records must be an object")

        if stat_key is not None:
            self._manifest_cache = (stat_key, doc)
        return doc

    def entries(self) -> dict[str, dict]:
        doc = self._load_manifest()
        return dict(doc["records"]) if doc is not None else {}

    def ids(self) -> list[str]:
        return sorted(self.entries().keys())

    def has(self, ident: str) -> bool:
        try:
            packet_id(ident)
        except ValueError:
            return False

        doc = self._load_manifest()
        return doc is not None and ident in doc["records"]

    def blob_path(self, ident: str) -> Path:
        return self.blobs_dir / (packet_id(ident) + ".json.deflate")

    def read(self, ident: str) -> tuple[dict, bytes]:
        ident = packet_id(ident)
        doc = self._load_manifest()
        if doc is None or ident not in doc["records"]:
            raise FileNotFoundError(
                errno.ENOENT, os.strerror(errno.ENOENT), str(self.blob_path(ident))
            )

        entry = doc["records"][ident]
        require(isinstance(entry, dict), f"invalid cold archive entry: {ident}")

        raw_bytes = entry.get("raw_bytes")
        raw_sha256 = entry.get("raw_sha256")
        require(
            type(raw_bytes) is int and raw_bytes >= 0, f"invalid cold archive byte count: {ident}"
        )
        digest(raw_sha256, "cold archive raw_sha256")

        blob = self.blob_path(ident)
        active_session = self._active_pack_session
        if blob.exists() or blob.is_symlink():
            raw = _decompress_exact(blob, raw_bytes, raw_sha256)
        elif active_session is not None:
            packed_raw = active_session.read(raw_sha256, retained=True, expected_bytes=raw_bytes)
            if packed_raw is None:
                raise ValueError(f"archived record missing from cold archive packs: {ident}")
            raw = packed_raw
        elif self._has_pack_pointer():
            with PackSession(self.packs_dir) as session:
                packed_raw = session.read(raw_sha256, retained=True, expected_bytes=raw_bytes)
            if packed_raw is None:
                raise ValueError(f"archived record missing from cold archive packs: {ident}")
            raw = packed_raw
        else:
            raw = _decompress_exact(blob, raw_bytes, raw_sha256)

        record = strict_json(raw)
        require(
            record.get("id") == ident and record.get("format") == self.record_format,
            "archived record identity or format changed",
        )
        require(
            record.get("sha256") == sha(encode({k: v for k, v in record.items() if k != "sha256"})),
            "archived record digest changed",
        )
        require(
            record.get("revision") == entry.get("revision")
            and record.get("sha256") == entry.get("sha256"),
            "archived record head differs from cold archive manifest",
        )
        return record, raw

    def pack_blobs(self, *, shard_target_bytes: int = DEFAULT_SHARD_TARGET_BYTES) -> dict[str, Any]:
        """Pack loose cold-archived blobs into shard slabs under exchange/cold-records/packs/."""
        from managed_ai_information.store.records.pack_slab import pack_cold_archive_blobs

        return pack_cold_archive_blobs(self, shard_target_bytes=shard_target_bytes)

    def save_manifest(self, instance_id: str, records: dict[str, dict[str, Any]]) -> None:
        self._manifest_cache = None
        self.directory.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            self.manifest_path,
            {
                "format": MANIFEST_FORMAT,
                "instance_id": instance_id,
                "record_format": self.record_format,
                "records": records,
            },
        )
        self._manifest_cache = None


def _sealed_markers(system: Any) -> tuple[set[str], set[str]]:
    """Collect sealed catalogue IDs and body paths strictly from metadata (never opens bodies)."""
    sealed_ids: set[str] = set()
    sealed_paths: set[str] = set()

    for entry in system.domain_selection.entries:
        if entry.get("availability") == "sealed":
            if isinstance(entry.get("id"), str) and entry["id"]:
                sealed_ids.add(entry["id"])
            if isinstance(entry.get("body_path"), str) and entry["body_path"]:
                sealed_paths.add(entry["body_path"])

    return sealed_ids, sealed_paths


def _collect_strings(value: Any, out: set[str]) -> None:
    if isinstance(value, str):
        out.add(value)
    elif isinstance(value, list):
        for item in value:
            _collect_strings(item, out)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_strings(v, out)


def _references_sealed(
    record: dict[str, Any], sealed_ids: set[str], sealed_paths: set[str]
) -> bool:
    if not sealed_ids and not sealed_paths:
        return False

    tokens: set[str] = set()
    _collect_strings(record.get("content"), tokens)
    _collect_strings(record.get("relations"), tokens)
    _collect_strings(record.get("basis"), tokens)

    for token in tokens:
        if token in sealed_ids or token in sealed_paths:
            return True
        if any(sp in token for sp in sealed_paths):
            return True
        if any(sid in token for sid in sealed_ids):
            return True
    return False


def _active_outbound_targets(records: list[dict[str, Any]]) -> set[str]:
    targets: set[str] = set()
    for rec in records:
        if not rec.get("active", True):
            continue
        for rel in rec.get("relations", ()):
            if isinstance(rel, dict) and isinstance(rel.get("target"), str):
                targets.add(rel["target"])
        for values in extract_content_record_links(rec.get("content")).values():
            targets.update(values)
    return targets


def _external_hot_record_refs(system: Any) -> dict[str, set[str]]:
    """Find hot record IDs pinned by interpretations, packets, attention, or uncommitted turns."""
    pinned: dict[str, set[str]] = {}

    def mark_map(found: dict[str, list[dict[str, str]]], reason: str) -> None:
        for record_id in found:
            pinned.setdefault(record_id, set()).add(reason)

    interp_refs, _ = scan_interpretation_refs(system, extract_hot_record_id, fail_closed=True)
    mark_map(interp_refs, "referenced-by-interpretation")

    packet_refs, committed_packets, _ = scan_packet_refs(
        system,
        extract_hot_record_id,
        open_only=True,
        fail_closed=True,
    )
    mark_map(packet_refs, "referenced-by-open-packet")

    att_refs, _ = scan_attention_refs(system, extract_hot_record_id, fail_closed=True)
    mark_map(att_refs, "referenced-by-active-attention")

    uncom_refs, _ = scan_uncommitted_turn_refs(
        system,
        committed_packets,
        extract_hot_record_id,
        include_record_ids=True,
        fail_closed=True,
    )
    mark_map(uncom_refs, "referenced-by-uncommitted-turn")

    return pinned


def _evaluate_candidates(
    system: Any, candidate_ids: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_records = system.record_store.scan_records()
    by_id = {rec["id"]: rec for rec in all_records}
    active_targets = _active_outbound_targets(all_records)
    external_pins = _external_hot_record_refs(system)
    sealed_ids, sealed_paths = _sealed_markers(system)
    already_archived = set(system.record_store.cold_archive.ids())
    protected_ids = PROTECTED_IDS | getattr(
        system.domain_selection, "protected_record_ids", frozenset()
    )

    eligible: list[dict] = []
    ineligible: list[dict] = []

    for ident in sorted(dict.fromkeys(candidate_ids)):
        reasons: list[str] = []
        if ident in already_archived:
            reasons.append("already-archived")

        rec = by_id.get(ident)
        if rec is None:
            if not reasons:
                reasons.append("missing-hot-record")
            ineligible.append({"id": ident, "reasons": reasons})
            continue

        if ident in protected_ids:
            reasons.append("protected-governing-id")
        if rec["kind"] in PROTECTED_KINDS:
            reasons.append("protected-record-kind:" + rec["kind"])
        content = rec.get("content")
        if isinstance(content, dict) and content.get("format") == COMPOSITION_FORMAT:
            reasons.append("protected-composition-record")
        if rec.get("active", True):
            reasons.append("active-record")
        if ident in active_targets:
            reasons.append("referenced-by-active-record")
        if ident in external_pins:
            reasons.extend(sorted(external_pins[ident]))
        if _references_sealed(rec, sealed_ids, sealed_paths):
            reasons.append("references-sealed-source")

        if reasons:
            ineligible.append({"id": ident, "reasons": reasons})
        else:
            raw_bytes = system.record_store.size(ident)
            eligible.append(
                {
                    "id": ident,
                    "kind": rec["kind"],
                    "revision": rec["revision"],
                    "sha256": rec["sha256"],
                    "raw_bytes": raw_bytes,
                }
            )

    return eligible, ineligible


def plan_archive_records(
    system: Any, *, ids: list[str] | None = None, kinds: list[str] | None = None
) -> dict[str, Any]:
    """Read-only audit nominating eligible active=False cold records for compressed archival."""
    nominated: list[str] = []
    if ids:
        for ident in strings(list(ids), "ids"):
            nominated.append(packet_id(ident))

    if kinds:
        wanted_kinds = set(strings(list(kinds), "kinds"))
        summaries = system.record_store.indexed_summaries()
        if summaries is not None:
            for ident, entry in summaries.items():
                if entry["kind"] in wanted_kinds and not entry.get("active", True):
                    nominated.append(ident)
        else:
            for rec in system.record_store.scan_records():
                if rec["kind"] in wanted_kinds and not rec.get("active", True):
                    nominated.append(rec["id"])

    require(bool(nominated), "nominate at least one record --id or inactive --kind to archive")

    eligible, ineligible = _evaluate_candidates(system, nominated)
    reclaimable = sum(row["raw_bytes"] for row in eligible)
    body = {
        "format": PLAN_FORMAT,
        "instance_id": system.instance_id,
        "eligible": eligible,
        "ineligible": ineligible,
        "reclaimable_hot_bytes": reclaimable,
        "reclaimable_records": len(eligible),
    }
    return dict(body, plan_sha256=sha(encode(body)))


def _validate_archive_plan(
    system: Any, plan: dict[str, Any], expected_plan: str, reason: str
) -> list[str]:
    text(reason, "archive reason")
    digest(expected_plan, "expected plan sha256")
    exact_fields(
        plan,
        {
            "format",
            "instance_id",
            "eligible",
            "ineligible",
            "reclaimable_hot_bytes",
            "reclaimable_records",
            "plan_sha256",
        },
        set(),
        "archive-records plan",
    )
    require(plan["format"] == PLAN_FORMAT, "unsupported archive-records plan format")
    require(
        plan["instance_id"] == system.instance_id, "archive-records plan belongs to another store"
    )

    body = {k: v for k, v in plan.items() if k != "plan_sha256"}
    computed_sha = sha(encode(body))
    require(
        plan["plan_sha256"] == computed_sha and computed_sha == expected_plan,
        "archive-records plan digest differs from expected plan",
    )
    require(
        isinstance(plan["eligible"], list) and len(plan["eligible"]) > 0,
        "archive-records plan has no eligible records",
    )

    nominated_ids = [packet_id(row["id"]) for row in plan["eligible"]]
    require(len(nominated_ids) == len(set(nominated_ids)), "duplicate record IDs in archive plan")

    live_eligible, live_ineligible = _evaluate_candidates(system, nominated_ids)
    require(
        not live_ineligible,
        "records became ineligible since planning: "
        + ", ".join(f"{r['id']} ({','.join(r['reasons'])})" for r in live_ineligible),
    )

    live_by_id = {row["id"]: row for row in live_eligible}
    for planned in plan["eligible"]:
        current = live_by_id.get(planned["id"])
        require(
            current is not None
            and current["revision"] == planned["revision"]
            and current["sha256"] == planned["sha256"]
            and current["kind"] == planned["kind"],
            "planned record head changed since planning: " + planned["id"],
        )
    return nominated_ids


def _stage_archive_blobs(
    system: Any,
    archive: ColdArchive,
    planned_rows: list[dict[str, Any]],
    manifest_records: dict[str, dict[str, Any]],
    archived_at: str,
    reason: str,
) -> tuple[list[dict[str, Any]], int, int]:
    staged_reads: list[tuple[str, dict, bytes]] = []
    for planned in planned_rows:
        ident = planned["id"]
        record, raw = system.record_store.read(ident)
        require(
            record["revision"] == planned["revision"] and record["sha256"] == planned["sha256"],
            "record changed during archive read: " + ident,
        )
        staged_reads.append((ident, record, raw))

    archived_rows: list[dict] = []
    compressed_total = 0
    raw_total = 0

    for ident, record, raw in staged_reads:
        raw_sha = sha(raw)
        compressed = zlib.compress(raw, level=6)
        blob = archive.blob_path(ident)
        atomic_bytes(blob, compressed)

        verified_raw = _decompress_exact(blob, len(raw), raw_sha)
        require(verified_raw == raw, "archived blob verification failed: " + ident)

        compressed_total += len(compressed)
        raw_total += len(raw)
        basis_objects = sorted(
            {item["sha256"] for item in record.get("basis", ()) if item.get("sha256")}
        )
        manifest_records[ident] = {
            "id": ident,
            "kind": record["kind"],
            "revision": record["revision"],
            "sha256": record["sha256"],
            "active": record["active"],
            "raw_bytes": len(raw),
            "compressed_bytes": len(compressed),
            "raw_sha256": raw_sha,
            "basis_objects": basis_objects,
            "archived_at": archived_at,
            "reason": reason,
        }
        archived_rows.append(
            {
                "id": ident,
                "kind": record["kind"],
                "revision": record["revision"],
                "sha256": record["sha256"],
                "raw_bytes": len(raw),
                "compressed_bytes": len(compressed),
            }
        )

    return archived_rows, raw_total, compressed_total


def execute_archive_records(
    system: Any, *, plan: dict[str, Any], expected_plan: str, reason: str
) -> dict[str, Any]:
    """Move verified eligible cold records into exchange/cold-records/ under owner.lock."""
    nominated_ids = _validate_archive_plan(system, plan, expected_plan, reason)

    archive = system.record_store.cold_archive
    archive.directory.mkdir(parents=True, exist_ok=True)
    archive.blobs_dir.mkdir(parents=True, exist_ok=True)
    manifest_records = archive.entries()

    archived_at = now()
    archived_rows, raw_total, compressed_total = _stage_archive_blobs(
        system,
        archive,
        plan["eligible"],
        manifest_records,
        archived_at,
        reason,
    )

    archive.save_manifest(system.instance_id, manifest_records)
    sync_dir(archive.blobs_dir)

    records_dir = system.work / "records"
    for ident in nominated_ids:
        system.record_store.path(ident).unlink()
    sync_dir(records_dir)
    system.record_store.index.remove_if_present(system.record_store.format, nominated_ids)

    with archive.pack_session():
        for planned in plan["eligible"]:
            reloaded = system.record_store.show(planned["id"])
            require(
                reloaded["revision"] == planned["revision"]
                and reloaded["sha256"] == planned["sha256"],
                "post-archive readback mismatch: " + planned["id"],
            )

    receipt_dir = system.ex.path(".system/maintenance")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / f"archive-records-{expected_plan[:16]}.json"
    receipt = {
        "format": RECEIPT_FORMAT,
        "instance_id": system.instance_id,
        "plan_sha256": expected_plan,
        "reason": reason,
        "archived_at": archived_at,
        "archived_count": len(archived_rows),
        "reclaimed_hot_bytes": raw_total,
        "cold_compressed_bytes": compressed_total,
        "records": archived_rows,
    }
    write_json(receipt_path, receipt)
    return dict(receipt, receipt_path=str(receipt_path))


def restore_archived_records(system: Any, *, ids: list[str], reason: str) -> dict[str, Any]:
    """Explicitly restore cold-archived records back into work/records/ under owner.lock."""
    reason = text(reason, "restore reason")
    requested = [packet_id(ident) for ident in strings(list(ids), "ids")]
    require(bool(requested), "nominate at least one archived record --id to restore")

    archive = system.record_store.cold_archive
    manifest_records = archive.entries()

    staged: list[tuple[str, dict, bytes]] = []
    with archive.pack_session():
        for ident in requested:
            require(ident in manifest_records, "record is not in cold archive: " + ident)
            hot_path = system.record_store.path(ident)
            require(
                not hot_path.exists() and not hot_path.is_symlink(),
                "hot record path already exists: " + ident,
            )
            record, raw = archive.read(ident)
            staged.append((ident, record, raw))

    records_dir = system.work / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    restored_rows: list[dict] = []
    index_updates: dict[str, dict] = {}
    restored_bytes = 0

    for ident, record, raw in staged:
        hot_path = system.record_store.path(ident)
        atomic_bytes(hot_path, raw)
        system.record_store.receipts.issue(hot_path, record, raw)
        index_updates[ident] = build_entry(record, raw, hot_path.lstat())
        restored_bytes += len(raw)
        restored_rows.append(
            {
                "id": ident,
                "kind": record["kind"],
                "revision": record["revision"],
                "sha256": record["sha256"],
                "raw_bytes": len(raw),
            }
        )

    sync_dir(records_dir)
    if system.record_store.index.exists():
        system.record_store.index.update_if_present(system.record_store.format, index_updates)

    removed_raw_shas: set[str] = set()
    for ident in requested:
        entry = manifest_records.pop(ident, None)
        if isinstance(entry, dict) and isinstance(entry.get("raw_sha256"), str):
            removed_raw_shas.add(entry["raw_sha256"])
    archive.save_manifest(system.instance_id, manifest_records)

    for ident in requested:
        archive.blob_path(ident).unlink(missing_ok=True)
    if archive.blobs_dir.exists():
        sync_dir(archive.blobs_dir)

    if removed_raw_shas and archive._has_pack_pointer():
        still_used = {
            row["raw_sha256"]
            for row in manifest_records.values()
            if isinstance(row, dict) and isinstance(row.get("raw_sha256"), str)
        }
        dead_shas = removed_raw_shas - still_used
        if dead_shas:
            from managed_ai_information.store.records.pack_slab import gc_packed_objects

            gc_packed_objects(archive.packs_dir, dead_shas)

    for ident, record, _ in staged:
        reloaded, _ = system.record_store.read(ident)
        require(
            reloaded["revision"] == record["revision"] and reloaded["sha256"] == record["sha256"],
            "post-restore readback mismatch: " + ident,
        )
        require(not archive.has(ident), "record remained in cold archive after restore: " + ident)

    restored_at = now()
    receipt = {
        "format": RESTORE_RECEIPT_FORMAT,
        "instance_id": system.instance_id,
        "reason": reason,
        "restored_at": restored_at,
        "restored_count": len(restored_rows),
        "restored_bytes": restored_bytes,
        "records": restored_rows,
    }
    receipt_dir = system.ex.path(".system/maintenance")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_tag = sha(encode({"ids": requested, "restored_at": restored_at}))[:16]
    receipt_path = receipt_dir / f"restore-records-{receipt_tag}.json"
    write_json(receipt_path, receipt)
    return dict(receipt, receipt_path=str(receipt_path))
