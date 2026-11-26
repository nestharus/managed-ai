"""Guarded multi-record batch updates with full pre-validation, predecessor pinning and resumable receipts."""
from __future__ import annotations

import copy
import json
import stat

from core.ownership import packet_id
from core.storage import MAX_BYTES, check_ordinary_ancestors, encode, is_ordinary_file_stat, sha, sync_dir, write_json
from store.protocol.identities import digest, exact_fields, nonempty, require, strict_json
from store.protocol.inputs import integer, strings

from core.formats import BATCH_FORMAT, BATCH_RECEIPT_FORMAT as RECEIPT_FORMAT
RECEIPT_DIRECTORY = '.system/batches'
RECEIPT_REQUIRED = {'format', 'id', 'instance_id', 'spec_sha256', 'pin_predecessors', 'complete', 'records'}
RECEIPT_OPTIONAL = {'pending'}
PENDING_FIELDS = {'id', 'before_revision', 'before_sha256', 'pinned_object',
                  'expected_after_revision', 'expected_fields_sha256'}
TOP_REQUIRED = {'format', 'id', 'operations'}
TOP_OPTIONAL = {'instance_id', 'pin_predecessors', 'account'}
REPLACE_REQUIRED = {'id', 'expected_revision', 'mode', 'record'}
REPLACE_OPTIONAL = {'expected_sha256'}
PATCH_REQUIRED = {'id', 'expected_revision', 'expected_sha256', 'mode'}
PATCH_OPTIONAL = {'add_relations', 'remove_relations', 'set_active',
                  'replace_content_fields', 'remove_content_fields', 'basis'}


def _fields_digest(record: dict) -> str:
    return sha(encode({k: record[k] for k in ('id', 'kind', 'content', 'relations', 'basis', 'active')}))


def _spec_identity(system, spec):
    json.dumps(spec, allow_nan=False)
    exact_fields(spec, TOP_REQUIRED, TOP_OPTIONAL, 'batch update specification')
    require(spec['format'] == BATCH_FORMAT, 'unsupported batch update format')
    ident = packet_id(spec['id'])
    if 'instance_id' in spec:
        require(spec['instance_id'] == system.instance_id, 'batch update targets a different instance')
    pin_predecessors = spec.get('pin_predecessors', False)
    require(type(pin_predecessors) is bool, 'pin_predecessors must be Boolean')
    if 'account' in spec:
        nonempty(spec['account'], 'batch account')
    operations = spec['operations']
    require(isinstance(operations, list) and len(operations) > 0, 'batch update requires a nonempty operations list')
    ids = []
    for op in operations:
        require(isinstance(op, dict), 'each batch operation must be an object')
        ids.append(packet_id(op.get('id', '')))
    require(len(ids) == len(set(ids)), 'batch update requires unique record identities')
    return ident, ids, pin_predecessors, sha(encode(spec))


def _receipt_path(system, ident):
    return system.ex.path(f'{RECEIPT_DIRECTORY}/{packet_id(ident)}.json')


def _load_receipt(path):
    if path.parent.exists():
        check_ordinary_ancestors(path.parent, expect_dir=True, error_prefix='ordinary batch update receipt required')
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    require(is_ordinary_file_stat(info), 'ordinary batch update receipt required: ' + str(path))
    return strict_json(path.read_bytes())


def _validate_pending(pending, expected_id):
    exact_fields(pending, PENDING_FIELDS, set(), 'batch pending entry')
    require(packet_id(pending['id']) == expected_id, 'batch pending entry does not match next operation')
    integer(pending['before_revision'], 'before_revision')
    if pending['before_revision'] == 0:
        require(pending['before_sha256'] is None and pending['pinned_object'] is None,
                'invalid revision-0 pending predecessor pin')
    else:
        digest(pending['before_sha256'], 'before_sha256')
        if pending['pinned_object'] is not None:
            digest(pending['pinned_object'], 'pinned_object')
    integer(pending['expected_after_revision'], 'expected_after_revision', 1)
    require(pending['expected_after_revision'] == pending['before_revision'] + 1,
            'invalid pending revision increment')
    digest(pending['expected_fields_sha256'], 'expected_fields_sha256')


