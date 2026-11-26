#!/usr/bin/env python3
"""Anti-spam container image build-gate, immutable tag planner, and registry GC planner."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(REPO_ROOT / "src"), str(REPO_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from managed_ai.platform import (
    check_ordinary_ancestors,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    strict_json_loads,
)

CONTEXT_HASH_FORMAT = "managed-ai-image-context-hash/1"
TAG_PLAN_FORMAT = "managed-ai-image-tag-plan/1"
GC_PLAN_FORMAT = "managed-ai-registry-gc-plan/1"
CATALOG_FORMAT = "managed-ai-registry-catalog/1"

_SHA_HEX_RE = re.compile(r"^[0-9a-fA-F]{12,40}$")
_CONTEXT_SHA_HEX_RE = re.compile(r"^[0-9a-fA-F]{12,64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GITOPS_PIN_RE = re.compile(r"(?:@|\bdigest['\"]?\s*:\s*['\"]?)(sha256:[0-9a-f]{64})\b")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_RELEASE_TAG_RE = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_MAIN_TAG_RE = re.compile(r"^sha-[0-9a-f]{12}$")
_PR_TAG_RE = re.compile(r"^pr-(?:[1-9][0-9]*-)?[0-9a-f]{12}$")
_LOCAL_TAG_RE = re.compile(r"^local-[0-9a-f]{12}$")
_CTX_TAG_RE = re.compile(r"^ctx-[0-9a-f]{12}$")
_REGISTRY_RE = re.compile(r"^[a-z0-9._-]+(?::[0-9]{1,5})?(?:/[a-z0-9._-]+)*$")
_IMAGE_RE = re.compile(r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*$")

_IGNORED_DIR_NAMES = frozenset({"__pycache__", ".git", ".pytest_cache", ".mypy_cache"})
_IGNORED_FILE_SUFFIXES = (".pyc", ".pyo")
_GITOPS_SCAN_SUFFIXES = frozenset({".yaml", ".yml", ".json", ".toml", ".tf", ".tfvars", ".txt"})


def _sha256_file(path: Path) -> tuple[int, str]:
    hasher = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            total += len(chunk)
            hasher.update(chunk)
    return total, hasher.hexdigest()


def _parse_iso_utc(raw: object, label: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} must be a non-empty ISO-8601 UTC timestamp")
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp for {label}: {raw}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include an explicit timezone offset")
    return parsed.astimezone(timezone.utc)


def _walk_ordinary_files(root: Path, base_dir: Path, *, suffixes: frozenset[str] | None = None) -> list[tuple[str, Path]]:
    collected: list[tuple[str, Path]] = []
    stack = [base_dir]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            ordered = sorted(entries, key=lambda item: item.name)
        for entry in ordered:
            name = entry.name
            if name in _IGNORED_DIR_NAMES or name == ".DS_Store":
                continue
            path = Path(entry.path)
            info = path.lstat()
            if is_ordinary_dir_stat(info):
                stack.append(path)
                continue
            if not is_ordinary_file_stat(info):
                raise ValueError(f"ordinary file or directory required: {path}")
            if name.endswith(_IGNORED_FILE_SUFFIXES):
                continue
            if suffixes is not None and path.suffix.lower() not in suffixes:
                continue
            rel = path.relative_to(root).as_posix()
            collected.append((rel, path))
    collected.sort(key=lambda item: item[0])
    return collected


def compute_context_hash(root: Path) -> dict:
    """Compute a deterministic cross-OS SHA-256 over container build inputs only."""
    repo_root = check_ordinary_ancestors(
        root, expect_dir=True, error_prefix="ordinary repository root required",
    ).resolve(strict=True)

    candidates: list[tuple[str, Path]] = []
    pyproject = check_ordinary_ancestors(
        repo_root / "pyproject.toml", expect_dir=False, error_prefix="ordinary pyproject.toml required",
    )
    candidates.append(("pyproject.toml", pyproject))

    containers_dir = repo_root / "containers"
    if os.path.lexists(containers_dir):
        safe_containers = check_ordinary_ancestors(
            containers_dir, expect_dir=True, error_prefix="ordinary containers directory required",
        )
        containerfile = safe_containers / "Containerfile"
        if os.path.lexists(containerfile):
            safe_cf = check_ordinary_ancestors(
                containerfile, expect_dir=False, error_prefix="ordinary Containerfile required",
            )
            candidates.append(("containers/Containerfile", safe_cf))

    pkg_dir = check_ordinary_ancestors(
        repo_root / "src/managed_ai", expect_dir=True, error_prefix="ordinary src/managed_ai directory required",
    )
    candidates.extend(_walk_ordinary_files(repo_root, pkg_dir))
    candidates.sort(key=lambda item: item[0])

    outer = hashlib.sha256()
    files: list[dict] = []
    for rel, path in candidates:
        size, digest = _sha256_file(path)
        outer.update(f"{rel}\0{size}\0{digest}\n".encode("utf-8"))
        files.append({"path": rel, "bytes": size, "sha256": digest})

    return {
        "format": CONTEXT_HASH_FORMAT,
        "context_sha256": outer.hexdigest(),
        "file_count": len(files),
        "files": files,
    }


def build_tag_plan(
    *,
    channel: str,
    git_sha: str,
    context_sha: str | None = None,
    pr_number: int | None = None,
    version: str | None = None,
    registry: str = "ghcr.io/nestharus",
    image: str = "managed-ai",
    allow_pr_push: bool = False,
    pr_ttl_days: int = 7,
) -> dict:
    """Build a deterministic immutable tag & push plan (strictly prohibiting :latest)."""
    if channel not in ("local", "pr", "main", "release"):
        raise ValueError(f"unsupported channel: {channel}")
    if not isinstance(git_sha, str) or not _SHA_HEX_RE.fullmatch(git_sha):
        raise ValueError("git_sha must be 12 to 40 hexadecimal characters")
    if context_sha is not None and (not isinstance(context_sha, str) or not _CONTEXT_SHA_HEX_RE.fullmatch(context_sha)):
        raise ValueError("context_sha must be 12 to 64 hexadecimal characters")
    if not isinstance(registry, str) or not _REGISTRY_RE.fullmatch(registry) or ".." in registry:
        raise ValueError(f"invalid registry: {registry}")
    if not isinstance(image, str) or not _IMAGE_RE.fullmatch(image) or ".." in image:
        raise ValueError(f"invalid image name: {image}")
    if type(pr_ttl_days) is not int or not (1 <= pr_ttl_days <= 90):
        raise ValueError("pr_ttl_days must be an integer between 1 and 90")
    if pr_number is not None and (type(pr_number) is not int or pr_number < 1):
        raise ValueError("pr_number must be a positive integer")

    sha_lower = git_sha.lower()
    sha12 = sha_lower[:12]
    ctx_tag = f"ctx-{context_sha.lower()[:12]}" if context_sha is not None else None

    if channel == "local":
        if allow_pr_push or version is not None:
            raise ValueError("local channel does not accept --allow-pr-push or --version")
        push_remote = False
        promotion_tags = [f"local-{sha12}"]
        promote_by = "local-only"
        ttl_days: int | None = None
    elif channel == "pr":
        if version is not None:
            raise ValueError("pr channel does not accept --version")
        if allow_pr_push and pr_number is None:
            raise ValueError("--allow-pr-push requires --pr-number")
        push_remote = bool(allow_pr_push)
        promotion_tags = [f"pr-{pr_number}-{sha12}" if pr_number is not None else f"pr-{sha12}"]
        promote_by = "none"
        ttl_days = pr_ttl_days
    elif channel == "main":
        if allow_pr_push or version is not None:
            raise ValueError("main channel does not accept --allow-pr-push or --version")
        push_remote = True
        promotion_tags = [f"sha-{sha12}"]
        promote_by = "digest"
        ttl_days = None
    else:
        if allow_pr_push:
            raise ValueError("release channel does not accept --allow-pr-push")
        if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
            raise ValueError("release channel requires a valid SemVer --version (e.g. 0.5.0)")
        push_remote = True
        promotion_tags = [f"v{version}", f"sha-{sha12}"]
        promote_by = "digest"
        ttl_days = None

    tags = list(promotion_tags)
    if channel in ("main", "release") and ctx_tag is not None:
        tags.append(ctx_tag)

    if any(tag.lower() == "latest" or ":latest" in tag.lower() for tag in tags):
        raise ValueError("mutable :latest tags are strictly prohibited")

    references = [f"{registry}/{image}:{tag}" for tag in tags]
    return {
        "format": TAG_PLAN_FORMAT,
        "channel": channel,
        "git_sha": sha_lower,
        "sha12": sha12,
        "context_tag": ctx_tag,
        "registry": registry,
        "image": image,
        "push_remote": push_remote,
        "immutable": True,
        "forbids_latest": True,
        "promote_by": promote_by,
        "ttl_days": ttl_days,
        "promotion_tags": promotion_tags,
        "tags": tags,
        "references": references,
    }


def _collect_gitops_pins(gitops_dir: Path | None) -> dict[str, list[str]]:
    if gitops_dir is None:
        return {}
    safe_dir = check_ordinary_ancestors(
        gitops_dir, expect_dir=True, error_prefix="ordinary gitops directory required",
    ).resolve(strict=True)
    pins: dict[str, set[str]] = {}
    for rel, path in _walk_ordinary_files(safe_dir, safe_dir, suffixes=_GITOPS_SCAN_SUFFIXES):
        text = path.read_bytes().decode("utf-8", errors="replace")
        for digest in _GITOPS_PIN_RE.findall(text):
            pins.setdefault(digest, set()).add(rel)
    return {digest: sorted(paths) for digest, paths in sorted(pins.items())}


def _load_validated_catalog(catalog_path: Path) -> tuple[str, list[dict]]:
    safe_catalog = check_ordinary_ancestors(
        catalog_path, expect_dir=False, error_prefix="ordinary catalog file required",
    )
    doc = strict_json_loads(safe_catalog.read_bytes(), safe_catalog, require_object=True)
    if doc.get("format") != CATALOG_FORMAT:
        raise ValueError(f"catalog format must be {CATALOG_FORMAT}")
    repository = doc.get("repository")
    if not isinstance(repository, str) or not repository.strip():
        raise ValueError("catalog repository must be a non-empty string")
    manifests_raw = doc.get("manifests")
    if not isinstance(manifests_raw, list):
        raise ValueError("catalog manifests must be a list")

    seen_digests: set[str] = set()
    manifests: list[dict] = []
    for item in manifests_raw:
        if not isinstance(item, dict):
            raise ValueError("each catalog manifest must be an object")
        digest = item.get("digest")
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise ValueError(f"invalid manifest digest: {digest!r}")
        if digest in seen_digests:
            raise ValueError(f"duplicate manifest digest in catalog: {digest}")
        seen_digests.add(digest)
        created_dt = _parse_iso_utc(item.get("created_at"), f"manifest {digest} created_at")
        tags = item.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip() for t in tags):
            raise ValueError(f"manifest {digest} tags must be a list of non-empty strings")
        if len(set(tags)) != len(tags):
            raise ValueError(f"manifest {digest} contains duplicate tags")
        manifests.append({
            "digest": digest,
            "created_at": item["created_at"],
            "created_dt": created_dt,
            "tags": sorted(tags),
        })
    return repository, manifests


def _classify_tag(
    tag: str,
    created_dt: datetime,
    *,
    newest_main_tags: set[str],
    keep_main: int,
    pr_ttl_days: int,
    now_dt: datetime,
) -> tuple[bool, str]:
    if _RELEASE_TAG_RE.fullmatch(tag):
        return True, "release-semver-tag"
    if tag in newest_main_tags:
        return True, f"newest-main-top-{keep_main}"
    if _MAIN_TAG_RE.fullmatch(tag):
        return False, f"main-beyond-keep-{keep_main}"
    if _PR_TAG_RE.fullmatch(tag):
        age_days = (now_dt - created_dt).total_seconds() / 86400.0
        if age_days < pr_ttl_days:
            return True, f"pr-within-ttl-{pr_ttl_days}d"
        return False, f"pr-expired-{pr_ttl_days}d"
    if _LOCAL_TAG_RE.fullmatch(tag):
        return False, "ephemeral-local-tag"
    if _CTX_TAG_RE.fullmatch(tag):
        return False, "context-cache-tag-expired"
    return True, "unmanaged-tag-preserved"


def _evaluate_manifest_gc(
    manifest: dict,
    *,
    gitops_pins: dict[str, list[str]],
    explicit_pins: set[str],
    newest_main_tags: set[str],
    keep_main: int,
    pr_ttl_days: int,
    now_dt: datetime,
) -> tuple[dict | None, list[dict], dict | None]:
    digest = manifest["digest"]
    reasons: list[str] = []
    durable_ctx_anchor = False
    if digest in gitops_pins:
        reasons.append("gitops-pinned:" + ",".join(gitops_pins[digest]))
        durable_ctx_anchor = True
    if digest in explicit_pins:
        reasons.append("explicit-protected-digest")
        durable_ctx_anchor = True

    expired_tags: list[tuple[str, str]] = []
    ctx_tags: list[str] = []
    for tag in manifest["tags"]:
        if _CTX_TAG_RE.fullmatch(tag):
            ctx_tags.append(tag)
            continue
        protected, reason = _classify_tag(
            tag,
            manifest["created_dt"],
            newest_main_tags=newest_main_tags,
            keep_main=keep_main,
            pr_ttl_days=pr_ttl_days,
            now_dt=now_dt,
        )
        if protected:
            reasons.append(f"tag:{tag}:{reason}")
            if reason == "release-semver-tag" or reason.startswith("newest-main-top-"):
                durable_ctx_anchor = True
        else:
            expired_tags.append((tag, reason))

    for ctx_tag in ctx_tags:
        if durable_ctx_anchor:
            reasons.append(f"tag:{ctx_tag}:context-cache-anchor-live")
        else:
            expired_tags.append((ctx_tag, "context-cache-tag-expired"))

    if reasons:
        kept = {
            "digest": digest,
            "created_at": manifest["created_at"],
            "tags": manifest["tags"],
            "reasons": reasons,
        }
        untag = [
            {"digest": digest, "tag": tag, "reason": reason, "digest_protected_by": reasons}
            for tag, reason in expired_tags
        ]
        return kept, untag, None

    prune_reasons = (
        [f"tag:{tag}:{reason}" for tag, reason in expired_tags]
        if expired_tags else ["unreferenced-dangling-manifest"]
    )
    pruned = {
        "digest": digest,
        "created_at": manifest["created_at"],
        "tags": manifest["tags"],
        "reasons": prune_reasons,
    }
    return None, [], pruned


def plan_registry_gc(
    *,
    catalog_path: Path,
    gitops_dir: Path | None = None,
    protected_digests: list[str] | tuple[str, ...] = (),
    keep_main: int = 5,
    pr_ttl_days: int = 7,
    now: str | None = None,
) -> dict:
    """Compute a dry-run registry GC plan with strict shared-digest and GitOps pin safety."""
    if type(keep_main) is not int or keep_main < 1:
        raise ValueError("keep_main must be an integer >= 1")
    if type(pr_ttl_days) is not int or pr_ttl_days < 1:
        raise ValueError("pr_ttl_days must be an integer >= 1")

    explicit_pins: set[str] = set()
    for digest in protected_digests:
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise ValueError(f"invalid --protected-digest: {digest!r}")
        explicit_pins.add(digest)

    now_dt = datetime.now(timezone.utc) if now is None else _parse_iso_utc(now, "--now")
    repository, manifests = _load_validated_catalog(catalog_path)
    gitops_pins = _collect_gitops_pins(gitops_dir)

    main_candidates = [
        (m["created_dt"], tag, m["digest"])
        for m in manifests
        for tag in m["tags"]
        if _MAIN_TAG_RE.fullmatch(tag)
    ]
    main_candidates.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    newest_main_tags = {tag for _, tag, _ in main_candidates[:keep_main]}

    keep_manifests: list[dict] = []
    prune_manifests: list[dict] = []
    untag_only: list[dict] = []

    for manifest in sorted(manifests, key=lambda item: (item["created_dt"], item["digest"])):
        kept, untag, pruned = _evaluate_manifest_gc(
            manifest,
            gitops_pins=gitops_pins,
            explicit_pins=explicit_pins,
            newest_main_tags=newest_main_tags,
            keep_main=keep_main,
            pr_ttl_days=pr_ttl_days,
            now_dt=now_dt,
        )
        if kept is not None:
            keep_manifests.append(kept)
            untag_only.extend(untag)
        if pruned is not None:
            prune_manifests.append(pruned)

    return {
        "format": GC_PLAN_FORMAT,
        "dry_run": True,
        "repository": repository,
        "now": now_dt.isoformat().replace("+00:00", "Z"),
        "keep_main": keep_main,
        "pr_ttl_days": pr_ttl_days,
        "protected_gitops_digests": gitops_pins,
        "protected_explicit_digests": sorted(explicit_pins),
        "keep_manifests": keep_manifests,
        "untag_only": untag_only,
        "prune_manifests": prune_manifests,
        "summary": {
            "total_manifests": len(manifests),
            "kept_manifests": len(keep_manifests),
            "pruned_manifests": len(prune_manifests),
            "untag_only_count": len(untag_only),
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    ctx = sub.add_parser("context-hash", allow_abbrev=False, help="Hash container build inputs deterministically")
    ctx.add_argument("--root", type=Path, default=REPO_ROOT, help="Repository root directory")

    tag = sub.add_parser("tag-plan", allow_abbrev=False, help="Compute immutable image tags and push policy")
    tag.add_argument("--channel", required=True, choices=("local", "pr", "main", "release"))
    tag.add_argument("--git-sha", required=True, help="Git commit SHA (12-40 hex chars)")
    tag.add_argument("--context-sha", default=None, help="Optional build context SHA-256 for ctx-<12> cache tag")
    tag.add_argument("--pr-number", type=int, default=None, help="Pull request number (for pr channel)")
    tag.add_argument("--version", default=None, help="SemVer version without leading v (for release channel)")
    tag.add_argument("--registry", default="ghcr.io/nestharus", help="Container registry prefix")
    tag.add_argument("--image", default="managed-ai", help="Container image name")
    tag.add_argument("--allow-pr-push", action="store_true", help="Allow isolated TTL-bounded PR push")
    tag.add_argument("--pr-ttl-days", type=int, default=7, help="TTL days for PR tags (1..90)")

    gc = sub.add_parser("plan-gc", allow_abbrev=False, help="Plan safe dry-run registry garbage collection")
    gc.add_argument("--catalog", required=True, type=Path, help="Registry catalog JSON path")
    gc.add_argument("--gitops-dir", type=Path, default=None, help="GitOps directory to scan for @sha256 pins")
    gc.add_argument("--protected-digest", action="append", default=[], help="Additional sha256:... digest to protect")
    gc.add_argument("--keep-main", type=int, default=5, help="Newest main sha-* tags to keep")
    gc.add_argument("--pr-ttl-days", type=int, default=7, help="Maximum age in days for pr-* tags")
    gc.add_argument("--now", default=None, help="Override UTC reference timestamp (ISO-8601)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "context-hash":
            payload = compute_context_hash(args.root)
        elif args.command == "tag-plan":
            payload = build_tag_plan(
                channel=args.channel,
                git_sha=args.git_sha,
                context_sha=args.context_sha,
                pr_number=args.pr_number,
                version=args.version,
                registry=args.registry,
                image=args.image,
                allow_pr_push=args.allow_pr_push,
                pr_ttl_days=args.pr_ttl_days,
            )
        else:
            payload = plan_registry_gc(
                catalog_path=args.catalog,
                gitops_dir=args.gitops_dir,
                protected_digests=args.protected_digest,
                keep_main=args.keep_main,
                pr_ttl_days=args.pr_ttl_days,
                now=args.now,
            )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"image lifecycle error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
