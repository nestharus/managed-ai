#!/usr/bin/env python3
"""Capture and recover bounded, exact support for a declared continuation.

This optional POSIX operation reads a cooperative owner. It never invokes an
effect, changes a runtime binding, or installs a recovered projection as an owner.
"""
from __future__ import annotations
import argparse
from collections import deque
from contextlib import contextmanager
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import zipfile

FORMAT = 'g60-continuation-support/1'
SYSTEM = 'g60-agent-system/0.1'
DEFAULTS = {'max_files': 256, 'max_file_bytes': 4 * 1024 * 1024,
            'max_total_bytes': 32 * 1024 * 1024,
            'max_metadata_bytes': 64 * 1024 * 1024, 'max_record_scan': 512}
CEILINGS = {'max_files': 4096, 'max_file_bytes': 64 * 1024 * 1024,
            'max_total_bytes': 256 * 1024 * 1024,
            'max_metadata_bytes': 128 * 1024 * 1024, 'max_record_scan': 4096}
ARCHIVE_LIMIT = 300 * 1024 * 1024
MANIFEST_LIMIT = 8 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, 'duplicate JSON key: ' + key)
        result[key] = value
    return result


def decode(data):
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON: ' + value)))


def integer(value, label, maximum=None):
    require(type(value) is int and value >= 0 and (maximum is None or value <= maximum), 'invalid ' + label)
    return value


def sha_value(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value), 'invalid SHA-256')
    return value


def relative(value):
    require(isinstance(value, str) and value and '\\' not in value and ':' not in value and '\0' not in value,
            'ordinary relative POSIX path required')
    require(not PurePosixPath(value).is_absolute()
            and all(part not in ('', '.', '..') for part in value.split('/')), 'unsafe relative path: ' + value)
    return value


def ordinary_path(path, *, directory=False, allow_absent=False):
    path = Path(path).absolute()
    require('..' not in path.parts, 'parent aliases are not supported: ' + str(path))
    for parent in reversed([path, *path.parents]):
        require(not parent.is_symlink(), 'symbolic links are not supported: ' + str(parent))
    if allow_absent and not path.exists():
        return path
    mode = path.stat().st_mode
    require((stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)) and not mode & 0o7000,
            'ordinary file/directory and ordinary modes required: ' + str(path))
    return path


def read_bytes(path, limit):
    path = ordinary_path(path)
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_size <= limit, 'file exceeds bound: ' + str(path))
        chunks, size = [], 0
        while block := stream.read(min(65536, limit + 1 - size)):
            chunks.append(block)
            size += len(block)
            require(size <= limit, 'file exceeds bound: ' + str(path))
        after = os.fstat(stream.fileno())
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
            'file changed while reading: ' + str(path))
    data = b''.join(chunks)
    return data, {'bytes': len(data), 'sha256': digest(data), 'mode': stat.S_IMODE(after.st_mode)}


def checked_binding(data, expected, label):
    require(isinstance(expected, dict), 'binding required: ' + label)
    integer(expected.get('bytes'), label + ' byte count')
    sha_value(expected.get('sha256'))
    require(len(data) == expected['bytes'] and digest(data) == expected['sha256'], 'identity changed: ' + label)


def record(data):
    try:
        value = decode(data)
    except (UnicodeDecodeError, ValueError, RecursionError):
        # A selected ordinary body can be arbitrary bytes, including malformed
        # JSON. Known record positions separately require a recognized record.
        return None
    if not isinstance(value, dict) or value.get('format') != SYSTEM or 'revision' not in value:
        return None
    required = {'id', 'kind', 'basis', 'active', 'revision', 'sha256', 'updated_at', 'content', 'relations'}
    require(required <= value.keys(), 'incomplete declared system record')
    require(isinstance(value['id'], str) and re.fullmatch(NATIVE_ID, value['id']), 'invalid record ID')
    integer(value['revision'], 'record revision')
    require(value['revision'] >= 1 and type(value['active']) is bool, 'invalid record state')
    require(isinstance(value['basis'], list), 'record basis must be a list')
    require(sha_value(value['sha256']) == digest(encode({k:v for k,v in value.items() if k != 'sha256'})),
            'intrinsic record digest changed')
    require(data == encode(value), 'system record is not its canonical encoded occurrence')
    return value