def _validate_receipt(receipt, ident, instance_id):
    exact_fields(receipt, RECEIPT_REQUIRED, RECEIPT_OPTIONAL, 'batch update receipt')
    require(receipt['format'] == RECEIPT_FORMAT, 'unsupported batch update receipt format')
    require(receipt['instance_id'] == instance_id and receipt['id'] == ident,
            'batch receipt belongs to another store or batch identity')
    digest(receipt['spec_sha256'], 'batch receipt specification digest')
    require(type(receipt['pin_predecessors']) is bool, 'batch receipt pin_predecessors must be Boolean')
    require(type(receipt['complete']) is bool, 'batch receipt completion state must be Boolean')
    require(isinstance(receipt['records'], list), 'batch receipt records must be a list')
    if receipt['complete']:
        require(receipt.get('pending') is None, 'completed batch receipt may not retain a pending entry')
    ids = []
    for row in receipt['records']:
        exact_fields(row, {'id', 'before_revision', 'before_sha256', 'pinned_object',
                           'after_revision', 'after_sha256'}, set(), 'batch receipt row')
        rid = packet_id(row['id'])
        integer(row['before_revision'], 'before_revision')
        if row['before_revision'] == 0:
            require(row['before_sha256'] is None and row['pinned_object'] is None,
                    'invalid revision-0 predecessor pin in batch receipt')
        else:
            digest(row['before_sha256'], 'before_sha256')
            if row['pinned_object'] is not None:
                digest(row['pinned_object'], 'pinned_object')
        integer(row['after_revision'], 'after_revision', 1)
        digest(row['after_sha256'], 'after_sha256')
        ids.append(rid)
    require(len(ids) == len(set(ids)), 'batch receipt contains duplicate record identities')
    return ids


def _bound_receipt(path, instance_id, ident, ids, spec_sha, pin_predecessors):
    receipt = _load_receipt(path)
    if receipt is None:
        return None
    accepted = _validate_receipt(receipt, ident, instance_id)
    require(receipt['spec_sha256'] == spec_sha and receipt['pin_predecessors'] == pin_predecessors,
            'batch identity already binds different input')
    require(accepted == ids[:len(accepted)], 'batch receipt progress differs from specification')
    require(not receipt['complete'] or len(accepted) == len(ids),
            'completed batch receipt does not cover specification')
    if receipt.get('pending') is not None:
        require(len(accepted) < len(ids), 'batch pending entry exceeds remaining operations')
        _validate_pending(receipt['pending'], ids[len(accepted)])
    return receipt


def _verify_progress(system, acknowledged):
    for row in acknowledged:
        current = system.record_store.show(row['id'])
        require(current['revision'] == row['after_revision'] and current['sha256'] == row['after_sha256'],
                'batch acknowledged head changed: ' + row['id'])


