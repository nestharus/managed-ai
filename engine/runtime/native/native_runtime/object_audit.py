"""Storage deduplication, transparent DEFLATE compaction, and 9-layer transitive object audit/GC."""
from __future__ import annotations

from collections import deque
import errno
import json
import os
from pathlib import Path
import re
import stat
import zlib

from continuation_support.ownership import relative_path
from continuation_support.storage import (
    _OPEN_RO_NOFOLLOW,
    atomic_bytes,
    encode,
    now,
    read_json,
    sha,
    sync_dir,
    write_json,
)
from native_runtime.identities import digest, exact_fields, require, strict_json
from native_runtime.inputs import integer, text


DEDUP_PLAN_FORMAT = 'managed-ai-deduplicate-originals-plan/1'
DEDUP_RECEIPT_FORMAT = 'managed-ai-deduplicate-originals-receipt/1'
COMPACT_PLAN_FORMAT = 'managed-ai-compact-objects-plan/1'
COMPACT_RECEIPT_FORMAT = 'managed-ai-compact-objects-receipt/1'
AUDIT_FORMAT = 'managed-ai-object-audit/1'
GC_RECEIPT_FORMAT = 'managed-ai-gc-objects-receipt/1'
_SHA_RE = re.compile(r'^[0-9a-f]{64}$')
_CONTENT_ADDRESSED_NAME_RE = re.compile(r'^([0-9a-f]{2})/([0-9a-f]{64})\.([A-Za-z0-9._-]+)$')


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
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'ordinary file required: {path}')
    return path.read_bytes()


def _write_receipt(system, name: str, payload: dict) -> Path:
    receipt_dir = system.ex.path('.system/maintenance')
    receipt_dir.mkdir(parents=True, exist_ok=True)
    path = receipt_dir / name
    write_json(path, payload)
    return path


def _verify_plan(plan: dict, expected_format: str, required_fields: set[str],
                 expected_plan: str, instance_id: str, reason: str, label: str) -> None:
    text(reason, f'{label} reason')
    digest(expected_plan, 'expected plan sha256')
    exact_fields(plan, required_fields, set(), f'{label} plan')
    require(plan['format'] == expected_format, f'unsupported {label} plan format')
    require(plan['instance_id'] == instance_id, f'{label} plan belongs to another store')
    body = {k: v for k, v in plan.items() if k != 'plan_sha256'}
    computed = sha(encode(body))
    require(plan['plan_sha256'] == computed and computed == expected_plan,
            f'{label} plan digest differs from expected plan')


# ---------------------------------------------------------------------------
# G2S-04 Phase A: Deduplicate content-addressed workspace originals
# ---------------------------------------------------------------------------

def _walk_ordinary_files(root: Path, base: Path) -> list[Path]:
    """Walk ordinary subdirectories without following symlinks."""
    if not root.exists():
        return []
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f'ordinary directory required: {root}')
    found: list[Path] = []
    pending = [root]
    while pending:
        cur = pending.pop()
        with os.scandir(cur) as items:
            for item in items:
                child = cur / item.name
                st = item.stat(follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode):
                    pending.append(child)
                elif stat.S_ISREG(st.st_mode):
                    found.append(child)
                else:
                    raise ValueError(f'ordinary file or directory required: {child}')
    found.sort(key=lambda p: p.relative_to(base).as_posix())
    return found


def plan_deduplicate_originals(system, *, prefix: str = 'evidence/research-originals') -> dict:
    """Verify byte+SHA-256 parity in exchange/objects for content-addressed workspace copies."""
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
        actual_sha = sha(raw)
        if actual_sha != expected_sha:
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


