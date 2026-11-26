"""Scope preparation, overlap indexing, guarded receive, crash resume, supersession, and status."""
from __future__ import annotations

from pathlib import Path
import stat

from occurrence_files import (
    atomic_bytes,
    check_unfollowed_ancestors,
    digest,
    ident,
    nonempty,
    now,
    read,
)

HERE = Path(__file__).resolve().parent


def prepare_questions(supplied_questions, selected_sources, task):
    # Resolve the question input contract before any host/native effects.
    # Omission deliberately leaves a question unbound; an unknown spelling
    # or unresolved identity must never be converted into that omission.
    if not isinstance(selected_sources, list) or any(not isinstance(r, dict) for r in selected_sources):
        raise ValueError('sources must be a list of selected resource objects')
    selected_ids = [ident(r['id']) for r in selected_sources]
    if len(set(selected_ids)) != len(selected_ids):
        raise ValueError('Duplicate selected resource')
    if not isinstance(supplied_questions, list):
        raise ValueError('questions must be a list')
    questions = []
    for i, q in enumerate(supplied_questions):
        label = 'questions[' + str(i) + ']'
        if not isinstance(q, dict):
            raise ValueError(label + ' must be an object')
        unknown = set(q) - {'question', 'use', 'sources'}
        if unknown:
            raise ValueError(label + ' has unsupported fields: ' + ', '.join(sorted(unknown)) +
                             '; use sources for selected resource IDs')
        refs = q.get('sources', [])
        if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
            raise ValueError(label + '.sources must be a list of selected resource IDs')
        if len(set(refs)) != len(refs):
            raise ValueError(label + '.sources contains duplicates')
        missing = set(refs) - set(selected_ids)
        if missing:
            raise ValueError(label + '.sources names unselected resource IDs: ' + ', '.join(sorted(missing)))
        questions.append({
            'question': nonempty(q.get('question'), label + '.question'),
            'use': nonempty(q.get('use', task), label + '.use'),
            'sources': refs,
        })
    return questions


def prepare_attention_selection(spec):
    # Native Attention owns the meaning, current treatment and active-ID
    # checks. Preserve the caller's explicit earlier selection instead of
    # silently replacing it with only this occurrence's new questions.
    attention = {}
    for key, limit in (('attention_items', 2000), ('attention_groups', 100)):
        value = spec.get(key, [])
        if not isinstance(value, list) or len(value) > limit or any(not isinstance(v, str) for v in value):
            raise ValueError(key + ' must be a bounded list of strings')
        if len(value) != len(set(value)):
            raise ValueError(key + ' contains duplicates')
        attention[key] = value
    return attention


def overlap_rows(previous_scopes, sources, reads, writes):
    """Nominate declared footprints and observed source/output identity, without semantic ranking."""
    rows = []
    a = set(reads)
    b = set(writes)
    current_by_hash = {}
    for source in sources:
        current_by_hash.setdefault(source['sha256'], []).append(source['id'])
    for previous in previous_scopes:
        c = set(previous['reads'])
        d = set(previous['writes'])
        conflict = sorted((b & (c | d)) | (d & a))
        shared = sorted(a & c)
        prior_by_hash = {}
        for source in previous['sources']:
            prior_by_hash.setdefault(source['sha256'], []).append(source['id'])
        same = [{'sha256': key, 'current_source_ids': current_by_hash[key], 'prior_source_ids': prior_by_hash[key]}
                for key in sorted(current_by_hash.keys() & prior_by_hash.keys())]
        output_uses = [{'sha256': out['sha256'], 'current_source_ids': current_by_hash[out['sha256']],
                        'received_output': out['path']}
                       for out in previous.get('result', {}).get('outputs', []) if out['sha256'] in current_by_hash]
        if not (conflict or shared or same or output_uses):
            continue
        scope_state = (
            'partially-received' if previous.get('result', {}).get('partial')
            else 'returned' if 'result' in previous
            else 'superseded-without-output' if 'superseded' in previous
            else 'prepared-or-unreconciled'
        )
        rows.append({
            'scope': previous['id'],
            'prepared_at_utc': previous['prepared_at_utc'],
            'read_write': conflict,
            'shared_reads': shared,
            'shared_source_bodies': same,
            'received_output_uses': output_uses,
            'scope_state': scope_state,
            'successor': previous.get('superseded', {}).get('successor'),
            'return_sha256': previous.get('result', {}).get('return', {}).get('return_sha256'),
        })
    # These categories expose concrete relationships first; they are not inferred importance.
    rows.sort(
        key=lambda r: (
            bool(r['received_output_uses']),
            bool(r['read_write']),
            r['scope_state'] == 'prepared-or-unreconciled',
            r['prepared_at_utc'],
            r['scope'],
        ),
        reverse=True,
    )
    return rows


