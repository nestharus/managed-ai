"""Explicit context, return, and single-file publication across local invocations.

The caller supplies meaning and authority. This module supplies byte-bound
transport and optional publication, not a cognitive schema or semantic selector.
Trusted local POSIX use only; it is not a filesystem or process sandbox.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from stat import FILE_ATTRIBUTE_REPARSE_POINT, S_ISDIR, S_ISREG
import sys
from typing import Any
import uuid

from .ownership import (EffectAuthority, NativeLayout, StandaloneAuthority, external_root,
                        packet_id as _id, relative_path as _relative)
from .packet_transport import PacketTransport
from .storage import (MAX_BYTES, ObjectStore, atomic_bytes as _atomic_bytes, encode, exclusive,
                      now, read_bytes as _bytes, read_json, sha, sync_dir, write_json)

MAX_PACKET_BYTES = 8 * 1024 * 1024
TRANSPORT_FORMAT = 'material-objects-v1'
MAX_BASIS_ROWS = 8
MAX_BASIS_PATH_CHARS = 32767
MAX_BASIS_PATH_BUDGET = 8192
PUBLICATION_ALREADY_ATTEMPTED = 'publication already attempted; inspect its receipt, do not replay'


def _run_nominations(producer: str, mappings: dict[str, str]) -> dict:
    """The frozen target is text. A file operand and its physical origin are OS bytes."""
    origin = None
    if any(not os.path.isabs(source) for source in mappings.values()):
        cwd = os.stat('.')
        origin = {'device': cwd.st_dev, 'inode': cwd.st_ino, 'path_hex': os.getcwdb().hex()}
    return {'version': 1, 'producer': producer, 'origin': origin,
            'files': [[target, os.fsencode(source).hex()] for target, source in mappings.items()]}


def _components(path: bytes):
    if not os.path.isabs(path) or b'\0' in path:
        raise ValueError('absolute proposal origin required')
    yield b'//' if path.startswith(b'//') and not path.startswith(b'///') else b'/'
    yield from filter(None, path.split(b'/'))


@contextmanager
def _proposal_directory(origin):
    """Walk the original directory once. Each lookup uses just one component."""
    if origin is None:
        yield None
        return
    path = bytes.fromhex(origin['path_hex'])
    identity = origin['device'], origin['inode']
    search = getattr(os, 'O_SEARCH', getattr(os, 'O_PATH', None))
    directory = None
    try:
        if search is None:
            # Traverse search-only ancestors, then pin the reached directory.
            previous = os.getcwdb()
            try:
                for part in _components(path):
                    os.chdir(part)
                directory = os.open(b'.', os.O_RDONLY | os.O_DIRECTORY)
            finally:
                for part in _components(previous):
                    os.chdir(part)
        else:
            for part in _components(path):
                child = os.open(part, search | os.O_DIRECTORY, dir_fd=directory)
                if directory is not None:
                    os.close(directory)
                directory = child
        reached = os.fstat(directory)
        if (reached.st_dev, reached.st_ino) != identity:
            raise ValueError('proposal origin directory changed')
        yield directory
    finally:
        if directory is not None:
            os.close(directory)


def _proposal_bytes(source: str, directory: int | None = None) -> bytes:
    """Read the checked regular file through the same descriptor, using the raw POSIX operand."""
    try:
        expected = os.stat(source, dir_fd=directory, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ValueError(f'ordinary file required: {source}') from exc
    if not S_ISREG(expected.st_mode):
        raise ValueError(f'ordinary file required: {source}')
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY
    fd = os.open(source, flags, dir_fd=directory)
    try:
        opened = os.fstat(fd)
        if not S_ISREG(opened.st_mode) or not os.path.samestat(expected, opened):
            raise ValueError(f'proposal file changed during lookup: {source}')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            body = stream.read(MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(body) > MAX_BYTES:
        raise ValueError(f'file exceeds {MAX_BYTES} bytes: {source}')
    return body


def _publication_name(target: str, prefix: str = 'publish') -> str:
    return prefix + '-' + sha(target.encode()) + '.json'


def _native_timestamp(value):
    if isinstance(value, str):
        try:
            observed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if observed.utcoffset() == timedelta(0) and observed.isoformat() == value:
            return observed
    return None


def _validate_publication_record(record):
    # Publication receipts are unversioned. Both phases retain the writer's
    # intent, and only the completion transition adds completed_at.
    reason = record.get('reason')
    if _native_timestamp(record.get('started_at')) is None or not isinstance(reason, str) or not reason.strip():
        raise ValueError('invalid publication intent metadata')
    if record['phase'] == 'intent':
        if 'completed_at' in record:
            raise ValueError('publication intent contains completion metadata')
    elif _native_timestamp(record.get('completed_at')) is None:
        raise ValueError('invalid publication completion metadata')


class PublicationBasisChanged(ValueError):
    def __init__(self):
        super().__init__('declared basis changed; preserve this return and construct its current use')
        self.basis = None
        self.native_basis = None


def _publication_basis(packet, target, request, record, proposal, changed):
    declared = next((row for row in request['targets'] if row['path'] == target), None)
    if (declared is None or 'base_sha256' not in proposal or declared['sha256'] != proposal['base_sha256']
            or packet != request['packet'] or packet != record['packet']
            or len(target) > MAX_BASIS_PATH_CHARS):
        return None
    for row in changed:
        for key in ('expected_sha256', 'current_sha256'):
            value = row[key]
            if value is not None and (not isinstance(value, str) or len(value) != 64
                                      or any(c not in '0123456789abcdef' for c in value)):
                return None
    rows, path_budget = [], MAX_BASIS_PATH_BUDGET
    # Give the requested target priority. The first eligible row may exceed the
    # packing budget, which limits the escaped path text of subsequent rows.
    for row in sorted(changed, key=lambda row: row['path'] != target):
        if len(rows) == MAX_BASIS_ROWS:
            break
        if len(row['path']) > MAX_BASIS_PATH_CHARS:
            continue
        size = len(json.dumps(row['path']))
        if rows and size > path_budget:
            continue
        rows.append({key: row[key] for key in ('path', 'expected_sha256', 'current_sha256')})
        path_budget -= size
    return {'packet': packet, 'target': target, 'artifact': packet + ':' + target,
            'request_sha256': record['request_sha256'], 'return_sha256': record['return_sha256'],
            'proposal_sha256': proposal['sha256'], 'base_sha256': declared['sha256'],
            'changed': rows, 'omitted': len(changed) - len(rows),
            'inspect': {'entry': 'prototype/work.py', 'root': 'store exchange',
                        'arguments': ['inspect', packet]}}


def _absent_under(path: Path, workspace: Path) -> bool:
    # Windows can report a missing path when an ancestor is a file. An absent
    # chain must end at an observed directory inside the workspace.
    for ancestor in (path, *path.parents):
        if not ancestor.is_relative_to(workspace):
            break
        try:
            entry = ancestor.lstat()
        except FileNotFoundError:
            continue
        return (ancestor != path and S_ISDIR(entry.st_mode)
                and not getattr(entry, 'st_file_attributes', 0) & FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _external(root: Path, role: str = 'runtime root') -> Path:
    return external_root(root, role, module_file=__file__)


class Exchange:
    """One cooperative owner; all mutation calls are serialized by the CLI."""

    def __init__(self, root: Path, *, authority: EffectAuthority | None = None):
        self.root = _external(root)
        if not (self.root / 'workspace.json').is_file():
            raise ValueError('initialize the external exchange first')
        binding = read_json(self.root / 'workspace.json')
        if binding.get('version') == 2:
            if binding.get('workspace_relative') != '../work':
                raise ValueError('unsupported portable native workspace binding')
            descriptor = NativeLayout(self.root.parent).read_descriptor()
            if descriptor.get('instance_id') != binding.get('instance_id'):
                raise ValueError('portable workspace belongs to a different instance')
            self.workspace = (self.root / binding['workspace_relative']).resolve()
        else:
            self.workspace = Path(binding['workspace']).resolve()
        self.objects = ObjectStore(self.root / 'objects')
        self._authority = StandaloneAuthority() if authority is None else authority
        self.transport = PacketTransport(self.root / 'carrier', self.workspace, self._authority)

    @staticmethod
    def initialize(root: Path, workspace: Path, *, instance_id: str | None = None) -> dict[str, Any]:
        root = _external(root)
        workspace = workspace.expanduser().resolve(strict=True)
        if not workspace.is_dir() or root == workspace or root.is_relative_to(workspace) or workspace.is_relative_to(root):
            raise ValueError('workspace and exchange must be disjoint directories')
        _external(workspace, 'workspace')
        root.mkdir(parents=True, exist_ok=True)
        with exclusive(root / 'owner.lock'):
            if instance_id is not None:
                if workspace != root.parent / 'work' or root.name != 'exchange':
                    raise ValueError('portable binding requires the native store layout')
                record = {'version': 2, 'workspace_relative': '../work', 'instance_id': instance_id}
            else:
                record = {'version': 1, 'workspace': str(workspace)}
            path = root / 'workspace.json'
            if path.exists():
                if read_json(path) != record:
                    raise ValueError('exchange already belongs to another workspace')
            else:
                if any(p.name != 'owner.lock' for p in root.iterdir()):
                    raise ValueError('new exchange directory must be empty')
                (root / 'objects').mkdir()
                (root / 'packets').mkdir()
                write_json(path, record)
        return record

    def path(self, value: str) -> Path:
        rel = _relative(value)
        path = self.workspace / rel
        cursor = self.workspace
        for part in Path(rel).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError('workspace symlinks are not supported')
        if not path.resolve().is_relative_to(self.workspace):
            raise ValueError('path is outside the workspace')
        return path

    def put(self, data: bytes) -> str:
        return self.objects.put(data)

    def get(self, key: str) -> bytes:
        return self.objects.read(key)

    def snapshot(self, value: str, *, optional: bool = False) -> dict[str, Any]:
        path = self.path(value)
        if not path.exists() and optional:
            return {'path': _relative(value), 'sha256': None, 'bytes': 0}
        data = _bytes(path)
        return {'path': _relative(value), 'sha256': self.put(data), 'bytes': len(data)}

    def directory(self, packet: str) -> Path:
        return self.root / 'packets' / _id(packet)

    def load(self, packet: str) -> tuple[Path, dict[str, Any]]:
        return self._load_directory(self.directory(packet))

    def _load_directory(self, directory: Path) -> tuple[Path, dict[str, Any]]:
        binding = read_json(directory / 'binding.json')
        request = read_json(directory / 'request.json')
        if sha(encode(request)) != binding['request_sha256'] or sha(_bytes(directory / 'context.md', MAX_PACKET_BYTES)) != binding['context_sha256']:
            raise ValueError('frozen request/context changed')
        for entry in [request['brief'], *request['methods'], *request['materials'], *request['targets'], *request['returns'], *request.get('artifacts', [])]:
            if entry['sha256'] is not None:
                self.get(entry['sha256'])
        for entry in request['returns']:
            origin = entry['origin']
            prior_dir = self.directory(origin['packet'])
            prior = read_json(prior_dir / 'return.json')
            self.validate_return(prior_dir, prior)
            if prior['return_sha256'] != origin['return_sha256'] or prior['reply_sha256'] != entry['sha256']:
                raise ValueError('prior return correspondence changed')
        for entry in request.get('artifacts', []):
            origin = entry['origin']
            prior_dir = self.directory(origin['packet'])
            prior = read_json(prior_dir / 'return.json')
            self.validate_return(prior_dir, prior)
            expected = next((p for p in prior['proposals'] if p['path'] == origin['proposal_path']), None)
            if (prior['return_sha256'] != origin['return_sha256'] or expected is None
                    or entry['sha256'] != expected['sha256'] or entry['bytes'] != expected['bytes']):
                raise ValueError('prior proposal correspondence changed')
        return directory, request

    @staticmethod
    def return_origin(record: dict[str, Any]) -> dict[str, Any]:
        return {k: record[k] for k in ('packet', 'consumer', 'producer',
                                      'request_sha256', 'return_sha256')} | {
            'transport_kind': record['transport']['kind']}

    def context(self, packet: str) -> dict[str, Any]:
        """Recover the validated originating context without a current effect check."""
        directory, request = self.load(packet)
        binding = read_json(directory / 'binding.json')
        data = _bytes(directory / 'context.md', MAX_PACKET_BYTES)
        if (sha(data) != binding['context_sha256'] or
                sha(encode(request)) != binding['request_sha256']):
            raise ValueError('frozen request/context changed')
        return {'packet': packet, 'consumer': request['consumer'],
                'request_sha256': binding['request_sha256'],
                'context_sha256': binding['context_sha256'], 'context_bytes': len(data),
                'context': data.decode('utf-8'),
                'scope': 'Captured originating material; no current effect authority or semantic acceptance.'}

    def prepare(self, packet: str, consumer: str, brief: str, methods: list[str],
                materials: list[str], targets: list[str], returns: list[str],
                artifacts: list[str] | None = None, *, staged: bool = False) -> dict[str, Any]:
        _id(packet)
        if not consumer.strip():
            raise ValueError('a named receiving interest/consumer is required')
        directory = self.directory(packet)
        if directory.exists() or self.prepared_directory(packet).exists():
            raise ValueError('packet identity already used or reserved')
        if len(set(map(_relative, targets))) != len(targets):
            raise ValueError('duplicate publication target')
        request = {'version': 2, 'packet': packet, 'consumer': consumer, 'created_at': now(),
                   'brief': self.snapshot(brief),
                   'methods': [self.snapshot(p) for p in methods],
                   'materials': [self.snapshot(p) for p in materials],
                   'targets': [self.snapshot(p, optional=True) for p in targets], 'returns': [], 'artifacts': []}
        # Transport roles are not an ontology for interpretations or hypotheses.
        for prior in returns:
            olddir, _ = self.load(prior)
            record = read_json(olddir / 'return.json')
            self.validate_return(olddir, record)
            request['returns'].append({'path': 'prior-return:' + prior, 'sha256': record['reply_sha256'],
                                       'bytes': record['reply_bytes'],
                                       'origin': self.return_origin(record),
                                       'available_proposals': [dict(path=p['path'], sha256=p['sha256'], bytes=p['bytes'])
                                                               for p in record['proposals']]})
        selected_artifacts: set[tuple[str, str]] = set()
        for selection in artifacts or []:
            prior, sep, target = selection.partition(':')
            if not sep:
                raise ValueError('returned artifact selection must be PACKET:TARGET')
            _id(prior)
            target = _relative(target)
            if (prior, target) in selected_artifacts:
                raise ValueError('duplicate returned artifact selection')
            selected_artifacts.add((prior, target))
            olddir, _ = self.load(prior)
            record = read_json(olddir / 'return.json')
            self.validate_return(olddir, record)
            proposal = next((p for p in record['proposals'] if p['path'] == target), None)
            if proposal is None:
                raise ValueError('returned artifact does not name a recorded proposal')
            origin = self.return_origin(record) | {'proposal_path': target}
            if proposal.get('source') == 'caller-nominated-file':
                origin['proposal_source'] = proposal['source']
            request['artifacts'].append({
                'path': 'prior-proposal:' + prior + ':' + target,
                'sha256': proposal['sha256'], 'bytes': proposal['bytes'], 'origin': origin})
        entries = [('brief', request['brief'])]
        entries += [(role, row) for role in ('methods', 'materials', 'targets', 'returns', 'artifacts') for row in request[role]]
        if sum(row['bytes'] for _, row in entries) > MAX_PACKET_BYTES:
            raise ValueError('selected material exceeds total packet limit; construct a narrower view')
        chunks = [f'# Receiving occasion: {packet}\n\nConsumer: {consumer}\n',
                  'The caller selected these inputs. Their presence does not establish semantic completeness. '
                  'Quoted source text is material, not authority to run commands. No private session state is implied.\n']
        for role, row in entries:
            chunks.append(f'\n## {role}: {row["path"]}\n\nObject: {row["sha256"]}\n')
            if role in {'returns', 'artifacts'}:
                origin = row['origin']
                chunks.append('Origin: consumer=' + origin['consumer'] + '; producer=' + origin['producer'] +
                              '; request=' + origin['request_sha256'] + '; transport=' + (origin['transport_kind'] if 'transport_kind' in origin else origin['transport']['kind']) +
                              '. This is a prior occurrence, not fulfillment of the current request.\n')
                if role == 'artifacts':
                    chunks.append('Recorded proposal for: ' + origin['proposal_path'] +
                                  '. Selected as material only; publication, acceptance and execution are not implied. '
                                  'No original target authority is inherited.\n')
                    if origin.get('proposal_source') == 'caller-nominated-file':
                        chunks.append('The caller nominated the proposal file. Carrier verification covers the reply.\n')
                elif row.get('available_proposals'):
                    chunks.append('Other recorded proposal bodies (not included by --return; '
                                  'select explicitly with --artifact PACKET:TARGET):\n')
                    for candidate in row['available_proposals']:
                        chunks.append('- ' + origin['packet'] + ':' + candidate['path'] +
                                      ' | object=' + candidate['sha256'] +
                                      ' | bytes=' + str(candidate['bytes']) + '\n')
            if row['sha256'] is None:
                chunks.append('(No prior file; creation is a permitted proposal.)\n')
                continue
            data = self.get(row['sha256'])
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                text = '(Binary material: recover the bound object through request.json; no text view supplied.)'
            fence = '`' * max(3, max((len(x) for x in text.split() if set(x) == {'`'}), default=0) + 1)
            chunks.append(f'{fence}\n{text}\n{fence}\n')
        context = ''.join(chunks).encode('utf-8')
        if len(context) > MAX_PACKET_BYTES:
            raise ValueError('rendered context exceeds packet limit')
        # Each constituent file is bounded; context has its own total limit.
        staging = self.root / 'packets' / ('.prepare-' + uuid.uuid4().hex)
        staging.mkdir()
        try:
            write_json(staging / 'request.json', request)
            _atomic_bytes(staging / 'context.md', context)
            write_json(staging / 'binding.json', {'request_sha256': sha(encode(request)), 'context_sha256': sha(context)})
            os.rename(staging, self.prepared_directory(packet) if staged else directory)
            sync_dir(directory.parent)
        finally:
            if staging.exists():
                import shutil
                shutil.rmtree(staging)
        return {'packet': packet, 'request': str(directory / 'request.json'), 'context': str(directory / 'context.md'),
                'request_sha256': sha(encode(request)), 'selected_bytes': sum(r['bytes'] for _, r in entries),
                **({'context_sha256': sha(context)} if staged else {})}

    def prepared_directory(self, packet: str) -> Path:
        return self.root / 'packets' / ('.sealed-' + _id(packet))

    def commit_prepared(self, packet: str, request_sha256: str, context_sha256: str, before_publish) -> None:
        """Verify the frozen input, then let the local owner commit its part before public visibility.

        The caller holds the exchange owner lock and supplies a recoverable, idempotent callback.
        A retry after an interrupted callback reuses exactly this packet.
        """
        pending = self.prepared_directory(packet)
        directory = self.directory(packet)
        if directory.exists():
            raise ValueError('packet identity already used')
        _, request = self._load_directory(pending)
        binding = read_json(pending / 'binding.json')
        if (request['packet'] != packet or binding['request_sha256'] != request_sha256
                or binding['context_sha256'] != context_sha256):
            raise ValueError('sealed packet differs from originating preparation')
        before_publish()
        os.rename(pending, directory)
        sync_dir(directory.parent)

    def dispatch(self, packet: str, argv: list[str], *, proposals: list[str] | None = None,
                 producer: str | None = None) -> dict[str, Any]:
        directory, request = self.load(packet)
        if (directory / 'return.json').exists():
            raise ValueError('packet already has a received return')
        if (directory / 'transport-outcome.json').exists():
            self._transport_outcome(directory, request)
            raise ValueError('terminal transport outcome recorded; prepare a fresh occasion')
        nominations = None
        if proposals:
            mappings = self._proposal_mappings(request, proposals)
            if producer is not None:
                if not producer.strip():
                    raise ValueError('producer attribution is required')
                nominations = _run_nominations(producer, mappings)
        return self.transport.dispatch(packet, directory, request, argv, nominations=nominations)

    @staticmethod
    def _proposal_mappings(request: dict[str, Any], proposals: list[str]) -> dict[str, str]:
        declared = {row['path'] for row in request['targets']}
        mappings = {}
        for item in proposals:
            target, sep, source = item.partition('=')
            target = _relative(target)
            if not sep or not source or '\0' in source or target not in declared or target in mappings:
                raise ValueError('proposal must name one unique declared target as target=file')
            mappings[target] = source
        return mappings

    def validate_return(self, directory: Path, record: dict[str, Any]) -> None:
        if (directory / 'transport-outcome.json').exists():
            raise ValueError('packet contains both a return and a terminal outcome')
        if record['return_sha256'] != sha(encode({k: v for k, v in record.items() if k != 'return_sha256'})):
            raise ValueError('return record changed')
        request = read_json(directory / 'request.json')
        if record['request_sha256'] != sha(encode(request)):
            raise ValueError('return belongs to another request')
        if type(record['reply_bytes']) is not int or record['reply_bytes'] < 0:
            raise ValueError('invalid reply length')
        self.objects.read(record['reply_sha256'], expected_bytes=record['reply_bytes'])
        for row in record['proposals']:
            if type(row['bytes']) is not int or row['bytes'] < 0:
                raise ValueError('invalid proposal length')
            self.objects.read(row['sha256'], expected_bytes=row['bytes'])
        view = self._transport_view(record['transport'])
        if view is not None:
            stdout = view['streams']['stdout']
            if (stdout['sha256'] != record['reply_sha256']
                    or stdout['byte_count'] != record['reply_bytes']):
                raise ValueError('transport stdout does not match the reply')

    @staticmethod
    def _carrier_materials(view: dict[str, Any]):
        """Visit the Collector's material slots without changing their observations."""
        required = (view['origin_request']['origin_brief'],
                    view['streams']['stdout'], view['streams']['stderr'])
        for material in required:
            if material.get('present') is not True:
                raise ValueError('required transport material is absent')
            yield material
        if 'current_brief' in view:
            yield view['current_brief']

    @staticmethod
    def _utf8(data: bytes) -> str | None:
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            return None

    def _transport_body(self, material: dict[str, Any], *, retained: bool) -> bytes | None:
        if type(material['present']) is not bool:
            raise ValueError('invalid transport material presence')
        if not material['present']:
            return None
        count = material['byte_count']
        if type(count) is not int or count < 0:
            raise ValueError('invalid transport material length')
        if retained:
            if 'base64' in material or 'utf8_text' in material:
                raise ValueError('compact transport contains inline material')
            data = self.objects.read(material['sha256'], retained=True, expected_bytes=count)
        else:
            data = base64.b64decode(material['base64'], validate=True)
            if (material['base64'] != base64.b64encode(data).decode('ascii')
                    or material['utf8_text'] != self._utf8(data)):
                raise ValueError('transport text differs from captured bytes')
        if sha(data) != material['sha256'] or len(data) != material['byte_count']:
            raise ValueError('transport material identity or length changed')
        return data

    def _retain_transport(self, view: dict[str, Any]) -> dict[str, Any]:
        """Retain each raw body once, durably, before the receipt is published."""
        compact = deepcopy(view)
        for material in self._carrier_materials(compact):
            data = self._transport_body(material, retained=False)
            if data is not None:
                # Context has the packet bound. Finite captured stderr may exceed
                # the ordinary object bound. Reply/proposal limits stay separate.
                self.objects.put_retained(data, max_bytes=len(data))
                del material['base64']
                del material['utf8_text']
        return {'kind': 'local-command', 'carrier_format': TRANSPORT_FORMAT, 'carrier': compact}

    def _transport_view(self, transport: dict[str, Any], *, recover: bool = False) -> dict[str, Any] | None:
        if transport['kind'] == 'caller-import':
            return None
        if transport['kind'] != 'local-command':
            raise ValueError('unknown return transport')
        representation = transport.get('carrier_format')
        if representation not in (None, TRANSPORT_FORMAT):
            raise ValueError('unknown captured transport format')
        view = deepcopy(transport['carrier']) if recover else transport['carrier']
        for material in self._carrier_materials(view):
            data = self._transport_body(material, retained=representation == TRANSPORT_FORMAT)
            if recover and representation == TRANSPORT_FORMAT and data is not None:
                material['base64'] = base64.b64encode(data).decode('ascii')
                material['utf8_text'] = self._utf8(data)
        return view

    def recover_transport(self, packet: str) -> dict[str, Any] | None:
        """Recover the exact captured carrier view, using only retained evidence.

        Old inline local-command records remain readable. Caller-import returns
        have no carrier view. This read grants no effect authority or retry.
        """
        directory, request = self.load(packet)
        if (directory / 'return.json').exists():
            record = read_json(directory / 'return.json')
            self.validate_return(directory, record)
        else:
            record = self._transport_outcome(directory, request)
            if record is None:
                raise ValueError('no retained return or terminal transport outcome')
        return self._transport_view(record['transport'], recover=True)

    @staticmethod
    def _terminal_reason(view: dict[str, Any]) -> str:
        if view.get('stream_integrity') != 'verified':
            raise ValueError('a verified terminal command observation is required')
        result = view.get('result', {})
        if view.get('status') == result.get('status') == 'launch-failed':
            if result.get('returncode') is not None or result.get('signal') is not None:
                raise ValueError('launch failure cannot contain a child exit')
            return 'launch-failed'
        if (view.get('status') == result.get('status') == 'completed'
                and view['streams']['stdout']['byte_count'] > MAX_BYTES):
            return 'reply-limit-exceeded'
        raise ValueError('terminal closure requires launch failure or an oversized completed reply')

    def _transport_outcome(self, directory: Path, request: dict[str, Any]) -> dict[str, Any] | None:
        path = directory / 'transport-outcome.json'
        if not path.exists():
            return None
        record = read_json(path)
        if (directory / 'return.json').exists():
            raise ValueError('packet contains both a return and a terminal outcome')
        if (type(record['version']) is not int or record['version'] != 1
                or record['packet'] != request['packet']
                or record['request_sha256'] != sha(encode(request))
                or record['outcome_sha256'] != sha(encode({k: v for k, v in record.items() if k != 'outcome_sha256'}))):
            raise ValueError('terminal transport outcome binding changed')
        if not isinstance(record['account'], str) or not record['account'].strip():
            raise ValueError('terminal transport outcome requires an owner account')
        view = self._transport_view(record['transport'])
        if view is None or record['reason'] != self._terminal_reason(view):
            raise ValueError('terminal transport reason differs from captured evidence')
        return record

    def close_transport(self, packet: str, account: str) -> dict[str, Any]:
        """Record an owner-accounted terminal observation without a contribution."""
        directory, request = self.load(packet)
        self._authority.authorize_dispatch(NativeLayout.owner_of(self.workspace), packet)
        if not isinstance(account, str) or not account.strip():
            raise ValueError('an explicit transport closure account is required')
        if (directory / 'return.json').exists():
            raise ValueError('packet already has a received return')
        prior = self._transport_outcome(directory, request)
        if prior is not None:
            if prior['account'] != account:
                raise ValueError('terminal transport outcome already has a different account')
            return prior
        _, view = self.transport.collect(packet, directory, request, wait=0)
        reason = self._terminal_reason(view)
        record = {'version': 1, 'packet': packet, 'request_sha256': sha(encode(request)),
                  'reason': reason, 'account': account, 'recorded_at': now(),
                  'transport': self._retain_transport(view)}
        record['outcome_sha256'] = sha(encode(record))
        write_json(directory / 'transport-outcome.json', record)
        return record

    def receive(self, packet: str, reply: Path | None, producer: str | None,
                proposals: list[str], *, wait: float = 0, run_nominations: bool = False) -> dict[str, Any]:
        directory, request = self.load(packet)
        if (directory / 'return.json').exists():
            raise ValueError('return already recorded; preserve it and prepare another occasion')
        if (directory / 'transport-outcome.json').exists():
            self._transport_outcome(directory, request)
            raise ValueError('terminal transport outcome recorded; prepare a fresh occasion')
        if run_nominations and (reply is not None or proposals or producer is not None):
            raise ValueError('original run nominations carry their own files, carrier and producer')
        if not run_nominations and (not producer or not producer.strip()):
            raise ValueError('producer attribution is required; it is caller-asserted, not authenticated')
        mappings = self._proposal_mappings(request, proposals)
        transport: dict[str, Any]
        origin = None
        bound = False
        if reply is None:
            marker = self.transport.bound_marker(packet, directory, request)
            if 'run_nominations' in marker:
                saved = marker['run_nominations']
                if saved['version'] != 1 or not saved['producer'].strip():
                    raise ValueError('invalid original run nominations')
                if producer is not None and producer != saved['producer']:
                    raise ValueError('receive producer differs from original bounded run')
                producer = saved['producer']
                if mappings and _run_nominations(producer, mappings) != saved:
                    raise ValueError('receive nominations differ from original bounded run')
                bound = True
            elif run_nominations:
                raise ValueError('original run nominations unavailable; use the original explicit receive')
            data, view = self.transport.collect_marked(marker, wait)
            pending = {'packet': packet, 'received': False, 'carrier': view}
            if bound:
                pending['run_nominations'] = True
            if data is None:
                return pending
            if bound:
                proposals = [target + '=' + os.fsdecode(bytes.fromhex(raw)) for target, raw in saved['files']]
                mappings = self._proposal_mappings(request, proposals)
                origin = saved['origin']
        else:
            if self.transport.has_attempt(directory):
                raise ValueError('a dispatched packet must receive its actual carrier return')
            data = _bytes(reply)
            transport = {'kind': 'caller-import', 'source_path': str(reply.resolve()),
                         'scope': 'caller-asserted producing occurrence; not independently authenticated'}
        expected = {r['path']: r for r in request['targets']}
        changes = []
        with _proposal_directory(origin) as source_directory:
            for target, source in mappings.items():
                try:
                    body = _proposal_bytes(source, source_directory)
                except (OSError, ValueError) as exc:
                    if reply is not None:
                        raise
                    # Diagnostics are Unicode text. Recovery carries the original argument bytes.
                    display = source.encode('utf-8', 'backslashreplace').decode('utf-8')
                    error = {'target': target, 'file': display,
                             'error': str(exc).encode('utf-8', 'backslashreplace').decode('utf-8')}
                    if display != source:
                        error['file_bytes_hex'] = os.fsencode(source).hex()
                    return dict(pending, proposal_error=error)
                proposal = {'path': target, 'sha256': self.put(body), 'bytes': len(body),
                            'base_sha256': expected[target]['sha256']}
                if reply is None:
                    proposal['source'] = 'caller-nominated-file'
                changes.append(proposal)
        reply_sha256 = self.put(data)
        if reply is None:
            transport = self._retain_transport(view)
        record = {'version': 1, 'packet': packet, 'consumer': request['consumer'],
                  'request_sha256': sha(encode(request)), 'producer': producer, 'received_at': now(),
                  'reply_sha256': reply_sha256, 'reply_bytes': len(data), 'proposals': changes, 'transport': transport}
        record['return_sha256'] = sha(encode(record))
        write_json(directory / 'return.json', record)
        return record

    @staticmethod
    def _bound_publication_proposal(packet, request, received, target):
        if (received is None or not isinstance(target, str)
                or request['packet'] != packet or received['packet'] != packet):
            return None
        declared = [row for row in request['targets'] if row['path'] == target]
        proposals = [row for row in received['proposals'] if row.get('path') == target]
        if len(declared) != 1 or len(proposals) != 1:
            return None
        proposal = proposals[0]
        if 'base_sha256' not in proposal or proposal['base_sha256'] != declared[0]['sha256']:
            return None
        return proposal

    @classmethod
    def _publication_matches(cls, packet, request, receipt, received):
        if (received is None or not isinstance(receipt, dict)
                or receipt.get('phase') not in ('intent', 'complete')
                or receipt.get('packet') != packet
                or receipt.get('return_sha256') != received['return_sha256']):
            return False
        proposal = cls._bound_publication_proposal(packet, request, received, receipt.get('path'))
        return (proposal is not None and receipt.get('sha256') == proposal['sha256']
                and 'base_sha256' in receipt and receipt['base_sha256'] == proposal['base_sha256'])

    def basis(self, packet: str) -> list[dict[str, Any]]:
        return self._basis(packet, *self.load(packet))

    @staticmethod
    def _publication_inventory(directory):
        # Observe the entire listing before interpreting any record. Glob can
        # suppress scan errors, and an interrupted listing cannot establish absence.
        publications, reconciliations, unreadable = {}, {}, {}
        for path in sorted(directory.iterdir()):
            name = path.name.lower()
            if not name.endswith('.json'):
                continue
            if name.startswith('publish-'):
                inventory = publications
            elif name.startswith('reconcile-'):
                inventory = reconciliations
            else:
                continue
            try:
                entry = path.lstat()
                if (not S_ISREG(entry.st_mode)
                        or getattr(entry, 'st_file_attributes', 0) & FILE_ATTRIBUTE_REPARSE_POINT):
                    raise ValueError('ordinary publication record required: ' + path.name)
                raw = path.read_bytes()
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError('publication record must be an object: ' + path.name)
                inventory[path.name] = (record, sha(raw))
            except (OSError, ValueError) as exc:
                unreadable[path.name] = exc
        return publications, reconciliations, unreadable

    def _publication_state(self, packet, directory, request, received, target):
        publications, reconciliations, unreadable = self._publication_inventory(directory)
        for error in unreadable.values():
            if isinstance(error, OSError):
                raise error
        if publications and received is None:
            received = read_json(directory / 'return.json')
            self.validate_return(directory, received)
        bound = {name: (record, digest) for name, (record, digest) in publications.items()
                 if self._publication_matches(packet, request, record, received)}
        if target is not None:
            occupied = []
            for prefix, label, records in (('publish', 'receipt', publications),
                                           ('reconcile', 'reconciliation', reconciliations)):
                name = _publication_name(target, prefix)
                if name in records:
                    recorded_target = records[name][0].get('path')
                    if isinstance(recorded_target, str) and recorded_target != target:
                        occupied.append(f'publication {label} slot is occupied by a record naming another target')
                elif name not in unreadable:
                    # The receiver can resolve the canonical name through another
                    # spelling even when the exact name was absent from the listing.
                    try:
                        (directory / name).lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        occupied.append(f'publication {label} slot is occupied by another entry')
            if any(record['path'] == target for record, _ in bound.values()):
                raise ValueError(PUBLICATION_ALREADY_ATTEMPTED)
            if occupied:
                raise ValueError(occupied[0])
        if unreadable:
            raise next(iter(unreadable.values()))
        if len(bound) != len(publications):
            raise ValueError('publication receipt binding changed')
        publication_targets = [record['path'] for record, _ in bound.values()]
        if len(set(publication_targets)) != len(publication_targets):
            raise ValueError('conflicting publication records for the same target')
        if any(name != _publication_name(record['path']) for name, (record, _) in bound.items()):
            raise ValueError('publication receipt is outside its canonical slot')
        intents = {receipt['path']: (receipt, digest) for receipt, digest in bound.values()}
        accounts = self._publication_accounts(request, received, intents, reconciliations)
        for receipt, _ in intents.values():
            _validate_publication_record(receipt)

        applied = {}
        pending = False
        for path, (receipt, _) in intents.items():
            if path in accounts:
                applied[path] = accounts[path]['observed_sha256']
            elif receipt['phase'] == 'complete':
                applied[path] = receipt['sha256']
            else:
                pending = True
        if pending:
            raise ValueError('interrupted publication requires manual reconciliation')
        return applied

    def _publication_accounts(self, request, received, intents, reconciliations):
        canonical = {_publication_name(path, 'reconcile'): path for path in intents}
        intent_paths = {digest: path for path, (_, digest) in intents.items()}
        accounts = {}
        misplaced = False
        for name, (record, _) in reconciliations.items():
            path = canonical.get(name, record.get('path'))
            if not isinstance(path, str) or path not in intents:
                identity = record.get('intent_sha256')
                path = intent_paths.get(identity) if isinstance(identity, str) else None
                if path is None:
                    raise ValueError('publication reconciliation has no canonical intent')
            receipt, digest = intents[path]
            reconciliation = self._validate_publication_reconciliation(
                request, received, receipt, digest, record)
            if path in accounts:
                raise ValueError('conflicting publication reconciliations for the same target')
            accounts[path] = reconciliation
            misplaced = misplaced or name not in canonical
        if misplaced:
            raise ValueError('publication reconciliation is outside its canonical slot')
        return accounts

    def _observe_target(self, name):
        path = self.path(name)
        try:
            path.stat()
        except FileNotFoundError:
            if not _absent_under(path, self.workspace):
                raise
            return {'path': name, 'sha256': None, 'bytes': 0, 'exists': False}
        data = _bytes(path)
        return {'path': name, 'sha256': sha(data), 'bytes': len(data), 'exists': True}

    def _basis(self, packet, directory, request, received=None, *, publishing_target=None,
               observe=None) -> list[dict[str, Any]]:
        applied = self._publication_state(packet, directory, request, received, publishing_target)
        rows = [request['brief'], *request['methods'], *request['materials'], *request['targets']]
        if observe is None:
            observe = self._observe_target
        checks = []
        for row in rows:
            current = observe(row['path'])['sha256']
            expected = applied.get(row['path'], row['sha256'])
            checks.append({'path': row['path'], 'expected_sha256': expected, 'current_sha256': current,
                           'unchanged': current == expected})
        return checks

    def list(self, consumer: str | None = None) -> list[dict[str, Any]]:
        """Expose retained occasions without inventing a semantic completion flag."""
        rows = []
        for directory in sorted((self.root / 'packets').iterdir()):
            if directory.name.startswith('.') or not directory.is_dir():
                continue
            try:
                _, request = self.load(directory.name)
                if consumer is not None and request['consumer'] != consumer:
                    continue
                has_return = (directory / 'return.json').exists()
                if has_return:
                    self.validate_return(directory, read_json(directory / 'return.json'))
                outcome = self._transport_outcome(directory, request)
                publications = [read_json(p) for p in directory.glob('publish-*.json')]
                transport = 'prepared'
                if self.transport.has_attempt(directory):
                    transport = 'dispatch-attempted'
                if has_return:
                    transport = 'returned'
                if outcome is not None:
                    transport = 'terminal-without-return'
                row = {'packet': directory.name, 'consumer': request['consumer'], 'transport': transport,
                       'publications': [{'path': p['path'], 'phase': p['phase']} for p in publications],
                       'semantic_fulfillment': 'not-decided-by-exchange'}
                if outcome is not None:
                    row['transport_outcome'] = {k: outcome[k] for k in ('reason', 'account', 'outcome_sha256')}
                rows.append(row)
            except (ValueError, KeyError, OSError, TypeError) as exc:
                rows.append({'packet': directory.name, 'transport': 'record-unresolved', 'error': str(exc)})
        return rows

    def inspect(self, packet: str) -> dict[str, Any]:
        directory, request = self.load(packet)
        # Current effect eligibility must not gate reading an intact old return.
        # load() above still validates the immutable request, context and objects.
        result = {'packet': packet, 'consumer': request['consumer'], 'basis': None, 'return': None}
        observations = {}

        def observe(path):
            # The basis and receipt rows share the same lookup, including a failure.
            if path not in observations:
                try:
                    observations[path] = self._observe_target(path)
                except (OSError, ValueError) as exc:
                    observations[path] = exc
            observation = observations[path]
            if isinstance(observation, Exception):
                raise observation
            return observation

        try:
            result['basis'] = self._basis(packet, directory, request, observe=observe)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result['basis_error'] = str(exc)
        result['publications'] = []
        if (directory / 'return.json').exists():
            record = read_json(directory / 'return.json')
            self.validate_return(directory, record)
            result['return'] = record
            data = self.get(record['reply_sha256'])
            try:
                result['reply_text'] = data.decode('utf-8')
            except UnicodeDecodeError:
                result['reply_text'] = None
        self._inspect_publications(directory, request, result, observe)
        result['inspection_scope'] = (
            'Read-only observations in one cooperative-owner invocation; not an atomic external-writer '
            'snapshot, historical completion proof, reconciliation or permission to replay.')
        outcome = self._transport_outcome(directory, request)
        if outcome is not None:
            result['transport_outcome'] = outcome
        return result

    def _inspect_publications(self, directory, request, result, observe):
        try:
            publications, reconciliations, unreadable = self._publication_inventory(directory)
        except OSError as exc:
            result['publication_inventory_error'] = str(exc)
            return
        targets = {row['path']: row for row in request['targets']}
        received = result['return']
        attached = set()
        for name, (record, digest) in publications.items():
            observation = {'record_file': name, 'record': record}
            try:
                target = record.get('path')
                if not isinstance(target, str) or target not in targets:
                    raise ValueError('publication path is not a declared target')
                try:
                    current = observe(target).copy()
                except (OSError, ValueError) as exc:
                    observation['observation_error'] = str(exc)
                else:
                    current['matches_base'] = current['sha256'] == targets[target]['sha256']
                    observation['current_target'] = current
                    proposal = self._bound_publication_proposal(result['packet'], request, received, target)
                    if proposal is not None:
                        current['matches_proposal'] = current['sha256'] == proposal['sha256']
                if not self._publication_matches(result['packet'], request, record, received):
                    raise ValueError('publication receipt binding changed')
                _validate_publication_record(record)
                canonical = _publication_name(target, 'reconcile')
                matching = [slot for slot, (account, _) in reconciliations.items()
                            if account.get('intent_sha256') == digest]
                slot = canonical if canonical in reconciliations else matching[0] if len(matching) == 1 else None
                if slot is not None:
                    account = reconciliations[slot][0]
                    observation['reconciliation'] = self._validate_publication_reconciliation(
                        request, received, record, digest, account)
                    attached.add(slot)
                    if slot != canonical:
                        observation['reconciliation_file'] = slot
            except (OSError, ValueError, KeyError, TypeError) as exc:
                observation.setdefault('observation_error', str(exc))
            result['publications'].append(observation)
        unattached = [{'record_file': name, 'record': record}
                      for name, (record, _) in reconciliations.items() if name not in attached]
        for name, error in unreadable.items():
            row = {'record_file': name, 'observation_error': str(error)}
            (result['publications'] if name.lower().startswith('publish-') else unattached).append(row)
        if unattached:
            result['unattached_reconciliations'] = unattached

    @staticmethod
    def _validate_publication_reconciliation(request, received, intent, digest, record):
        if intent['phase'] != 'intent':
            raise ValueError('reconciliation requires the bound original publication intent')
        if (type(record['version']) is not int or record['version'] != 1
                or record['packet'] != request['packet'] or record['path'] != intent['path']
                or record['request_sha256'] != sha(encode(request))
                or record['return_sha256'] != received['return_sha256']
                or record['intent_sha256'] != digest
                or record['reconciliation_sha256'] != sha(encode({k: v for k, v in record.items() if k != 'reconciliation_sha256'}))):
            raise ValueError('publication reconciliation binding changed')
        if record.get('historical_completion') != 'unestablished':
            raise ValueError('publication reconciliation does not establish historical completion')
        if not isinstance(record['account'], str) or not record['account'].strip():
            raise ValueError('publication reconciliation requires an owner account')
        if _native_timestamp(record.get('recorded_at')) is None:
            raise ValueError('invalid publication reconciliation metadata')
        if record['observed_sha256'] not in (intent['base_sha256'], intent['sha256']):
            raise ValueError('reconciliation observation differs from the base and proposal')
        return record

    def reconcile_publication(self, packet: str, target: str, expected_sha256: str | None,
                              account: str) -> dict[str, Any]:
        """Acknowledge current bytes while preserving the original effect uncertainty."""
        target = _relative(target)
        output = _external(self.path(target), 'publication destination')
        self._authority.authorize_publication(NativeLayout.owner_of(output), packet, target)
        directory, request = self.load(packet)
        if not isinstance(account, str) or not account.strip():
            raise ValueError('an explicit publication reconciliation account is required')
        if expected_sha256 == 'absent':
            expected_sha256 = None
        if expected_sha256 is not None and (not isinstance(expected_sha256, str)
                or len(expected_sha256) != 64 or any(c not in '0123456789abcdef' for c in expected_sha256)):
            raise ValueError('expected target identity must be a SHA-256 digest or absent')
        intent_name = _publication_name(target)
        account_name = _publication_name(target, 'reconcile')
        publications, reconciliations, unreadable = self._publication_inventory(directory)
        if intent_name in unreadable:
            raise unreadable[intent_name]
        if intent_name not in publications:
            if any(record.get('path') == target for record, _ in publications.values()):
                raise ValueError('publication receipt is outside its canonical slot')
            (directory / intent_name).lstat()
            raise ValueError('publication receipt slot is occupied by another entry')
        intent, intent_digest = publications[intent_name]
        received = read_json(directory / 'return.json')
        self.validate_return(directory, received)
        for error in unreadable.values():
            if isinstance(error, OSError):
                raise error
        if (not self._publication_matches(packet, request, intent, received)
                or intent['phase'] != 'intent' or intent['path'] != target):
            raise ValueError('reconciliation requires the bound original publication intent')
        if unreadable:
            raise next(iter(unreadable.values()))
        if any(name != intent_name and record.get('path') == target
               for name, (record, _) in publications.items()):
            raise ValueError('conflicting publication records for the same target')
        _validate_publication_record(intent)
        matching = {name: entry for name, entry in reconciliations.items()
                    if name == account_name or entry[0].get('path') == target
                    or entry[0].get('intent_sha256') == intent_digest}
        if not matching:
            # Absence of the exact spelling does not establish a vacant slot.
            try:
                (directory / account_name).lstat()
            except FileNotFoundError:
                pass
            else:
                raise ValueError('publication reconciliation slot is occupied by another entry')
        accounts = self._publication_accounts(request, received, {target: (intent, intent_digest)}, matching)
        prior = accounts.get(target)
        if prior is not None:
            if prior['account'] != account or prior['observed_sha256'] != expected_sha256:
                raise ValueError('publication reconciliation already has a different account or observation')
            return prior
        observed = self._observe_target(target)['sha256']
        if observed != expected_sha256:
            raise ValueError('current target differs from the expected reconciliation observation')
        if observed not in (intent['base_sha256'], intent['sha256']):
            raise ValueError('target differs from the base and proposal; prepare a fresh receiving occasion')
        record = {'version': 1, 'packet': packet, 'path': target, 'account': account,
                  'request_sha256': sha(encode(request)), 'return_sha256': received['return_sha256'],
                  'intent_sha256': intent_digest, 'observed_sha256': observed,
                  'recorded_at': now(), 'historical_completion': 'unestablished'}
        record['reconciliation_sha256'] = sha(encode(record))
        write_json(directory / account_name, record)
        return record

    def publish(self, packet: str, target: str, reason: str) -> dict[str, Any]:
        target = _relative(target)
        output = _external(self.path(target), 'publication destination')
        self._authority.authorize_publication(NativeLayout.owner_of(output), packet, target)
        directory, request = self.load(packet)
        if not reason.strip():
            raise ValueError('explicit publication reason is required')
        record = read_json(directory / 'return.json')
        self.validate_return(directory, record)
        proposals = [proposal for proposal in record['proposals'] if proposal['path'] == target]
        if not proposals:
            raise ValueError('no received proposal for that target')
        declared = [row for row in request['targets'] if row['path'] == target]
        if (len(declared) != 1 or len(proposals) != 1 or record['packet'] != packet
                or request['packet'] != packet):
            raise ValueError('publication proposal binding changed')
        proposal = proposals[0]
        if 'base_sha256' in proposal and proposal['base_sha256'] != declared[0]['sha256']:
            raise ValueError('publication proposal binding changed')
        path = directory / _publication_name(target)
        basis = self._basis(packet, directory, request, record, publishing_target=target)
        changed = [row for row in basis if not row['unchanged']]
        if changed:
            failure = PublicationBasisChanged()
            try:
                failure.basis = _publication_basis(packet, target, request, record, proposal, changed)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                pass  # Optional evidence must not displace the original refusal.
            raise failure
        if not output.parent.is_dir():
            raise ValueError('publication parent must already exist')
        body = self.get(proposal['sha256'])
        effect = {'packet': packet, 'path': target, 'phase': 'intent', 'reason': reason,
                  'sha256': proposal['sha256'], 'base_sha256': proposal['base_sha256'],
                  'return_sha256': record['return_sha256'], 'started_at': now()}
        write_json(path, effect)
        _atomic_bytes(output, body)
        effect.update(phase='complete', completed_at=now())
        write_json(path, effect)
        return effect


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True, type=Path)
    sub = p.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init'); init.add_argument('--workspace', type=Path, required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('packet'); prep.add_argument('--consumer', required=True); prep.add_argument('--brief', required=True)
    for flag in ('method', 'material', 'target', 'return'):
        prep.add_argument('--' + flag, dest=flag + 's', action='append', default=[])
    prep.add_argument('--artifact', dest='artifacts', action='append', default=[],
                      metavar='PACKET:TARGET', help='include a recorded proposal as material, without publication')
    dis = sub.add_parser('dispatch'); dis.add_argument('packet'); dis.add_argument('argv', nargs=argparse.REMAINDER)
    rec = sub.add_parser('receive'); rec.add_argument('packet'); rec.add_argument('--reply', type=Path)
    rec.add_argument('--producer', required=True); rec.add_argument('--proposal', dest='proposals', action='append', default=[])
    rec.add_argument('--wait', type=float, default=0)
    listing = sub.add_parser('list'); listing.add_argument('--consumer')
    ins = sub.add_parser('inspect'); ins.add_argument('packet')
    context = sub.add_parser('context', help='read the validated originating context')
    context.add_argument('packet'); context.add_argument('--text', action='store_true')
    recovery = sub.add_parser('recover-transport', help='recover the captured carrier view from retained evidence')
    recovery.add_argument('packet'); recovery.set_defaults(action='recover_transport')
    closure = sub.add_parser('close-transport', help='record an owner-accounted terminal failure without a contribution')
    closure.add_argument('packet'); closure.add_argument('--account', required=True)
    closure.set_defaults(action='close_transport')
    reconciliation = sub.add_parser('reconcile-publication', help='acknowledge observed target bytes without replaying publication')
    reconciliation.add_argument('packet'); reconciliation.add_argument('--target', required=True)
    reconciliation.add_argument('--expected-sha256', required=True, metavar='SHA256_OR_absent')
    reconciliation.add_argument('--account', required=True)
    reconciliation.set_defaults(action='reconcile_publication')
    pub = sub.add_parser('publish'); pub.add_argument('packet'); pub.add_argument('--target', required=True); pub.add_argument('--reason', required=True)
    args = vars(p.parse_args())
    root, action = args.pop('root'), args.pop('action')
    text = args.pop('text', False)
    try:
        if action == 'init':
            result = Exchange.initialize(root, **args)
        else:
            ex = Exchange(root)
            with exclusive(ex.root / 'owner.lock', allow_readonly_open=(action == 'inspect')):
                if action == 'dispatch' and args['argv'][:1] == ['--']:
                    args['argv'] = args['argv'][1:]
                result = getattr(ex, action)(**args)
        if text:
            sys.stdout.write(result['context'])
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, RuntimeError, StopIteration) as exc:
        print(json.dumps({'error': str(exc), 'operation': action}), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
