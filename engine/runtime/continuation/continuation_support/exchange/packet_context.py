"""Bind a workspace, freeze packet inputs and commit prepared contexts."""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from ..ownership import (
    EffectAuthority,
    NativeLayout,
    StandaloneAuthority,
    external_root,
    packet_id as _id,
    relative_path as _relative,
)
from ..packet_transport import PacketTransport
from ..storage import (
    ObjectStore,
    atomic_bytes as _atomic_bytes,
    encode,
    exclusive,
    now,
    read_bytes as _bytes,
    read_json,
    sha,
    sync_dir,
    write_json,
)


MAX_PACKET_BYTES = 8 * 1024 * 1024


class PacketContext:
    """One cooperative owner; the caller serializes mutations."""


    def __init__(self, root: Path, *, authority: EffectAuthority | None = None):
        self.root = external_root(root)
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
        root = external_root(root)
        workspace = workspace.expanduser().resolve(strict=True)
        if not workspace.is_dir() or root == workspace or root.is_relative_to(workspace) or workspace.is_relative_to(root):
            raise ValueError('workspace and exchange must be disjoint directories')
        external_root(workspace, 'workspace')
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
