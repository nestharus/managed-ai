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

from managed_ai_information.core.ownership import packet_id  # noqa: E402
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


def file_digest_entry(root: Path, rel_path: str) -> dict[str, Any]:
    """Stream-hash one ordinary source file relative to root."""
    target = check_ordinary_ancestors(
        root / rel_path,
        expect_dir=False,
        error_prefix=f"ordinary source file required ({rel_path})",
    )
    info = target.lstat()
    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary source file required ({rel_path}): {target}")
    digest = sha256_file(target)
    return {"path": rel_path, "bytes": info.st_size, "sha256": digest}


def write_digest_basis(
    system: System,
    record_id: str,
    *,
    source_repo: str,
    files: list[dict[str, Any]],
) -> tuple[str, bool, str]:
    """Write work/sources/repo-digests/<record_id>.json only if content changed.

    Returns (workspace_rel_path, changed, file_sha256).
    """
    rel_path = f"{DIGEST_REL_DIR}/{record_id}.json"
    dest = system.work / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "managed-ai-source-digest-pin/1",
        "record_id": record_id,
        "source_repo": source_repo,
        "files": sorted(files, key=lambda item: item["path"]),
    }
    encoded = canonical_bytes(payload)
    new_sha = hashlib.sha256(encoded).hexdigest()
    if dest.is_file():
        old_bytes = dest.read_bytes()
        if old_bytes == encoded:
            return rel_path, False, new_sha
    atomic_bytes(dest, encoded)
    return rel_path, True, new_sha


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
    dry_run: bool = False,
) -> dict[str, Any]:
    """Idempotently apply only added/changed records via guarded put_batch + reindex + lint."""
    existing = load_existing_records(system)
    operations: list[dict[str, Any]] = []
    added_ids: list[str] = []
    updated_ids: list[str] = []
    unchanged_ids: list[str] = []

    for rec in sorted(desired_records, key=lambda item: item["id"]):
        rec_id = rec["id"]
        prev = existing.get(rec_id)
        if record_matches_desired(prev, rec, basis_shas_by_id.get(rec_id, {})):
            unchanged_ids.append(rec_id)
            continue
        expected_rev = 0 if prev is None else int(prev["revision"])
        if prev is None:
            added_ids.append(rec_id)
        else:
            updated_ids.append(rec_id)
        op: dict[str, Any] = {
            "id": rec_id,
            "expected_revision": expected_rev,
            "mode": "replace",
            "record": {
                "kind": rec["kind"],
                "content": rec["content"],
                "relations": normalize_relations(rec.get("relations", [])),
                "basis": list(rec.get("basis", [])),
                "active": bool(rec.get("active", True)),
            },
        }
        if prev is not None:
            op["expected_sha256"] = str(prev["sha256"])
        operations.append(op)

    if dry_run or not operations:
        return {
            "applied": False,
            "dry_run": dry_run,
            "added": added_ids,
            "updated": updated_ids,
            "unchanged_count": len(unchanged_ids),
            "written_count": 0,
            "batch_id": None,
            "lint_anomalies": None,
        }

    batch_hash = hashlib.sha256(canonical_bytes(operations)).hexdigest()[:12]
    batch_id = slugify_id(batch_prefix, batch_hash)
    spec = {
        "format": "managed-ai-batch-update/1",
        "id": batch_id,
        "operations": operations,
    }
    with exclusive(system.ex.root / "owner.lock"):
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