# Mirrors continuation_support.exchange._id for independently portable verification.
NATIVE_ID = r'[A-Za-z0-9_-]{1,120}'


def limits(spec):
    supplied = spec.get('limits', {})
    require(isinstance(supplied, dict) and supplied.keys() <= DEFAULTS.keys(), 'unknown resource limit')
    result = {**DEFAULTS, **supplied}
    for key, value in result.items():
        require(integer(value, key, CEILINGS[key]) > 0, 'positive resource limit required')
    return result


@contextmanager
def owner_lock(root, name):
    path = ordinary_path(root / relative(name))
    if fcntl is not None:
        with path.open('rb') as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError('another cooperative owner holds the declared lock') from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt

        class _Overlapped(ctypes.Structure):
            _fields_ = [
                ('Internal', ctypes.c_void_p),
                ('InternalHigh', ctypes.c_void_p),
                ('Offset', wintypes.DWORD),
                ('OffsetHigh', wintypes.DWORD),
                ('hEvent', wintypes.HANDLE),
            ]

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        with path.open('r+b') as stream:
            handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
            ov = _Overlapped()
            ok = kernel32.LockFileEx(handle, 0x00000001 | 0x00000002, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(ov))
            if not ok:
                err = ctypes.get_last_error()
                if err in (32, 33):
                    raise ValueError('another cooperative owner holds the declared lock')
                raise OSError(f'LockFileEx failed with Windows error {err}')
            try:
                yield
            finally:
                ov_u = _Overlapped()
                kernel32.UnlockFileEx(handle, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(ov_u))
        return
    raise OSError('cooperative file locking is unavailable on this platform')


def base_binding(spec, root, budget):
    expected = spec['base']
    require(isinstance(expected, dict) and set(expected) == {'archive', 'bytes', 'sha256', 'snapshot_sha256'}, 'complete base binding required')
    require(isinstance(expected['archive'], str) and Path(expected['archive']).is_absolute(), 'absolute base archive path required')
    path = ordinary_path(Path(expected['archive']))
    integer(expected['bytes'], 'base archive bytes')
    sha_value(expected['sha256']); sha_value(expected['snapshot_sha256'])
    with path.open('rb') as stream:
        initial = os.fstat(stream.fileno())
        require(initial.st_size == expected['bytes'], 'base archive size differs')
        h = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
        require(h.hexdigest() == expected['sha256'], 'base archive digest differs')
        stream.seek(0)
        with zipfile.ZipFile(stream) as z:
            names = z.namelist()
            require(len(names) == len(set(names)) and names.count('snapshot.json') == 1, 'ambiguous base snapshot membership')
            info = z.getinfo('snapshot.json')
            require(info.file_size <= budget['max_metadata_bytes'], 'base snapshot metadata exceeds bound')
            data = z.read(info)
        final = os.fstat(stream.fileno())
    require((initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns)
            == (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns), 'base archive changed during verification')
    require(digest(data) == expected['snapshot_sha256'], 'base snapshot digest differs')
    snapshot = decode(data)
    require(snapshot.get('format') == 'g60-deployment-snapshot/1' and snapshot.get('original_root') == str(root),
            'base snapshot format or original-root relationship differs')
    require(isinstance(snapshot.get('files'), dict) and isinstance(snapshot.get('directories'), dict), 'base membership tables required')
    return {'archive': {'path': str(path), 'bytes': expected['bytes'], 'sha256': expected['sha256']},
            'snapshot': {'bytes': len(data), 'sha256': digest(data)},
            'verification': 'Actual complete archive digest and embedded snapshot digest verified. Original-root relationship checked. Base objects were not restored or individually revalidated by this support capture.'}, snapshot


