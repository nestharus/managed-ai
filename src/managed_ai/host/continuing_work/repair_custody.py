"""Retain a failure, qualify its generation inputs and accept its received repair."""
from pathlib import Path

from managed_ai.host.continuing_work.occurrence_files import ident, nonempty, now, read, write


class RepairCustody:
    """Composed collaborator owning failure custody, repair-generation checks, and acceptance."""

    def __init__(self, host):
        self._host = host

    def generation_repairs(self, repair_ids, sources):
        producer = [r for r in sources if r['role'] == 'producer']
        repairs = []
        for rid in repair_ids:
            repair = read(self._host.root / 'repairs' / f'{ident(rid)}.json')
            old = {r['id']: r for r in repair['producer_before']}
            changed_means = [r for r in producer if r['id'] in old and r['sha256'] != old[r['id']]['sha256']]
            if not changed_means:
                raise ValueError('Repair generation requires a selected changed producing means, not only a changed output')
            original_inputs = {r['sha256'] for r in repair['inputs']}
            selected_inputs = {r['sha256'] for r in sources}
            if not original_inputs.issubset(selected_inputs):
                raise ValueError('Original-case repair generation must select the retained original inputs; run transfers as separate scopes')
            repairs.append({'id': rid, 'changed_producers': changed_means,
                            'original_inputs': [r['sha256'] for r in repair['inputs']]})
        return repairs

    def repair_open(self, spec):
        rid = ident(spec['id'])
        path = self._host.root / 'repairs' / f'{rid}.json'
        if path.exists():
            raise ValueError('Preserve the original failure occurrence')
        row = {'id': rid, 'opened_at_utc': now(), 'failure_account': nonempty(spec['failure_account'], 'failure account'),
               'causal_hypothesis': nonempty(spec['causal_hypothesis'], 'causal hypothesis'),
               'inputs': [self._host.pin(p) for p in spec['inputs']],
               'failures': [self._host.pin(p) for p in spec['failures']],
               'producer_before': [dict(self._host.pin(x['path']), id=ident(x['id'])) for x in spec['producer_before']]}
        if not row['inputs'] or not row['failures'] or not row['producer_before']:
            raise ValueError('A repair needs retained inputs, observed failure and responsible producing means')
        write(path, row)
        self._host.event('repair-opened', {'id': rid, 'retained': self._host.pin(path)})
        return row

    def repair_accept(self, spec):
        rid = ident(spec['id'])
        repair_path = self._host.root / 'repairs' / f'{rid}.json'
        repair = read(repair_path)
        if 'acceptance' in repair:
            raise ValueError('Repair acceptance already recorded; new evidence requires a new occurrence')
        gen = read(self._host.root / 'scopes' / f'{ident(spec["generation"])}.json')
        rec = read(self._host.root / 'scopes' / f'{ident(spec["receiving"])}.json')
        if gen['id'] == rec['id'] or 'result' not in gen or 'result' not in rec:
            raise ValueError('Require distinct actually returned generation and receiving scopes')
        self._host.current(self._host.config['commission'], 'Current commission')
        if self._host.goal()['sha256'] != rec['parent_goal']['sha256']:
            raise ValueError('Parent duty changed after receiving; requalify this repair under current work')
        self._host.received_outputs(gen)
        receiving_outputs = self._host.received_outputs(rec)
        if gen['result']['producer'] == rec['result']['producer']:
            raise ValueError('This acceptance route requires a separately attributed actual receiver')
        if rid not in [r['id'] for r in gen['repairs']]:
            raise ValueError('Generation did not select this changed producer')
        if rec['prepared_at_utc'] <= gen['result']['at_utc']:
            raise ValueError('Receiver must encounter the actual returned product')
        generated = {p['sha256'] for p in gen['result']['outputs']}
        seen = {p['sha256'] for p in rec['sources']}
        if not generated.issubset(seen):
            raise ValueError('Receiver did not select all actual generated outputs')
        failed = {p['sha256'] for p in repair['failures']}
        if not failed.issubset(seen):
            raise ValueError('Receiver must encounter retained failure outputs')
        causal_sources = {p['sha256'] for p in [*repair['inputs'], *repair['producer_before']]}
        changed = next(r for r in gen['repairs'] if r['id'] == rid)['changed_producers']
        causal_sources.update(p['sha256'] for p in changed)
        if not causal_sources.issubset(seen):
            raise ValueError('Receiver must encounter original inputs and actual old/new producing means')
        assessment_target = 'outputs/' + rec['id'] + '/' + ident(spec['assessment'])
        if assessment_target not in rec['targets']:
            raise ValueError('Assessment must be an actual returned receiving target')
        received_pin = next((p for p in receiving_outputs if p['path'] == str((self._host.work / assessment_target).resolve())), None)
        if received_pin is None:
            raise ValueError('Assessment is not one of the received output occurrences')
        assessment = read(self._host.root / received_pin['object'])
        if assessment.get('finding') != 'fixed-within-stated-scope':
            raise ValueError('Receiver has not accepted this repair')
        for key in ('causal_account', 'comparison', 'transfer_or_rival'):
            nonempty(assessment.get(key), key)
        limits = assessment.get('limits')
        if isinstance(limits, list):
            if not limits:
                raise ValueError('limits must contain at least one substantive limit')
            for item in limits:
                nonempty(item, 'limit')
        else:
            nonempty(limits, 'limits')
        named = assessment.get('generated_sha256')
        if isinstance(named, dict):
            expected = {Path(p['path']).name: p['sha256'] for p in gen['result']['outputs']}
            if named != expected:
                raise ValueError('Assessment names different output names or bytes')
        elif not isinstance(named, list) or len(named) != len(generated) or any(not isinstance(x, str) for x in named) or set(named) != generated:
            raise ValueError('Assessment names different output bytes')
        acceptance = {'at_utc': now(), 'generation': gen['id'], 'receiving': rec['id'],
                      'assessment': received_pin, 'account': assessment,
                      'scope': 'Mechanical lineage plus attributed semantic receiving; not proof of universal efficacy.'}
        repair['acceptance'] = acceptance
        write(repair_path, repair)
        self._host.event('repair-accepted', {'id': rid, 'generation': gen['id'], 'receiving': rec['id']})
        return acceptance
