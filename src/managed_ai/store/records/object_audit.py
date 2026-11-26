"""9-layer transitive object reachability audit/GC + re-exports for dedup and compaction."""
from __future__ import annotations

import errno
import hmac
import os
from pathlib import Path
import re
import stat

from managed_ai.core.formats import (
    OBJECT_AUDIT_FORMAT as AUDIT_FORMAT,
    OBJECT_GC_RECEIPT_FORMAT as GC_RECEIPT_FORMAT,
)
from managed_ai.core.ownership import packet_id
from managed_ai.core.storage import (
    _OPEN_RO_NOFOLLOW,
    encode,
    is_ordinary_file_stat,
    now,
    open_directory,
    sha,
    sync_dir,
)
from managed_ai.platform import strict_json_loads
from managed_ai.store.protocol.identities import digest, exact_fields, require
from managed_ai.store.protocol.inputs import parse_input_bytes
from managed_ai.store.records.compaction import execute_compact_objects, plan_compact_objects
from managed_ai.store.records.deduplication import (
    _read_ordinary_unbounded,
    _verify_ordinary_file,
    _verify_plan,
    _walk_ordinary_files,
    _write_receipt,
    execute_deduplicate_originals,
    plan_deduplicate_originals,
)

_SHA_RE = re.compile(r'^[0-9a-f]{64}$')
_BYTE_HEX64_RE = re.compile(rb'"([0-9a-f]{64})"')


def _extract_object_hashes(value, known_objects: set[str], out: set[str]) -> None:
    """Recursively find all stored exchange object digests referenced in parsed JSON."""
    if isinstance(value, str):
        if value in known_objects:
            out.add(value)
    elif isinstance(value, list):
        for item in value:
            _extract_object_hashes(item, known_objects, out)
    elif isinstance(value, dict):
        for k, v in value.items():
            if k in known_objects:
                out.add(k)
            _extract_object_hashes(v, known_objects, out)


def _has_unvisited_hex_token(raw: bytes, known_bytes: set[bytes], skip_bytes: set[bytes] | None = None) -> bool:
    for match in _BYTE_HEX64_RE.finditer(raw):
        token = match.group(1)
        if token in known_bytes and (skip_bytes is None or token not in skip_bytes):
            return True
    return False


def _read_live_record_raw(store, ident: str, records_fd: int | None, dir_str: str | None) -> tuple[bytes, Path]:
    filename = packet_id(ident) + '.json'
    if records_fd is not None and hasattr(os, 'O_NOFOLLOW'):
        full_path = store.work / 'records' / filename
        try:
            fd = os.open(filename, _OPEN_RO_NOFOLLOW, dir_fd=records_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                raise ValueError(f'ordinary JSON file required: {full_path}') from None
            raise

        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f'ordinary JSON file required: {full_path}')
            stream = os.fdopen(fd, 'rb')
        except BaseException:
            os.close(fd)
            raise

        with stream:
            return stream.read(), full_path

    if dir_str is not None:
        full_str = dir_str + os.sep + filename
        full_path = Path(full_str)
        try:
            info = os.lstat(full_str)
        except OSError:
            raise ValueError(f'ordinary JSON file required: {full_str}') from None
        if not is_ordinary_file_stat(info):
            raise ValueError(f'ordinary JSON file required: {full_str}')
        with open(full_str, 'rb') as stream:
            return stream.read(), full_path

    full_path = store.path(ident)
    return _read_ordinary_unbounded(full_path), full_path


