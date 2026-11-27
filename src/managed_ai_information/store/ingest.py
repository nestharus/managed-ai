"""Domain-neutral pure-stdlib helpers for deterministic store ingestion and digest pinning."""

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from managed_ai_information.core.ownership import packet_id, relative_path
from managed_ai_information.core.storage import (
    MAX_BYTES,
    OPEN_RO_NOFOLLOW,
    atomic_bytes,
    check_ordinary_ancestors,
    exclusive,
    is_ordinary_file_stat,
)

DIGEST_REL_DIR = "sources/repo-digests"
DEFAULT_CHUNK_SIZE = 200
DEFAULT_MAX_BATCH_BYTES = MAX_BYTES - 131_072
INGEST_CHECKPOINT_FORMAT = "managed-ai-ingest-checkpoint/1"
_BATCH_ENVELOPE_OVERHEAD = 512
_NON_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")
_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
_PROVENANCE_SELF_LOOPS = frozenset({"revises native navigation from"})

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_MAX_BATCH_BYTES",
    "DIGEST_REL_DIR",
    "INGEST_CHECKPOINT_FORMAT",
    "apply_desired_records",
    "apply_desired_records_chunked",
    "canonical_bytes",
    "compute_digest_basis",
    "dump_json",
    "file_digest_entry",
    "git_head_sha",
    "load_existing_records",
    "normalize_relations",
    "partition_ingest_operations",
    "record_matches_desired",
    "slugify_id",
    "write_digest_basis",
    "write_digest_basis_bytes",
]


def dump_json(payload: dict[str, Any]) -> str:
    """Render a mapping as deterministic, strict UTF-8 JSON (rejecting NaN/Infinity)."""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)


def canonical_bytes(payload: Any) -> bytes:
    """Encode payload as deterministic UTF-8 JSON bytes with a trailing LF."""
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def slugify_id(prefix: str, raw: str) -> str:
    """Build a valid packet_id (<=120 chars) deterministically from prefix and raw name."""
    cleaned = _NON_ID_RE.sub("-", raw).strip("-").lower() or "item"
    candidate = f"{prefix}-{cleaned}"
    if len(candidate) > 120:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        candidate = f"{candidate[:107].rstrip('-')}-{digest}"
    return packet_id(candidate)


