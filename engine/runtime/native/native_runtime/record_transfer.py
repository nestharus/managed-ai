"""Export pinned records and receive a distinct instance's observation into the current store."""
import base64
import json
from pathlib import Path

from continuation_support.ownership import packet_id
from continuation_support.storage import atomic_bytes, encode, sha
from native_runtime.identities import identity, record_identity, require, strict_json
from native_runtime.selection.domain_selection import load_json


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


def export(system, ident, output):
    record, raw = system.record_store.read(ident)
    source = {'instance_id': system.instance_id, 'record_id': record['id'],
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


def receive(system, path, ident, account, expected):
    packet_id(ident)
    if not isinstance(account, str) or not account.strip():
        raise ValueError('source observation requires a receiving account')
    bundle = load_json(path)
    source, _, raw = unpack_transfer(bundle, system.record_store)
    if source['instance_id'] == system.instance_id:
        raise ValueError('observation requires a distinct declared source instance')
    bundle_sha = sha(encode(bundle))
    old = system.record_store.show(ident) if system.record_store.path(ident).exists() else None
    if old and old['kind'] == 'source-observation' and old['content'].get('transfer_sha256') == bundle_sha:
        return {'replayed': True, 'record': {k: old[k] for k in ('id', 'revision', 'sha256')}, 'source': source}
    current_revision = old['revision'] if old else 0
    if current_revision != expected:
        raise ValueError('receiving observation revision conflict')
    key = system.ex.objects.put(raw)
    relative = 'sources/received/' + key + '.json'
    target = system.work / relative
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
    result = system.record_store._replace(value, expected, observation_write=True)
    return {'replayed': False, 'record': result, 'source': source}


def inspect(system, ident):
    observation = system.record_store.show(ident)
    if observation['kind'] != 'source-observation':
        raise ValueError('record is not a cross-instance observation')
    raw = system.ex.objects.read(observation['content']['object_sha256'])
    return {'observation': observation, 'source_record': json.loads(raw)}
