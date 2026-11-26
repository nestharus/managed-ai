"""Read and validate publication intents, completions and reconciliation receipts."""
from __future__ import annotations

from datetime import datetime, timedelta
from stat import S_ISREG

from ..storage import encode, is_ordinary_file_stat, sha, strict_json_loads


def publication_name(target: str, prefix: str = 'publish') -> str:
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


def validate_publication_record(record):
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


def bound_publication_proposal(packet, request, received, target):
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


def publication_matches(packet, request, receipt, received):
    if (received is None or not isinstance(receipt, dict)
            or receipt.get('phase') not in ('intent', 'complete')
            or receipt.get('packet') != packet
            or receipt.get('return_sha256') != received['return_sha256']):
        return False
    proposal = bound_publication_proposal(packet, request, received, receipt.get('path'))
    return (proposal is not None and receipt.get('sha256') == proposal['sha256']
            and 'base_sha256' in receipt and receipt['base_sha256'] == proposal['base_sha256'])


def publication_inventory(directory):
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
            if not is_ordinary_file_stat(entry):
                raise ValueError('ordinary publication record required: ' + path.name)
            raw = path.read_bytes()
            record = strict_json_loads(raw, path.name, require_object=True)
            inventory[path.name] = (record, sha(raw))
        except (OSError, ValueError) as exc:
            unreadable[path.name] = exc
    return publications, reconciliations, unreadable


def publication_accounts(exchange, request, received, intents, reconciliations):
    canonical = {publication_name(path, 'reconcile'): path for path in intents}
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
        reconciliation = validate_publication_reconciliation(
            request, received, receipt, digest, record)
        if path in accounts:
            raise ValueError('conflicting publication reconciliations for the same target')
        accounts[path] = reconciliation
        misplaced = misplaced or name not in canonical
    if misplaced:
        raise ValueError('publication reconciliation is outside its canonical slot')
    return accounts


def validate_publication_reconciliation(request, received, intent, digest, record):
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
