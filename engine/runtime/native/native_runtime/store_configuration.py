"""Change the store's selected domains and catalogues against the reviewed descriptor."""
from pathlib import Path
import json

from continuation_support.ownership import packet_id
from continuation_support.storage import encode, sha, write_json
from native_runtime.selection.domain_selection import DomainSelection, ordinary, pin


REVIEWED_CONFIGURE = 'managed-ai-reviewed-configure/1'
SELECTION_KEYS = ('domains', 'catalogues')


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


def configure(system, spec):
    descriptor = system.root / 'system.json'
    current = configuration_descriptor(system.root, spec)
    selected = DomainSelection(system.root, current)
    from native_runtime.selection.installed_catalogue import InstalledCatalogue
    selected.validate_catalogue({e['id']: e for e in InstalledCatalogue(system.techniques.root, selected).index()['entries']})
    previous_kinds = system.persisted_descriptor.get('schema_bindings', system.domain_selection.schemas)
    for record in system.record_store.all():
        if record['kind'] in previous_kinds and record['kind'] not in selected.schemas:
            raise ValueError('selected schema still owns stored records: ' + record['kind'])
        selected.validate_record(record)
    write_json(descriptor, current)
    receipt = {'instance_id': system.instance_id, 'descriptor_sha256': sha(descriptor.read_bytes()),
               'selection': selected.view()}
    if spec.get('format') == REVIEWED_CONFIGURE:
        receipt['reviewed_plan'] = {'format': REVIEWED_CONFIGURE, 'prior': spec['prior'],
                                    'selection': {key: current[key] for key in SELECTION_KEYS}}
    return receipt
