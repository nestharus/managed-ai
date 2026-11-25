"""Own native import receipts and resume acknowledgements against verified heads."""
import stat

from core.ownership import packet_id
from core.storage import encode, sha, sync_dir, write_json
from store.protocol.identities import digest, exact_fields, identity, record_identity, require, strict_json

from core.formats import IMPORT_FORMAT, IMPORT_RECEIPT_FORMAT as RECEIPT_FORMAT
RECEIPT_DIRECTORY = '.system/imports'
RECEIPT_FIELDS = {'format', 'id', 'instance_id', 'spec_sha256', 'complete', 'records'}


def _specification_identity(spec):
    if spec.get('format') != IMPORT_FORMAT:
        raise ValueError('unsupported current import format')
    ident = packet_id(spec['id'])
    ids = [packet_id(row['id']) for row in spec['records']]
    if len(ids) != len(set(ids)) or 'goal' in ids:
        raise ValueError('import needs unique non-goal record identities')
    return ident, ids, sha(encode(spec))


def _receipt_path(system, ident):
    return system.ex.path(f'{RECEIPT_DIRECTORY}/{packet_id(ident)}.json')


def _load_receipt(path):
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None
    require(stat.S_ISREG(mode), 'ordinary native import receipt required: ' + str(path))
    return strict_json(path.read_bytes())


def _validate_receipt(receipt, ident, instance_id):
    exact_fields(receipt, RECEIPT_FIELDS, set(), 'native import receipt')
    require(receipt['format'] == RECEIPT_FORMAT, 'unsupported native import receipt format')
    require(receipt['instance_id'] == instance_id and receipt['id'] == ident,
            'import receipt belongs to another store or import identity')
    digest(receipt['spec_sha256'], 'import receipt specification digest')
    require(type(receipt['complete']) is bool, 'import receipt completion state must be Boolean')
    require(isinstance(receipt['records'], list), 'import receipt records must be a list')
    ids = [identity(record)['id'] for record in receipt['records']]
    require(len(ids) == len(set(ids)) and 'goal' not in ids,
            'import receipt needs unique non-goal record identities')
    return ids


def _bound_receipt(path, instance_id, ident, ids, spec_sha):
    receipt = _load_receipt(path)
    if receipt is None:
        return None
    accepted = _validate_receipt(receipt, ident, instance_id)
    require(receipt['spec_sha256'] == spec_sha, 'import identity already binds different current input')
    require(accepted == ids[:len(accepted)], 'import receipt progress differs from specification')
    require(not receipt['complete'] or len(accepted) == len(ids), 'completed import receipt does not cover specification')
    return receipt


def _verify_progress(system, acknowledged):
    for accepted in acknowledged:
        ident = accepted['id']
        current = system.record_store.show(ident)
        require(accepted == record_identity(current), 'import acknowledged head changed: ' + ident)


def read_receipt(system, spec):
    """Read the original acknowledgements bound to an exact batch, or None if absent.

    The caller holds the store owner guard. Acknowledgements identify accepted
    revisions; a caller verifying content must open those editions separately.
    """
    ident, ids, spec_sha = _specification_identity(spec)
    receipt = _bound_receipt(_receipt_path(system, ident), system.instance_id, ident, ids, spec_sha)
    if receipt is not None and not receipt['complete']:
        _verify_progress(system, receipt['records'])
    return receipt


def completed_receipt_records(system):
    """Enumerate the exact record identities acknowledged by this store's complete imports."""
    directory = system.ex.path(RECEIPT_DIRECTORY)
    for path in sorted(directory.glob('*.json')):
        receipt = _load_receipt(_receipt_path(system, path.stem))
        if receipt is None or receipt.get('format') != RECEIPT_FORMAT or receipt.get('instance_id') != system.instance_id:
            continue
        _validate_receipt(receipt, path.stem, system.instance_id)
        if receipt['complete']:
            yield from receipt['records']


def import_records(system, spec):
    ident, ids, spec_sha = _specification_identity(spec)
    receipt = _receipt_path(system, ident)
    old = _bound_receipt(receipt, system.instance_id, ident, ids, spec_sha)
    rows = spec['records']
    # Qualify all rows without capturing basis objects or changing any head.
    prefix = old['records'] if old is not None else []
    for row in rows:
        system.record_store.validate_import(row)
    # Refuse pre-existing revision >= 2 heads on unacknowledged rows (prevents co-opting evolved heads).
    for row in rows[len(prefix):]:
        if system.record_store.path(row['id']).exists():
            existing = system.record_store.show(row['id'])
            require(existing['revision'] == 1,
                    f"fresh import refuses pre-existing head at revision {existing['revision']}: {row['id']}")
    _verify_progress(system, prefix)
    if old is not None:
        # A replacement may have become visible before its directory sync completed.
        sync_dir(receipt.parent)
        if old['complete']:
            return old

    # Only a new or interrupted batch may publish acknowledgements.
    result = {'format': RECEIPT_FORMAT, 'id': ident,
              'spec_sha256': spec_sha, 'instance_id': system.instance_id, 'records': list(prefix)}
    receipt.parent.mkdir(parents=True, exist_ok=True)
    sync_dir(receipt.parent.parent)
    if old is None:
        write_json(receipt, dict(result, complete=False))
    with system.record_store.batch_index_updates():
        for row in rows[len(prefix):]:
            if not system.record_store.path(row['id']).exists():
                system.put(row, 0)
            current = system.record_store.show(row['id'])
            result['records'].append(record_identity(current))
            write_json(receipt, dict(result, complete=False))
    result['complete'] = True
    write_json(receipt, result)
    return result