def capture(root, spec_path, output):
    root = ordinary_path(root, directory=True)
    spec_data, _ = read_bytes(spec_path, MANIFEST_LIMIT)
    spec = decode(spec_data)
    require(isinstance(spec, dict) and spec.get('format') == 'g60-continuation-capture/1', 'unsupported capture specification')
    allowed = {'format', 'work_root', 'object_root', 'owner_lock', 'interpretation', 'expected_interpretation',
               'expected_records', 'extra_files', 'base', 'limits'}
    require(spec.keys() <= allowed, 'unknown capture specification field')
    budget = limits(spec)
    work, objects = relative(spec['work_root']), relative(spec['object_root'])
    interpretation_path = relative(spec['interpretation'])
    require(interpretation_path.startswith(work + '/interpretations/'), 'interpretation must be in the declared work interpretations directory')
    output = ordinary_path(output, allow_absent=True)
    ordinary_path(output.parent, directory=True)
    require(root != output and root not in output.parents and not output.exists(), 'fresh output outside the owner required')
    base, snapshot = base_binding(spec, root, budget)
    files, bodies, directories, absent, queue, seen_records, basis_edges = {}, {}, {}, set(), deque(), set(), []
    total = 0
    root_mode = stat.S_IMODE(root.stat().st_mode)

    def include(name, expected=None, reason='declared support'):
        nonlocal total
        name = relative(name)
        data, meta = read_bytes(root / name, budget['max_file_bytes'])
        if expected is not None:
            checked_binding(data, expected, name)
        if name in files:
            require({k:files[name][k] for k in ('bytes','sha256','mode')} == meta, 'source changed during capture: ' + name)
            return data
        require(name not in absent and len(files) < budget['max_files'], 'inconsistent or over-budget file coverage')
        total += len(data)
        require(total <= budget['max_total_bytes'], 'declared support exceeds total byte bound')
        files[name] = {**meta, 'reason': reason}
        bodies.setdefault(meta['sha256'], data)
        for parent in PurePosixPath(name).parents:
            if str(parent) == '.':
                continue
            p = ordinary_path(root / str(parent), directory=True)
            mode = stat.S_IMODE(p.stat().st_mode)
            require(str(parent) not in directories or directories[str(parent)] == mode, 'directory mode changed')
            directories[str(parent)] = mode
        parsed = record(data)
        if parsed is not None and meta['sha256'] not in seen_records:
            seen_records.add(meta['sha256'])
            queue.append((name, parsed))
        return data

    def census():
        folder = ordinary_path(root / work / 'records', directory=True)
        entries = sorted(folder.glob('*.json'))
        require(len(entries) <= budget['max_record_scan'], 'current record census exceeds bound')
        rows, scanned = [], 0
        for p in entries:
            data, _ = read_bytes(p, budget['max_file_bytes'])
            scanned += len(data)
            require(scanned <= budget['max_total_bytes'], 'current record census bytes exceed bound')
            r = record(data)
            require(r is not None and p.name == r['id'] + '.json', 'invalid current record census member')
            if r['kind'] == 'obligation':
                rows.append({k:r[k] for k in ('id','revision','sha256')})
        return rows, [p.name for p in entries], scanned

    with owner_lock(root, spec['owner_lock']):
        raw = include(interpretation_path, spec['expected_interpretation'], 'declared immutable interpretation')
        interpretation = decode(raw)
        require(isinstance(interpretation, dict) and interpretation.get('format') == SYSTEM
                and isinstance(interpretation.get('evidence'), list)
                and isinstance(interpretation.get('obligation_basis'), list), 'unsupported interpretation')
        goal = record(include(work + '/records/goal.json', reason='interpretation goal'))
        require(goal is not None and goal['id'] == 'goal'
                and goal['revision'] == interpretation['goal_revision']
                and goal['sha256'] == interpretation['goal_sha256'], 'interpretation goal changed')
        obligations, record_names, scanned = census()
        require(obligations == interpretation['obligation_basis'], 'interpretation obligation membership or identity changed')
        for expected in obligations:
            r = record(include(work + '/records/' + expected['id'] + '.json', reason='interpretation obligation basis'))
            require(r is not None and all(r[k] == expected[k] for k in ('id','revision','sha256')), 'obligation identity changed')
        for row in interpretation['evidence']:
            require(isinstance(row, dict) and row.get('sha256') is not None, 'required interpretation evidence must have a body')
            include(work + '/' + relative(row['path']), row, 'interpretation evidence')
        expected_records = spec.get('expected_records', [])
        require(isinstance(expected_records, list), 'expected_records must be a list')
        verified_records, expected_paths, expected_ids = [], set(), set()
        for expected in expected_records:
            require(isinstance(expected, dict) and set(expected) == {'path','id','revision','sha256'}, 'expected record receipt fields required')
            name = relative(expected['path'])
            require(name not in expected_paths and expected['id'] not in expected_ids, 'duplicate expected record path or ID')
            expected_paths.add(name)
            expected_ids.add(expected['id'])
            r = record(include(name, reason='actual generated-record receipt'))
            require(r is not None and all(r[k] == expected[k] for k in ('id','revision','sha256')), 'generated record differs from successful write receipt')
            verified_records.append({**expected, 'file': {k:files[name][k] for k in ('bytes','sha256','mode')}})
        extras = spec.get('extra_files', [])
        require(isinstance(extras, list) and len(extras) == len(set(extras)), 'unique extra file paths required')
        for name in extras:
            include(name, reason='explicit extra support')
        while queue:
            record_path, parsed = queue.popleft()
            for row in parsed['basis']:
                require(isinstance(row, dict) and set(row) == {'path','bytes','sha256'}, 'unsupported record-basis binding')
                name = work + '/' + relative(row['path'])
                integer(row['bytes'], 'record-basis bytes')
                captured_object = None
                if row['sha256'] is None:
                    require(row['bytes'] == 0, 'invalid captured absence')
                else:
                    captured_object = objects + '/' + sha_value(row['sha256'])
                    include(captured_object, row, 'captured record-basis body')
                named = ordinary_path(root / name, allow_absent=True)
                if named.exists():
                    include(name, reason='current named state of a declared record basis')
                    current = files[name]['sha256']
                else:
                    require(name not in files, 'named source disappeared during capture')
                    absent.add(name)
                    current = None
                basis_edges.append({'record_file': record_path, 'declared_path': row['path'],
                                    'captured_sha256': row['sha256'], 'captured_object': captured_object,
                                    'current_named_sha256': current})
        require(census()[:2] == (obligations, record_names), 'current record census changed during capture')
        for name, expected in files.items():
            _, actual = read_bytes(root / name, budget['max_file_bytes'])
            require(actual == {k:expected[k] for k in ('bytes','sha256','mode')}, 'source changed before publication: ' + name)
        for name in absent:
            require(not ordinary_path(root / name, allow_absent=True).exists(), 'observed absent path appeared')
        for name, mode in directories.items():
            require(stat.S_IMODE(ordinary_path(root / name, directory=True).stat().st_mode) == mode, 'directory changed before publication')
        require(stat.S_IMODE(root.stat().st_mode) == root_mode, 'source root mode changed before publication')
        relations = {}
        for name, row in files.items():
            previous = snapshot['files'].get(name)
            same = previous is not None and all(previous[k] == row[k] for k in ('bytes','sha256','mode'))
            relations[name] = 'same-as-base' if same else ('replacement-relative-to-base' if previous else 'added-after-base')
        manifest = {'format': FORMAT, 'source_root': str(root), 'source_root_mode': root_mode,
                    'selection': {'work_root': work, 'object_root': objects,
                                  'owner_lock': relative(spec['owner_lock']), 'extra_files': extras},
                    'base': base, 'limits': budget, 'files': files, 'directories': directories,
                    'observed_absent_paths': sorted(absent), 'base_relations': relations,
                    'interpretation': {'path': interpretation_path, 'id': interpretation['id'],
                                       'bytes': len(raw), 'sha256': digest(raw)},
                    'verified_records': verified_records, 'record_basis_edges': basis_edges,
                    'current_obligation_basis': obligations,
                    'record_census': {'records': len(record_names), 'names': record_names, 'bytes_read': scanned},
                    'source_stability_checked': True,
                    'closure': 'Declared interpretation goal, obligation membership and direct evidence; recursively declared record-basis objects and current named-path state. Previous-object chains, arbitrary content links and undeclared semantic dependencies are not automatically selected.',
                    'recovery': 'Fresh exact support projection only. This is a selected partial tree, not a runnable owner, automatic patch, authorization or effect replay. Composing with the separately verified base requires an explicit compatible realization; replacements and observed absences retain their stated meanings.'}
        verify_closure(manifest, bodies)
        descriptor = encode(manifest)
        require(len(descriptor) <= MANIFEST_LIMIT, 'support manifest exceeds bound')
        fd, temporary_name = tempfile.mkstemp(prefix='support-', suffix='.tmp', dir=output.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, 'w+b') as stream:
                with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                    for name, body in [('manifest.json', descriptor), *[('objects/' + k, v) for k,v in sorted(bodies.items())]]:
                        info = zipfile.ZipInfo(name)
                        info.external_attr = 0o100644 << 16
                        info.compress_type = zipfile.ZIP_DEFLATED
                        z.writestr(info, body)
                stream.flush(); os.fsync(stream.fileno())
            require(temporary.stat().st_size <= ARCHIVE_LIMIT, 'support archive exceeds bound')
            os.link(temporary, output)
            if hasattr(os, 'O_DIRECTORY'):
                directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    archive_data, archive_meta = read_bytes(output, ARCHIVE_LIMIT)
    return {'status': 'captured', 'archive': {'path': str(output), 'bytes': len(archive_data), 'sha256': archive_meta['sha256']},
            'files': len(files), 'file_bytes': total, 'interpretation': manifest['interpretation'],
            'verified_records': verified_records, 'base': base, 'source_stability_checked': True,
            'scope': manifest['closure'] + ' ' + manifest['recovery']}


