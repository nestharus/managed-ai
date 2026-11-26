"""Deduplication of object-backed content-addressed workspace originals + shared maintenance helpers."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import re
import stat

from managed_ai.core.formats import DEDUP_PLAN_FORMAT, DEDUP_RECEIPT_FORMAT
from managed_ai.core.ownership import relative_path
from managed_ai.core.storage import (
    _OPEN_RO_NOFOLLOW,
    encode,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    now,
    sha,
    sync_dir,
    write_json,
)
from managed_ai.store.protocol.identities import digest, exact_fields, require
from managed_ai.store.protocol.inputs import integer, text

_CONTENT_ADDRESSED_NAME_RE = re.compile(r'^([0-9a-f]{2})/([0-9a-f]{64})\.([A-Za-z0-9._-]+)$')


def _verify_ordinary_file(path: Path, message: str) -> os.stat_result:
    """Verify path exists and is an ordinary non-symlink/non-reparse regular file."""
    try:
        info = path.lstat()
    except OSError:
        raise ValueError(f'{message}: {path}') from None

    if not is_ordinary_file_stat(info):
        raise ValueError(f'{message}: {path}')
    return info


def _read_ordinary_unbounded(path: Path) -> bytes:
    """Read an ordinary non-symlink regular file without following symlinks."""
    if hasattr(os, 'O_NOFOLLOW'):
        try:
            fd = os.open(path, _OPEN_RO_NOFOLLOW)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                raise ValueError(f'ordinary file required: {path}') from None
            raise

        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f'ordinary file required: {path}')
            stream = os.fdopen(fd, 'rb')
        except BaseException:
            os.close(fd)
            raise

        with stream:
            return stream.read()

    info = _verify_ordinary_file(path, 'ordinary file required')
    _ = info
    return path.read_bytes()


def _write_receipt(system, name: str, payload: dict) -> Path:
    receipt_dir = system.ex.path('.system/maintenance')
    receipt_dir.mkdir(parents=True, exist_ok=True)
    path = receipt_dir / name
    write_json(path, payload)
    return path


def _verify_plan(
    plan: dict,
    expected_format: str,
    required_fields: set[str],
    expected_plan: str,
    instance_id: str,
    reason: str,
    label: str,
) -> None:
    text(reason, f'{label} reason')
    digest(expected_plan, 'expected plan sha256')
    exact_fields(plan, required_fields, set(), f'{label} plan')
    require(plan['format'] == expected_format, f'unsupported {label} plan format')
    require(plan['instance_id'] == instance_id, f'{label} plan belongs to another store')

    body = {k: v for k, v in plan.items() if k != 'plan_sha256'}
    computed = sha(encode(body))
    require(
        plan['plan_sha256'] == computed and computed == expected_plan,
        f'{label} plan digest differs from expected plan',
    )


def _walk_ordinary_files(root: Path, base: Path) -> list[Path]:
    """Walk ordinary subdirectories without following symlinks or reparse points."""
    if not root.exists():
        return []

    info = root.lstat()
    if not is_ordinary_dir_stat(info):
        raise ValueError(f'ordinary directory required: {root}')

    found: list[Path] = []
    pending = [root]
    while pending:
        cur = pending.pop()
        with os.scandir(cur) as items:
            for item in items:
                child = cur / item.name
                st = item.stat(follow_symlinks=False)
                if is_ordinary_dir_stat(st):
                    pending.append(child)
                elif is_ordinary_file_stat(st):
                    found.append(child)
                else:
                    raise ValueError(f'ordinary file or directory required: {child}')

    found.sort(key=lambda p: p.relative_to(base).as_posix())
    return found


def plan_deduplicate_originals(system, *, prefix: str | None = None) -> dict:
    """Verify byte+SHA-256 parity in exchange/objects for content-addressed workspace copies."""
    if prefix is None:
        declared = getattr(system.domain_selection, 'content_addressed_basis_prefixes', ())
        require(
            len(declared) == 1,
            'supply --prefix or declare one content_addressed_basis_prefixes entry in a selected domain manifest',
        )
        prefix = declared[0]

    rel_prefix = relative_path(prefix.rstrip('/'))
    target_dir = system.ex.path(rel_prefix)
    files = _walk_ordinary_files(target_dir, system.work) if target_dir.exists() else []

    candidates: list[dict] = []
    skipped: list[dict] = []
    objects = system.ex.objects

    for path in files:
        rel = path.relative_to(system.work).as_posix()
        sub = path.relative_to(target_dir).as_posix()
        match = _CONTENT_ADDRESSED_NAME_RE.fullmatch(sub)
        if not match or match.group(1) != match.group(2)[:2]:
            skipped.append({'path': rel, 'reason': 'not-content-addressed-path'})
            continue

        expected_sha = match.group(2)
        raw = _read_ordinary_unbounded(path)
        if sha(raw) != expected_sha:
            skipped.append({'path': rel, 'reason': 'workspace-digest-mismatch'})
            continue

        if not objects.has(expected_sha):
            skipped.append({'path': rel, 'reason': 'missing-exchange-object'})
            continue

        try:
            retained = objects.read(expected_sha, retained=True, expected_bytes=len(raw))
        except (OSError, ValueError) as exc:
            skipped.append({'path': rel, 'reason': f'exchange-object-unreadable: {exc}'})
            continue

        if retained != raw:
            skipped.append({'path': rel, 'reason': 'exchange-object-bytes-mismatch'})
            continue

        candidates.append({'path': rel, 'sha256': expected_sha, 'bytes': len(raw)})

    reclaimable = sum(row['bytes'] for row in candidates)
    body = {
        'format': DEDUP_PLAN_FORMAT,
        'instance_id': system.instance_id,
        'prefix': rel_prefix,
        'candidates': candidates,
        'skipped': skipped,
        'reclaimable_files': len(candidates),
        'reclaimable_bytes': reclaimable,
    }
    return dict(body, plan_sha256=sha(encode(body)))


def _verify_dedup_candidates(system, rel_prefix: str, candidates: list[dict]) -> list[tuple[Path, dict]]:
    objects = system.ex.objects
    verified: list[tuple[Path, dict]] = []
    seen: set[str] = set()

    for row in candidates:
        exact_fields(row, {'path', 'sha256', 'bytes'}, set(), 'deduplicate candidate')
        rel = relative_path(row['path'])
        require(rel not in seen, 'duplicate candidate path in plan: ' + rel)
        seen.add(rel)

        require(rel.startswith(rel_prefix + '/'), 'candidate outside declared prefix: ' + rel)
        sub = rel[len(rel_prefix) + 1:]
        match = _CONTENT_ADDRESSED_NAME_RE.fullmatch(sub)
        require(
            match is not None and match.group(1) == match.group(2)[:2] and match.group(2) == row['sha256'],
            'candidate path is not content-addressed to its declared digest: ' + rel,
        )

        digest(row['sha256'], 'candidate sha256')
        integer(row['bytes'], 'candidate bytes')
        path = system.ex.path(rel)
        raw = _read_ordinary_unbounded(path)
        require(
            len(raw) == row['bytes'] and sha(raw) == row['sha256'],
            'workspace original changed since planning: ' + rel,
        )

        retained = objects.read(row['sha256'], retained=True, expected_bytes=row['bytes'])
        require(retained == raw, 'exchange object parity mismatch: ' + rel)
        verified.append((path, row))

    return verified


def execute_deduplicate_originals(system, *, plan: dict, expected_plan: str, reason: str) -> dict:
    """Re-verify 100% parity under owner.lock, then remove duplicate workspace copies."""
    _verify_plan(
        plan,
        DEDUP_PLAN_FORMAT,
        {
            'format', 'instance_id', 'prefix', 'candidates', 'skipped',
            'reclaimable_files', 'reclaimable_bytes', 'plan_sha256',
        },
        expected_plan,
        system.instance_id,
        reason,
        'deduplicate-originals',
    )

    rel_prefix = relative_path(plan['prefix'])
    candidates = plan['candidates']
    require(
        isinstance(candidates, list) and len(candidates) > 0,
        'deduplicate-originals plan has no candidates',
    )

    verified = _verify_dedup_candidates(system, rel_prefix, candidates)

    touched_dirs: set[Path] = set()
    reclaimed_bytes = 0
    for path, row in verified:
        path.unlink()
        touched_dirs.add(path.parent)
        reclaimed_bytes += row['bytes']

    base_dir = system.ex.path(rel_prefix)
    for directory in sorted(touched_dirs, key=lambda p: len(p.parts), reverse=True):
        if directory.exists():
            sync_dir(directory)
            if directory != base_dir and not any(directory.iterdir()):
                directory.rmdir()

    if base_dir.exists():
        sync_dir(base_dir)

    receipt = {
        'format': DEDUP_RECEIPT_FORMAT,
        'instance_id': system.instance_id,
        'plan_sha256': expected_plan,
        'prefix': rel_prefix,
        'reason': reason,
        'executed_at': now(),
        'removed_files': len(verified),
        'reclaimed_bytes': reclaimed_bytes,
    }
    receipt_path = _write_receipt(system, f'deduplicate-originals-{expected_plan[:16]}.json', receipt)
    return dict(receipt, receipt_path=str(receipt_path))