def _build_candidate(op, previous):
    mode = op.get('mode')
    require(mode in ('replace', 'patch'), 'batch operation mode must be replace or patch')
    expected_rev = integer(op.get('expected_revision'), 'expected_revision')
    if mode == 'replace':
        exact_fields(op, REPLACE_REQUIRED, REPLACE_OPTIONAL, 'replace batch operation')
        if expected_rev == 0:
            require(op.get('expected_sha256') is None, 'revision 0 replace must not supply expected_sha256')
            require(previous is None, f"record already exists for revision 0 replace: {op['id']}")
        else:
            require(previous is not None, f"missing record for replace: {op['id']}")
            expected_sha = digest(op.get('expected_sha256'), 'expected_sha256')
            require(previous['revision'] == expected_rev and previous['sha256'] == expected_sha,
                    f"stale record guard for {op['id']}: expected rev {expected_rev} ({expected_sha})")
        raw_rec = op['record']
        require(isinstance(raw_rec, dict), 'replace operation record must be an object')
        candidate = copy.deepcopy(raw_rec)
        if 'id' in candidate:
            require(candidate['id'] == op['id'], 'replace record id differs from operation id')
        else:
            candidate['id'] = op['id']
        return candidate, None

    exact_fields(op, PATCH_REQUIRED, PATCH_OPTIONAL, 'patch batch operation')
    require(expected_rev >= 1, 'patch mode requires expected_revision >= 1')
    require(previous is not None, f"missing record for patch: {op['id']}")
    expected_sha = digest(op['expected_sha256'], 'expected_sha256')
    require(previous['revision'] == expected_rev and previous['sha256'] == expected_sha,
            f"stale record guard for {op['id']}: expected rev {expected_rev} ({expected_sha})")
    require(any(k in op for k in PATCH_OPTIONAL), f"patch operation has no mutations: {op['id']}")

    content = copy.deepcopy(previous['content'])
    if 'remove_content_fields' in op:
        require(isinstance(content, dict), 'remove_content_fields requires object content')
        for key in strings(op['remove_content_fields'], 'remove_content_fields'):
            content.pop(key, None)
    if 'replace_content_fields' in op:
        require(isinstance(content, dict), 'replace_content_fields requires object content')
        patch_fields = op['replace_content_fields']
        require(isinstance(patch_fields, dict), 'replace_content_fields must be an object')
        for key, value in patch_fields.items():
            require(isinstance(key, str) and bool(key), 'content field name must be nonempty string')
            content[key] = copy.deepcopy(value)

    relations = copy.deepcopy(previous['relations'])
    if 'remove_relations' in op:
        removals = op['remove_relations']
        require(isinstance(removals, list) and all(isinstance(r, dict) for r in removals),
                'remove_relations must be a list of objects')
        remove_keys = {encode(r) for r in removals}
        relations = [r for r in relations if encode(r) not in remove_keys]
    if 'add_relations' in op:
        additions = op['add_relations']
        require(isinstance(additions, list) and all(isinstance(r, dict) for r in additions),
                'add_relations must be a list of objects')
        seen = {encode(r) for r in relations}
        for rel in additions:
            encoded_rel = encode(rel)
            if encoded_rel not in seen:
                relations.append(copy.deepcopy(rel))
                seen.add(encoded_rel)

    active = previous['active']
    if 'set_active' in op:
        require(type(op['set_active']) is bool, 'set_active must be Boolean')
        active = op['set_active']

    candidate = {
        'id': previous['id'],
        'kind': previous['kind'],
        'content': content,
        'relations': relations,
        'active': active,
    }
    if 'basis' in op:
        candidate['basis'] = copy.deepcopy(op['basis'])
        return candidate, None
    return candidate, copy.deepcopy(previous['basis'])


def _reconcile_pending(store, op, pending, previous, pin_predecessors):
    """If a crash occurred right after _replace() wrote rev N+1, verify and recover the committed row."""
    if pending is None or previous is None or previous['revision'] != pending['expected_after_revision']:
        return None
    require(pending['before_revision'] == op.get('expected_revision'),
            'batch pending revision differs from specification')
    require(pending['before_sha256'] == op.get('expected_sha256'),
            'batch pending predecessor digest differs from specification')
    require(_fields_digest(previous) == pending['expected_fields_sha256'],
            f"record {op['id']} at rev {previous['revision']} differs from pending batch operation")
    pinned = pending['pinned_object']
    if pin_predecessors and pending['before_revision'] > 0:
        require(pinned is not None, 'missing predecessor pin on pending batch operation')
        prior_raw = store.objects.read(pinned, retained=True)
        prior_rec = strict_json(prior_raw)
        require(prior_rec.get('id') == op['id']
                and prior_rec.get('revision') == pending['before_revision']
                and prior_rec.get('sha256') == pending['before_sha256'],
                'pinned predecessor differs from pending batch operation')
    return {
        'id': op['id'],
        'before_revision': pending['before_revision'],
        'before_sha256': pending['before_sha256'],
        'pinned_object': pinned,
        'after_revision': previous['revision'],
        'after_sha256': previous['sha256'],
    }