def verify_closure(manifest, bodies):
    """Recheck the declared structural selection using only retained bodies.

    Current membership/stability and base checking are capture observations. A
    portable archive cannot re-observe the source owner or its separately held
    base; these observations are authenticated by its caller-supplied byte pin.
    """
    files = manifest['files']
    selected, seen_records, queue, absences, edges = set(), set(), deque(), set(), []
    selection = manifest.get('selection')
    require(isinstance(selection, dict) and set(selection) == {'work_root','object_root','owner_lock','extra_files'}, 'complete structural selection required')
    work, objects = relative(selection['work_root']), relative(selection['object_root'])
    relative(selection['owner_lock'])

    def include(name, expected=None):
        name = relative(name)
        require(name in files, 'required structural support is missing: ' + name)
        body = bodies[files[name]['sha256']]
        if expected is not None:
            checked_binding(body, expected, name)
        selected.add(name)
        parsed = record(body)
        if parsed is not None and files[name]['sha256'] not in seen_records:
            seen_records.add(files[name]['sha256'])
            queue.append((name, parsed))
        return body

    pin = manifest.get('interpretation')
    require(isinstance(pin, dict) and set(pin) == {'path','id','bytes','sha256'}, 'interpretation binding required')
    require(relative(pin['path']).startswith(work + '/interpretations/'), 'interpretation outside declared work area')
    raw = include(pin['path'], pin)
    row = decode(raw)
    require(isinstance(row, dict) and row.get('format') == SYSTEM and row.get('id') == pin['id']
            and isinstance(row.get('evidence'), list) and isinstance(row.get('obligation_basis'), list), 'invalid retained interpretation')
    goal = record(include(work + '/records/goal.json'))
    require(goal is not None and goal['id'] == 'goal' and goal['revision'] == row['goal_revision']
            and goal['sha256'] == row['goal_sha256'], 'retained interpretation goal differs')
    obligations = row['obligation_basis']
    require(obligations == manifest.get('current_obligation_basis'), 'retained obligation basis differs')
    obligation_ids = []
    for expected in obligations:
        require(isinstance(expected, dict) and set(expected) == {'id','revision','sha256'}, 'invalid obligation binding')
        r = record(include(work + '/records/' + relative(expected['id']) + '.json'))
        require(r is not None and r['kind'] == 'obligation'
                and all(r[k] == expected[k] for k in ('id','revision','sha256')), 'retained obligation differs')
        obligation_ids.append(expected['id'])
    # Native System.records and capture.census sort <id>.json filenames.
    # Bare-ID order differs for prefix-related identities; uniqueness remains required.
    require(obligation_ids == sorted(set(obligation_ids), key=lambda ident: ident + '.json'),
            'duplicate or unordered obligation membership')
    for expected in row['evidence']:
        require(isinstance(expected, dict) and expected.get('sha256') is not None, 'evidence must have retained bytes')
        include(work + '/' + relative(expected['path']), expected)
    receipts = manifest.get('verified_records')
    require(isinstance(receipts, list), 'generated record bindings required')
    paths, ids = set(), set()
    for receipt in receipts:
        require(isinstance(receipt, dict) and set(receipt) == {'path','id','revision','sha256','file'}, 'invalid generated record binding')
        name = relative(receipt['path'])
        require(name not in paths and receipt['id'] not in ids, 'duplicate generated record binding')
        paths.add(name); ids.add(receipt['id'])
        r = record(include(name))
        require(r is not None and all(r[k] == receipt[k] for k in ('id','revision','sha256')), 'retained generated record differs')
        require(receipt['file'] == {k:files[name][k] for k in ('bytes','sha256','mode')}, 'generated record file binding differs')
    extras = selection['extra_files']
    require(isinstance(extras, list) and len(extras) == len(set(extras)), 'unique explicit extra paths required')
    for name in extras:
        include(name)
    while queue:
        record_path, parsed = queue.popleft()
        for basis in parsed['basis']:
            require(isinstance(basis, dict) and set(basis) == {'path','bytes','sha256'}, 'invalid retained record-basis binding')
            name = work + '/' + relative(basis['path'])
            integer(basis['bytes'], 'retained basis bytes')
            captured_object = None
            if basis['sha256'] is None:
                require(basis['bytes'] == 0, 'invalid retained captured absence')
            else:
                captured_object = objects + '/' + sha_value(basis['sha256'])
                include(captured_object, basis)
            if name in files:
                include(name)
                current = files[name]['sha256']
            else:
                require(name in manifest['observed_absent_paths'], 'required named-path state is missing: ' + name)
                absences.add(name)
                current = None
            edges.append({'record_file':record_path, 'declared_path':basis['path'],
                          'captured_sha256':basis['sha256'], 'captured_object':captured_object,
                          'current_named_sha256':current})
    require(selected == set(files), 'support membership exceeds declared structural selection')
    require(absences == set(manifest['observed_absent_paths']) and edges == manifest.get('record_basis_edges'), 'retained record-basis closure differs')
    census = manifest.get('record_census')
    require(isinstance(census, dict) and set(census) == {'records','names','bytes_read'}, 'captured membership observation required')
    count = integer(census['records'], 'record census count', manifest['limits']['max_record_scan'])
    integer(census['bytes_read'], 'record census byte count', manifest['limits']['max_total_bytes'])
    names = census['names']
    require(isinstance(names, list) and all(isinstance(n, str) and re.fullmatch(NATIVE_ID + r'\.json', n) for n in names)
            and names == sorted(set(names)) and len(names) == count
            and {'goal.json', *[n + '.json' for n in obligation_ids]} <= set(names), 'invalid captured current record membership')
    require(manifest.get('source_stability_checked') is True, 'capture stability observation required')
    base = manifest.get('base')
    require(isinstance(base, dict) and set(base) == {'archive','snapshot','verification'}, 'captured base verification required')
    for key in ('archive','snapshot'):
        integer(base[key]['bytes'], 'base ' + key + ' bytes')
        sha_value(base[key]['sha256'])
    require(isinstance(base['archive'].get('path'), str) and Path(base['archive']['path']).is_absolute()
            and isinstance(base['verification'], str), 'invalid base capture observation')
    relations = manifest.get('base_relations')
    require(isinstance(relations, dict) and relations.keys() == files.keys()
            and set(relations.values()) <= {'same-as-base','replacement-relative-to-base','added-after-base'}, 'invalid declared base relationship table')
    require(isinstance(manifest.get('source_root'), str) and Path(manifest['source_root']).is_absolute()
            and isinstance(manifest.get('closure'), str) and isinstance(manifest.get('recovery'), str), 'scope and original source-root statements required')


