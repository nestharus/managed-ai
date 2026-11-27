"""Shared pure-stdlib helpers for deterministic store ingestion and consolidation."""

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
for _entry in (str(SRC_ROOT), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai_information.core.ownership import packet_id, relative_path  # noqa: E402
from managed_ai_information.core.storage import exclusive  # noqa: E402
from managed_ai_information.platform import (  # noqa: E402
    atomic_bytes,
    check_ordinary_ancestors,
    is_ordinary_file_stat,
    sha256_file,
)
from managed_ai_information.store.query import graph_query  # noqa: E402
from managed_ai_information.store.records.record_batch import put_batch  # noqa: E402
from managed_ai_information.store.system import System  # noqa: E402

DIGEST_REL_DIR = "sources/repo-digests"
_NON_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")


def dump_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)


def canonical_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def slugify_id(prefix: str, raw: str) -> str:
    """Build a valid packet_id (<=120 chars) deterministically from prefix and raw name."""
    cleaned = _NON_ID_RE.sub("-", raw).strip("-").lower()
    if not cleaned:
        cleaned = "item"
    candidate = f"{prefix}-{cleaned}"
    if len(candidate) > 120:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        candidate = f"{candidate[:107].rstrip('-')}-{digest}"
    return packet_id(candidate)


def git_head_sha(repo_root: Path) -> str:
    """Return current HEAD commit SHA for repo_root, or 'unknown' if git is absent."""
    git_bin = shutil.which("git")
    if git_bin is None or not (repo_root / ".git").exists():
        return "unknown"
    proc = subprocess.run(  # noqa: S603
        [git_bin, "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        return "unknown"
    return proc.stdout.strip() or "unknown"


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
            root,
            expect_dir=True,
            error_prefix="ordinary source root required",
        ).resolve(strict=True)
    target = checked_root / safe_rel
    parent = target.parent
    if verified_dirs is None or parent not in verified_dirs:
        checked_parent = check_ordinary_ancestors(
            parent,
            expect_dir=True,
            error_prefix=f"ordinary source file required ({safe_rel})",
        ).resolve(strict=True)
        if not checked_parent.is_relative_to(checked_root):
            raise ValueError(f"source path escapes repository root: {safe_rel}")
        if verified_dirs is not None:
            verified_dirs.add(parent)
            cur = parent
            while cur != checked_root and cur.parent != cur:
                cur = cur.parent
                verified_dirs.add(cur)
    info = target.lstat()
    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary source file required ({safe_rel}): {target}")
    digest = sha256_file(target)
    return {"path": safe_rel, "bytes": info.st_size, "sha256": digest}


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
    system: System,
    record_id: str,
    encoded: bytes,
) -> tuple[str, bool, str]:
    """Safely write work/sources/repo-digests/<record_id>.json with symlink/ancestor guards."""
    safe_id = packet_id(record_id)
    rel_path = f"{DIGEST_REL_DIR}/{safe_id}.json"
    checked_work = check_ordinary_ancestors(
        system.work,
        expect_dir=True,
        error_prefix="ordinary workspace directory required",
    ).resolve(strict=True)

    cur = checked_work
    for segment in DIGEST_REL_DIR.split("/"):
        cur = cur / segment
        if not cur.exists() and not cur.is_symlink():
            cur.mkdir()
        check_ordinary_ancestors(
            cur,
            expect_dir=True,
            error_prefix="ordinary digest directory required",
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
        if dest.read_bytes() == encoded:
            return rel_path, False, new_sha

    resolved_dest = checked_dir.resolve(strict=True) / f"{safe_id}.json"
    if not resolved_dest.is_relative_to(checked_work):
        raise ValueError(f"digest pin destination escapes workspace: {resolved_dest}")
    atomic_bytes(resolved_dest, encoded)
    return rel_path, True, new_sha


def write_digest_basis(
    system: System,
    record_id: str,
    *,
    source_repo: str,
    files: list[dict[str, Any]],
) -> tuple[str, bool, str]:
    """Compute and safely persist work/sources/repo-digests/<record_id>.json if changed."""
    _, encoded, _ = compute_digest_basis(record_id, source_repo=source_repo, files=files)
    return write_digest_basis_bytes(system, record_id, encoded)


def normalize_relations(relations: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    ordered: list[dict[str, str]] = []
    for item in sorted(relations, key=lambda r: (r["relation"], r["target"])):
        key = (item["relation"], item["target"])
        if key not in seen:
            seen.add(key)
            ordered.append({"relation": item["relation"], "target": item["target"]})
    return ordered


def load_existing_records(system: System) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for rec in system.records():
        records[str(rec["id"])] = rec
    return records


def record_matches_desired(
    existing: dict[str, Any] | None,
    desired: dict[str, Any],
    desired_basis_shas: dict[str, str],
) -> bool:
    """Return True if existing record matches desired kind, content, relations, active, basis."""
    if existing is None:
        return False
    if existing.get("kind") != desired["kind"]:
        return False
    if bool(existing.get("active", True)) != bool(desired.get("active", True)):
        return False
    if existing.get("content") != desired["content"]:
        return False
    if normalize_relations(existing.get("relations", [])) != normalize_relations(
        desired.get("relations", [])
    ):
        return False
    existing_basis = {
        str(item["path"]): str(item.get("sha256") or "") for item in existing.get("basis", [])
    }
    return existing_basis == desired_basis_shas


def apply_desired_records(
    system: System,
    desired_records: list[dict[str, Any]],
    basis_shas_by_id: dict[str, dict[str, str]],
    *,
    batch_prefix: str,
    pending_digests: dict[str, bytes] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Idempotently apply only added/changed records under exclusive owner.lock."""

    def _plan_ops(
        existing: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str], list[str], list[str], set[str]]:
        ops: list[dict[str, Any]] = []
        added: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
        touched_basis_paths: set[str] = set()
        for rec in sorted(desired_records, key=lambda item: item["id"]):
            rec_id = rec["id"]
            prev = existing.get(rec_id)
            if record_matches_desired(prev, rec, basis_shas_by_id.get(rec_id, {})):
                unchanged.append(rec_id)
                continue
            expected_rev = 0 if prev is None else int(prev["revision"])
            if prev is None:
                added.append(rec_id)
            else:
                updated.append(rec_id)
            basis_list = list(rec.get("basis", []))
            touched_basis_paths.update(basis_list)
            op: dict[str, Any] = {
                "id": rec_id,
                "expected_revision": expected_rev,
                "mode": "replace",
                "record": {
                    "kind": rec["kind"],
                    "content": rec["content"],
                    "relations": normalize_relations(rec.get("relations", [])),
                    "basis": basis_list,
                    "active": bool(rec.get("active", True)),
                },
            }
            if prev is not None:
                op["expected_sha256"] = str(prev["sha256"])
            ops.append(op)
        return ops, added, updated, unchanged, touched_basis_paths

    if dry_run:
        existing = load_existing_records(system)
        _, added_ids, updated_ids, unchanged_ids, _ = _plan_ops(existing)
        return {
            "applied": False,
            "dry_run": True,
            "added": added_ids,
            "updated": updated_ids,
            "unchanged_count": len(unchanged_ids),
            "written_count": 0,
            "batch_id": None,
            "lint_anomalies": None,
        }

    with exclusive(system.ex.root / "owner.lock"):
        existing = load_existing_records(system)
        operations, added_ids, updated_ids, unchanged_ids, touched_basis = _plan_ops(existing)
        if not operations:
            return {
                "applied": False,
                "dry_run": False,
                "added": added_ids,
                "updated": updated_ids,
                "unchanged_count": len(unchanged_ids),
                "written_count": 0,
                "batch_id": None,
                "lint_anomalies": None,
            }

        if pending_digests:
            for digest_id, encoded in sorted(pending_digests.items()):
                rel_path = f"{DIGEST_REL_DIR}/{packet_id(digest_id)}.json"
                if rel_path in touched_basis:
                    write_digest_basis_bytes(system, digest_id, encoded)

        batch_hash = hashlib.sha256(canonical_bytes(operations)).hexdigest()[:12]
        batch_id = slugify_id(batch_prefix, batch_hash)
        spec = {
            "format": "managed-ai-batch-update/1",
            "id": batch_id,
            "operations": operations,
        }
        put_batch(system, spec)
        system.reindex()
        lint_report = graph_query.lint(system)

    return {
        "applied": True,
        "dry_run": False,
        "added": added_ids,
        "updated": updated_ids,
        "unchanged_count": len(unchanged_ids),
        "written_count": len(operations),
        "batch_id": batch_id,
        "lint_anomalies": int(lint_report.get("anomaly_count", 0)),
    }