def execute_deduplicate_originals(system, *, plan: dict, expected_plan: str, reason: str) -> dict:
    """Re-verify 100% parity under owner.lock, then remove duplicate workspace copies."""
    _verify_plan(
        plan,
        DEDUP_PLAN_FORMAT,
        {'format', 'instance_id', 'prefix', 'candidates', 'skipped',
         'reclaimable_files', 'reclaimable_bytes', 'plan_sha256'},
        expected_plan,
        system.instance_id,
        reason,
        'deduplicate-originals',
    )
    rel_prefix = relative_path(plan['prefix'])
    objects = system.ex.objects
    candidates = plan['candidates']
    require(isinstance(candidates, list) and len(candidates) > 0,
            'deduplicate-originals plan has no candidates')

    # Phase 1: Re-verify EVERY candidate byte-for-byte before unlinking ANY file.
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
        require(match is not None and match.group(1) == match.group(2)[:2] and match.group(2) == row['sha256'],
                'candidate path is not content-addressed to its declared digest: ' + rel)
        digest(row['sha256'], 'candidate sha256')
        integer(row['bytes'], 'candidate bytes')
        path = system.ex.path(rel)
        raw = _read_ordinary_unbounded(path)
        require(len(raw) == row['bytes'] and sha(raw) == row['sha256'],
                'workspace original changed since planning: ' + rel)
        retained = objects.read(row['sha256'], retained=True, expected_bytes=row['bytes'])
        require(retained == raw, 'exchange object parity mismatch: ' + rel)
        verified.append((path, row))

    # Phase 2: Unlink verified duplicate workspace copies and prune empty shard dirs.
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


# ---------------------------------------------------------------------------
# G2S-04 Phase B: Compact uncompressed exchange/objects into .deflate
# ---------------------------------------------------------------------------

def plan_compact_objects(system, *, min_savings_bytes: int = 64) -> dict:
    """Scan uncompressed exchange/objects and nominate blobs that compress strictly smaller."""
    integer(min_savings_bytes, 'min_savings_bytes', 1)
    objects = system.ex.objects
    entries = objects.entries()
    compressed_keys = {key for key, _, is_comp, _ in entries if is_comp}

    candidates: list[dict] = []
    skipped_incompressible = 0
    already_compressed = len(compressed_keys)
    total_raw = 0
    total_comp = 0
    for key, path, is_comp, disk_bytes in entries:
        if is_comp:
            continue
        raw = _read_ordinary_unbounded(path)
        if sha(raw) != key or len(raw) != disk_bytes:
            raise ValueError(f'corrupt object encountered during compaction plan: {key}')
        if key in compressed_keys:
            # Duplicate raw copy left alongside already-valid .deflate; nominate cleanup only when .deflate is strictly smaller.
            existing = objects.read(key, retained=True, expected_bytes=len(raw))
            require(existing == raw, f'conflicting raw and compressed object: {key}')
            deflate_path = objects.directory / (key + '.deflate')
            info = deflate_path.lstat()
            require(not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode),
                    f'ordinary compressed object file required: {deflate_path}')
            comp_bytes = info.st_size
            if comp_bytes < len(raw) and len(raw) >= min_savings_bytes:
                candidates.append({
                    'object': key, 'raw_bytes': len(raw),
                    'compressed_bytes': comp_bytes, 'saved_bytes': len(raw),
                    'already_deflated': True,
                })
                total_raw += len(raw)
                total_comp += comp_bytes
            else:
                skipped_incompressible += 1
            continue
        comp = zlib.compress(raw, level=6)
        saved = len(raw) - len(comp)
        if saved >= min_savings_bytes and len(comp) < len(raw):
            candidates.append({
                'object': key,
                'raw_bytes': len(raw),
                'compressed_bytes': len(comp),
                'saved_bytes': saved,
                'already_deflated': False,
            })
            total_raw += len(raw)
            total_comp += len(comp)
        else:
            skipped_incompressible += 1

    reclaimable = sum(row['saved_bytes'] for row in candidates)
    body = {
        'format': COMPACT_PLAN_FORMAT,
        'instance_id': system.instance_id,
        'min_savings_bytes': min_savings_bytes,
        'candidates': candidates,
        'candidate_count': len(candidates),
        'skipped_incompressible': skipped_incompressible,
        'already_compressed': already_compressed,
        'total_raw_bytes': total_raw,
        'total_compressed_bytes': total_comp,
        'reclaimable_bytes': reclaimable,
    }
    return dict(body, plan_sha256=sha(encode(body)))


