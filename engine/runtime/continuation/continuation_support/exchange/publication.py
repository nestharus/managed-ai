"""Check the declared basis, publish a nominated target and reconcile an interrupted intent."""
from __future__ import annotations

import json
from pathlib import Path
from stat import FILE_ATTRIBUTE_REPARSE_POINT, S_ISDIR
from typing import Any

from ..ownership import NativeLayout, external_root, relative_path as _relative
from ..storage import (
    atomic_bytes as _atomic_bytes,
    encode,
    now,
    read_bytes as _bytes,
    read_json,
    sha,
    write_json,
)
from .publication_receipts import (
    _publication_name,
    _validate_publication_record,
    publication_accounts,
    publication_inventory,
    publication_matches,
)


MAX_BASIS_ROWS = 8
MAX_BASIS_PATH_CHARS = 32767
MAX_BASIS_PATH_BUDGET = 8192
PUBLICATION_ALREADY_ATTEMPTED = 'publication already attempted; inspect its receipt, do not replay'


class PublicationBasisChanged(ValueError):
    def __init__(self):
        super().__init__('declared basis changed; preserve this return and construct its current use')
        self.basis = None
        self.native_basis = None


def _publication_basis(packet, target, request, record, proposal, changed):
    declared = next((row for row in request['targets'] if row['path'] == target), None)
    if (declared is None or 'base_sha256' not in proposal or declared['sha256'] != proposal['base_sha256']
            or packet != request['packet'] or packet != record['packet']
            or len(target) > MAX_BASIS_PATH_CHARS):
        return None
    for row in changed:
        for key in ('expected_sha256', 'current_sha256'):
            value = row[key]
            if value is not None and (not isinstance(value, str) or len(value) != 64
                                      or any(c not in '0123456789abcdef' for c in value)):
                return None
    rows, path_budget = [], MAX_BASIS_PATH_BUDGET
    # Give the requested target priority. The first eligible row may exceed the
    # packing budget, which limits the escaped path text of subsequent rows.
    for row in sorted(changed, key=lambda row: row['path'] != target):
        if len(rows) == MAX_BASIS_ROWS:
            break
        if len(row['path']) > MAX_BASIS_PATH_CHARS:
            continue
        size = len(json.dumps(row['path']))
        if rows and size > path_budget:
            continue
        rows.append({key: row[key] for key in ('path', 'expected_sha256', 'current_sha256')})
        path_budget -= size
    return {'packet': packet, 'target': target, 'artifact': packet + ':' + target,
            'request_sha256': record['request_sha256'], 'return_sha256': record['return_sha256'],
            'proposal_sha256': proposal['sha256'], 'base_sha256': declared['sha256'],
            'changed': rows, 'omitted': len(changed) - len(rows),
            'inspect': {'entry': 'runtime/exchange_packets.py', 'root': 'store exchange',
                        'arguments': ['inspect', packet]}}


