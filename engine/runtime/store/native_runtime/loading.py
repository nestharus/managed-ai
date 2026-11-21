"""Current record intake and explicit observation across native instances.

The receiving store owns its observation and judgment, never the source head.
Import receipts make retries resumable without assuming multi-record atomicity.
"""
from pathlib import Path
import base64
import json

from continuation_support.ownership import packet_id
from continuation_support.storage import encode, sha, write_json, atomic_bytes, sync_dir
from native_runtime.domains import DomainSelection, pin, load_json, ordinary
from native_runtime.identities import identity, record_identity, require, strict_json


REVIEWED_CONFIGURE = 'managed-ai-reviewed-configure/1'
SELECTION_KEYS = ('domains', 'catalogues')


def unpack_transfer(bundle, records):
    """Verify a supplied native export without receiving or retaining any material."""
    require(bundle.get('format') == 'managed-ai-transfer/1', 'unsupported source transfer')
    require(isinstance(bundle.get('body_base64'), str), 'expected encoded transfer body')
    raw = base64.b64decode(bundle['body_base64'], validate=True)
    require(sha(raw) == bundle.get('object_sha256') and type(bundle.get('bytes')) is int and
            len(raw) == bundle['bytes'], 'transferred source bytes differ')
    source = bundle.get('source')
    require(isinstance(source, dict) and {'instance_id', 'record_id', 'revision', 'sha256'} <= source.keys(),
            'incomplete transfer source identity')
    require(isinstance(source['instance_id'], str) and source['instance_id'].strip(),
            'source transfer requires a stable declared source instance')
    wanted = identity(dict(id=source['record_id'], revision=source['revision'], sha256=source['sha256']))
    record = strict_json(raw)
    require(record.get('format') == records.format and record.get('sha256') ==
            sha(encode({k: v for k, v in record.items() if k != 'sha256'})), 'transferred record identity differs')
    require({'id', 'revision', 'sha256'} <= record.keys() and identity(record_identity(record)) == wanted,
            'source pin differs from transferred record')
    return source, record, raw