def _collect_live_record_refs(system, known: set[str], known_bytes: set[bytes], out: set[str]) -> None:
    """Layer 1: Stream live hot records in bounded chunks without holding all parsed dicts in RAM."""
    store = system.record_store
    idents = store.ids()
    if not idents:
        return

    indexed = store.index.load(store.format)
    raw_digests: dict[str, str] | None = (
        {
            ident: entry['raw_sha256']
            for ident, entry in indexed.items()
            if isinstance(entry, dict) and isinstance(entry.get('raw_sha256'), str)
        }
        if indexed is not None
        else None
    )
    del indexed

    def inspect_chunk(chunk: list[str], records_fd: int | None, dir_str: str | None) -> set[str]:
        found: set[str] = set()
        for ident in chunk:
            raw, full_path = _read_live_record_raw(store, ident, records_fd, dir_str)
            expected_sha = raw_digests.get(ident) if raw_digests is not None else None
            if expected_sha is not None and hmac.compare_digest(sha(raw), expected_sha):
                if not known_bytes:
                    continue
                if b'\\u' not in raw and not _has_unvisited_hex_token(raw, known_bytes):
                    continue
                rec = strict_json_loads(raw, full_path, require_object=True)
                _extract_object_hashes(rec, known, found)
                continue

            rec = parse_input_bytes(raw, full_path)
            store._verify_record(ident, rec, raw)
            if known:
                _extract_object_hashes(rec, known, found)
        return found

    def run_chunks(records_fd: int | None, dir_str: str | None) -> None:
        if len(idents) < 32:
            out.update(inspect_chunk(idents, records_fd, dir_str))
            return

        from concurrent.futures import ThreadPoolExecutor

        chunk_size = max(32, min(256, (len(idents) + 7) // 8))
        chunks = [idents[i:i + chunk_size] for i in range(0, len(idents), chunk_size)]
        workers = min(8, len(chunks))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for found in pool.map(lambda c: inspect_chunk(c, records_fd, dir_str), chunks):
                out.update(found)

    if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
        with system.ex.open_workspace_dir('records') as records_fd:
            run_chunks(records_fd, None)
    else:
        _, dir_str = store._validated_records_dir_win()
        run_chunks(None, dir_str)


def _expand_object_wave(
    objects,
    keys: list[str],
    known: set[str],
    known_bytes: set[bytes],
    reachable_bytes: set[bytes],
    objects_fd: int | None,
) -> tuple[set[str], set[str]]:
    """Read, SHA-verify, and extract newly referenced hashes for one BFS frontier in parallel."""
    def inspect_chunk(chunk: list[str]) -> tuple[set[str], set[str]]:
        found: set[str] = set()
        fvo_found: set[str] = set()

        for key in chunk:
            raw = objects.read(key, retained=True, dir_fd=objects_fd)
            stripped = raw.lstrip()
            if stripped[:1] not in (b'{', b'['):
                continue

            has_escape = b'\\u' in raw
            has_fvo = b'"full_view_object"' in raw
            if not has_escape and not has_fvo:
                if not _has_unvisited_hex_token(raw, known_bytes, reachable_bytes):
                    continue

            try:
                doc = strict_json_loads(raw, key, require_object=False)
            except (UnicodeDecodeError, ValueError):
                continue

            if not isinstance(doc, (dict, list)):
                continue

            _extract_object_hashes(doc, known, found)
            if isinstance(doc, dict):
                fvo = doc.get('full_view_object')
                if isinstance(fvo, str) and fvo in known:
                    fvo_found.add(fvo)

        return found, fvo_found

    if len(keys) < 16:
        return inspect_chunk(keys)

    from concurrent.futures import ThreadPoolExecutor

    workers = min(8, len(keys))
    step = (len(keys) + workers - 1) // workers
    chunks = [keys[i:i + step] for i in range(0, len(keys), step)]
    discovered: set[str] = set()
    fvo_all: set[str] = set()

    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        for sub_found, sub_fvo in pool.map(inspect_chunk, chunks):
            discovered.update(sub_found)
            fvo_all.update(sub_fvo)

    return discovered, fvo_all


def _scan_json_tree(directory: Path, known_objects: set[str], out: set[str]) -> None:
    if not directory.exists():
        return

    for path in _walk_ordinary_files(directory, directory):
        if path.suffix.lower() != '.json':
            continue

        raw = _read_ordinary_unbounded(path)
        try:
            parsed = strict_json_loads(raw, path, require_object=False)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f'unreadable root JSON during object audit ({path}): {exc}') from None

        _extract_object_hashes(parsed, known_objects, out)


def _collect_packet_and_exposure_refs(
    system,
    known: set[str],
    direct_out: set[str],
    exposure_out: set[str],
) -> None:
    """Layers 3 & 4: exchange/packets/** + transitive exposure.json -> full_view_object."""
    packets_root = system.ex.root / 'packets'
    exposure_objects: set[str] = set()

    if packets_root.exists():
        for path in _walk_ordinary_files(packets_root, packets_root):
            if path.suffix.lower() != '.json':
                continue

            raw = _read_ordinary_unbounded(path)
            try:
                parsed = strict_json_loads(raw, path, require_object=False)
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError(f'unreadable packet JSON during object audit ({path}): {exc}') from None

            _extract_object_hashes(parsed, known, direct_out)
            if path.name == 'request.json' and isinstance(parsed, dict):
                for mat in parsed.get('materials', ()):
                    if not isinstance(mat, dict):
                        continue
                    mat_path = mat.get('path', '')
                    mat_sha = mat.get('sha256')
                    if (
                        isinstance(mat_path, str)
                        and (mat_path == 'exposure.json' or mat_path.endswith('/exposure.json'))
                        and isinstance(mat_sha, str)
                        and mat_sha in known
                    ):
                        exposure_objects.add(mat_sha)

    objects = system.ex.objects
    for exp_key in sorted(exposure_objects):
        raw_exp = objects.read(exp_key, retained=True)
        try:
            exp_doc = strict_json_loads(raw_exp, exp_key, require_object=False)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f'unreadable exposure object during object audit ({exp_key}): {exc}') from None

        _extract_object_hashes(exp_doc, known, exposure_out)
        fvo = exp_doc.get('full_view_object') if isinstance(exp_doc, dict) else None
        if isinstance(fvo, str) and fvo in known:
            exposure_out.add(fvo)
            raw_view = objects.read(fvo, retained=True)
            try:
                view_doc = strict_json_loads(raw_view, fvo, require_object=False)
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError(f'unreadable full_view_object during object audit ({fvo}): {exc}') from None
            _extract_object_hashes(view_doc, known, exposure_out)