def _absent_under(path: Path, workspace: Path) -> bool:
    # Windows can report a missing path when an ancestor is a file. An absent
    # chain must end at an observed directory inside the workspace.
    for ancestor in (path, *path.parents):
        if not ancestor.is_relative_to(workspace):
            break
        try:
            entry = ancestor.lstat()
        except FileNotFoundError:
            continue
        return (ancestor != path and S_ISDIR(entry.st_mode)
                and not getattr(entry, 'st_file_attributes', 0) & FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def publication_state(exchange, packet, directory, request, received, target):
    publications, reconciliations, unreadable = publication_inventory(directory)
    for error in unreadable.values():
        if isinstance(error, OSError):
            raise error
    if publications and received is None:
        received = read_json(directory / 'return.json')
        exchange.validate_return(directory, received)
    bound = {name: (record, digest) for name, (record, digest) in publications.items()
             if publication_matches(packet, request, record, received)}
    if target is not None:
        occupied = []
        for prefix, label, records in (('publish', 'receipt', publications),
                                       ('reconcile', 'reconciliation', reconciliations)):
            name = _publication_name(target, prefix)
            if name in records:
                recorded_target = records[name][0].get('path')
                if isinstance(recorded_target, str) and recorded_target != target:
                    occupied.append(f'publication {label} slot is occupied by a record naming another target')
            elif name not in unreadable:
                # The receiver can resolve the canonical name through another
                # spelling even when the exact name was absent from the listing.
                try:
                    (directory / name).lstat()
                except FileNotFoundError:
                    pass
                else:
                    occupied.append(f'publication {label} slot is occupied by another entry')
        if any(record['path'] == target for record, _ in bound.values()):
            raise ValueError(PUBLICATION_ALREADY_ATTEMPTED)
        if occupied:
            raise ValueError(occupied[0])
    if unreadable:
        raise next(iter(unreadable.values()))
    if len(bound) != len(publications):
        raise ValueError('publication receipt binding changed')
    publication_targets = [record['path'] for record, _ in bound.values()]
    if len(set(publication_targets)) != len(publication_targets):
        raise ValueError('conflicting publication records for the same target')
    if any(name != _publication_name(record['path']) for name, (record, _) in bound.items()):
        raise ValueError('publication receipt is outside its canonical slot')
    intents = {receipt['path']: (receipt, digest) for receipt, digest in bound.values()}
    accounts = publication_accounts(exchange, request, received, intents, reconciliations)
    for receipt, _ in intents.values():
        _validate_publication_record(receipt)

    applied = {}
    pending = False
    for path, (receipt, _) in intents.items():
        if path in accounts:
            applied[path] = accounts[path]['observed_sha256']
        elif receipt['phase'] == 'complete':
            applied[path] = receipt['sha256']
        else:
            pending = True
    if pending:
        raise ValueError('interrupted publication requires manual reconciliation')
    return applied


def observe_target(exchange, name):
    path = exchange.path(name)
    try:
        path.stat()
    except FileNotFoundError:
        if not _absent_under(path, exchange.workspace):
            raise
        return {'path': name, 'sha256': None, 'bytes': 0, 'exists': False}
    data = _bytes(path)
    return {'path': name, 'sha256': sha(data), 'bytes': len(data), 'exists': True}


def observe_basis(exchange, packet, directory, request, received=None, *, publishing_target=None,
           observe=None) -> list[dict[str, Any]]:
    applied = publication_state(exchange, packet, directory, request, received, publishing_target)
    rows = [request['brief'], *request['methods'], *request['materials'], *request['targets']]
    if observe is None:
        def observe(name):
            return observe_target(exchange, name)
    checks = []
    for row in rows:
        current = observe(row['path'])['sha256']
        expected = applied.get(row['path'], row['sha256'])
        checks.append({'path': row['path'], 'expected_sha256': expected, 'current_sha256': current,
                       'unchanged': current == expected})
    return checks


class TargetPublication:
    def basis(self, packet: str) -> list[dict[str, Any]]:
        return observe_basis(self, packet, *self.load(packet))

    def reconcile_publication(self, packet: str, target: str, expected_sha256: str | None,
                              account: str) -> dict[str, Any]:
        """Acknowledge current bytes while preserving the original effect uncertainty."""
        target = _relative(target)
        output = external_root(self.path(target), 'publication destination')
        self._authority.authorize_publication(NativeLayout.owner_of(output), packet, target)
        directory, request = self.load(packet)
        if not isinstance(account, str) or not account.strip():
            raise ValueError('an explicit publication reconciliation account is required')
        if expected_sha256 == 'absent':
            expected_sha256 = None
        if expected_sha256 is not None and (not isinstance(expected_sha256, str)
                or len(expected_sha256) != 64 or any(c not in '0123456789abcdef' for c in expected_sha256)):
            raise ValueError('expected target identity must be a SHA-256 digest or absent')
        intent_name = _publication_name(target)
        account_name = _publication_name(target, 'reconcile')
        publications, reconciliations, unreadable = publication_inventory(directory)
        if intent_name in unreadable:
            raise unreadable[intent_name]
        if intent_name not in publications:
            if any(record.get('path') == target for record, _ in publications.values()):
                raise ValueError('publication receipt is outside its canonical slot')
            (directory / intent_name).lstat()
            raise ValueError('publication receipt slot is occupied by another entry')
        intent, intent_digest = publications[intent_name]
        received = read_json(directory / 'return.json')
        self.validate_return(directory, received)
        for error in unreadable.values():
            if isinstance(error, OSError):
                raise error
        if (not publication_matches(packet, request, intent, received)
                or intent['phase'] != 'intent' or intent['path'] != target):
            raise ValueError('reconciliation requires the bound original publication intent')
        if unreadable:
            raise next(iter(unreadable.values()))
        if any(name != intent_name and record.get('path') == target
               for name, (record, _) in publications.items()):
            raise ValueError('conflicting publication records for the same target')
        _validate_publication_record(intent)
        matching = {name: entry for name, entry in reconciliations.items()
                    if name == account_name or entry[0].get('path') == target
                    or entry[0].get('intent_sha256') == intent_digest}
        if not matching:
            # Absence of the exact spelling does not establish a vacant slot.
            try:
                (directory / account_name).lstat()
            except FileNotFoundError:
                pass
            else:
                raise ValueError('publication reconciliation slot is occupied by another entry')
        accounts = publication_accounts(self, request, received, {target: (intent, intent_digest)}, matching)
        prior = accounts.get(target)
        if prior is not None:
            if prior['account'] != account or prior['observed_sha256'] != expected_sha256:
                raise ValueError('publication reconciliation already has a different account or observation')
            return prior
        observed = observe_target(self, target)['sha256']
        if observed != expected_sha256:
            raise ValueError('current target differs from the expected reconciliation observation')
        if observed not in (intent['base_sha256'], intent['sha256']):
            raise ValueError('target differs from the base and proposal; prepare a fresh receiving occasion')
        record = {'version': 1, 'packet': packet, 'path': target, 'account': account,
                  'request_sha256': sha(encode(request)), 'return_sha256': received['return_sha256'],
                  'intent_sha256': intent_digest, 'observed_sha256': observed,
                  'recorded_at': now(), 'historical_completion': 'unestablished'}
        record['reconciliation_sha256'] = sha(encode(record))
        write_json(directory / account_name, record)
        return record

    def publish(self, packet: str, target: str, reason: str) -> dict[str, Any]:
        target = _relative(target)
        output = external_root(self.path(target), 'publication destination')
        self._authority.authorize_publication(NativeLayout.owner_of(output), packet, target)
        directory, request = self.load(packet)
        if not reason.strip():
            raise ValueError('explicit publication reason is required')
        record = read_json(directory / 'return.json')
        self.validate_return(directory, record)
        proposals = [proposal for proposal in record['proposals'] if proposal['path'] == target]
        if not proposals:
            raise ValueError('no received proposal for that target')
        declared = [row for row in request['targets'] if row['path'] == target]
        if (len(declared) != 1 or len(proposals) != 1 or record['packet'] != packet
                or request['packet'] != packet):
            raise ValueError('publication proposal binding changed')
        proposal = proposals[0]
        if 'base_sha256' in proposal and proposal['base_sha256'] != declared[0]['sha256']:
            raise ValueError('publication proposal binding changed')
        path = directory / _publication_name(target)
        basis = observe_basis(self, packet, directory, request, record, publishing_target=target)
        changed = [row for row in basis if not row['unchanged']]
        if changed:
            failure = PublicationBasisChanged()
            try:
                failure.basis = _publication_basis(packet, target, request, record, proposal, changed)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                pass  # Optional evidence must not displace the original refusal.
            raise failure
        if not output.parent.is_dir():
            raise ValueError('publication parent must already exist')
        body = self.get(proposal['sha256'])
        effect = {'packet': packet, 'path': target, 'phase': 'intent', 'reason': reason,
                  'sha256': proposal['sha256'], 'base_sha256': proposal['base_sha256'],
                  'return_sha256': record['return_sha256'], 'started_at': now()}
        write_json(path, effect)
        _atomic_bytes(output, body)
        effect.update(phase='complete', completed_at=now())
        write_json(path, effect)
        return effect