def execute_compact_objects(system, *, plan: dict, expected_plan: str, reason: str) -> dict:
    """Atomically replace verified uncompressed objects with .deflate copies under owner.lock."""
    _verify_plan(
        plan,
        COMPACT_PLAN_FORMAT,
        {'format', 'instance_id', 'min_savings_bytes', 'candidates', 'candidate_count',
         'skipped_incompressible', 'already_compressed', 'total_raw_bytes',
         'total_compressed_bytes', 'reclaimable_bytes', 'plan_sha256'},
        expected_plan,
        system.instance_id,
        reason,
        'compact-objects',
    )
    objects = system.ex.objects
    candidates = plan['candidates']
    require(isinstance(candidates, list) and len(candidates) > 0,
            'compact-objects plan has no candidates')

    # Phase 1: Pre-verify EVERY candidate completely before writing or unlinking any object.
    seen: set[str] = set()
    to_compact: list[tuple[str, Path, Path, bytes, bytes | None, int, int]] = []
    for row in candidates:
        exact_fields(row, {'object', 'raw_bytes', 'compressed_bytes', 'saved_bytes', 'already_deflated'},
                     set(), 'compact candidate')
        require(type(row['already_deflated']) is bool, 'already_deflated must be a boolean')
        key = digest(row['object'], 'object sha256')
        require(key not in seen, 'duplicate object in compact plan: ' + key)
        seen.add(key)
        integer(row['raw_bytes'], 'raw_bytes')
        integer(row['compressed_bytes'], 'compressed_bytes')
        integer(row['saved_bytes'], 'saved_bytes', 1)
        raw_path = objects.directory / key
        deflate_path = objects.directory / (key + '.deflate')
        raw = _read_ordinary_unbounded(raw_path)
        require(len(raw) == row['raw_bytes'] and sha(raw) == key,
                'object changed since compaction plan: ' + key)
        if row['already_deflated']:
            info = deflate_path.lstat()
            require(not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode),
                    'existing compressed object required: ' + key)
            require(info.st_size == row['compressed_bytes'] and info.st_size < len(raw)
                    and row['saved_bytes'] == len(raw),
                    'dual-file compressed object changed since compaction plan: ' + key)
            with deflate_path.open('rb') as stream:
                roundtrip = objects._decompress_bounded(stream, len(raw), deflate_path)
            require(roundtrip == raw and sha(roundtrip) == key,
                    'existing .deflate verification failed: ' + key)
            to_compact.append((key, raw_path, deflate_path, raw, None, info.st_size, len(raw)))
        else:
            require(not deflate_path.exists() and not deflate_path.is_symlink(),
                    'object state changed since compaction plan: ' + key)
            compressed = zlib.compress(raw, level=6)
            require(len(compressed) < len(raw), 'object no longer compresses smaller: ' + key)
            require(len(compressed) == row['compressed_bytes']
                    and len(raw) - len(compressed) == row['saved_bytes'],
                    'compressed size differs from compaction plan: ' + key)
            require(zlib.decompress(compressed) == raw, 'round-trip decompression mismatch: ' + key)
            to_compact.append((key, raw_path, deflate_path, raw, compressed, len(compressed), len(raw) - len(compressed)))

    # Phase 2: Commit verified .deflate files (if needed) and unlink verified raw copies.
    compacted = 0
    raw_bytes_total = 0
    comp_bytes_total = 0
    saved_bytes_total = 0
    for key, raw_path, deflate_path, raw, compressed, comp_size, saved_bytes in to_compact:
        if compressed is not None:
            atomic_bytes(deflate_path, compressed)
            with deflate_path.open('rb') as stream:
                roundtrip = objects._decompress_bounded(stream, len(raw), deflate_path)
            require(roundtrip == raw and sha(roundtrip) == key, 'written .deflate verification failed: ' + key)
        raw_path.unlink()
        sync_dir(objects.directory)
        # Verify transparent ObjectStore.read() succeeds from .deflate.
        require(objects.read(key, retained=True, expected_bytes=len(raw)) == raw,
                'post-compaction ObjectStore.read failed: ' + key)
        compacted += 1
        raw_bytes_total += len(raw)
        comp_bytes_total += comp_size
        saved_bytes_total += saved_bytes

    receipt = {
        'format': COMPACT_RECEIPT_FORMAT,
        'instance_id': system.instance_id,
        'plan_sha256': expected_plan,
        'reason': reason,
        'executed_at': now(),
        'compacted_objects': compacted,
        'raw_bytes': raw_bytes_total,
        'compressed_bytes': comp_bytes_total,
        'saved_bytes': saved_bytes_total,
    }
    receipt_path = _write_receipt(system, f'compact-objects-{expected_plan[:16]}.json', receipt)
    return dict(receipt, receipt_path=str(receipt_path))