class ScopeLifecycle:
    """Composed collaborator owning scope preparation, receiving, resume, supersession, and status."""

    def __init__(self, host):
        self._host = host

    def write_overlap_index(self, sid, sources, resource_changes, reads, writes, overlap_limit):
        changed = list(resource_changes)
        previous_scopes = self._host.scopes()
        for source in sources:
            if any(c['resource'] == source['id'] for c in changed):
                continue
            prior = [
                r for old_scope in sorted(previous_scopes, key=lambda x: x['prepared_at_utc'], reverse=True)
                for r in old_scope['sources']
                if r['path'] == source['path'] and r['sha256'] != source['sha256']
            ]
            if prior:
                changed.append({
                    'resource': source['id'],
                    'before': prior[0],
                    'current': source,
                    'nomination_basis': 'same selected current source path under possibly different resource IDs',
                })
        for change in changed:
            change['affected_prior_consumers'] = [
                s['id'] for s in previous_scopes
                if any((r['id'] == change['resource'] or r['path'] == change['current']['path'])
                       and r['sha256'] != change['current']['sha256']
                       for r in s['sources'])
            ]
        all_overlaps = overlap_rows(previous_scopes, sources, reads, writes)
        affected = {name for change in changed for name in change['affected_prior_consumers']}
        # Keep current selected output use ahead of affected-history nominees.
        # Stable sorting preserves the existing order within each category.
        all_overlaps.sort(key=lambda r: (bool(r['received_output_uses']), r['scope'] in affected), reverse=True)
        overlaps = all_overlaps[:overlap_limit]
        index = self._host.root / 'overlap-index' / f'{sid}.json'
        self._host._write(index, {
            'scope': sid,
            'previous_scope_count': len(previous_scopes),
            'rows': all_overlaps,
            'meaning': 'Author-declared footprints plus exact same-source/received-output identity. Shared reading need not conflict. Disjoint identities do not establish semantic independence.',
        })
        overlap_pin = self._host.pin(index)
        coverage = {
            'previous_scopes': len(previous_scopes),
            'matching_scopes': len(all_overlaps),
            'delivered': len(overlaps),
            'omitted': len(all_overlaps) - len(overlaps),
            'limit': overlap_limit,
            'omitted_scope_sample': [r['scope'] for r in all_overlaps[overlap_limit:overlap_limit + 20]],
            'complete_index': overlap_pin,
            'meaning': 'Nominations, not inferred conflicts, automatic scheduling or semantic completeness. Acquire the complete index when omitted or new relationships matter.',
        }
        return changed, overlaps, coverage

    def prepare(self, spec):
        sid = ident(spec['id'])
        saved = self._host.root / 'scopes' / f'{sid}.json'
        if saved.exists():
            raise ValueError('A scope is one immutable occurrence; select a fresh identity')
        task = nonempty(spec['task'], 'task')
        prediction = nonempty(spec['prediction'], 'prediction')
        selected_sources = spec.get('sources', [])
        questions = prepare_questions(spec.get('questions', []), selected_sources, task)
        for ordinal, question in enumerate(questions, 1):
            question['id'] = ident(f'{sid}-q{ordinal}')
        resource_paths = [(item, Path(item['path'])) for item in selected_sources]
        attention = prepare_attention_selection(spec)
        overlap_limit = spec.get('overlap_limit', 8)
        if type(overlap_limit) is not int or not 1 <= overlap_limit <= 200:
            raise ValueError('overlap_limit must be an integer from 1 through 200')
        outnames = [ident(n) for n in spec['outputs']]
        if not outnames or len(set(outnames)) != len(outnames):
            raise ValueError('Select distinct new output names')
        targets = ['outputs/' + sid + '/' + n for n in outnames]
        reads = list(dict.fromkeys([*spec.get('reads', []), *(r['id'] for r in selected_sources)]))
        writes = list(dict.fromkeys(spec.get('writes', [])))

        duty_ids = spec['duty_ids']
        requested_duties = set(duty_ids)
        if not requested_duties:
            raise ValueError('Bind actual current parent duties')
        repair_ids = [ident(value) for value in spec.get('repairs', [])]

        self._host.current(self._host.config['commission'], 'Current commission')
        goal = self._host.goal()
        known = {r['id']: r for r in goal['content']['obligations']}
        if requested_duties - known.keys():
            raise ValueError('Bind actual current parent duties')
        sources = []
        resource_changes = []
        for item, path in resource_paths:
            row, old = self._host.refresh_resource(item['id'], path)
            row = dict(row, role=item.get('role', 'source'))
            sources.append(row)
            if old:
                resource_changes.append({'resource': row['id'], 'before': old, 'current': row})
        changed, overlaps, coverage = self.write_overlap_index(
            sid, sources, resource_changes, reads, writes, overlap_limit,
        )
        if any((self._host.work / p).exists() for p in targets):
            raise ValueError('Fresh generation targets must be absent')
        # Native publication deliberately requires an existing parent at capture.
        for target in targets:
            (self._host.work / target).parent.mkdir(parents=True, exist_ok=True)
        repairs = self._host.repairs.generation_repairs(repair_ids, sources)
        source_note = {
            'scope': sid,
            'prediction': prediction,
            'duty_ids': duty_ids,
            'changed_sources': changed,
            'overlap_nominations': overlaps,
            'overlap_coverage': coverage,
            'authored_relations': spec.get('relations', []),
            'selected_treatments': spec.get('treatments', []),
            'unknowns': spec.get('unknowns', ['Unnominated semantic dependencies may remain.']),
            'repairs': repairs,
            'receiving_question': spec.get(
                'receiving_question',
                'Did the actual contribution produce the predicted useful effect under current sources?',
            ),
        }
        note = 'sources/host/' + sid + '-prospect.json'
        self._host._write(self._host.work / note, source_note)
        items = []
        for q in questions:
            items.append({
                'id': q['id'],
                'question': q['question'],
                'use': q['use'],
                'basis': [r['work_path'] for r in sources if r['id'] in q['sources']],
                'groups': [sid],
            })
        if changed:
            items.append({
                'id': ident(sid + '-changed-basis'),
                'question': 'How do these changed sources alter current dependent work or previously adequate partials?',
                'use': task,
                'basis': [note, *[r['current']['work_path'] for r in changed]],
                'groups': [sid],
            })
        if items:
            self._host.call('track', '--spec', self._host.temp(sid + '-attention.json', {'items': items}))
        request = {
            'task': task,
            'consumer': spec.get('consumer', sid),
            'sources': [],
            'targets': targets,
            'attention_items': list(dict.fromkeys([
                'continuing-commission',
                *[r['id'] for r in items],
                *attention['attention_items'],
            ])),
            'attention_groups': attention['attention_groups'],
            'attention_limit': spec.get('attention_limit', max(8, len(items) + 1)),
        }
        if 'attention_max_bytes' in spec:
            request['attention_max_bytes'] = spec['attention_max_bytes']
        method_raw = (HERE / 'OPERATION.md').read_bytes()
        method_sha = digest(method_raw)
        method_path = 'sources/host/method-' + method_sha + '.md'
        method_file = self._host.work / method_path
        if method_file.exists() and method_file.read_bytes() != method_raw:
            raise ValueError('Retained host method identity differs')
        if not method_file.exists():
            atomic_bytes(method_file, method_raw)
        request['sources'] = ['sources/host/COMMISSION.md', method_path, note, *[r['work_path'] for r in sources]]
        for key in ('techniques', 'discovery', 'compositions'):
            if key in spec:
                request[key] = spec[key]
        result = self._host.call('context', sid, '--request', self._host.temp(sid + '-request.json', request))
        recover = self._host.call('recover', sid)
        context_file = self._host.root / 'contexts' / f'{sid}.md'
        atomic_bytes(context_file, recover['context'].encode('utf-8'))
        scope = {
            'format': 'g60-continuing-work-scope/1',
            'id': sid,
            'prepared_at_utc': now(),
            'host_producer': self._host.pin((HERE / 'continuing_work.py').resolve()),
            'host_dependencies': self._host.producer_dependencies(),
            'host_method': self._host.pin(HERE / 'OPERATION.md'),
            'spec': spec,
            'parent_goal': {k: goal[k] for k in ('revision', 'sha256')},
            'reads': reads,
            'writes': writes,
            'sources': sources,
            'targets': targets,
            'repairs': repairs,
            'nominations': source_note,
            'context': str(context_file),
            'native_context': result,
        }
        self._host._write(saved, scope)
        retained = self._host.pin(saved)
        self._host.event('prepared', {'id': sid, 'scope': retained, 'context_sha256': digest(context_file.read_bytes())})
        return {
            'id': sid,
            'context': str(context_file),
            'selected_source_bytes': sum(r['bytes'] for r in sources),
            'changed_sources': len(changed),
            'overlap_nominations': len(overlaps),
            'targets': targets,
            'parent_owed': [r['id'] for r in known.values() if r['disposition'] not in ('fulfilled', 'released')],
        }

    def record_partial_publication(self, path, scope, *, producer, account, returned, publications,
                                   published_outputs, pending_targets, state, failed_target=None, error=None):
        entry = {
            'at_utc': now(),
            'producer': producer,
            'account': account,
            'return': returned,
            'publications': list(publications),
            'outputs': list(published_outputs),
            'pending_targets': list(pending_targets),
            'state': state,
        }
        if failed_target is not None:
            entry['failed_target'] = failed_target
        if error is not None:
            entry['error'] = error
        scope['partial_publication'] = entry
        self._host._write(path, scope)
        if state != 'publishing':
            event_payload = {
                'id': scope['id'],
                'scope': self._host.pin(path),
                'published_targets': [p['path'] for p in publications],
                'pending_targets': list(pending_targets),
            }
            if failed_target is not None:
                event_payload['failed_target'] = failed_target
            if error is not None:
                event_payload['error'] = error
            self._host.event('partial-publication', event_payload)

    def receive(self, sid, spec):
        sid = ident(sid)
        path = self._host.root / 'scopes' / f'{sid}.json'
        scope = read(path)
        if 'superseded' in scope:
            raise ValueError('This preparation was superseded; use its actual successor')
        if 'result' in scope:
            raise ValueError('Retain the completed occurrence; use native recovery for uncertain transport')
        if 'supersession_request' in scope:
            successor = scope['supersession_request']['successor']
            raise ValueError(f'{sid}: inspect the outstanding handoff to {successor} before receiving another return')
        goal = self._host.goal()
        if goal['sha256'] != scope['parent_goal']['sha256']:
            raise ValueError('Parent commission/duties changed; retain the proposal and prepare a fresh receiving occasion')
        self._host.current(self._host.config['commission'], 'Current commission')
        for source in scope['sources']:
            self._host.current(source, 'Selected current source ' + source['id'])
        proposals = spec['proposals']
        expected = {Path(t).name: t for t in scope['targets']}
        if set(proposals) != set(expected):
            raise ValueError('Receive exactly the selected output names')
        account = nonempty(spec['account'], 'receiving account')
        producer = nonempty(spec['producer'], 'producer')
        reply_checked = check_unfollowed_ancestors(spec['reply'], 'Receive reply', include_target=True)
        if not reply_checked.exists() or not stat.S_ISREG(reply_checked.lstat().st_mode):
            raise ValueError('Receive reply must be an existing ordinary file')
        args = ['receive', sid, '--reply', str(reply_checked.resolve()), '--producer', producer]
        for name, source in proposals.items():
            prop_checked = check_unfollowed_ancestors(source, f'Proposal {name}', include_target=True)
            if not prop_checked.exists() or not stat.S_ISREG(prop_checked.lstat().st_mode):
                raise ValueError(f'Proposal {name} must be an existing ordinary file')
            args += ['--proposal', expected[name] + '=' + str(prop_checked.resolve())]
        if spec.get('assessment'):
            assess_checked = check_unfollowed_ancestors(spec['assessment'], 'Assessment file', include_target=True)
            args += ['--assessment', str(assess_checked.resolve())]
        result = self._host.call(*args)
        scope['transport'] = {'at_utc': now(), 'return': result['return'], 'account': account}
        self._host._write(path, scope)
        self._host.event('native-return-retained', {'id': sid, 'scope': self._host.pin(path)})
        publications = []
        published_outputs = []
        for idx, target in enumerate(scope['targets']):
            try:
                publication = self._host.call('adopt', sid, '--target', target, '--reason', account)['publication']
            except Exception as exc:
                if publications:
                    self.record_partial_publication(
                        path, scope, producer=producer, account=account, returned=result['return'],
                        publications=publications, published_outputs=published_outputs,
                        pending_targets=scope['targets'][idx:],
                        state='partial-publication-needs-reconciliation',
                        failed_target=target, error=str(exc).strip(),
                    )
                raise
            publications.append(publication)
            published_outputs.append(self._host.pin(self._host.work / target))
            if idx + 1 < len(scope['targets']):
                self.record_partial_publication(
                    path, scope, producer=producer, account=account, returned=result['return'],
                    publications=publications, published_outputs=published_outputs,
                    pending_targets=scope['targets'][idx + 1:], state='publishing',
                )
        scope.pop('partial_publication', None)
        scope['result'] = {
            'at_utc': now(),
            'producer': producer,
            'return': result['return'],
            'publications': publications,
            'outputs': published_outputs,
            'account': account,
        }
        self._host._write(path, scope)
        self._host.event('received', {
            'id': sid,
            'return_sha256': result['return']['return_sha256'],
            'scope': self._host.pin(path),
            'parent_duties_closed': False,
        })
        return {'id': sid, 'outputs': scope['result']['outputs'], 'parent_duties_closed': False}

    def _settle_partial_return(self, path, scope, *, sid, returned, account, completed_pubs,
                               completed_outputs, remaining_targets, reconciled_captures):
        scope.pop('partial_publication', None)
        scope['result'] = {
            'at_utc': now(),
            'producer': returned['producer'],
            'return': returned,
            'publications': completed_pubs,
            'outputs': completed_outputs,
            'unpublished_targets': remaining_targets,
            'partial': True,
            'account': account,
            'recovered_from_native': True,
        }
        self._host._write(path, scope)
        self._host.event('resumed-partial-return', {
            'id': sid,
            'scope': self._host.pin(path),
            'published_targets': [p['path'] for p in completed_pubs],
            'unpublished_targets': remaining_targets,
            'new_receive_performed': False,
            'previous_publications_replayed': False,
        })
        res = {
            'id': sid,
            'outputs': scope['result']['outputs'],
            'published_targets': [p['path'] for p in completed_pubs],
            'unpublished_targets': remaining_targets,
            'partial': True,
            'recovered': True,
            'parent_duties_closed': False,
        }
        if reconciled_captures:
            res['reconciled_captures'] = reconciled_captures
        return res

    def resume(self, sid, spec):
        """Recover an actual retained return; never repeat receive or attempted publication."""
        sid = ident(sid)
        path = self._host.root / 'scopes' / f'{sid}.json'
        scope = read(path)
        unknown = set(spec) - {'account', 'settle_partial'}
        if unknown:
            raise ValueError('resume has unsupported fields: ' + ', '.join(sorted(unknown)))
        account = nonempty(spec['account'], 'recovery account')
        settle_partial = spec.get('settle_partial', False)
        if type(settle_partial) is not bool:
            raise ValueError('settle_partial must be a boolean')
        if 'superseded' in scope:
            return {'id': sid, 'state': 'superseded-without-output', 'successor': scope['superseded']['successor']}
        if 'result' in scope:
            self._host.received_outputs(scope)
            out = {'id': sid, 'already_received': True, 'outputs': scope['result']['outputs']}
            if scope['result'].get('partial'):
                out['partial'] = True
                out['unpublished_targets'] = scope['result'].get('unpublished_targets', [])
            return out
        reconciled_captures = self._host.bridge._reconcile_pending_captures(sid)
        goal = self._host.goal()
        observed = self._host.inspect_native(sid)
        returned = observed.get('return')
        handoff = scope.get('supersession_request')
        if handoff is not None and returned is None:
            raise ValueError(f"{sid}: no return confirms the handoff to {handoff['successor']}; "
                             'inspect the original packet before retrying supersede')
        if returned is None:
            if reconciled_captures:
                return {'id': sid, 'state': 'open-with-reconciled-captures',
                        'reconciled_captures': reconciled_captures, 'parent_duties_closed': False}
            raise ValueError('No native return exists; ordinary receive is still the appropriate operation')
        if handoff is not None:
            # A bound carrier excludes caller-import at Exchange. Its actual
            # candidate can still use ordinary guarded publication on this turn.
            if returned['transport']['kind'] != 'local-command' or not returned['proposals']:
                return self.finish_supersession(path, scope, returned)
            if returned['packet'] != sid or returned['request_sha256'] != handoff['request_sha256']:
                raise ValueError(f'{sid}: carrier return differs from the prepared handoff occurrence')
        if not returned['proposals']:
            raise ValueError(f'{sid}: no-output return has no recorded successor binding; '
                             'retain the original return and reconcile the handoff at its host')
        proposals = {r['path']: r for r in returned['proposals']}
        if set(proposals) != set(scope['targets']):
            raise ValueError('Observed native return differs from selected host targets')
        known = {p['record']['path']: p for p in observed['publications'] if isinstance(p.get('record'), dict)}
        completed_pubs = []
        completed_outputs = []
        remaining_targets = []
        for target in scope['targets']:
            if target in known:
                row = known[target]
                if (row.get('observation_error')
                        or row['record'].get('phase') != 'complete'
                        or not row.get('current_target', {}).get('matches_proposal')):
                    raise ValueError(f'{sid}: publication of {target} has an unresolved outcome; '
                                     'inspect and reconcile the original native publication before continuing')
                completed_pubs.append(row['record'])
                completed_outputs.append(self._host.pin(self._host.work / target))
            else:
                remaining_targets.append(target)
        if settle_partial:
            if not completed_pubs:
                raise ValueError(f'{sid}: no published targets to settle partially')
            if remaining_targets:
                return self._settle_partial_return(
                    path, scope, sid=sid, returned=returned, account=account,
                    completed_pubs=completed_pubs, completed_outputs=completed_outputs,
                    remaining_targets=remaining_targets, reconciled_captures=reconciled_captures,
                )
        try:
            if goal['sha256'] != scope['parent_goal']['sha256']:
                raise ValueError('Parent duty changed; preserve old return and prepare a new current receiving scope')
            self._host.current(self._host.config['commission'], 'Current commission')
            for source in scope['sources']:
                self._host.current(source, 'Selected current source ' + source['id'])
        except ValueError as exc:
            if completed_pubs and remaining_targets:
                detail = str(exc).strip()
                self.record_partial_publication(
                    path, scope, producer=returned['producer'], account=account, returned=returned,
                    publications=completed_pubs, published_outputs=completed_outputs,
                    pending_targets=remaining_targets,
                    state='partial-publication-needs-reconciliation', error=detail,
                )
                raise ValueError(
                    f'{sid}: partial publication recorded ({len(completed_pubs)} published, '
                    f'{len(remaining_targets)} pending); {detail}. '
                    'Inspect and settle with settle_partial=true or prepare a fresh receiving scope',
                ) from exc
            raise
        pubs_by_target = {p['path']: p for p in completed_pubs}
        outs_by_target = {p['path']: o for p, o in zip(completed_pubs, completed_outputs)}
        for idx, target in enumerate(remaining_targets):
            # Only an unattempted target may be published, through current native guards.
            (self._host.work / target).parent.mkdir(parents=True, exist_ok=True)
            try:
                pub = self._host.call('adopt', sid, '--target', target, '--reason', account)['publication']
            except Exception as exc:
                if pubs_by_target:
                    detail = str(exc).strip()
                    ordered_pubs = [pubs_by_target[t] for t in scope['targets'] if t in pubs_by_target]
                    ordered_outs = [outs_by_target[t] for t in scope['targets'] if t in outs_by_target]
                    pending_targets = list(remaining_targets[idx:])
                    self.record_partial_publication(
                        path, scope, producer=returned['producer'], account=account, returned=returned,
                        publications=ordered_pubs, published_outputs=ordered_outs,
                        pending_targets=pending_targets,
                        state='partial-publication-needs-reconciliation',
                        failed_target=target, error=detail,
                    )
                    raise ValueError(
                        f'{sid}: partial publication recorded ({len(ordered_pubs)} published, '
                        f'{len(pending_targets)} pending) after {target} failed: {detail}; '
                        'inspect and settle with settle_partial=true or prepare a fresh receiving scope',
                    ) from exc
                raise
            pubs_by_target[target] = pub
            outs_by_target[target] = self._host.pin(self._host.work / target)
            if idx + 1 < len(remaining_targets):
                ordered_pubs = [pubs_by_target[t] for t in scope['targets'] if t in pubs_by_target]
                ordered_outs = [outs_by_target[t] for t in scope['targets'] if t in outs_by_target]
                self.record_partial_publication(
                    path, scope, producer=returned['producer'], account=account, returned=returned,
                    publications=ordered_pubs, published_outputs=ordered_outs,
                    pending_targets=remaining_targets[idx + 1:], state='publishing',
                )
        publications = [pubs_by_target[t] for t in scope['targets']]
        outputs = [outs_by_target[t] for t in scope['targets']]
        scope.pop('partial_publication', None)
        scope['result'] = {
            'at_utc': now(),
            'producer': returned['producer'],
            'return': returned,
            'publications': publications,
            'outputs': outputs,
            'account': account,
            'recovered_from_native': True,
        }
        self._host._write(path, scope)
        self._host.event('resumed-return', {
            'id': sid,
            'scope': self._host.pin(path),
            'new_receive_performed': False,
            'previous_publications_replayed': False,
        })
        res = {'id': sid, 'outputs': scope['result']['outputs'], 'recovered': True, 'parent_duties_closed': False}
        if reconciled_captures:
            res['reconciled_captures'] = reconciled_captures
        return res

    def finish_supersession(self, path, scope, returned):
        sid = scope['id']
        request = scope['supersession_request']
        successor = request['successor']
        transport = returned['transport']
        if (returned['packet'] != sid or returned['request_sha256'] != request['request_sha256']
                or returned['producer'] != request['producer'] or returned['proposals']
                or returned['reply_sha256'] != request['reply_sha256'] or transport['kind'] != 'caller-import'
                or transport.get('source_path') != request['reply_path']):
            raise ValueError(f'{sid}: native return does not confirm the recorded handoff to {successor}; '
                             'retain both for host reconciliation')
        scope['superseded'] = {
            'at_utc': now(),
            'successor': successor,
            'account': request['account'],
            'return': returned,
        }
        del scope['supersession_request']
        self._host._write(path, scope)
        self._host.event('preparation-superseded', {
            'id': sid,
            'successor': successor,
            'scope': self._host.pin(path),
            'parent_duties_closed': False,
        })
        return {
            'id': sid,
            'state': 'superseded-without-output',
            'successor': successor,
            'return_sha256': returned['return_sha256'],
            'parent_duties_closed': False,
        }

    def supersede(self, sid, spec):
        """Return an obsolete preparation without a candidate; carry work to a real successor."""
        sid = ident(sid)
        path = self._host.root / 'scopes' / f'{sid}.json'
        scope = read(path)
        if 'result' in scope or 'superseded' in scope:
            raise ValueError('The occurrence already has a host outcome; preserve it')
        successor = read(self._host.root / 'scopes' / f'{ident(spec["successor"])}.json')
        if successor['id'] == sid or successor['prepared_at_utc'] <= scope['prepared_at_utc']:
            raise ValueError('Select a distinct later actual preparation')
        if not set(scope['spec']['duty_ids']).issubset(successor['spec']['duty_ids']):
            raise ValueError('The actual successor must retain every selected parent duty')
        observed = self._host.inspect_native(sid)
        if observed.get('return') is not None or observed.get('transport_outcome') is not None:
            raise ValueError('Native outcome already exists; inspect/reconcile it instead of receiving twice')
        account = nonempty(spec['account'], 'supersession account')
        producer = nonempty(spec['producer'], 'actual responding participant')
        reply = self._host.root / 'requests' / f'{sid}-supersession.md'
        body = (account + '\n\nNo candidate output is returned by this occurrence. Actual continuing scope: '
                + successor['id'] + '. Parent duties remain owed.\n').encode('utf-8')
        # Bind the host decision before Exchange can accept its no-output reply.
        # The ordinary candidate path still depends on a complete native proposal roster.
        request_sha = scope['native_context'].get('request_sha256')
        if request_sha is None:
            request_sha = self._host.call('recover', sid)['request_sha256']
        request = {
            'successor': successor['id'],
            'account': account,
            'producer': producer,
            'request_sha256': request_sha,
            'reply_path': str(reply),
            'reply_sha256': digest(body),
        }
        if 'supersession_request' in scope and scope['supersession_request'] != request:
            raise ValueError(f"{sid}: the outstanding handoff to {scope['supersession_request']['successor']} differs; "
                             'inspect the original request')
        atomic_bytes(reply, body)
        scope['supersession_request'] = request
        self._host._write(path, scope)
        returned = self._host.call('receive', sid, '--reply', reply, '--producer', producer)['return']
        return self.finish_supersession(path, scope, returned)

    def status(self):
        native = self._host.call('status')
        transport = {r['packet']: r for r in native['turns']}
        scopes = []
        for scope in self._host.scopes():
            current = transport.get(scope['id'], {})
            if 'result' in scope:
                label = 'partially-received' if scope['result'].get('partial') else 'received'
            elif 'superseded' in scope:
                label = 'superseded-without-output'
            elif 'partial_publication' in scope:
                label = 'partial-publication-needs-reconciliation'
            elif current.get('transport') == 'returned':
                label = 'native-return-needs-host-reconciliation'
            else:
                label = 'prepared'
            row = {'id': scope['id'], 'host_state': label, 'native': current}
            if 'result' in scope and scope['result'].get('partial'):
                row['published_targets'] = [p['path'] for p in scope['result']['publications']]
                row['unpublished_targets'] = scope['result'].get('unpublished_targets', [])
            if 'partial_publication' in scope and 'result' not in scope:
                partial = scope['partial_publication']
                row['published_targets'] = [p['path'] for p in partial['publications']]
                row['pending_targets'] = partial['pending_targets']
                if 'failed_target' in partial:
                    row['failed_target'] = partial['failed_target']
                if 'error' in partial:
                    row['error'] = partial['error']
            if 'supersession_request' in scope and 'result' not in scope:
                row.update(
                    host_state='supersession-needs-reconciliation',
                    successor=scope['supersession_request']['successor'],
                )
            pending_caps = [
                p.name.removeprefix('.pending-').removesuffix('.json')
                for p in self._host.bridge._pending_capture_paths(scope['id'])
            ]
            if pending_caps:
                row['pending_captures'] = pending_caps
            scopes.append(row)
        return {
            'native': native,
            'scopes': scopes,
            'repairs': [read(x) for x in sorted((self._host.root / 'repairs').glob('*.json'))],
        }