def verified_archive(path, expected_sha):
    sha_value(expected_sha)
    data, meta = read_bytes(path, ARCHIVE_LIMIT)
    require(meta['sha256'] == expected_sha, 'support archive digest differs')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        names = [row.filename for row in infos]
        require(len(names) == len(set(names)) and names.count('manifest.json') == 1, 'duplicate or missing archive member')
        require(len(names) <= CEILINGS['max_files'] + 1, 'archive member count exceeds bound')
        for row in infos:
            mode = row.external_attr >> 16
            require(not row.is_dir() and stat.S_IFMT(mode) in (0, stat.S_IFREG) and not mode & 0o7000, 'ordinary archive members required')
            require(row.filename == 'manifest.json' or re.fullmatch('objects/[0-9a-f]{64}', row.filename), 'unexpected archive member')
            require(not row.flag_bits & 1, 'encrypted support members are not supported')
        require(z.getinfo('manifest.json').file_size <= MANIFEST_LIMIT, 'support manifest exceeds bound')
        manifest = decode(z.read('manifest.json'))
        require(isinstance(manifest, dict) and manifest.get('format') == FORMAT, 'unsupported support format')
        budget = limits({'limits': manifest.get('limits')})
        files, directories = manifest.get('files'), manifest.get('directories')
        require(isinstance(files, dict) and isinstance(directories, dict) and 0 < len(files) <= budget['max_files'], 'invalid support file table')
        integer(manifest.get('source_root_mode'), 'root mode', 0o777)
        object_rows, parents, total = {}, set(), 0
        for name, row in files.items():
            relative(name)
            require(isinstance(row, dict) and {'bytes','sha256','mode'} <= row.keys(), 'invalid file entry')
            size = integer(row['bytes'], 'file byte count', budget['max_file_bytes'])
            key = sha_value(row['sha256']); integer(row['mode'], 'file mode', 0o777)
            total += size
            require(total <= budget['max_total_bytes'], 'support total byte bound exceeded')
            require(key not in object_rows or object_rows[key] == size, 'inconsistent shared object size')
            object_rows[key] = size
            parents.update(str(p) for p in PurePosixPath(name).parents if str(p) != '.')
        require(set(directories) == parents and not set(files) & parents, 'inconsistent directory/file topology')
        for name, mode in directories.items():
            relative(name); integer(mode, 'directory mode', 0o777)
        absent = manifest.get('observed_absent_paths')
        require(isinstance(absent, list) and len(absent) == len(set(absent)), 'invalid absence list')
        for name in absent:
            relative(name)
            require(name not in files and name not in directories
                    and not any(p.startswith(name + '/') for p in files), 'absence conflicts with support membership')
        expected_members = {'manifest.json'} | {'objects/' + key for key in object_rows}
        require(set(names) == expected_members, 'archive object membership differs from declared support')
        bodies = {}
        for key, size in object_rows.items():
            info = z.getinfo('objects/' + key)
            require(info.file_size == size, 'archive object size differs')
            body = z.read(info)
            require(len(body) == size and digest(body) == key, 'archive object bytes differ')
            bodies[key] = body
    verify_closure(manifest, bodies)
    return manifest, bodies, {'path': str(Path(path).absolute()), 'bytes': len(data), 'sha256': meta['sha256']}


