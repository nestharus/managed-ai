"""Export pinned records and receive a distinct instance's observation into the current store."""
from __future__ import annotations

import base64
from pathlib import Path

from managed_ai.core.formats import SOURCE_OBSERVATION_FORMAT, TRANSFER_FORMAT
from managed_ai.core.ownership import packet_id
from managed_ai.core.storage import (
    atomic_bytes,
    check_ordinary_ancestors,
    encode,
    is_ordinary_file_stat,
    is_reparse_or_symlink,
    sha,
)
from managed_ai.store.protocol.identities import identity, record_identity, require, strict_json
from managed_ai.store.selection.domain_selection import load_json


def unpack_transfer(bundle, records):
    """Verify a supplied native export without receiving or retaining any material."""
    require(bundle.get('format') == TRANSFER_FORMAT, 'unsupported source transfer')
    require(isinstance(bundle.get('body_base64'), str), 'expected encoded transfer body')

    raw = base64.b64decode(bundle['body_base64'], validate=True)
    require(
        sha(raw) == bundle.get('object_sha256')
        and type(bundle.get('bytes')) is int
        and len(raw) == bundle['bytes'],
        'transferred source bytes differ',
    )

    source = bundle.get('source')
    require(
        isinstance(source, dict) and {'instance_id', 'record_id', 'revision', 'sha256'} <= source.keys(),
        'incomplete transfer source identity',
    )
    require(
        isinstance(source['instance_id'], str) and source['instance_id'].strip(),
        'source transfer requires a stable declared source instance',
    )

    wanted = identity(dict(id=source['record_id'], revision=source['revision'], sha256=source['sha256']))
    record = strict_json(raw)
    require(
        record.get('format') == records.format
        and record.get('sha256') == sha(encode({k: v for k, v in record.items() if k != 'sha256'})),
        'transferred record identity differs',
    )
    require(
        {'id', 'revision', 'sha256'} <= record.keys() and identity(record_identity(record)) == wanted,
        'source pin differs from transferred record',
    )
    return source, record, raw


def _check_output_ancestors(output: Path) -> None:
    """Reject symlinks/NTFS junctions along any existing ancestor or target of output."""
    if output.anchor and not output.is_absolute():
        raise ValueError(f'ordinary transfer output required: {output}')

    for candidate in (output, *output.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ValueError(f'ordinary transfer output required: {candidate}') from None

        if is_reparse_or_symlink(info):
            raise ValueError(f'ordinary transfer output required: {candidate}')


def export(system, ident, output):
    record, raw = system.record_store.read(ident)
    source = {
        'instance_id': system.instance_id,
        'record_id': record['id'],
        'revision': record['revision'],
        'sha256': record['sha256'],
    }
    if not source['instance_id']:
        raise ValueError('cross-instance observation requires a stable source instance identity')

    bundle = {
        'format': TRANSFER_FORMAT,
        'source': source,
        'object_sha256': sha(raw),
        'bytes': len(raw),
        'body_base64': base64.b64encode(raw).decode('ascii'),
        'scope': (
            'Exact captured record from the named source instance; '
            'no receiving adoption or current-head claim after export.'
        ),
    }

    output = Path(output)
    _check_output_ancestors(output)
    encoded = encode(bundle)

    try:
        out_info = output.lstat()
    except FileNotFoundError:
        out_info = None

    if out_info is not None:
        require(is_ordinary_file_stat(out_info), f'ordinary transfer output required: {output}')
        if output.read_bytes() != encoded:
            raise ValueError('transfer output contains another body')

    output.parent.mkdir(parents=True, exist_ok=True)
    check_ordinary_ancestors(output.parent, expect_dir=True, error_prefix='ordinary transfer output required')
    atomic_bytes(output, encoded)
    return {'source': source, 'bundle': str(output), 'sha256': sha(encoded)}


def receive(system, path, ident, account, expected):
    packet_id(ident)
    if not isinstance(account, str) or not account.strip():
        raise ValueError('source observation requires a receiving account')

    bundle = load_json(path)
    source, _, raw = unpack_transfer(bundle, system.record_store)
    if source['instance_id'] == system.instance_id:
        raise ValueError('observation requires a distinct declared source instance')

    bundle_sha = sha(encode(bundle))
    hot_path = system.record_store.path(ident)
    if not hot_path.exists() and system.record_store.cold_archive.has(ident):
        raise ValueError(f'record {ident} is in cold archive; restore explicitly before modifying')

    old = system.record_store.show(ident) if hot_path.exists() else None
    if old and old['kind'] == 'source-observation' and old['content'].get('transfer_sha256') == bundle_sha:
        return {'replayed': True, 'record': {k: old[k] for k in ('id', 'revision', 'sha256')}, 'source': source}

    current_revision = old['revision'] if old else 0
    if current_revision != expected:
        raise ValueError('receiving observation revision conflict')

    key = sha(raw)
    relative = 'sources/received/' + key + '.json'
    target = system.ex.path(relative)

    try:
        target_info = target.lstat()
    except FileNotFoundError:
        target_info = None

    if target_info is not None:
        require(is_ordinary_file_stat(target_info), f'ordinary observation target required: {target}')
        if target.read_bytes() != raw:
            raise ValueError('retained observation body changed')

    value = {
        'id': ident,
        'kind': 'source-observation',
        'content': {
            'format': SOURCE_OBSERVATION_FORMAT,
            'source': source,
            'object_sha256': key,
            'transfer_sha256': bundle_sha,
            'account': account,
            'scope': (
                'Receiver-owned observation of exact supplied source bytes. '
                'The source instance retains ownership; arrival does not accept the finding or close its duty.'
            ),
        },
        'basis': [relative],
    }

    previous, previous_raw = system.record_store.read(ident) if hot_path.exists() else (None, None)
    prepared = system.record_store._prepare_write(
        value,
        previous,
        previous_raw,
        observation_write=True,
        retain_basis=False,
        preserve_basis=[{'path': relative, 'sha256': key, 'bytes': len(raw)}],
        verify_preserved_objects=False,
    )

    key = system.ex.objects.put(raw)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target_info is None:
        atomic_bytes(target, raw)

    result = system.record_store._replace(
        value,
        expected,
        observation_write=True,
        _preloaded=(previous, previous_raw),
        _prepared=prepared,
    )
    return {'replayed': False, 'record': result, 'source': source}


def inspect(system, ident):
    observation = system.record_store.show(ident)
    if observation['kind'] != 'source-observation':
        raise ValueError('record is not a cross-instance observation')

    raw = system.ex.objects.read(observation['content']['object_sha256'], retained=True)
    return {'observation': observation, 'source_record': strict_json(raw)}
