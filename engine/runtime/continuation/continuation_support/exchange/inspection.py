"""Inspect packet, transport and publication state through read-only observations."""
from __future__ import annotations

from typing import Any

from ..storage import read_json
from .carrier_receipts import transport_outcome
from .publication import observe_basis, observe_target
from .publication_receipts import (
    _publication_name,
    _validate_publication_record,
    bound_publication_proposal,
    publication_inventory,
    publication_matches,
    validate_publication_reconciliation,
)


def inspect_publications(exchange, directory, request, result, observe):
    try:
        publications, reconciliations, unreadable = publication_inventory(directory)
    except OSError as exc:
        result['publication_inventory_error'] = str(exc)
        return
    targets = {row['path']: row for row in request['targets']}
    received = result['return']
    attached = set()
    for name, (record, digest) in publications.items():
        observation = {'record_file': name, 'record': record}
        try:
            target = record.get('path')
            if not isinstance(target, str) or target not in targets:
                raise ValueError('publication path is not a declared target')
            try:
                current = observe(target).copy()
            except (OSError, ValueError) as exc:
                observation['observation_error'] = str(exc)
            else:
                current['matches_base'] = current['sha256'] == targets[target]['sha256']
                observation['current_target'] = current
                proposal = bound_publication_proposal(result['packet'], request, received, target)
                if proposal is not None:
                    current['matches_proposal'] = current['sha256'] == proposal['sha256']
            if not publication_matches(result['packet'], request, record, received):
                raise ValueError('publication receipt binding changed')
            _validate_publication_record(record)
            canonical = _publication_name(target, 'reconcile')
            matching = [slot for slot, (account, _) in reconciliations.items()
                        if account.get('intent_sha256') == digest]
            slot = canonical if canonical in reconciliations else matching[0] if len(matching) == 1 else None
            if slot is not None:
                account = reconciliations[slot][0]
                observation['reconciliation'] = validate_publication_reconciliation(
                    request, received, record, digest, account)
                attached.add(slot)
                if slot != canonical:
                    observation['reconciliation_file'] = slot
        except (OSError, ValueError, KeyError, TypeError) as exc:
            observation.setdefault('observation_error', str(exc))
        result['publications'].append(observation)
    unattached = [{'record_file': name, 'record': record}
                  for name, (record, _) in reconciliations.items() if name not in attached]
    for name, error in unreadable.items():
        row = {'record_file': name, 'observation_error': str(error)}
        (result['publications'] if name.lower().startswith('publish-') else unattached).append(row)
    if unattached:
        result['unattached_reconciliations'] = unattached


class PacketInspection:
    def list(self, consumer: str | None = None) -> list[dict[str, Any]]:
        """Expose retained occasions without inventing a semantic completion flag."""
        rows = []
        for directory in sorted((self.root / 'packets').iterdir()):
            if directory.name.startswith('.') or not directory.is_dir():
                continue
            try:
                _, request = self.load(directory.name)
                if consumer is not None and request['consumer'] != consumer:
                    continue
                has_return = (directory / 'return.json').exists()
                if has_return:
                    self.validate_return(directory, read_json(directory / 'return.json'))
                outcome = transport_outcome(self, directory, request)
                publications = [read_json(p) for p in directory.glob('publish-*.json')]
                transport = 'prepared'
                if self.transport.has_attempt(directory):
                    transport = 'dispatch-attempted'
                if has_return:
                    transport = 'returned'
                if outcome is not None:
                    transport = 'terminal-without-return'
                row = {'packet': directory.name, 'consumer': request['consumer'], 'transport': transport,
                       'publications': [{'path': p['path'], 'phase': p['phase']} for p in publications],
                       'semantic_fulfillment': 'not-decided-by-exchange'}
                if outcome is not None:
                    row['transport_outcome'] = {k: outcome[k] for k in ('reason', 'account', 'outcome_sha256')}
                rows.append(row)
            except (ValueError, KeyError, OSError, TypeError) as exc:
                rows.append({'packet': directory.name, 'transport': 'record-unresolved', 'error': str(exc)})
        return rows

    def inspect(self, packet: str) -> dict[str, Any]:
        directory, request = self.load(packet)
        # Current effect eligibility must not gate reading an intact old return.
        # load() above still validates the immutable request, context and objects.
        result = {'packet': packet, 'consumer': request['consumer'], 'basis': None, 'return': None}
        observations = {}

        def observe(path):
            # The basis and receipt rows share the same lookup, including a failure.
            if path not in observations:
                try:
                    observations[path] = observe_target(self, path)
                except (OSError, ValueError) as exc:
                    observations[path] = exc
            observation = observations[path]
            if isinstance(observation, Exception):
                raise observation
            return observation

        try:
            result['basis'] = observe_basis(self, packet, directory, request, observe=observe)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result['basis_error'] = str(exc)
        result['publications'] = []
        if (directory / 'return.json').exists():
            record = read_json(directory / 'return.json')
            self.validate_return(directory, record)
            result['return'] = record
            data = self.get(record['reply_sha256'])
            try:
                result['reply_text'] = data.decode('utf-8')
            except UnicodeDecodeError:
                result['reply_text'] = None
        inspect_publications(self, directory, request, result, observe)
        result['inspection_scope'] = (
            'Read-only observations in one cooperative-owner invocation; not an atomic external-writer '
            'snapshot, historical completion proof, reconciliation or permission to replay.')
        outcome = transport_outcome(self, directory, request)
        if outcome is not None:
            result['transport_outcome'] = outcome
        return result