def _expand_transitive_closure(
    objects,
    known: set[str],
    known_bytes: set[bytes],
    layer_sets: dict[str, set[str]],
) -> set[str]:
    """Fixed-point wave-BFS expansion over reached JSON objects."""
    reachable: set[str] = set()
    for keys in layer_sets.values():
        reachable.update(keys)

    reachable_bytes = {k.encode('ascii') for k in reachable}
    frontier = sorted(reachable)
    visited_expansion: set[str] = set()

    objects_fd = None
    if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY') and objects.directory.is_dir():
        try:
            objects_fd = open_directory(objects.directory)
        except OSError:
            objects_fd = None

    try:
        while frontier:
            wave = [key for key in frontier if key not in visited_expansion]
            if not wave:
                break
            visited_expansion.update(wave)
            discovered, fvo_found = _expand_object_wave(
                objects, wave, known, known_bytes, reachable_bytes, objects_fd,
            )
            if fvo_found:
                layer_sets['packets_transitive_exposure'].update(fvo_found)

            new_children = sorted(discovered - reachable)
            for child in new_children:
                reachable.add(child)
                reachable_bytes.add(child.encode('ascii'))
                layer_sets['transitive_objects'].add(child)
            frontier = new_children
    finally:
        if objects_fd is not None:
            os.close(objects_fd)

    return reachable


