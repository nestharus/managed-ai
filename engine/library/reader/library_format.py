"""Catalogue format, validation and bounded file access for the source library."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

_REPARSE_ATTR = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)


def _is_reparse_or_symlink(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & _REPARSE_ATTR)


FORMAT = 'knowledge-library/1'
SELECTION = 'knowledge-selection/1'
MAX_MANIFEST = 4 * 1024 * 1024
MAX_BODY = 2 * 1024 * 1024
MAX_BODIES = 2048
MAX_MODULES = 256
MAX_PACKAGE = 32 * 1024 * 1024
MAX_ARCHIVE = 40 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{64}\Z')
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,239}\Z')


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def need(condition, message):
    if not condition:
        raise ValueError(message)


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, 'duplicate JSON key: ' + key)
        result[key] = value
    return result


def decoded(data):
    def refuse_constant(value):
        raise ValueError('non-JSON numeric constant: ' + value)
    return json.loads(data, object_pairs_hook=unique, parse_constant=refuse_constant)


def ordinary(path):
    path = Path(path).expanduser().absolute()
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        need(not _is_reparse_or_symlink(info), 'symlink path is unsupported: ' + str(part))
    return path


def _read_stream_bounded(path, info, limit):
    need(not _is_reparse_or_symlink(info) and stat.S_ISREG(info.st_mode), 'ordinary file required: ' + str(path))
    need(info.st_size <= limit, 'file exceeds byte bound: ' + str(path))
    with path.open('rb') as stream:
        # A read(limit + 1) can allocate the entire allowed bound even for a
        # small file. Bound each allocation while still detecting growth past
        # the limit and consuming short reads until actual EOF.
        chunks, remaining = [], limit + 1
        while remaining:
            chunk = stream.read(min(64 * 1024, remaining))
            if not chunk: break
            chunks.append(chunk); remaining -= len(chunk)
        body = b''.join(chunks)
    need(len(body) <= limit, 'file grew beyond byte bound: ' + str(path))
    return body


def read_bytes(path, limit):
    path = ordinary(path)
    try:
        info = path.lstat()
    except OSError:
        raise ValueError('ordinary file required: ' + str(path)) from None
    return _read_stream_bounded(path, info, limit)


def read_bytes_under(validated_root, rel_path, limit):
    """Read an ordinary file below an already-validated root, checking every descendant segment."""
    parts = [p for p in str(rel_path).split('/') if p not in ('', '.')]
    need(bool(parts) and all(p != '..' for p in parts), 'unsafe relative identity')
    cur = Path(validated_root)
    for segment in parts[:-1]:
        cur = cur / segment
        try:
            info = cur.lstat()
        except OSError:
            raise ValueError('ordinary file required: ' + str(cur)) from None
        need(not _is_reparse_or_symlink(info) and stat.S_ISDIR(info.st_mode), 'symlink path is unsupported: ' + str(cur))
    leaf = cur / parts[-1]
    try:
        info = leaf.lstat()
    except OSError:
        raise ValueError('ordinary file required: ' + str(leaf)) from None
    return _read_stream_bounded(leaf, info, limit)


def relative(value):
    need(isinstance(value, str) and IDENT.fullmatch(value), 'invalid relative identity')
    need(
        not value.startswith('/')
        and '\\' not in value
        and not (len(value) >= 2 and value[1] == ':')
        and all(x not in {'', '.', '..'} and not (len(x) >= 2 and x[1] == ':') for x in value.split('/')),
        'unsafe relative identity',
    )
    return value


def text(value, label, maximum=16384):
    need(isinstance(value, str) and value.strip() and len(value.encode()) <= maximum, 'invalid ' + label)
    return value


def sha(value):
    need(isinstance(value, str) and SHA.fullmatch(value), 'invalid SHA-256')
    return value


def integer(value, label, low, high):
    need(type(value) is int and low <= value <= high, 'invalid ' + label)
    return value


def check_manifest(value):
    need(isinstance(value, dict) and set(value) == {'format', 'edition', 'bodies', 'modules', 'provenance'}, 'invalid library fields')
    need(value['format'] == FORMAT, 'unsupported library format; explicit migration required')
    text(value['edition'], 'edition', 512)
    need(isinstance(value['provenance'], dict), 'provenance must be an object')
    bodies, modules = value['bodies'], value['modules']
    need(isinstance(bodies, list) and 1 <= len(bodies) <= MAX_BODIES, 'invalid body count')
    need(isinstance(modules, list) and len(modules) <= MAX_MODULES, 'invalid module count')
    by_id = {}
    for row in bodies:
        need(isinstance(row, dict) and set(row) == {'id', 'title', 'summary', 'object', 'sha256', 'bytes', 'source', 'link_base', 'references'}, 'invalid body fields')
        ident = relative(row['id'])
        need(ident not in by_id, 'duplicate body identity: ' + ident)
        text(row['title'], 'title', 1024); text(row['summary'], 'summary', 4096)
        h = sha(row['sha256'])
        need(row['object'] == 'objects/' + h + '.txt', 'body object is not content-addressed')
        integer(row['bytes'], 'body bytes', 0, MAX_BODY)
        need(isinstance(row['source'], dict) and set(row['source']) == {'path', 'sha256'}, 'invalid body source')
        relative(row['source']['path']); relative(row['link_base'])
        need(row['source']['sha256'] == h, 'complete copied source must have the body digest')
        need(isinstance(row['references'], list) and len(row['references']) <= 2048, 'invalid references')
        for ref in row['references']:
            need(isinstance(ref, dict) and set(ref) == {'target', 'anchor', 'kind'}, 'invalid reference')
            text(ref['target'], 'reference target', 8192)
            need(isinstance(ref['anchor'], str) and len(ref['anchor']) <= 1024, 'invalid reference anchor')
            need(isinstance(ref['kind'], str) and ref['kind'] in {'source', 'url'}, 'invalid reference kind')
        by_id[ident] = row
    need(sum(row['bytes'] for row in bodies) <= MAX_PACKAGE, 'library exceeds declared total body byte bound')
    module_ids = set()
    for row in modules:
        need(isinstance(row, dict) and set(row) == {'id', 'title', 'purpose', 'members', 'requires', 'parents', 'external_participation'}, 'invalid module fields')
        ident = relative(row['id']); need(ident not in module_ids, 'duplicate module identity: ' + ident)
        module_ids.add(ident)
        text(row['title'], 'module title', 1024); text(row['purpose'], 'module purpose')
        for key in ('members', 'requires'):
            need(isinstance(row[key], list) and len(row[key]) <= MAX_BODIES, 'invalid module ' + key)
            for member in row[key]: relative(member)
            need(len(row[key]) == len(set(row[key])), 'duplicate module ' + key)
        need(row['members'], 'a module needs at least one complete member body')
        need(all(x in by_id for x in row['members']), 'module has an unavailable required body: ' + ident)
        need(isinstance(row['parents'], list) and len(row['parents']) <= MAX_MODULES, 'invalid parents')
        for parent in row['parents']:
            need(isinstance(parent, dict) and set(parent) == {'id', 'role'}, 'invalid parent relation')
            relative(parent['id']); text(parent['role'], 'parent role', 4096)
        need(isinstance(row['external_participation'], list) and len(row['external_participation']) <= 256, 'invalid external participation')
        for item in row['external_participation']: text(item, 'external participation')
    for row in modules:
        need(all(x in module_ids for x in row['requires']), 'module has an unavailable required module: ' + row['id'])
    return value
