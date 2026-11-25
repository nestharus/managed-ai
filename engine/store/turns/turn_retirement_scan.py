"""Read-only structural reference scanners for turn working-copy retirement."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import os
from pathlib import Path
import stat
from typing import Any

from core.storage import (
    _OPEN_RO_NOFOLLOW,
    encode,
    is_ordinary_file_stat,
    is_reparse_or_symlink,
    sha,
)
from store.protocol.identities import require, strict_json
from store.records.hot_refs import (
    extract_turn_ref as _extract_referenced_turn,
    scan_attention_refs,
    scan_interpretation_refs,
    scan_packet_refs,
    scan_uncommitted_turn_refs,
)


def _scan_turn_directory(turn_dir: Path) -> tuple[int, int, str, list[str], dict[str, str], list[str]]:
    """Inspect a working turn directory without following symlinks or reparse points.

    Returns (file_count, total_bytes, inventory_sha256, structural_issues, file_hashes, dir_paths).
    """
    issues: list[str] = []
    entries: list[dict[str, Any]] = []
    file_hashes: dict[str, str] = {}
    dir_paths: list[str] = []
    file_count = 0
    total_bytes = 0

    def walk_dir(current: Path, rel_prefix: str) -> None:
        nonlocal file_count, total_bytes
        for child in sorted(current.iterdir(), key=lambda p: p.name):
            rel = f'{rel_prefix}/{child.name}' if rel_prefix else child.name
            info = child.lstat()
            mode = info.st_mode
            if is_reparse_or_symlink(info):
                issues.append(f'symlink:{rel}')
                continue
            if stat.S_ISDIR(mode):
                dir_paths.append(rel)
                entries.append({'path': rel, 'type': 'dir'})
                walk_dir(child, rel)
            elif stat.S_ISREG(mode):
                ctx = (open(child, 'rb', opener=lambda p, flags: os.open(p, _OPEN_RO_NOFOLLOW))
                       if hasattr(os, 'O_NOFOLLOW') else child.open('rb'))
                with ctx as stream:
                    finfo = os.fstat(stream.fileno())
                    if not stat.S_ISREG(finfo.st_mode):
                        issues.append(f'non_regular:{rel}')
                        continue
                    file_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
                    size = finfo.st_size
                file_count += 1
                total_bytes += size
                file_hashes[rel] = file_sha
                entries.append({'path': rel, 'type': 'file', 'bytes': size, 'sha256': file_sha})
            else:
                issues.append(f'special:{rel}')

    walk_dir(turn_dir, '')
    return file_count, total_bytes, sha(encode(entries)), issues, file_hashes, dir_paths


def _collect_record_references(system, candidate_set: set[str]) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Scan live work/records/*.json for structural references to any candidate turn working dir."""
    refs: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    if not candidate_set:
        return refs, errors
    records_dir = system.ex.path('records')
    if not records_dir.exists():
        return refs, errors
    require(not records_dir.is_symlink() and records_dir.is_dir(),
            'ordinary records directory required')
    filenames = sorted(p.name for p in records_dir.iterdir()
                       if p.name.endswith('.json') and not p.name.startswith('.'))
    if not filenames:
        return refs, errors

    marker = b'.system'
    unicode_ascii_escape = b'\\u00'

    def inspect_chunk(names: list[str], dir_fd: int | None) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
        found: list[tuple[str, dict[str, Any]]] = []
        chunk_errors: list[str] = []
        for name in names:
            full = records_dir / name
            if dir_fd is not None and hasattr(os, 'O_NOFOLLOW'):
                try:
                    fd = os.open(name, _OPEN_RO_NOFOLLOW, dir_fd=dir_fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                        raise ValueError(f'ordinary record file required: {full}') from None
                    chunk_errors.append(f'unreadable record {name}: {exc}')
                    continue
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise ValueError(f'ordinary record file required: {full}')
                    stream = os.fdopen(fd, 'rb')
                except BaseException:
                    os.close(fd)
                    raise
                with stream:
                    raw = stream.read()
            else:
                info = full.lstat()
                require(is_ordinary_file_stat(info), f'ordinary record file required: {full}')
                raw = full.read_bytes()
            if marker not in raw and unicode_ascii_escape not in raw:
                continue
            try:
                record = strict_json(raw)
            except (ValueError, TypeError) as exc:
                chunk_errors.append(f'unreadable record {name}: {exc}')
                continue
            rec_id = record.get('id', full.stem)
            for item in record.get('basis', []):
                if isinstance(item, dict):
                    path = item.get('path')
                    target_turn = _extract_referenced_turn(path)
                    if target_turn in candidate_set:
                        found.append((target_turn, {'record': rec_id, 'field': 'basis', 'path': path}))
            for rel in record.get('relations', []):
                if isinstance(rel, dict):
                    for rel_field in ('target', 'origin'):
                        val = rel.get(rel_field)
                        target_turn = _extract_referenced_turn(val)
                        if target_turn in candidate_set:
                            found.append((target_turn, {
                                'record': rec_id,
                                'field': f'relations.{rel_field}',
                                'path': val,
                            }))
                    aliases = rel.get('aliases')
                    if isinstance(aliases, list):
                        for alias in aliases:
                            target_turn = _extract_referenced_turn(alias)
                            if target_turn in candidate_set:
                                found.append((target_turn, {
                                    'record': rec_id,
                                    'field': 'relations.aliases',
                                    'path': alias,
                                }))
            content = record.get('content')
            if isinstance(content, dict):
                origin = content.get('origin')
                target_turn = _extract_referenced_turn(origin)
                if target_turn in candidate_set:
                    found.append((target_turn, {
                        'record': rec_id,
                        'field': 'content.origin',
                        'path': origin,
                    }))
                obligations = content.get('obligations')
                if isinstance(obligations, list):
                    for ob in obligations:
                        if isinstance(ob, dict) and isinstance(ob.get('evidence'), list):
                            for ev in ob['evidence']:
                                target_turn = _extract_referenced_turn(ev)
                                if target_turn in candidate_set:
                                    found.append((target_turn, {
                                        'record': rec_id,
                                        'field': 'content.obligations.evidence',
                                        'path': ev,
                                    }))
                if isinstance(content.get('evidence'), list):
                    for ev in content['evidence']:
                        target_turn = _extract_referenced_turn(ev)
                        if target_turn in candidate_set:
                            found.append((target_turn, {
                                'record': rec_id,
                                'field': 'content.evidence',
                                'path': ev,
                            }))
        return found, chunk_errors

    if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
        with system.ex.open_workspace_dir('records') as records_fd:
            if len(filenames) >= 64:
                workers = min(8, len(filenames))
                chunk_size = (len(filenames) + workers - 1) // workers
                chunks = [filenames[i:i + chunk_size] for i in range(0, len(filenames), chunk_size)]
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for batch, chunk_errors in pool.map(lambda c: inspect_chunk(c, records_fd), chunks):
                        for target_turn, ref in batch:
                            refs.setdefault(target_turn, []).append(ref)
                        errors.extend(chunk_errors)
            else:
                batch, chunk_errors = inspect_chunk(filenames, records_fd)
                for target_turn, ref in batch:
                    refs.setdefault(target_turn, []).append(ref)
                errors.extend(chunk_errors)
    else:
        batch, chunk_errors = inspect_chunk(filenames, None)
        for target_turn, ref in batch:
            refs.setdefault(target_turn, []).append(ref)
        errors.extend(chunk_errors)

    for turn_refs in refs.values():
        turn_refs.sort(key=lambda r: (r['record'], r['field'], r['path']))
    errors.sort()
    return refs, errors


def _collect_cross_packet_references(system, candidate_set: set[str]) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    """Scan all exchange/packets/*/request.json (including staged .sealed-*/.prepare-*) for cross-turn refs."""
    if not candidate_set:
        return {}, []
    all_refs, _, scan_errors = scan_packet_refs(
        system,
        _extract_referenced_turn,
        open_only=False,
        skip_self_packet=True,
        fail_closed=False,
    )
    return {turn: rows for turn, rows in all_refs.items() if turn in candidate_set}, scan_errors


def _collect_attention_references(system, candidate_set: set[str]) -> tuple[dict[str, list[dict[str, str]]], str | None]:
    """Scan active native Attention field, item basis, and latest assessment products for working-dir refs."""
    if not candidate_set:
        return {}, None
    all_refs, err = scan_attention_refs(system, _extract_referenced_turn, fail_closed=False)
    return {turn: rows for turn, rows in all_refs.items() if turn in candidate_set}, err


def _collect_interpretation_references(system, candidate_set: set[str]) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    """Scan work/interpretations/*.json evidence paths (snapshot dicts or strings) for working-dir refs."""
    if not candidate_set:
        return {}, []
    all_refs, errors = scan_interpretation_refs(system, _extract_referenced_turn, fail_closed=False)
    return {turn: rows for turn, rows in all_refs.items() if turn in candidate_set}, errors


def _collect_uncommitted_turn_references(
    system,
    turns_root: Path,
    candidate_set: set[str],
) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    """Check active uncommitted turn staging requests under work/.system/turns/ for references."""
    if not candidate_set or not turns_root.is_dir():
        return {}, []
    packets_root = system.ex.root / 'packets'
    committed = (
        {p.name for p in packets_root.iterdir() if not p.name.startswith('.')}
        if packets_root.is_dir() else set()
    )
    all_refs, errors = scan_uncommitted_turn_refs(
        system,
        committed,
        _extract_referenced_turn,
        include_record_ids=False,
        skip_self_turn=True,
        fail_closed=False,
    )
    return {turn: rows for turn, rows in all_refs.items() if turn in candidate_set}, errors
