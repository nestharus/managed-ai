"""Build one turn from the owner's current records, selection and installed methods."""
from __future__ import annotations

from continuation_support.ownership import packet_id, relative_path
from continuation_support.storage import encode, write_json
from composition import Composition
from discovery import Discovery
from feedback import Feedback
from information import Information
from native_runtime.inputs import (accounted_closure, artifact_target, fields, integer, strings,
                                   text, write_input)
from native_runtime.scope import TurnScope, additional_basis
from turns import TurnPreparation, feedback_id


class TurnCapture:
    """Each instance is created for one owner-locked preparation."""

    def __init__(self, system, guide_file, *, entry_sources=(), explicit_techniques=()):
        self.records = system.record_store
        self.projects = system.projects
        self.techniques = system.techniques
        self.goal_use = system.goal_use
        self.ex = system.ex
        self.command = system.command
        self.discovery = Discovery(system)
        self.composition = Composition(system)
        self.information = Information(system)
        self.feedback = Feedback(system)
        self.guide_file = guide_file
        self.entry_sources = set(entry_sources)
        self.explicit_techniques = set(explicit_techniques)
        self.origins = {}

    def _write_input(self, directory, name, data):
        return write_input(self.ex.workspace, directory, name, data)

    def _origin(self, role, path, **reason):
        reasons = self.origins.setdefault((role, path), [])
        if reason not in reasons:
            reasons.append(reason)

    def _request(self, request: dict) -> dict:
        fields(request, {'task', 'consumer', 'records', 'sources', 'techniques', 'targets',
                         'returns', 'artifacts', 'attention', 'record_limit',
                         'attention_limit', 'attention_max_bytes', 'attention_groups',
                         'attention_items', 'project_sources', 'discovery', 'source_pins',
                         'compositions', 'feedback_uses'}, 'context request')
        normalized = dict(request)
        task = request['task'] if 'task' in request else self.records.show('goal')['content']['contribution']
        normalized['task'] = text(task, 'task')
        normalized['consumer'] = text(request.get('consumer', 'current capable agent'), 'consumer')
        for key in ('records', 'sources', 'techniques', 'targets', 'returns', 'artifacts',
                    'project_sources', 'attention_groups', 'attention_items'):
            normalized[key] = strings(request.get(key, []), key)
        if 'feedback_uses' in request:
            normalized['feedback_uses'] = strings(request['feedback_uses'], 'feedback_uses')
        normalized['project_sources'] = [relative_path(p) for p in normalized['project_sources']]
        strings(normalized['project_sources'], 'normalized project_sources')
        if normalized['project_sources'] and self.projects.current is None:
            raise ValueError('project_sources requires an explicitly selected deployment; use deploy.py ... system context')
        normalized['targets'] = [artifact_target(p) for p in normalized['targets']]
        for key, default in (('record_limit', 20), ('attention_limit', 8), ('attention_max_bytes', 16000)):
            normalized[key] = integer(request.get(key, default), key, 1)
        if 'attention' in request and not isinstance(request['attention'], dict):
            raise ValueError('attention must be an object')
        if 'discovery' in request and not isinstance(request['discovery'], dict):
            raise ValueError('discovery must be a request object')
        normalized['source_pins'] = request.get('source_pins', [])
        if not isinstance(normalized['source_pins'], list):
            raise ValueError('source_pins must be a list of {ref, source} objects')
        selections = request.get('compositions', [])
        if not isinstance(selections, list):
            raise ValueError('compositions must be a list of explicit view selections')
        if len(selections) > 20:
            raise ValueError('select at most 20 composition views in one context')
        normalized['compositions'] = []
        for selection in selections:
            if not isinstance(selection, dict):
                raise ValueError('each composition selection must be an object')
            fields(selection, {'record', 'focus', 'limit', 'pair_limit'}, 'composition selection')
            ident = packet_id(selection.get('record', ''))
            row = {'record': ident, 'limit': integer(selection.get('limit', 20), 'composition limit', 1),
                   'pair_limit': integer(selection.get('pair_limit', 200), 'composition pair_limit', 0)}
            if 'focus' in selection:
                row['focus'] = text(selection['focus'], 'composition focus')
            normalized['compositions'].append(row)
            if ident not in normalized['records']:
                normalized['records'].append(ident)
        if len({row['record'] for row in normalized['compositions']}) != len(normalized['compositions']):
            raise ValueError('a composition record may be selected only once per context')
        return normalized

    def _compositions(self, selections):
        compositions, composition_bytes = [], 0
        for row in selections:
            report = self.composition.inspect(row['record'], row.get('focus'), row['limit'], row['pair_limit'])
            composition_bytes += len(encode(report))
            if composition_bytes > 1024 * 1024:
                raise ValueError('combined composition views exceed the 1 MiB context budget; request fewer views or smaller detail/pair limits')
            compositions.append(report)
        return compositions

    def _record_materials(self, request, selected, record_origins):
        materials = []
        for record in selected:
            path = self.records.material(record['id'])
            materials.append(path)
            for reason in record_origins[record['id']]:
                self._origin('materials', path, record=record['id'], **reason)
        unavailable, historical_basis = [], []
        explicit_records = set(request['records'][:request['record_limit']])
        for record in selected:
            content = record['content']
            closed = record['kind'] == 'obligation' and accounted_closure(content)
            for item in record['basis']:
                if closed and record['id'] not in explicit_records:
                    historical_basis.append(dict(item, record=record['id'],
                        scope='Original closed-duty basis; current source not revalidated by this selection.'))
                elif self.ex.path(item['path']).is_file():
                    materials.append(item['path'])
                    self._origin('materials', item['path'], kind='selected-record-basis',
                                 record=record['id'], revision=record['revision'],
                                 expected={k: item.get(k) for k in ('sha256', 'bytes')})
                else:
                    unavailable.append({'record': record['id'], 'path': item['path']})
        return materials, unavailable, historical_basis

    def _discovery_summary(self, directory, query, discovery):
        # Full query/coverage remains exact in the recovered view. The
        # working exposure shows candidate routes, not every outside ID.
        report_object = self.ex.objects.put(encode(discovery))
        coverage = discovery['coverage']
        compact_coverage = {k: coverage[k] for k in
            ('requested_scopes', 'unavailable_default_scopes', 'inventory_counts',
             'examined_entries', 'examined_relation_rows', 'examined_body_bytes',
             'unexamined_count', 'matched_candidates', 'shown_candidates',
             'saturated', 'limits', 'search_mode')}
        compact_coverage.update({k + '_count': len(coverage[k]) for k in
            ('omitted_candidate_refs', 'skipped', 'unresolved_references', 'traversal_frontier')})
        summary = {'request': discovery.get('request', query),
                   'candidates': discovery.get('candidates', []),
                   'context_request': discovery.get('context_request', {}),
                   'coverage': compact_coverage,
                   'report_object': report_object,
                   'scope': 'Machine retrieval suggestions; inspect complete bodies and qualify their current use. Full coverage is retained in view TURN.'}
        if 'recovery' in discovery:
            summary['recovery'] = discovery['recovery']
        return self._write_input(directory, 'discovery-summary.json', encode(summary))

    def _working_view(self, directory, turn, techniques, view):
        encoded = encode(view)
        full_view = self._write_input(directory, 'view.json', encoded)
        exposure = {k: v for k, v in view.items() if k not in
                    {'outside_techniques', 'outside_project_sources', 'outside_record_basis', 'discovery',
                     'compositions', 'feedback_guard', 'inclusion_origins'}}
        exposure.update(full_view_object=self.ex.objects.put(encoded), full_view_path=full_view,
                        full_view_recovery=self.command('view', turn),
                        outside_technique_count=len(view['outside_techniques']),
                        outside_project_source_count=len(view['outside_project_sources']),
                        outside_record_basis_count=len(view['outside_record_basis']),
                        selected_technique_ids=techniques, omitted_technique_ids=[],
                        scope='Complete selected bodies delivered; outside identities are recoverable, not read or applied.')
        return exposure

    def build(self, prep: TurnPreparation, request: dict) -> tuple[dict, dict]:
        turn = prep.turn
        all_feedback = 'feedback_uses' not in request
        explicit_records = tuple(request.get('records', []))
        request = self._request(request)
        scope = TurnScope(self.records, request['records'][:request['record_limit']])
        selection = scope.route(request, self.feedback, all_feedback)
        selected = selection.records
        for ident, reasons in selection.origins.items():
            for reason in reasons:
                if reason['kind'] == 'normalized-request-record':
                    reason['kind'] = ('explicit-record' if ident in explicit_records else 'composition-record')
        for row in request['compositions']:
            reason = {'kind': 'composition-record'}
            if reason not in selection.origins[row['record']]:
                selection.origins[row['record']].append(reason)
        routes = selection.routes
        compositions = self._compositions(request['compositions'])
        pinned = self.discovery.verify_selection(request['source_pins'])
        actual_selectors = {'records': [r['id'] for r in selected],
                            'project_sources': request['project_sources'],
                            'techniques': request['techniques']}
        for key, identities in pinned['context_request'].items():
            if set(identities) - set(actual_selectors[key]):
                raise ValueError('a pinned discovery body is not selected in this context: ' + key)
        directory = prep.directory
        materials, unavailable, historical_basis = self._record_materials(request, selected, selection.origins)
        for path in request['sources']:
            path = relative_path(path)
            materials.append(path)
            self._origin('materials', path, kind='explicit-source')
        guide = self.guide_file.read_bytes()
        methods = [self._write_input(directory, 'method.md', guide)]
        self._origin('methods', methods[0], kind='application-guide')
        knowledge = self.techniques.capture(directory, request['techniques'], self._write_input)
        materials.extend(knowledge['paths'])
        for path, entry in zip(knowledge['paths'], knowledge['selected']):
            if entry['id'] in self.entry_sources:
                self._origin('materials', path, kind='domain-entry-technique', source=entry['id'])
            if entry['id'] in self.explicit_techniques:
                self._origin('materials', path, kind='explicit-technique', source=entry['id'])
        project = self.projects.capture(directory, request['project_sources'], self._write_input)
        materials.extend(project['paths'])
        for row in project['selected']:
            self._origin('materials', row['captured_path'], kind='explicit-project-source', source=row['path'])
        information = self.information.context_inputs(directory, selected)
        materials.extend(information['paths'])
        for row in information['selected']:
            self._origin('materials', row['captured_path'], kind='pinned-information-definition',
                         record=row['record']['id'], contract=row['contract'])
        discovery = self.discovery.search(request['discovery']) if 'discovery' in request else None
        if discovery is not None:
            path = self._discovery_summary(directory, request['discovery'], discovery)
            materials.append(path)
            self._origin('materials', path, kind='requested-discovery-summary')
        self._write_input(directory, 'request.json', encode(request))
        if 'attention' in request:
            spec = self._write_input(directory, 'attention-spec.json', encode(request['attention']))
            prep.draft.track(spec)
        feedback = prep.draft.feedback(feedback_id(turn),
                                           groups=request['attention_groups'],
                                           items=request['attention_items'],
                                           limit=request['attention_limit'],
                                           max_bytes=request['attention_max_bytes'])
        path = self._write_input(directory, 'attention.md', feedback['context'].encode())
        materials.append(path)
        self._origin('materials', path, kind='attention-feedback', feedback=feedback_id(turn))
        extra_basis = additional_basis(unavailable, feedback)
        if compositions:
            publication_dependencies = {
                'governing_records': [{k: r[k] for k in ('id', 'revision', 'sha256')}
                                      for r in scope.governing.values()],
                'material_paths': list(dict.fromkeys(materials + methods)),
                'target_paths': request['targets'],
                'additional_basis': extra_basis,
                'scope': 'Existing publication checks every captured brief, method, material and target, plus governing membership and declared additional basis. This is the current context prerequisite footprint; the capture also checks its own generated view and exposure. Declared opportunity footprints do not establish independent publication. Reopening a shared field can require current-use reconsideration of an unchanged retained proposal.'}
            path = self._write_input(directory, 'compositions.json', encode({
                'views': compositions, 'publication_dependencies': publication_dependencies,
                'scope': 'Authored compositions and bounded mechanical projections, not semantic completeness, authority, selection or dispatch.'}))
            materials.append(path)
            for row in request['compositions']:
                self._origin('materials', path, kind='composition-view', record=row['record'])
        exposed_paths = set(materials + methods)
        outside_basis = [item for item in historical_basis if item['path'] not in exposed_paths]
        view = {'selected_records': [r['id'] for r in selected], 'omitted_records': selection.omitted,
                'outside_record_basis': outside_basis,
                'record_basis_scope': 'Every governing duty record is retained. Closed-duty evidence bodies are selected on explicit record/source request or through another selected basis; outside bindings retain their original occurrence, not a current source check.',
                'outside_view_records': selection.outside, 'unavailable_record_basis': unavailable,
                'techniques': knowledge['selected'], 'outside_techniques': knowledge['outside'],
                'information': information['selected'], 'discovery': discovery,
                'source_pins': pinned['verified'],
                'deployment': self.projects.view(), 'project_sources': project['selected'],
                'outside_project_sources': project['outside'],
                'attention_selection': {'groups': request['attention_groups'],
                                        'items': request['attention_items']},
                'attention_delivered': [r['id'] for r in feedback['delivered']],
                'attention_omitted': feedback['omitted'], 'attention_outside_view': feedback['outside_view'],
                'additional_basis': extra_basis,
                'current_use': self.goal_use.current_use(scope.governing.values(), goal=scope.governing['goal']),
                'governing_records': [r['id'] for r in scope.governing.values()]}
        view['feedback_guard'] = {'all': all_feedback, 'uses': selection.guard}
        if routes:
            view['feedback_uses'] = routes
        if compositions:
            view.update(compositions=compositions, publication_dependencies=publication_dependencies)
        prefix = directory.relative_to(self.ex.workspace).as_posix() + '/'
        self._origin('materials', prefix + 'exposure.json', kind='captured-selection-exposure')
        self._origin('brief', prefix + 'brief.md', kind='receiving-task')
        for path in request['targets']:
            self._origin('targets', path, kind='publication-target')
        for prior in request['returns']:
            self._origin('returns', 'prior-return:' + prior, kind='selected-prior-return', packet=prior)
        for reference in request['artifacts']:
            prior, _, path = reference.partition(':')
            path = relative_path(path)
            self._origin('artifacts', 'prior-proposal:' + prior + ':' + path,
                         kind='selected-retained-proposal', packet=prior, target=path)
        view['inclusion_origins'] = {
            'format': 'agent-system-inclusion-origins/1',
            'units': [dict(role=role, path=path, reasons=reasons)
                      for (role, path), reasons in self.origins.items()],
            'scope': 'Reasons observed at capture additions. A shared input can have several reasons; '
                     'reason multiplicity does not multiply its delivered bytes or establish semantic need.'}
        exposure = self._working_view(directory, turn, request['techniques'], view)
        materials.append(self._write_input(directory, 'exposure.json', encode(exposure)))
        brief_text = '# Current receiving task\n\n' + request['task'] + '\n'
        if routes:
            brief_text += '\n## Routed feedback work by scope\n'
            for row in routes:
                brief_text += f"\n- {row['id']} ({row['state']}) — {row['scope']}: {row['next']}\n"
        brief = self._write_input(directory, 'brief.md', brief_text.encode())
        write_json(directory / 'selection.json', {'request': request, 'view': view,
                                                'attention_feedback': feedback_id(turn)})
        result = self.ex.prepare(turn, request['consumer'], brief, methods,
                                 list(dict.fromkeys(materials)), request['targets'],
                                 request['returns'], request['artifacts'], staged=True)
        return dict(result, view=exposure, next={
            'read_context': self.command('recover', turn, '--text'),
            'recover_complete_view': self.command('view', turn),
            'receive_actual_output': self.command('receive', turn, '--reply', 'REPLY.txt', '--producer', 'ACTUAL PRODUCER'),
            'prepare_next_context': self.command('context', turn + '-next', '--task', 'Describe the next current use', '--text')}), feedback