def put_batch(system, spec):
    """Pre-validate all operations, then commit them with optional predecessor pinning and durable receipt."""
    ident, ids, pin_predecessors, spec_sha = _spec_identity(system, spec)
    receipt = _receipt_path(system, ident)
    old = _bound_receipt(receipt, system.instance_id, ident, ids, spec_sha, pin_predecessors)
    prefix = old['records'] if old is not None else []
    _verify_progress(system, prefix)
    if old is not None:
        sync_dir(receipt.parent)
        if old['complete']:
            return old

    pending_entry = old.get('pending') if old is not None else None
    operations = spec['operations'][len(prefix):]
    store = system.record_store

    # Phase 1: Full pre-validation of EVERY remaining operation before ANY mutation or pin.
    prepared = []
    for index, op in enumerate(operations):
        path = store.path(op['id'])
        if not path.exists() and store.cold_archive.has(op['id']):
            raise ValueError(f"record {op['id']} is in cold archive; restore explicitly before modifying")
        previous, previous_raw = store.read(op['id']) if path.exists() else (None, None)
        already_committed = None
        if index == 0 and pending_entry is not None:
            already_committed = _reconcile_pending(store, op, pending_entry, previous, pin_predecessors)
        if already_committed is not None:
            prepared.append((op, None, None, None, None, None, None, already_committed))
            continue
        candidate, preserve_basis = _build_candidate(op, previous)
        preview_body, preview_raw = store._prepare_write(candidate, previous, previous_raw, retain_basis=False,
                                                         preserve_basis=preserve_basis)
        # When preserve_basis is set or no new basis files require snapshotting, preview_body/raw are
        # already final and can be committed directly under the same exclusive owner.lock.
        can_reuse_prepared = preserve_basis is not None or not candidate.get('basis')
        prepared.append((
            op,
            candidate,
            previous,
            previous_raw,
            preserve_basis,
            _fields_digest(preview_body),
            (preview_body, preview_raw) if can_reuse_prepared else None,
            None,
        ))

    # Phase 2: Commit durable incomplete receipt, then apply operations sequentially with WAL pending entry.
    result = {
        'format': RECEIPT_FORMAT,
        'id': ident,
        'instance_id': system.instance_id,
        'spec_sha256': spec_sha,
        'pin_predecessors': pin_predecessors,
        'records': list(prefix),
    }
    receipt.parent.mkdir(parents=True, exist_ok=True)
    sync_dir(receipt.parent.parent)
    if old is None:
        write_json(receipt, dict(result, complete=False))

    with store.batch_index_updates():
        if old is not None:
            # Ensure earlier committed prefix entries are also fresh in HeadIndex even if resume fails again.
            for row in result['records']:
                store._deferred_index_ids.append(row['id'])
        try:
            for op, candidate, previous, previous_raw, preserve_basis, fields_sha, ready_write, already_committed in prepared:
                if already_committed is not None:
                    result['records'].append(already_committed)
                    store._deferred_index_ids.append(already_committed['id'])
                    write_json(receipt, dict(result, complete=False))
                    continue
                pinned_object = None
                if pin_predecessors and previous is not None:
                    if previous_raw is not None:
                        if len(previous_raw) <= MAX_BYTES:
                            pinned_object = store.objects.put(previous_raw)
                        else:
                            pinned_object = store.objects.put_retained(previous_raw, max_bytes=len(previous_raw))
                    else:
                        pinned_object = store.pin(previous)['object']
                before_rev = previous['revision'] if previous is not None else 0
                before_sha = previous['sha256'] if previous is not None else None
                pending = {
                    'id': op['id'],
                    'before_revision': before_rev,
                    'before_sha256': before_sha,
                    'pinned_object': pinned_object,
                    'expected_after_revision': before_rev + 1,
                    'expected_fields_sha256': fields_sha,
                }
                write_json(receipt, dict(result, complete=False, pending=pending))
                written = store._replace(
                    candidate,
                    op['expected_revision'],
                    preserve_basis=preserve_basis,
                    _preloaded=(previous, previous_raw),
                    _prepared=ready_write,
                )
                result['records'].append({
                    'id': op['id'],
                    'before_revision': before_rev,
                    'before_sha256': before_sha,
                    'pinned_object': pinned_object,
                    'after_revision': written['revision'],
                    'after_sha256': written['sha256'],
                })
        except BaseException:
            # Persist completed prefix before propagating any failure.
            if result['records']:
                try:
                    write_json(receipt, dict(result, complete=False))
                except OSError:
                    pass
            raise

    result['complete'] = True
    write_json(receipt, result)
    return result