def git_head_sha(repo_root: Path) -> str:
    """Return current HEAD commit SHA for repo_root, or 'unknown' if git or repo is absent."""
    git_bin = shutil.which("git")
    if git_bin is None or not (repo_root / ".git").exists():
        return "unknown"
    try:
        proc = subprocess.run(  # noqa: S603
            [git_bin, "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "unknown"
    return (proc.stdout.strip() or "unknown") if proc.returncode == 0 else "unknown"


def _stream_sha256_ordinary(target: Path, safe_rel: str) -> tuple[int, str]:
    if hasattr(os, "O_NOFOLLOW"):
        try:
            fd = os.open(target, OPEN_RO_NOFOLLOW)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                raise ValueError(f"ordinary source file required ({safe_rel}): {target}") from None
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"ordinary source file required ({safe_rel}): {target}")
            with os.fdopen(fd, "rb", closefd=False) as fh:
                digest = hashlib.file_digest(fh, "sha256").hexdigest()
            return info.st_size, digest
        finally:
            os.close(fd)

    info = target.lstat()
    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary source file required ({safe_rel}): {target}")
    with target.open("rb") as fh:
        digest = hashlib.file_digest(fh, "sha256").hexdigest()
    return info.st_size, digest


def file_digest_entry(
    root: Path,
    rel_path: str,
    *,
    checked_root: Path | None = None,
    verified_dirs: set[Path] | None = None,
) -> dict[str, Any]:
    """Stream-hash one ordinary source file strictly contained within root."""
    safe_rel = relative_path(rel_path)
    if checked_root is None:
        checked_root = check_ordinary_ancestors(
            root, expect_dir=True, error_prefix="ordinary source root required"
        ).resolve(strict=True)
    target = checked_root / safe_rel
    parent = target.parent
    if verified_dirs is None or parent not in verified_dirs:
        checked_parent = check_ordinary_ancestors(
            parent, expect_dir=True, error_prefix=f"ordinary source file required ({safe_rel})"
        ).resolve(strict=True)
        if not checked_parent.is_relative_to(checked_root):
            raise ValueError(f"source path escapes repository root: {safe_rel}")
        if verified_dirs is not None:
            cur = parent
            while cur != checked_root and cur.parent != cur:
                verified_dirs.add(cur)
                cur = cur.parent
            verified_dirs.add(checked_root)
    size, digest = _stream_sha256_ordinary(target, safe_rel)
    return {"path": safe_rel, "bytes": size, "sha256": digest}


def compute_digest_basis(
    record_id: str,
    *,
    source_repo: str,
    files: list[dict[str, Any]],
) -> tuple[str, bytes, str]:
    """Build deterministic digest-pin JSON bytes and SHA-256 purely in memory (zero disk writes)."""
    safe_id = packet_id(record_id)
    rel_path = f"{DIGEST_REL_DIR}/{safe_id}.json"
    payload = {
        "format": "managed-ai-source-digest-pin/1",
        "record_id": safe_id,
        "source_repo": source_repo,
        "files": sorted(files, key=lambda item: str(item["path"])),
    }
    encoded = canonical_bytes(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    return rel_path, encoded, digest


def write_digest_basis_bytes(
    system: Any,
    record_id: str,
    encoded: bytes,
) -> tuple[str, bool, str]:
    """Safely write work/sources/repo-digests/<record_id>.json with symlink/ancestor guards."""
    safe_id = packet_id(record_id)
    rel_path = f"{DIGEST_REL_DIR}/{safe_id}.json"
    checked_work = check_ordinary_ancestors(
        system.work, expect_dir=True, error_prefix="ordinary workspace directory required"
    ).resolve(strict=True)

    cur = checked_work
    for segment in DIGEST_REL_DIR.split("/"):
        cur = cur / segment
        if not cur.exists() and not cur.is_symlink():
            cur.mkdir()
        check_ordinary_ancestors(
            cur, expect_dir=True, error_prefix="ordinary digest directory required"
        )

    checked_dir = check_ordinary_ancestors(
        checked_work / DIGEST_REL_DIR,
        expect_dir=True,
        error_prefix="ordinary digest directory required",
    )
    dest = checked_dir / f"{safe_id}.json"
    new_sha = hashlib.sha256(encoded).hexdigest()
    try:
        info = dest.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if not is_ordinary_file_stat(info):
            raise ValueError(f"ordinary non-symlink digest pin file required: {dest}")
        if info.st_size == len(encoded) and dest.read_bytes() == encoded:
            return rel_path, False, new_sha

    resolved_dest = checked_dir.resolve(strict=True) / f"{safe_id}.json"
    if not resolved_dest.is_relative_to(checked_work):
        raise ValueError(f"digest pin destination escapes workspace: {resolved_dest}")
    atomic_bytes(resolved_dest, encoded)
    return rel_path, True, new_sha


def write_digest_basis(
    system: Any,
    record_id: str,
    *,
    source_repo: str,
    files: list[dict[str, Any]],
) -> tuple[str, bool, str]:
    """Compute and safely persist work/sources/repo-digests/<record_id>.json if changed."""
    _, encoded, _ = compute_digest_basis(record_id, source_repo=source_repo, files=files)
    return write_digest_basis_bytes(system, record_id, encoded)


def normalize_relations(relations: list[dict[str, str]]) -> list[dict[str, str]]:
    """Deduplicate and deterministically sort record relation entries."""
    seen: set[tuple[str, str]] = set()
    ordered: list[dict[str, str]] = []
    for item in sorted(relations, key=lambda r: (r["relation"], r["target"])):
        key = (item["relation"], item["target"])
        if key not in seen:
            seen.add(key)
            ordered.append({"relation": item["relation"], "target": item["target"]})
    return ordered


def load_existing_records(system: Any) -> dict[str, dict[str, Any]]:
    """Return a map of record ID -> current record body from the store."""
    return {str(rec["id"]): rec for rec in system.records()}


def _require_bool_active(record: Mapping[str, Any]) -> bool:
    val = record.get("active", True)
    if type(val) is not bool:
        raise ValueError("record active field must be a boolean")
    return val


def record_matches_desired(
    existing: dict[str, Any] | None,
    desired: dict[str, Any],
    desired_basis_shas: dict[str, str],
) -> bool:
    """Return True if existing record matches desired kind, content, relations, active, basis."""
    desired_active = _require_bool_active(desired)
    if existing is None or existing.get("kind") != desired["kind"]:
        return False
    if _require_bool_active(existing) != desired_active:
        return False
    if canonical_bytes(existing.get("content")) != canonical_bytes(desired["content"]):
        return False
    if normalize_relations(existing.get("relations", [])) != normalize_relations(
        desired.get("relations", [])
    ):
        return False
    existing_basis = {
        str(item["path"]): str(item.get("sha256") or "") for item in existing.get("basis", [])
    }
    return existing_basis == desired_basis_shas


def _count_structural_anomalies(system: Any) -> int:
    records = system.records()
    cold_ids = set(system.record_store.cold_archive.ids())
    known_ids = {str(r["id"]) for r in records} | cold_ids
    anomalies = 0
    for rec in records:
        rid = str(rec["id"])
        seen: set[tuple[str, str]] = set()
        for rel in rec.get("relations", ()):
            label, tgt = str(rel["relation"]), str(rel["target"])
            key = (label, tgt)
            if key in seen:
                anomalies += 1
            seen.add(key)
            if (tgt == rid and label not in _PROVENANCE_SELF_LOOPS) or (
                _RECORD_ID_RE.match(tgt) and tgt not in known_ids
            ):
                anomalies += 1
    return anomalies


def _plan_ingest_ops(
    desired_records: Iterable[dict[str, Any]],
    basis_shas_by_id: Mapping[str, dict[str, str]],
    existing: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str], set[str]]:
    ops: list[dict[str, Any]] = []
    added: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    touched_basis: set[str] = set()
    for rec in sorted(desired_records, key=lambda item: item["id"]):
        rec_id = rec["id"]
        prev = existing.get(rec_id)
        if record_matches_desired(prev, rec, basis_shas_by_id.get(rec_id, {})):
            unchanged.append(rec_id)
            continue
        expected_rev = 0 if prev is None else int(prev["revision"])
        (added if prev is None else updated).append(rec_id)
        basis_list = list(rec.get("basis", []))
        touched_basis.update(basis_list)
        op: dict[str, Any] = {
            "id": rec_id,
            "expected_revision": expected_rev,
            "mode": "replace",
            "record": {
                "kind": rec["kind"],
                "content": rec["content"],
                "relations": normalize_relations(rec.get("relations", [])),
                "basis": basis_list,
                "active": _require_bool_active(rec),
            },
        }
        if prev is not None:
            op["expected_sha256"] = str(prev["sha256"])
        ops.append(op)
    return ops, added, updated, unchanged, touched_basis


def partition_ingest_operations(
    operations: list[dict[str, Any]],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
) -> list[list[dict[str, Any]]]:
    """Deterministically split ingest ops into count- and byte-bounded batches (< MAX_BYTES)."""
    if type(chunk_size) is not int or chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")
    if type(max_batch_bytes) is not int or not (1 <= max_batch_bytes < MAX_BYTES):
        raise ValueError(f"max_batch_bytes must be an integer in [1, {MAX_BYTES - 1}]")

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = _BATCH_ENVELOPE_OVERHEAD

    for op in operations:
        raw = canonical_bytes(op)
        op_bytes = len(raw) + 4 * (raw.count(b"\n") + 1) + 2
        if _BATCH_ENVELOPE_OVERHEAD + op_bytes >= max_batch_bytes:
            raise ValueError(
                f"single ingest operation exceeds max_batch_bytes ({max_batch_bytes}): {op['id']}"
            )
        if current and (len(current) >= chunk_size or current_bytes + op_bytes >= max_batch_bytes):
            chunks.append(current)
            current = []
            current_bytes = _BATCH_ENVELOPE_OVERHEAD
        current.append(op)
        current_bytes += op_bytes

    if current:
        chunks.append(current)
    return chunks


def _write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    expanded = path.expanduser()
    abs_target = Path(os.path.abspath(expanded))
    parent = abs_target.parent
    ancestors = [*reversed(parent.parents), parent]
    for anc in ancestors:
        if anc.exists() or anc.is_symlink():
            check_ordinary_ancestors(
                anc, expect_dir=True, error_prefix="ordinary checkpoint directory required"
            )
        else:
            anc.mkdir()
            check_ordinary_ancestors(
                anc, expect_dir=True, error_prefix="ordinary checkpoint directory required"
            )
    checked_parent = check_ordinary_ancestors(
        parent, expect_dir=True, error_prefix="ordinary checkpoint directory required"
    )
    dest = checked_parent / abs_target.name
    try:
        info = dest.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary non-symlink checkpoint file required: {dest}")
    atomic_bytes(dest, canonical_bytes(payload))


def _write_chunk_digests(
    system: Any,
    chunk_ops: list[dict[str, Any]],
    pending_digests: Mapping[str, bytes] | None,
) -> None:
    if not pending_digests:
        return
    chunk_basis: set[str] = set()
    for op in chunk_ops:
        chunk_basis.update(op["record"].get("basis", ()))
    if not chunk_basis:
        return
    for digest_id, encoded in sorted(pending_digests.items()):
        rel_path = f"{DIGEST_REL_DIR}/{packet_id(digest_id)}.json"
        if rel_path in chunk_basis:
            write_digest_basis_bytes(system, digest_id, encoded)


def apply_desired_records_chunked(
    system: Any,
    desired_records: Iterable[dict[str, Any]],
    basis_shas_by_id: Mapping[str, dict[str, str]],
    *,
    batch_prefix: str,
    pending_digests: Mapping[str, bytes] | None = None,
    dry_run: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
    checkpoint_path: Path | str | None = None,
    on_chunk: Callable[[dict[str, Any]], None] | None = None,
    lint_fn: Callable[[Any], Mapping[str, Any]] | None = None,
    skip_lint: bool = False,
) -> dict[str, Any]:
    """Idempotently apply large desired-record sets in count- and byte-bounded batches."""
    records_list = list(desired_records)
    if dry_run:
        existing = load_existing_records(system)
        ops, added_ids, updated_ids, unchanged_ids, _ = _plan_ingest_ops(
            records_list, basis_shas_by_id, existing
        )
        chunks = partition_ingest_operations(
            ops, chunk_size=chunk_size, max_batch_bytes=max_batch_bytes
        )
        return {
            "applied": False,
            "dry_run": True,
            "added": added_ids,
            "updated": updated_ids,
            "unchanged_count": len(unchanged_ids),
            "written_count": 0,
            "chunk_count": len(chunks),
            "chunk_size": chunk_size,
            "max_batch_bytes": max_batch_bytes,
            "batch_ids": [],
            "batch_id": None,
            "lint_anomalies": None,
        }

    ckpt = Path(checkpoint_path) if checkpoint_path is not None else None
    batch_ids: list[str] = []
    written_total = 0

    with exclusive(system.ex.root / "owner.lock"):
        existing = load_existing_records(system)
        operations, added_ids, updated_ids, unchanged_ids, _ = _plan_ingest_ops(
            records_list, basis_shas_by_id, existing
        )
        chunks = partition_ingest_operations(
            operations, chunk_size=chunk_size, max_batch_bytes=max_batch_bytes
        )
        if not chunks:
            return {
                "applied": False,
                "dry_run": False,
                "added": added_ids,
                "updated": updated_ids,
                "unchanged_count": len(unchanged_ids),
                "written_count": 0,
                "chunk_count": 0,
                "chunk_size": chunk_size,
                "max_batch_bytes": max_batch_bytes,
                "batch_ids": [],
                "batch_id": None,
                "lint_anomalies": None,
            }

        total_chunks = len(chunks)
        try:
            for idx, chunk_ops in enumerate(chunks, 1):
                _write_chunk_digests(system, chunk_ops, pending_digests)
                chunk_hash = hashlib.sha256(canonical_bytes(chunk_ops)).hexdigest()[:12]
                prefix = batch_prefix if total_chunks == 1 else f"{batch_prefix}-c{idx:04d}"
                batch_id = slugify_id(prefix, chunk_hash)
                spec: dict[str, Any] = {
                    "format": "managed-ai-batch-update/1",
                    "id": batch_id,
                    "operations": chunk_ops,
                }
                system.put_batch(spec)
                batch_ids.append(batch_id)
                written_total += len(chunk_ops)
                progress = {
                    "format": INGEST_CHECKPOINT_FORMAT,
                    "batch_prefix": batch_prefix,
                    "chunk_index": idx,
                    "total_chunks": total_chunks,
                    "batch_id": batch_id,
                    "chunk_written_count": len(chunk_ops),
                    "cumulative_written_count": written_total,
                    "complete": idx == total_chunks,
                }
                if ckpt is not None:
                    _write_checkpoint(ckpt, progress)
                if on_chunk is not None:
                    on_chunk(progress)
        finally:
            if written_total > 0:
                system.reindex()
        anomalies: int | None
        if skip_lint:
            anomalies = None
        elif lint_fn is not None:
            anomalies = int(lint_fn(system).get("anomaly_count", 0))
        else:
            anomalies = _count_structural_anomalies(system)

    return {
        "applied": True,
        "dry_run": False,
        "added": added_ids,
        "updated": updated_ids,
        "unchanged_count": len(unchanged_ids),
        "written_count": written_total,
        "chunk_count": len(chunks),
        "chunk_size": chunk_size,
        "max_batch_bytes": max_batch_bytes,
        "batch_ids": batch_ids,
        "batch_id": batch_ids[-1] if batch_ids else None,
        "lint_anomalies": anomalies,
    }


def apply_desired_records(
    system: Any,
    desired_records: list[dict[str, Any]],
    basis_shas_by_id: dict[str, dict[str, str]],
    *,
    batch_prefix: str,
    pending_digests: dict[str, bytes] | None = None,
    dry_run: bool = False,
    lint_fn: Callable[[Any], Mapping[str, Any]] | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
    checkpoint_path: Path | str | None = None,
    on_chunk: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Idempotently apply added/changed records under exclusive owner.lock with safe chunking."""
    return apply_desired_records_chunked(
        system,
        desired_records,
        basis_shas_by_id,
        batch_prefix=batch_prefix,
        pending_digests=pending_digests,
        dry_run=dry_run,
        chunk_size=chunk_size,
        max_batch_bytes=max_batch_bytes,
        checkpoint_path=checkpoint_path,
        on_chunk=on_chunk,
        lint_fn=lint_fn,
    )
