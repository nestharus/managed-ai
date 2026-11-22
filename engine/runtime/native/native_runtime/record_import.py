"""Validate complete import batches and resume acknowledgements against verified heads."""
from continuation_support.ownership import packet_id
from continuation_support.storage import encode, sha, sync_dir, write_json
from native_runtime.domains import load_json


def import_records(system, spec):
    if spec.get('format') != 'managed-ai-import/1':
        raise ValueError('unsupported current import format')
    ident = packet_id(spec['id'])
    rows = spec['records']
    ids = [r['id'] for r in rows]
    if len(ids) != len(set(ids)) or 'goal' in ids:
        raise ValueError('import needs unique non-goal record identities')
    receipt = system.work / '.system/imports' / (ident + '.json')
    spec_sha = sha(encode(spec))
    old = load_json(receipt) if receipt.exists() else None
    if old is not None:
        if old['instance_id'] != system.instance_id or old['id'] != ident:
            raise ValueError('import receipt belongs to another store or import identity')
        if old['spec_sha256'] != spec_sha:
            raise ValueError('import identity already binds different current input')
    # Qualify all rows without capturing basis objects or changing any head.
    for row in rows:
        system.record_store.validate_import(row)
    recorded = old['records'] if old is not None else []
    prefix = recorded if old is not None and not old['complete'] else []
    if len(prefix) > len(rows):
        raise ValueError('import receipt progress exceeds specification')
    remaining = iter(rows)
    for acknowledged in prefix:
        row = next(remaining)
        current = system.record_store.show(row['id'])
        if acknowledged != {key: current[key] for key in ('id', 'revision', 'sha256')}:
            raise ValueError('import acknowledged head changed: ' + row['id'])
    result = {'format': 'managed-ai-import-receipt/1', 'id': ident,
              'spec_sha256': spec_sha, 'instance_id': system.instance_id, 'records': list(prefix)}
    receipt.parent.mkdir(parents=True, exist_ok=True)
    sync_dir(receipt.parent.parent)
    if old is None:
        write_json(receipt, dict(result, complete=False))
    else:
        # A previous replacement may have become visible before its sync was confirmed.
        sync_dir(receipt.parent)
    for row in remaining:
        if not system.record_store.path(row['id']).exists():
            system.put(row, 0)
        current = system.record_store.show(row['id'])
        result['records'].append({key: current[key] for key in ('id', 'revision', 'sha256')})
        if len(result['records']) > len(recorded):
            write_json(receipt, dict(result, complete=False))
    result['complete'] = True
    if result != old:
        write_json(receipt, result)
    return result
