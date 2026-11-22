"""Export pinned records and receive a distinct instance's observation into the current store."""
import base64
import json
from pathlib import Path

from continuation_support.ownership import packet_id
from continuation_support.storage import atomic_bytes, encode, sha
from native_runtime.domains import load_json


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
    if bundle.get('format') != 'managed-ai-transfer/1':
        raise ValueError('unsupported source transfer')
    raw = base64.b64decode(bundle['body_base64'], validate=True)
    if sha(raw) != bundle['object_sha256'] or len(raw) != bundle['bytes']:
        raise ValueError('transferred source bytes differ')
    record = json.loads(raw)
    if record.get('format') != system.record_store.format or record.get('sha256') != sha(encode({k: v for k, v in record.items() if k != 'sha256'})):
        raise ValueError('transferred record identity differs')
    source = bundle['source']
    if not source.get('instance_id') or source['instance_id'] == system.instance_id:
        raise ValueError('observation requires a distinct declared source instance')
    if {k: source[k] for k in ('record_id', 'revision', 'sha256')} != {
            'record_id': record['id'], 'revision': record['revision'], 'sha256': record['sha256']}:
        raise ValueError('source pin differs from transferred record')
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
