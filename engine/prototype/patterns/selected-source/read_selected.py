#!/usr/bin/env python3
"""Bounded, read-only acquisition of active-selected project source bodies.

This optional POSIX reader does not import an engine or replace deployment
verification. The caller supplies both the selection identity and manifest pin.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


FORMAT = 'selected-project-sources/1'
DEPLOYMENT_FORMAT = 'agent-system-deployment/1'
EDITION_FORMAT = 'agent-system-edition/1'
RUNTIME_FORMAT = 'g60-agent-system/0.1'
CONTROLLER_VERSION = '0.2.0'
CORE_VERSION = '0.4.1'
CHUNK_BYTES = 64 * 1024
SOURCE_BYTES = 2 * 1024 * 1024
DEFAULT_BYTES = 8 * 1024 * 1024
MAX_BYTES = 32 * 1024 * 1024
DEFAULT_METADATA_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024 * 1024
DEFAULT_OUTPUT_BYTES = 12 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_SOURCES = 64
IDENTITY = re.compile(r'[A-Za-z0-9_-]{1,120}\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')


class Refusal(ValueError):
    """An unsupported, changed, ambiguous or over-budget observation."""


def need(condition, message):
    if not condition:
        raise Refusal(message)


def integer(value, label, maximum=None):
    need(type(value) is int and value >= 0, 'invalid ' + label)
    need(maximum is None or value <= maximum, label + ' exceeds supported bound')
    return value


def limit(value, label, maximum):
    integer(value, label, maximum)
    need(value > 0, label + ' must be positive')
    return value


def string(value, label, maximum=4096):
    need(isinstance(value, str) and bool(value), 'invalid ' + label)
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError as exc:
        raise Refusal('invalid UTF-8 ' + label) from exc
    need(size <= maximum and '\0' not in value, 'invalid ' + label)
    return value


def identity(value):
    need(isinstance(value, str) and IDENTITY.fullmatch(value) is not None,
         'invalid selection or edition identity')
    return value


def sha(value):
    need(isinstance(value, str) and SHA256.fullmatch(value) is not None,
         'invalid SHA-256 pin')
    return value


def relative(value, *, directory=False):
    string(value, 'relative member path')
    if directory and value == '.':
        return value
    need('\\' not in value and ':' not in value and
         all(part not in {'', '.', '..'} for part in value.split('/')),
         'unsafe or noncanonical relative member path')
    return value


def absolute(value):
    need(isinstance(value, (str, Path)), 'canonical absolute path required')
    string(str(value), 'absolute path')
    path = Path(value)
    need(path.is_absolute() and str(path) == str(value) and
         '..' not in path.parts and '\\' not in str(path),
         'canonical absolute path required')
    return path


def shape(value, fields, label):
    need(isinstance(value, dict) and set(value) == set(fields),
         'unsupported or malformed ' + label)


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def decoded(data):
    def reject_constant(_value):
        raise Refusal('nonfinite JSON number')
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=unique,
                           parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise Refusal('invalid bounded JSON metadata') from exc
    need(isinstance(value, dict), 'JSON metadata object required')
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Budget:
    def __init__(self, maximum):
        self.maximum = maximum
        self.used = 0

    def admit(self, amount):
        need(amount <= self.maximum - self.used, 'aggregate read budget exceeded')

    def consume(self, amount):
        self.used += amount
        need(self.used <= self.maximum, 'aggregate read budget exceeded during read')


def fingerprint(info, *, directory=False):
    base = (info.st_dev, info.st_ino, info.st_mode)
    # Directory membership is deliberately outside selected-file assurance.
    return base if directory else base + (info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class Observation:
    def __init__(self):
        self.seen = {}

    def remember(self, path, info, *, directory=False):
        value = (directory, fingerprint(info, directory=directory))
        prior = self.seen.setdefault(path, value)
        need(prior == value, 'path changed during acquisition')

    def directory(self, path, expected_mode=None):
        # Inspect lexical ancestry before opening; do not resolve away links.
        for ancestor in (*reversed(path.parents), path):
            info = ancestor.lstat()
            need(stat.S_ISDIR(info.st_mode), 'ordinary directory ancestry required')
            self.remember(ancestor, info, directory=True)
        info = path.lstat()
        need(not info.st_mode & 0o7000, 'special directory mode is unsupported')
        if expected_mode is not None:
            need(stat.S_IMODE(info.st_mode) == expected_mode,
                 'selected directory mode differs from manifest')

    def file_info(self, path):
        self.directory(path.parent)
        info = path.lstat()
        need(stat.S_ISREG(info.st_mode), 'ordinary file required')
        need(not info.st_mode & 0o7000, 'special file mode is unsupported')
        return info

    def check(self):
        for path, (directory, expected) in self.seen.items():
            need(fingerprint(path.lstat(), directory=directory) == expected,
                 'observed path changed before acquisition completed')


def open_flags():
    need(hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_NONBLOCK'),
         'POSIX no-follow ordinary-file support is required')
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


def read_file(path, observation, budget, *, maximum, expected=None):
    """Stat/fstat, bounded chunks, one growth sentinel, then exact identity.

    No bytes are read for a static oversized file. A growth refusal can consume
    one byte beyond this file's admitted size, and at most one beyond the total
    budget, since the first such refusal ends the operation.
    """
    before = observation.file_info(path)
    if expected is not None:
        need(before.st_size == expected['bytes'], 'selected or entrance byte count differs')
        if 'mode' in expected:
            need(stat.S_IMODE(before.st_mode) == expected['mode'],
                 'selected file mode differs from manifest')
    need(before.st_size <= maximum, 'file exceeds its read bound')
    budget.admit(before.st_size)
    descriptor = os.open(path, open_flags())
    try:
        opened = os.fstat(descriptor)
        need(stat.S_ISREG(opened.st_mode) and fingerprint(opened) == fingerprint(before),
             'file changed between stat and open')
        remaining = opened.st_size + 1
        chunks = []
        while remaining:
            block = os.read(descriptor, min(CHUNK_BYTES, remaining))
            if not block:
                break
            budget.consume(len(block))
            chunks.append(block)
            remaining -= len(block)
        body = b''.join(chunks)
        need(len(body) == opened.st_size, 'file changed size during bounded read')
        need(fingerprint(os.fstat(descriptor)) == fingerprint(opened),
             'file changed during bounded read')
    finally:
        os.close(descriptor)
    need(fingerprint(path.lstat()) == fingerprint(opened), 'opened file path changed')
    if expected is not None:
        need(digest(body) == expected['sha256'], 'selected or entrance content digest differs')
    observation.remember(path, opened)
    return body


@contextmanager
def locked(path, observation):
    before = observation.file_info(path)
    descriptor = os.open(path, open_flags())  # Existing file only; never create.
    try:
        need(fingerprint(os.fstat(descriptor)) == fingerprint(before),
             'lock file changed between stat and open')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refusal('another owner holds a required cooperative lock') from exc
        observation.remember(path, before)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def tree_metadata(value, fields, label):
    shape(value, fields, label)
    need(isinstance(value['files'], list) and isinstance(value['directories'], list),
         'file and directory declarations must be lists')
    files, directories = {}, {}
    for row in value['files']:
        shape(row, ('path', 'mode', 'bytes', 'sha256'), 'file declaration')
        name = relative(row['path'])
        need(name not in files, 'duplicate declared file member')
        integer(row['mode'], 'file mode', 0o777)
        integer(row['bytes'], 'file byte count')
        sha(row['sha256'])
        files[name] = row
    for row in value['directories']:
        shape(row, ('path', 'mode'), 'directory declaration')
        name = relative(row['path'], directory=True)
        need(name not in directories and name not in files, 'duplicate or ambiguous declared member')
        integer(row['mode'], 'directory mode', 0o777)
        directories[name] = row
    need('.' in directories, 'declared project root directory is required')
    for name in (*files, *directories):
        if name == '.':
            continue
        parent = name.rpartition('/')[0] or '.'
        need(parent in directories, 'declared member has no declared directory parent')
    return files, directories


def selection_metadata(value):
    shape(value, ('format', 'selection_id', 'created_at', 'edition', 'manifest_sha256',
                  'previous_selection', 'reason'), 'selection descriptor')
    need(value['format'] == DEPLOYMENT_FORMAT, 'unsupported selection format')
    identity(value['selection_id']); identity(value['edition'])
    identity(value['previous_selection']); sha(value['manifest_sha256'])
    string(value['created_at'], 'selection time', 128)
    string(value['reason'], 'selection reason', 16384)


def acquire(deployment: Path, expected_selection: str, manifest_sha256: str,
            sources: list[str], *, max_bytes=DEFAULT_BYTES,
            max_metadata_bytes=DEFAULT_METADATA_BYTES):
    """Return complete checked source data; use render() before emitting it.

    No filesystem state is created or changed. This operation does not import
    deployment.py or selected engine code. Its guarantee ends with this bounded
    observation, not with an arbitrary later use of these bodies.
    """
    root = absolute(deployment)
    identity(expected_selection); sha(manifest_sha256)
    need(isinstance(sources, list) and 1 <= len(sources) <= MAX_SOURCES,
         'request requires between 1 and 64 selected sources')
    names = [relative(name) for name in sources]
    need(len(set(names)) == len(names), 'duplicate requested source')
    limit(max_bytes, 'selected byte budget', MAX_BYTES)
    limit(max_metadata_bytes, 'metadata byte budget', MAX_METADATA_BYTES)
    metadata, bodies = Budget(max_metadata_bytes), Budget(max_bytes)
    observation = Observation()
    observation.directory(root)

    def read_json(path, maximum=64 * 1024):
        raw = read_file(path, observation, metadata, maximum=maximum)
        return raw, decoded(raw)

    with locked(root / 'deployment.lock', observation):
        _, config = read_json(root / 'deployment.json', 256 * 1024)
        shape(config, ('format', 'controller_version', 'created_at', 'deployment', 'runtime',
                       'workspace', 'runtime_format', 'core_version', 'core', 'entrance_files'),
              'deployment configuration')
        need(config['format'] == DEPLOYMENT_FORMAT and
             config['controller_version'] == CONTROLLER_VERSION and
             config['runtime_format'] == RUNTIME_FORMAT and config['core_version'] == CORE_VERSION,
             'unsupported deployment/controller/runtime/core version')
        need(config['deployment'] == str(root), 'deployment absolute identity differs')
        string(config['created_at'], 'deployment time', 128)
        runtime = absolute(config['runtime'])
        need(root != runtime and not root.is_relative_to(runtime),
             'deployment metadata must be outside the runtime')
        workspace = runtime / 'work'
        need(config['workspace'] == str(workspace), 'configured workspace binding differs')
        tree_metadata(config['core'], ('files', 'directories'), 'accepted core declaration')
        observation.directory(runtime)
        observation.directory(runtime / 'exchange')

        with locked(runtime / 'exchange' / 'owner.lock', observation):
            _, runtime_config = read_json(runtime / 'system.json')
            shape(runtime_config, ('format', 'app_version', 'core_version', 'created_at'),
                  'runtime configuration')
            need(runtime_config['format'] == RUNTIME_FORMAT and
                 runtime_config['core_version'] == CORE_VERSION, 'runtime format/core binding differs')
            string(runtime_config['app_version'], 'runtime initialization app version', 120)
            string(runtime_config['created_at'], 'runtime initialization time', 128)
            _, workspace_config = read_json(runtime / 'exchange' / 'workspace.json')
            shape(workspace_config, ('version', 'workspace'), 'workspace configuration')
            need(type(workspace_config['version']) is int and workspace_config['version'] == 1 and
                 workspace_config['workspace'] == str(workspace), 'runtime workspace binding differs')
            observation.directory(workspace)

            need(isinstance(config['entrance_files'], list), 'entrance bindings must be a list')
            entrances = {}
            for row in config['entrance_files']:
                shape(row, ('path', 'bytes', 'sha256'), 'stable entrance binding')
                name = relative(row['path'])
                need(name not in entrances, 'duplicate stable entrance binding')
                integer(row['bytes'], 'entrance byte count'); sha(row['sha256'])
                entrances[name] = row
            need(set(entrances) == {'deploy.py', 'deployment.py'}, 'unsupported stable entrance set')
            for name, row in entrances.items():
                read_file(root / name, observation, metadata, maximum=SOURCE_BYTES, expected=row)

            _, active = read_json(root / 'active.json')
            selection_metadata(active)
            need(active['selection_id'] == expected_selection, 'active selection differs from caller expectation')
            need(active['manifest_sha256'] == manifest_sha256, 'active manifest differs from caller pin')
            observation.directory(root / 'selections')
            _, retained = read_json(root / 'selections' / (expected_selection + '.json'))
            selection_metadata(retained)
            need(retained == active, 'active selection differs from retained exact descriptor')

            observation.directory(root / 'editions')
            base = root / 'editions' / active['edition']
            observation.directory(base)
            manifest_path = base / 'manifest.json'
            raw_manifest, manifest = read_json(manifest_path, 8 * 1024 * 1024)
            need(digest(raw_manifest) == manifest_sha256, 'exact parsed manifest bytes differ from caller pin')
            shape(manifest, ('format', 'edition', 'created_at', 'project', 'engine', 'engine_subdir', 'scope'),
                  'edition manifest')
            need(manifest['format'] == EDITION_FORMAT and manifest['edition'] == active['edition'],
                 'manifest edition identity differs')
            string(manifest['created_at'], 'manifest time', 128)
            string(manifest['scope'], 'manifest scope', 16384)
            files, directories = tree_metadata(manifest['project'], ('files', 'directories', 'source'),
                                               'project declaration')
            tree_metadata(manifest['engine'], ('files', 'directories', 'source', 'app_version',
                                               'runtime_format', 'core_version'), 'engine declaration')
            absolute(manifest['project']['source']); absolute(manifest['engine']['source'])
            string(manifest['engine']['app_version'], 'declared engine app version', 120)
            need(manifest['engine']['runtime_format'] == RUNTIME_FORMAT and
                 manifest['engine']['core_version'] == CORE_VERSION, 'unsupported declared engine contract')
            if manifest['engine_subdir'] is not None:
                need(relative(manifest['engine_subdir']) in directories,
                     'declared engine subtree is not a project directory member')

            need(all(name in files for name in names), 'requested source is not a unique declared project file')
            need(all(files[name]['bytes'] <= SOURCE_BYTES for name in names),
                 'selected source exceeds the 2 MiB per-source bound')
            bodies.admit(sum(files[name]['bytes'] for name in names))
            project = base / 'project'
            selected_directories = {'.'}
            for name in names:
                parent = name.rpartition('/')[0]
                while parent:
                    selected_directories.add(parent)
                    parent = parent.rpartition('/')[0]
            for name in sorted(selected_directories):
                observation.directory(project if name == '.' else project / name,
                                      directories[name]['mode'])
            result_sources = []
            for name in names:
                row = files[name]
                raw = read_file(project / name, observation, bodies, maximum=SOURCE_BYTES, expected=row)
                try:
                    body = raw.decode('utf-8')
                except UnicodeError as exc:
                    raise Refusal('selected source is not complete UTF-8 text') from exc
                result_sources.append({'project_source': dict(row), 'path': str(project / name), 'body': body})
            observation.check()

    return {
        'format': FORMAT,
        'deployment': str(root), 'runtime': str(runtime), 'workspace': str(workspace),
        'selection': active,
        'manifest': {'path': str(manifest_path), 'sha256': manifest_sha256, 'bytes': len(raw_manifest)},
        'sources': result_sources,
        'verified_project_directories': [dict(directories[name]) for name in sorted(selected_directories)],
        'read_account': {'metadata_and_entrance_bytes': metadata.used, 'selected_body_bytes': bodies.used,
                         'max_metadata_bytes': max_metadata_bytes, 'max_selected_bytes': max_bytes},
        'assurance': {
            'verified': ['Ordinary deployment/runtime/workspace identity and stable entrance bytes.',
                         'Caller expected active selection, equal retained descriptor, and exact parsed manifest pin.',
                         'Unique safe declared selected files, complete bytes, modes, and declared directory ancestry.'],
            'unverified': ['Unselected project files and complete tree membership.',
                           'Engine and accepted core bytes, engine/project correspondence, and executable compatibility.',
                           'Historical selections beyond the retained current descriptor and all historical audit.',
                           'Current goal, owed effects, later working-source corrections, and every other current owner fact.'],
            'observation': 'One cooperative deployment-lock then runtime-owner-lock observation; no perpetual currentness, authentication or hostile filesystem isolation.',
            'use': 'Source evidence only. No engine import/execution, semantic acceptance, context capture, mutation or effect authority. Existing complete verification remains required for its own contract.',
            'bounds': 'Metadata and selected reads have separate aggregate budgets; a growth refusal may consume one excess sentinel byte before aborting. Rendered output has a separate byte limit, not a peak-allocation guarantee.',
        },
    }


def render(value, max_output_bytes=DEFAULT_OUTPUT_BYTES):
    """Encode completely within the output limit before emitting any bytes."""
    limit(max_output_bytes, 'output byte budget', MAX_OUTPUT_BYTES)
    chunks, used = [], 0
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    for chunk in encoder.iterencode(value):
        raw = chunk.encode('utf-8')
        used += len(raw)
        need(used < max_output_bytes, 'rendered output exceeds its byte bound')
        chunks.append(raw)
    chunks.append(b'\n')
    return b''.join(chunks)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal('invalid command arguments: ' + message)


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument('--deployment', required=True, type=Path)
        parser.add_argument('--expected-selection', required=True)
        parser.add_argument('--manifest-sha256', required=True)
        parser.add_argument('--source', action='append', required=True)
        parser.add_argument('--max-bytes', type=int, default=DEFAULT_BYTES)
        parser.add_argument('--max-metadata-bytes', type=int, default=DEFAULT_METADATA_BYTES)
        parser.add_argument('--max-output-bytes', type=int, default=DEFAULT_OUTPUT_BYTES)
        args = parser.parse_args(argv)
        limit(args.max_output_bytes, 'output byte budget', MAX_OUTPUT_BYTES)
        result = acquire(args.deployment, args.expected_selection, args.manifest_sha256,
                         args.source, max_bytes=args.max_bytes, max_metadata_bytes=args.max_metadata_bytes)
        output = render(result, args.max_output_bytes)
    except (ValueError, OSError, RecursionError) as exc:
        error = {'format': FORMAT, 'refused': True, 'error': str(exc)[:1000]}
        sys.stderr.buffer.write((json.dumps(error, ensure_ascii=True) + '\n').encode('ascii'))
        return 2
    sys.stdout.buffer.write(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
