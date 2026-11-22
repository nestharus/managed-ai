"""Resolve selected complete bodies and verify data bundle interchange."""
import io
import os
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile

from .library_format import (FORMAT, MAX_ARCHIVE, MAX_BODIES, MAX_BODY, MAX_MANIFEST,
                             MAX_PACKAGE, SELECTION, check_manifest, decoded,
                             digest, encoded, integer, need, ordinary, read_bytes,
                             relative, sha, text)


def resolve(libraries, selection, limit=MAX_PACKAGE):
    integer(limit, 'selection body bytes', 1, MAX_PACKAGE)
    need(isinstance(selection, dict) and set(selection) == {'format', 'purpose', 'bodies', 'modules'}, 'invalid selection fields')
    need(selection['format'] == SELECTION, 'unsupported selection format')
    text(selection['purpose'], 'receiving purpose')
    by_hash = {lib.sha256: lib for lib in libraries}
    chosen, chosen_modules, seen = {}, {}, set()
    for key in ('bodies', 'modules'):
        need(isinstance(selection[key], list) and len(selection[key]) <= MAX_BODIES, 'invalid selected ' + key)
        for pin in selection[key]:
            need(isinstance(pin, dict) and set(pin) == {'library_sha256', 'id', 'sha256'}, 'invalid source pin')
            h = sha(pin['library_sha256']); sha(pin['sha256']); relative(pin['id'])
            need(h in by_hash, 'selected library unavailable or changed')
            lib = by_hash[h]
            available = lib.bodies if key == 'bodies' else lib.modules
            need(pin['id'] in available, 'selected identity unavailable')
            expected = lib.pin(available[pin['id']], 'body' if key == 'bodies' else 'module')
            need(pin == expected, 'selected source pin changed')
            if key == 'bodies': chosen[(h, pin['id'])] = (lib, available[pin['id']])
            else:
                pending = [pin['id']]
                while pending:
                    ident = pending.pop()
                    if (h, ident) in seen: continue
                    seen.add((h, ident)); row = lib.modules[ident]
                    chosen_modules[(h, ident)] = (lib, row)
                    for body_id in row['members']: chosen[(h, body_id)] = (lib, lib.bodies[body_id])
                    pending.extend(row['requires'])
    need(chosen, 'selection needs at least one complete body')
    total, data = 0, {}
    for key, (lib, row) in chosen.items():
        need(row['bytes'] <= limit - total, 'selected complete bodies exceed byte bound')
        body = lib.body(row['id']); total += len(body); data[key] = body
    return chosen, chosen_modules, data, total


def context(libraries, selection, max_bytes=8 * 1024 * 1024):
    integer(max_bytes, 'context bytes', 1, MAX_PACKAGE)
    rows, modules, data, total = resolve(libraries, selection, max_bytes)
    result = {'format': 'knowledge-context/1', 'purpose': selection['purpose'], 'selection': selection,
            'body_bytes': total, 'modules': [{'pin': lib.pin(row, 'module'), 'entry': row} for lib, row in modules.values()],
            'bodies': [{'pin': lib.pin(row), 'entry': row, 'text': data[key].decode()} for key, (lib, row) in rows.items()],
            'scope': 'Complete selected source exposure. No inferred applicability, model invocation, publication or effect authority.'}
    need(len(encoded(result)) <= MAX_PACKAGE + MAX_MANIFEST, 'rendered context exceeds total byte bound')
    return result


def product(libraries, selection, edition):
    rows, modules, data, _ = resolve(libraries, selection)
    body_map, module_map, objects = {}, {}, {}
    for key, (_, row) in rows.items():
        need(row['id'] not in body_map or body_map[row['id']] == row, 'body identity collision; select one version explicitly: ' + row['id'])
        body_map[row['id']] = row; objects[row['object']] = data[key]
    for _, row in modules.values():
        need(row['id'] not in module_map or module_map[row['id']] == row, 'module identity collision; select one interpretation explicitly: ' + row['id'])
        module_map[row['id']] = row
    refs = [{'from': row['id'], **ref} for row in body_map.values() for ref in row['references']
            if ref['kind'] == 'url' or ref['target'] not in body_map]
    manifest = {'format': FORMAT, 'edition': text(edition, 'edition', 512),
        'bodies': sorted(body_map.values(), key=lambda x: x['id']),
        'modules': sorted(module_map.values(), key=lambda x: x['id']),
        'provenance': {'selection': selection, 'source_libraries': sorted({lib.sha256 for lib, _ in rows.values()}),
            'outside_reference_routes': refs,
            'scope': 'The required bodies/modules declared by this selection are included. Other reference routes are disclosed, not asserted to be supplied prerequisites. Parent roles do not imply inherited permission or execution.'}}
    check_manifest(manifest)
    raw = encoded(manifest); need(len(raw) <= MAX_MANIFEST, 'product manifest exceeds byte bound')
    return raw, objects