class Loading:
    def __init__(self, system):
        self.system = system
        self.records = system.record_store

    def import_records(self, spec):
        if spec.get('format') != 'managed-ai-import/1':
            raise ValueError('unsupported current import format')
        ident = packet_id(spec['id'])
        rows = spec['records']
        ids = [r['id'] for r in rows]
        if len(ids) != len(set(ids)) or 'goal' in ids:
            raise ValueError('import needs unique non-goal record identities')
        receipt = self.system.work / '.system/imports' / (ident + '.json')
        spec_sha = sha(encode(spec))
        old = load_json(receipt) if receipt.exists() else None
        if old is not None:
            if old['instance_id'] != self.system.instance_id or old['id'] != ident:
                raise ValueError('import receipt belongs to another store or import identity')
            if old['spec_sha256'] != spec_sha:
                raise ValueError('import identity already binds different current input')
        # Qualify all rows without capturing basis objects or changing any head.
        for row in rows:
            self.records.validate_import(row)
        recorded = old['records'] if old is not None else []
        prefix = recorded if old is not None and not old['complete'] else []
        if len(prefix) > len(rows):
            raise ValueError('import receipt progress exceeds specification')
        remaining = iter(rows)
        for acknowledged in prefix:
            row = next(remaining)
            current = self.records.show(row['id'])
            if acknowledged != {key: current[key] for key in ('id', 'revision', 'sha256')}:
                raise ValueError('import acknowledged head changed: ' + row['id'])
        result = {'format': 'managed-ai-import-receipt/1', 'id': ident,
                  'spec_sha256': spec_sha, 'instance_id': self.system.instance_id, 'records': list(prefix)}
        receipt.parent.mkdir(parents=True, exist_ok=True)
        sync_dir(receipt.parent.parent)
        if old is None:
            write_json(receipt, dict(result, complete=False))
        else:
            # A previous replacement may have become visible before its sync was confirmed.
            sync_dir(receipt.parent)
        for row in remaining:
            if not self.records.path(row['id']).exists():
                self.system.put(row, 0)
            current = self.records.show(row['id'])
            result['records'].append({key: current[key] for key in ('id', 'revision', 'sha256')})
            if len(result['records']) > len(recorded):
                write_json(receipt, dict(result, complete=False))
        result['complete'] = True
        if result != old:
            write_json(receipt, result)
        return result

    def configure(self, spec):
        descriptor = self.system.root / 'system.json'
        current = self.configuration_descriptor(self.system.root, spec)
        selected = DomainSelection(self.system.root, current)
        from native_runtime.catalogue import InstalledCatalogue
        selected.validate_catalogue({e['id']: e for e in InstalledCatalogue(self.system.techniques.root, selected).index()['entries']})
        previous_kinds = self.system.persisted_descriptor.get('schema_bindings', self.system.domain_selection.schemas)
        for record in self.records.all():
            if record['kind'] in previous_kinds and record['kind'] not in selected.schemas:
                raise ValueError('selected schema still owns stored records: ' + record['kind'])
            selected.validate_record(record)
        write_json(descriptor, current)
        receipt = {'instance_id': self.system.instance_id, 'descriptor_sha256': sha(descriptor.read_bytes()),
                   'selection': selected.view()}
        if spec.get('format') == REVIEWED_CONFIGURE:
            receipt['reviewed_plan'] = {'format': REVIEWED_CONFIGURE, 'prior': spec['prior'],
                                        'selection': {key: current[key] for key in SELECTION_KEYS}}
        return receipt

    @staticmethod
    def configuration_descriptor(root, spec):
        root = Path(root).resolve()
        if 'format' in spec:
            if spec['format'] != REVIEWED_CONFIGURE:
                raise ValueError('unsupported native configure format')
            # Omit the mandatory legacy guard at top level so old receivers refuse.
            if set(spec) != {'format', 'prior', 'selection', 'account'}:
                raise ValueError('reviewed configure requires its own envelope')
            prior = spec['prior']
            selection = spec['selection']
            if not isinstance(prior, dict) or set(prior) != {'instance_id', 'descriptor_sha256'}:
                raise ValueError('reviewed configure needs a prior instance and descriptor digest')
            if not isinstance(selection, dict) or set(selection) != set(SELECTION_KEYS):
                raise ValueError('reviewed configure needs ordered domain and catalogue bindings')
            expected = prior['descriptor_sha256']
        else:
            expected = spec['expected_sha256']
        raw = ordinary(root / 'system.json').read_bytes()
        if sha(raw) != expected:
            raise ValueError('store selection revision conflict')
        if not isinstance(spec.get('account'), str) or not spec['account'].strip():
            raise ValueError('selection change needs a receiving account')
        current = dict(json.loads(raw))
        if 'format' in spec:
            instance = prior['instance_id']
            if not isinstance(instance, str) or packet_id(instance) != current.get('instance_id'):
                raise ValueError('reviewed configure receiving instance differs')
            for key in SELECTION_KEYS:
                reviewed = selection[key]
                if not isinstance(reviewed, list):
                    raise ValueError('reviewed configure bindings must be ordered lists')
                actual = [{'path': binding['path'], 'sha256': pin(root, root / binding['path'])['sha256']}
                          for binding in current.get(key, [])]
                if len(reviewed) != len(actual):
                    raise ValueError(f'reviewed {key} binding count differs: '
                                     f'expected {len(reviewed)}, current {len(actual)}')
                for index, (wanted, observed) in enumerate(zip(reviewed, actual), 1):
                    if wanted != observed:
                        raise ValueError(f'reviewed {key} postimage differs at ordered row {index}: '
                                         f'expected {wanted!r}, current {observed!r}')
                current[key] = actual
        else:
            if 'record_retention' in spec:
                if spec['record_retention'] not in {'current-use', 'append-history'}:
                    raise ValueError('unsupported native record retention policy')
                current['record_retention'] = spec['record_retention']
            for key in SELECTION_KEYS:
                if key in spec:
                    current[key] = [pin(root, Path(path)) for path in spec[key]]
        selected = DomainSelection(root, current)
        current['schema_bindings'] = {kind: sha(encode(schema)) for kind, schema in selected.schemas.items()}
        current['selection_account'] = spec['account']
        return current

    def export(self, ident, output):
        record, raw = self.records.read(ident)
        source = {'instance_id': self.system.instance_id, 'record_id': record['id'],
                  'revision': record['revision'], 'sha256': record['sha256']}
        if not source['instance_id']:
            raise ValueError('cross-instance observation requires a stable source instance identity')
        bundle = {'format': 'managed-ai-transfer/1', 'source': source,
                  'object_sha256': sha(raw), 'bytes': len(raw),
                  'body_base64': base64.b64encode(raw).decode('ascii'),
                  'scope': 'Exact captured record from the named source instance; no receiving adoption or current-head claim after export.'}
        output = Path(output)
        if output.exists() and output.read_bytes() != encode(bundle):
            raise ValueError('transfer output contains another body')
        output.parent.mkdir(parents=True, exist_ok=True)
        atomic_bytes(output, encode(bundle))
        return {'source': source, 'bundle': str(output), 'sha256': sha(encode(bundle))}

    def receive(self, path, ident, account, expected):
        packet_id(ident)
        if not isinstance(account, str) or not account.strip():
            raise ValueError('source observation requires a receiving account')
        bundle = load_json(path)
        source, _, raw = unpack_transfer(bundle, self.records)
        if not source.get('instance_id') or source['instance_id'] == self.system.instance_id:
            raise ValueError('observation requires a distinct declared source instance')
        bundle_sha = sha(encode(bundle))
        old = self.records.show(ident) if self.records.path(ident).exists() else None
        if old and old['kind'] == 'source-observation' and old['content'].get('transfer_sha256') == bundle_sha:
            return {'replayed': True, 'record': {k: old[k] for k in ('id', 'revision', 'sha256')}, 'source': source}
        current_revision = old['revision'] if old else 0
        if current_revision != expected:
            raise ValueError('receiving observation revision conflict')
        key = self.system.ex.objects.put(raw)
        relative = 'sources/received/' + key + '.json'
        target = self.system.work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            atomic_bytes(target, raw)
        elif target.read_bytes() != raw:
            raise ValueError('retained observation body changed')
        value = {'id': ident, 'kind': 'source-observation', 'content': {
            'format': 'managed-ai-source-observation/1', 'source': source, 'object_sha256': key,
            'transfer_sha256': bundle_sha, 'account': account,
            'scope': 'Receiver-owned observation of exact supplied source bytes. The source instance retains ownership; arrival does not accept the finding or close its duty.'},
            'basis': [relative]}
        result = self.records._replace(value, expected, observation_write=True)
        return {'replayed': False, 'record': result, 'source': source}

    def inspect(self, ident):
        observation = self.records.show(ident)
        if observation['kind'] != 'source-observation':
            raise ValueError('record is not a cross-instance observation')
        raw = self.system.ex.objects.read(observation['content']['object_sha256'])
        return {'observation': observation, 'source_record': json.loads(raw)}