# ---------------------------------------------------------------------------
# G2S-06: 9-Layer Transitive Object Reachability Audit & Guarded GC
# ---------------------------------------------------------------------------

def _extract_object_hashes(value, known_objects: set[str], out: set[str]) -> None:
    """Recursively find all 64-hex strings in parsed JSON that correspond to stored exchange objects."""
    if isinstance(value, str):
        if len(value) == 64 and _SHA_RE.match(value) and value in known_objects:
            out.add(value)
    elif isinstance(value, list):
        for item in value:
            _extract_object_hashes(item, known_objects, out)
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and len(k) == 64 and _SHA_RE.match(k) and k in known_objects:
                out.add(k)
            _extract_object_hashes(v, known_objects, out)


def _scan_json_tree(directory: Path, known_objects: set[str], out: set[str]) -> None:
    if not directory.exists():
        return
    for path in _walk_ordinary_files(directory, directory):
        if path.suffix.lower() != '.json':
            continue
        raw = _read_ordinary_unbounded(path)
        try:
            parsed = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f'unreadable root JSON during object audit ({path}): {exc}') from None
        _extract_object_hashes(parsed, known_objects, out)


def audit_objects(system) -> dict:
    """Compute exact 9-layer transitive reachability over exchange/objects (read-only LOCK_SH).

    Layers traversed:
      1. live_records (work/records/*.json: basis, previous_object, embedded object refs)
      2. cold_archive (exchange/cold-records/manifest.json + archived record bodies)
      3. packets_direct (exchange/packets/** including .sealed-* pending packets)
      4. packets_transitive_exposure (exposure.json -> full_view_object -> view refs)
      5. attention (exchange/attention/** + referenced attention JSON objects)
      6. interpretations (work/interpretations/**)
      7. imports (work/.system/imports/**)
      8. batches (work/.system/batches/**)
      9. sources_received (work/sources/received/**) + turns_and_carrier (work/.system/turns/**, exchange/carrier/**)
    Plus fixed-point transitive expansion across referenced JSON objects (previous_object chains, pinned predecessors, etc.).
    """
    objects = system.ex.objects
    entries = objects.entries()
    obj_info: dict[str, dict] = {}
    for key, _, is_comp, disk_bytes in entries:
        if key not in obj_info:
            obj_info[key] = {'object': key, 'disk_bytes': disk_bytes, 'compressed': is_comp}
        else:
            obj_info[key]['disk_bytes'] += disk_bytes
    known = set(obj_info.keys())

    layer_sets: dict[str, set[str]] = {
        'live_records': set(),
        'cold_archive': set(),
        'packets_direct': set(),
        'packets_transitive_exposure': set(),
        'attention': set(),
        'interpretations': set(),
        'imports': set(),
        'batches': set(),
        'sources_received': set(),
        'turns_and_carrier': set(),
        'transitive_objects': set(),
    }

    # Layer 1: Live hot records
    for rec in system.record_store.scan_records():
        _extract_object_hashes(rec, known, layer_sets['live_records'])

    # Layer 2: Cold-archived records
    archive = system.record_store.cold_archive
    for ident in archive.ids():
        rec, _ = archive.read(ident)
        _extract_object_hashes(rec, known, layer_sets['cold_archive'])

    # Layer 3 & 4: exchange/packets/** (committed + .sealed-* pending) + transitive exposure.json -> full_view_object
    packets_root = system.ex.root / 'packets'
    exposure_objects: set[str] = set()
    if packets_root.exists():
        for path in _walk_ordinary_files(packets_root, packets_root):
            if path.suffix.lower() != '.json':
                continue
            raw = _read_ordinary_unbounded(path)
            try:
                parsed = json.loads(raw.decode('utf-8'))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError(f'unreadable packet JSON during object audit ({path}): {exc}') from None
            _extract_object_hashes(parsed, known, layer_sets['packets_direct'])
            if path.name == 'request.json' and isinstance(parsed, dict):
                for mat in parsed.get('materials', ()):
                    if not isinstance(mat, dict):
                        continue
                    mat_path = mat.get('path', '')
                    mat_sha = mat.get('sha256')
                    if (isinstance(mat_path, str)
                            and (mat_path == 'exposure.json' or mat_path.endswith('/exposure.json'))
                            and isinstance(mat_sha, str) and mat_sha in known):
                        exposure_objects.add(mat_sha)

    for exp_key in sorted(exposure_objects):
        raw_exp = objects.read(exp_key, retained=True)
        try:
            exp_doc = json.loads(raw_exp.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f'unreadable exposure object during object audit ({exp_key}): {exc}') from None
        _extract_object_hashes(exp_doc, known, layer_sets['packets_transitive_exposure'])
        fvo = exp_doc.get('full_view_object') if isinstance(exp_doc, dict) else None
        if isinstance(fvo, str) and fvo in known:
            layer_sets['packets_transitive_exposure'].add(fvo)
            raw_view = objects.read(fvo, retained=True)
            try:
                view_doc = json.loads(raw_view.decode('utf-8'))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError(f'unreadable full_view_object during object audit ({fvo}): {exc}') from None
            _extract_object_hashes(view_doc, known, layer_sets['packets_transitive_exposure'])

    # Layer 5: exchange/attention/**
    _scan_json_tree(system.ex.root / 'attention', known, layer_sets['attention'])

    # Layer 6: work/interpretations/**
    _scan_json_tree(system.work / 'interpretations', known, layer_sets['interpretations'])

    # Layer 7: work/.system/imports/**
    _scan_json_tree(system.work / '.system/imports', known, layer_sets['imports'])

    # Layer 8: work/.system/batches/**
    _scan_json_tree(system.work / '.system/batches', known, layer_sets['batches'])

    # Layer 9: work/sources/received/** + work/.system/turns/** + work/.system/retirements/** + exchange/carrier/**
    _scan_json_tree(system.work / 'sources/received', known, layer_sets['sources_received'])
    _scan_json_tree(system.work / '.system/turns', known, layer_sets['turns_and_carrier'])
    _scan_json_tree(system.work / '.system/retirements', known, layer_sets['turns_and_carrier'])
    _scan_json_tree(system.ex.root / 'carrier', known, layer_sets['turns_and_carrier'])

    # Fixed-point transitive closure over reached JSON objects (previous_object chains, attention objects, pinned heads, views)
    reachable: set[str] = set()
    for s in layer_sets.values():
        reachable.update(s)
    queue = deque(sorted(reachable))
    visited_expansion: set[str] = set()
    while queue:
        key = queue.popleft()
        if key in visited_expansion:
            continue
        visited_expansion.add(key)
        raw = objects.read(key, retained=True)
        stripped = raw.lstrip()
        if stripped[:1] not in (b'{', b'['):
            continue
        try:
            doc = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(doc, (dict, list)):
            continue
        discovered: set[str] = set()
        _extract_object_hashes(doc, known, discovered)
        if isinstance(doc, dict) and isinstance(doc.get('full_view_object'), str) and doc['full_view_object'] in known:
            layer_sets['packets_transitive_exposure'].add(doc['full_view_object'])
        for child in sorted(discovered - reachable):
            reachable.add(child)
            layer_sets['transitive_objects'].add(child)
            queue.append(child)

    unreachable_keys = sorted(known - reachable)
    unreachable = [obj_info[k] for k in unreachable_keys]
    by_layer = {
        name: {
            'objects': len(keys),
            'disk_bytes': sum(obj_info[k]['disk_bytes'] for k in keys),
        }
        for name, keys in layer_sets.items()
    }
    total_disk_bytes = sum(row['disk_bytes'] for row in obj_info.values())
    unreachable_bytes = sum(row['disk_bytes'] for row in unreachable)

    body = {
        'format': AUDIT_FORMAT,
        'instance_id': system.instance_id,
        'total_objects': len(obj_info),
        'total_disk_bytes': total_disk_bytes,
        'reachable_count': len(reachable),
        'unreachable_count': len(unreachable),
        'unreachable_bytes': unreachable_bytes,
        'by_layer': by_layer,
        'unreachable': unreachable,
    }
    return dict(body, plan_sha256=sha(encode(body)))