def verify(path, expected_sha):
    manifest, _, archive = verified_archive(path, expected_sha)
    return {'status': 'verified', 'archive': archive, 'files': len(manifest['files']),
            'file_bytes': sum(r['bytes'] for r in manifest['files'].values()),
            'interpretation': manifest['interpretation'], 'verified_records': manifest['verified_records'],
            'base': manifest['base'], 'source_stability_checked': manifest['source_stability_checked'],
            'scope': 'Exact declared structural support, ordinary modes and complete object bytes checked. Source membership, stability and base verification are retained capture observations, not a new observation of those separate artifacts. This does not establish semantic closure or replay an effect.'}


def recover(path, expected_sha, destination):
    manifest, bodies, archive = verified_archive(path, expected_sha)
    destination = ordinary_path(destination, allow_absent=True)
    ordinary_path(destination.parent, directory=True)
    require(not destination.exists(), 'recovery destination must be fresh and absent')
    destination.mkdir(mode=0o700)
    for name in sorted(manifest['directories'], key=lambda x:(x.count('/'),x)):
        (destination / name).mkdir()
    for name, row in sorted(manifest['files'].items()):
        target = destination / name
        with target.open('xb') as output:
            output.write(bodies[row['sha256']])
        os.chmod(target, row['mode'])
    for name, mode in sorted(manifest['directories'].items(), key=lambda x:(-x[0].count('/'),x[0])):
        os.chmod(destination / name, mode)
    os.chmod(destination, manifest['source_root_mode'])
    actual_files, actual_dirs = {}, {}
    for p in sorted(destination.rglob('*')):
        relative_name = p.relative_to(destination).as_posix()
        require(not p.is_symlink(), 'recovered symlink appeared')
        if p.is_file():
            _, actual_files[relative_name] = read_bytes(p, manifest['limits']['max_file_bytes'])
        elif p.is_dir():
            actual_dirs[relative_name] = stat.S_IMODE(p.stat().st_mode)
        else:
            raise ValueError('unexpected recovered file kind')
    expected = {n:{k:r[k] for k in ('bytes','sha256','mode')} for n,r in manifest['files'].items()}
    require(actual_files == expected and actual_dirs == manifest['directories'], 'recovered projection differs')
    return {'status': 'recovered-support-projection', 'destination': str(destination),
            'archive': archive, 'source_root': manifest['source_root'], 'files': len(actual_files),
            'file_bytes': sum(r['bytes'] for r in actual_files.values()),
            'scope': manifest['recovery'],
            'failure_boundary': 'All archive validation precedes creating the fresh destination. An unexpected filesystem failure during writing can leave a partial fresh directory; inspect it rather than overwrite or silently retry.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    c = sub.add_parser('capture'); c.add_argument('--root', type=Path, required=True); c.add_argument('--spec', type=Path, required=True); c.add_argument('--output', type=Path, required=True)
    for action in ('verify','recover'):
        a = sub.add_parser(action); a.add_argument('--archive', type=Path, required=True); a.add_argument('--sha256', required=True)
        if action == 'recover': a.add_argument('--to', type=Path, required=True)
    args = p.parse_args()
    try:
        if args.action == 'capture': result = capture(args.root, args.spec, args.output)
        elif args.action == 'verify': result = verify(args.archive, args.sha256)
        else: result = recover(args.archive, args.sha256, args.to)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError) as exc:
        print(json.dumps({'status':'unresolved','operation':args.action,'error':str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