def export(libraries, selection, edition, output):
    raw, objects = product(libraries, selection, edition)
    output = ordinary(output)
    need(output.parent.is_dir(), 'existing output parent required')
    need(not output.exists(), 'output exists; inspect it rather than overwrite')
    with output.open('xb') as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted({'catalogue.json': raw, **objects}.items()):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)); info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16; archive.writestr(info, data)
    return inspect_bundle(output)


def bundle_bytes(path):
    archive_bytes = read_bytes(path, MAX_ARCHIVE)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        infos = archive.infolist(); names = [x.filename for x in infos]
        need(len(names) == len(set(names)) and len(names) <= MAX_BODIES + 1, 'duplicate or excessive archive members')
        need('catalogue.json' in names, 'bundle catalogue absent')
        for info in infos:
            need(not info.is_dir() and not info.flag_bits & 1, 'unsupported bundle member')
            mode = info.external_attr >> 16
            need(stat.S_IFMT(mode) in {0, stat.S_IFREG}, 'bundle member is not an ordinary file')
            bound = MAX_MANIFEST if info.filename == 'catalogue.json' else MAX_BODY
            need(info.file_size <= bound, 'bundle member exceeds byte bound')
        need(sum(x.file_size for x in infos) <= MAX_PACKAGE + MAX_MANIFEST, 'bundle total exceeds byte bound')
        def get(name, bound):
            with archive.open(name) as stream: data = stream.read(bound + 1)
            need(len(data) <= bound, 'bundle member grew beyond byte bound')
            return data
        raw = get('catalogue.json', MAX_MANIFEST); manifest = check_manifest(decoded(raw))
        expected = {'catalogue.json'} | {row['object'] for row in manifest['bodies']}
        need(set(names) == expected, 'bundle has missing or unselected members')
        objects = {}
        for row in manifest['bodies']:
            if row['object'] not in objects: objects[row['object']] = get(row['object'], MAX_BODY)
            data = objects[row['object']]
            need(len(data) == row['bytes'] and digest(data) == row['sha256'], 'bundle body/source mismatch: ' + row['id'])
            data.decode('utf-8')
    return archive_bytes, raw, manifest, objects


def inspect_bundle(path):
    archive, raw, manifest, objects = bundle_bytes(path)
    return {'format': 'knowledge-package-inspection/1', 'archive_sha256': digest(archive),
        'archive_bytes': len(archive), 'library_sha256': digest(raw), 'edition': manifest['edition'],
        'bodies': len(manifest['bodies']), 'modules': len(manifest['modules']),
        'body_bytes': sum(len(x) for x in objects.values()), 'complete_declared_closure': True,
        'provenance': manifest['provenance'], 'scope': 'Verified data package; no semantic acceptance or executable installation.'}


def import_bundle(path, store, *, open_library):
    archive, raw, manifest, objects = bundle_bytes(path)
    store = ordinary(store); need(store.is_dir(), 'existing caller-owned store directory required')
    ident = digest(archive); destination = store / ident
    # POSIX file locking serializes short store commits; it is not a semantic supervisor.
    need(os.name == 'posix', 'cooperative import commit requires POSIX')
    import fcntl
    lock = ordinary(store / 'import.lock')
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(lock, flags, 0o600)
    try:
        need(stat.S_ISREG(os.fstat(fd).st_mode), 'import lock must be an ordinary file')
        fcntl.flock(fd, fcntl.LOCK_EX)
        need(not destination.is_symlink(), 'import destination is a symlink')
        if destination.exists():
            existing = open_library(destination); existing.verify()
            need(existing.raw == raw, 'content-addressed import destination differs')
            expected = {'catalogue.json'} | set(objects)
            actual = {p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file() or p.is_symlink()}
            need(actual == expected, 'existing import has unexpected members')
            status = 'already-present-verified'
        else:
            temporary = Path(tempfile.mkdtemp(prefix='.' + ident + '.', dir=store))
            try:
                (temporary / 'objects').mkdir()
                for name, body in {'catalogue.json': raw, **objects}.items():
                    with (temporary / name).open('xb') as stream:
                        stream.write(body); stream.flush(); os.fsync(stream.fileno())
                for directory in (temporary / 'objects', temporary):
                    directory_fd = os.open(directory, os.O_RDONLY)
                    try: os.fsync(directory_fd)
                    finally: os.close(directory_fd)
                os.rename(temporary, destination)
                directory_fd = os.open(store, os.O_RDONLY)
                try: os.fsync(directory_fd)
                finally: os.close(directory_fd)
                status = 'imported-data'
            except BaseException:
                if temporary.exists(): shutil.rmtree(temporary)
                raise
    finally:
        os.close(fd)
    return {'format': 'knowledge-import/1', 'status': status, 'archive_sha256': ident,
        'library_sha256': digest(raw), 'library': str(destination), 'edition': manifest['edition'],
        'bodies': len(manifest['bodies']), 'modules': len(manifest['modules']),
        'authority': 'Data only. No source owner, credentials, effect permission, current selection or executable capability is imported.'}