def gc_objects(system, *, plan: dict, expected_plan: str, reason: str) -> dict:
    """Explicitly prune ONLY strictly unreachable nominated objects after live 9-layer re-audit under owner.lock."""
    _verify_plan(
        plan,
        AUDIT_FORMAT,
        {'format', 'instance_id', 'total_objects', 'total_disk_bytes', 'reachable_count',
         'unreachable_count', 'unreachable_bytes', 'by_layer', 'unreachable', 'plan_sha256'},
        expected_plan,
        system.instance_id,
        reason,
        'gc-objects',
    )
    nominated_rows = plan['unreachable']
    require(isinstance(nominated_rows, list) and len(nominated_rows) > 0,
            'gc-objects plan has no unreachable objects to prune')

    nominated_keys: list[str] = []
    for row in nominated_rows:
        exact_fields(row, {'object', 'disk_bytes', 'compressed'}, set(), 'gc object candidate')
        nominated_keys.append(digest(row['object'], 'object sha256'))
    require(len(nominated_keys) == len(set(nominated_keys)), 'duplicate object identities in gc plan')

    # Re-audit FULL 9-layer transitive closure live under exclusive owner.lock!
    live_audit = audit_objects(system)
    live_unreachable = {row['object']: row for row in live_audit['unreachable']}
    became_reachable = [k for k in nominated_keys if k not in live_unreachable]
    require(not became_reachable,
            'refusing gc: nominated objects are reachable or absent on live re-audit: ' + ', '.join(became_reachable))

    objects = system.ex.objects
    removed: list[dict] = []
    reclaimed = 0
    for key in nominated_keys:
        removed_any = False
        for suffix in ('', '.deflate'):
            candidate = objects.directory / (key + suffix)
            if candidate.is_symlink():
                raise ValueError(f'ordinary object file required: {candidate}')
            if candidate.is_file():
                size = candidate.stat().st_size
                candidate.unlink()
                reclaimed += size
                removed_any = True
        require(removed_any, f'object missing during gc: {key}')
        removed.append({'object': key, 'reclaimed_bytes': live_unreachable[key]['disk_bytes']})
    sync_dir(objects.directory)

    receipt = {
        'format': GC_RECEIPT_FORMAT,
        'instance_id': system.instance_id,
        'plan_sha256': expected_plan,
        'reason': reason,
        'executed_at': now(),
        'removed_count': len(removed),
        'reclaimed_bytes': reclaimed,
        'removed': removed,
    }
    receipt_path = _write_receipt(system, f'gc-objects-{expected_plan[:16]}.json', receipt)
    return dict(receipt, receipt_path=str(receipt_path))