def audit_objects(system) -> dict:
    """Compute exact 9-layer transitive reachability over exchange/objects (read-only LOCK_SH)."""
    objects = system.ex.objects
    obj_info: dict[str, dict] = {}
    for key, _, is_comp, disk_bytes in objects.entries():
        if key not in obj_info:
            obj_info[key] = {'object': key, 'disk_bytes': disk_bytes, 'compressed': is_comp}
        else:
            obj_info[key]['disk_bytes'] += disk_bytes

    known = set(obj_info.keys())
    known_bytes = {k.encode('ascii') for k in known}

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

    # Layer 1: Live hot records (streamed in bounded chunks)
    _collect_live_record_refs(system, known, known_bytes, layer_sets['live_records'])

    # Layer 2: Cold-archived records
    archive = system.record_store.cold_archive
    for ident in archive.ids():
        rec, _ = archive.read(ident)
        _extract_object_hashes(rec, known, layer_sets['cold_archive'])

    # Layers 3 & 4: exchange/packets/** + transitive exposure.json -> full_view_object
    _collect_packet_and_exposure_refs(
        system,
        known,
        layer_sets['packets_direct'],
        layer_sets['packets_transitive_exposure'],
    )

    # Layers 5-9: attention, interpretations, imports, batches, sources, turns, carrier
    _scan_json_tree(system.ex.root / 'attention', known, layer_sets['attention'])
    _scan_json_tree(system.work / 'interpretations', known, layer_sets['interpretations'])
    _scan_json_tree(system.work / '.system/imports', known, layer_sets['imports'])
    _scan_json_tree(system.work / '.system/batches', known, layer_sets['batches'])
    _scan_json_tree(system.work / 'sources/received', known, layer_sets['sources_received'])
    _scan_json_tree(system.work / '.system/turns', known, layer_sets['turns_and_carrier'])
    _scan_json_tree(system.work / '.system/retirements', known, layer_sets['turns_and_carrier'])
    _scan_json_tree(system.ex.root / 'carrier', known, layer_sets['turns_and_carrier'])

    reachable = _expand_transitive_closure(objects, known, known_bytes, layer_sets)

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
        {
            'format', 'instance_id', 'total_objects', 'total_disk_bytes', 'reachable_count',
            'unreachable_count', 'unreachable_bytes', 'by_layer', 'unreachable', 'plan_sha256',
        },
        expected_plan,
        system.instance_id,
        reason,
        'gc-objects',
    )

    nominated_rows = plan['unreachable']
    require(
        isinstance(nominated_rows, list) and len(nominated_rows) > 0,
        'gc-objects plan has no unreachable objects to prune',
    )

    nominated_keys: list[str] = []
    for row in nominated_rows:
        exact_fields(row, {'object', 'disk_bytes', 'compressed'}, set(), 'gc object candidate')
        nominated_keys.append(digest(row['object'], 'object sha256'))
    require(len(nominated_keys) == len(set(nominated_keys)), 'duplicate object identities in gc plan')

    live_audit = audit_objects(system)
    live_unreachable = {row['object']: row for row in live_audit['unreachable']}
    became_reachable = [k for k in nominated_keys if k not in live_unreachable]
    require(
        not became_reachable,
        'refusing gc: nominated objects are reachable or absent on live re-audit: ' + ', '.join(became_reachable),
    )

    objects = system.ex.objects
    removed: list[dict] = []
    reclaimed = 0
    for key in nominated_keys:
        removed_any = False
        for suffix in ('', '.deflate'):
            candidate = objects.directory / (key + suffix)
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if not is_ordinary_file_stat(info):
                raise ValueError(f'ordinary object file required: {candidate}')
            size = info.st_size
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


__all__ = [
    'AUDIT_FORMAT',
    'GC_RECEIPT_FORMAT',
    'audit_objects',
    'execute_compact_objects',
    'execute_deduplicate_originals',
    'gc_objects',
    'plan_compact_objects',
    'plan_deduplicate_originals',
]
