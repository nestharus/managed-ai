"""A bounded view of a caller-authored composition, without effect authority.

This optional profile exposes declared predictions, offered contributions,
premises and interactions. It neither discovers semantic truth nor turns open
record kinds or arbitrary relation labels into a universal task graph.
"""
from __future__ import annotations

import hashlib
import json


PROFILE = 'g60-composition/1'
FORMAT = 'g60-composition-view/1'
MAX_ROWS = 2000
MAX_REFERENCES = 20000
MAX_PROFILE_BYTES = 2 * 1024 * 1024
MAX_BASIS_BYTES = 32 * 1024 * 1024
MAX_VIEW_BYTES = 1024 * 1024
COLLECTIONS = ('elements', 'relations', 'premises', 'opportunities')
PREFIXES = dict(zip(COLLECTIONS, ('element', 'relation', 'premise', 'opportunity')))
SCOPE = ('Caller-authored meanings and availability claims on nominated sources. '
         'No semantic completeness, effect authority, automatic dispatch or '
         'guarantee of actual parallel independence. Input/output resources are '
         'compared by exact declared identity; undeclared interactions remain unknown.')


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + ' must be nonempty text')
    return value


def _strings(value, label):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(label + ' must be a list of nonempty strings')
    if len(value) != len(set(value)):
        raise ValueError(label + ' contains duplicate references')
    return value


def _fields(value, allowed, label):
    if not isinstance(value, dict):
        raise ValueError(label + ' must be an object')
    extra = set(value) - set(allowed)
    if extra:
        raise ValueError('unknown ' + label + ' fields: ' + ', '.join(sorted(extra)))


def _integer(value, name, maximum, minimum=1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer from {minimum} to {maximum}')
    return value


class _ViewBudget:
    """Count encoder chunks before admitting another returned collection.

    The margin covers the small final counters/saturation labels. A single pair
    is charged inside its actual nesting, conservatively repeating its wrapper.
    No complete expanded JSON string is built to discover that it is too large.
    """
    def __init__(self):
        self.used = 1024

    def include(self, value):
        size = 0
        encoder = json.JSONEncoder(ensure_ascii=False, indent=2, allow_nan=False)
        for chunk in encoder.iterencode(value):
            size += len(chunk.encode('utf-8'))
            if self.used + size > MAX_VIEW_BYTES:
                return False
        self.used += size
        return True


class Composition:
    def __init__(self, system):
        self.system = system

    def profile(self, record):
        body = record.get('content')
        if not isinstance(body, dict) or body.get('format') != PROFILE:
            raise ValueError('compose requires ordinary record.content with format ' + PROFILE +
                             '; information envelopes are not implicitly unwrapped')
        _fields(body, {'format', 'purpose', *COLLECTIONS, 'unknowns', 'coverage', 'content'}, 'composition')
        _text(body.get('purpose'), 'composition purpose')
        input_bytes = 0
        for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(body):
            input_bytes += len(chunk.encode('utf-8'))
            if input_bytes > MAX_PROFILE_BYTES:
                raise ValueError('composition profile exceeds the 2 MiB view input bound')
        _strings(body.get('unknowns', []), 'composition unknowns')
        if 'coverage' in body and not isinstance(body['coverage'], (str, dict, list)):
            raise ValueError('composition coverage must be text, an object or a list')
        rows, references = {}, 0
        allowed = {
            'elements': {'id', 'account', 'content'},
            'relations': {'id', 'from', 'to', 'prediction', 'observation', 'account', 'basis', 'content'},
            'premises': {'id', 'account', 'disposition', 'basis', 'content'},
            'opportunities': {'id', 'at', 'contribution', 'requires', 'disposition', 'reads',
                              'writes', 'footprint', 'conflicts', 'phase', 'account', 'content'},
        }
        for collection in COLLECTIONS:
            source = body.get(collection, [])
            if not isinstance(source, list):
                raise ValueError(collection + ' must be a list')
            rows[collection] = {}
            for row in source:
                _fields(row, allowed[collection], collection + ' row')
                ident = _text(row.get('id'), collection + ' id')
                if ident in rows[collection]:
                    raise ValueError('duplicate ' + collection + ' id: ' + ident)
                rows[collection][ident] = row
                if 'account' in row:
                    _text(row['account'], collection + ' account')
                if collection == 'elements':
                    _text(row.get('account'), 'element account')
                elif collection == 'relations':
                    for key in ('from', 'to', 'basis'):
                        references += len(_strings(row.get(key, []), 'relation ' + key))
                    _text(row.get('prediction'), 'relation prediction')
                    observation = row.get('observation', 'unassessed')
                    if observation not in ('unassessed', 'supported', 'contradicted'):
                        raise ValueError(f"relation {ident!r} field 'observation' must be unassessed, supported or contradicted; "
                                         "put the explanatory observation in this relation's 'account' field")
                    if observation != 'unassessed':
                        _text(row.get('account'), f"relation {ident!r} field 'account' for an asserted observation")
                elif collection == 'premises':
                    _text(row.get('account'), 'premise account')
                    if row.get('disposition', 'unassessed') not in ('available', 'unavailable', 'unassessed'):
                        raise ValueError('premise disposition must be available, unavailable or unassessed')
                    references += len(_strings(row.get('basis', []), 'premise basis'))
                else:
                    _text(row.get('contribution'), 'opportunity contribution')
                    for key in ('at', 'reads', 'writes', 'conflicts'):
                        references += len(_strings(row.get(key, []), 'opportunity ' + key))
                    if row.get('disposition', 'open') not in ('open', 'held', 'complete'):
                        raise ValueError('opportunity disposition must be open, held or complete')
                    if row.get('footprint', 'unknown') not in ('declared', 'unknown'):
                        raise ValueError('opportunity footprint must be declared or unknown')
                    _text(row.get('phase', 'unknown'), 'opportunity phase')
                    alternatives = row.get('requires', [])
                    if not isinstance(alternatives, list):
                        raise ValueError('opportunity requires must be a list of alternative premise lists')
                    for group in alternatives:
                        references += len(_strings(group, 'opportunity premise alternative'))
        if sum(map(len, rows.values())) > MAX_ROWS:
            raise ValueError('composition exceeds the 2000 total declared-row bound')
        if references > MAX_REFERENCES:
            raise ValueError('composition exceeds the 20000 declared-reference bound')
        return body, rows, input_bytes, references

    def _basis(self, record):
        from native_runtime.records import is_content_addressed_basis

        captured, used = {}, 0
        declared = record.get('basis', [])
        if not isinstance(declared, list) or len(declared) > MAX_ROWS:
            raise ValueError('record basis must be a list of at most 2000 captured paths')
        for row in declared:
            if not isinstance(row, dict) or not isinstance(row.get('path'), str):
                raise ValueError('record basis must contain captured path objects')
            name = row['path']
            if name in captured:
                raise ValueError('duplicate captured record basis path: ' + name)
            observed = {'path': name, 'captured_sha256': row.get('sha256'),
                        'current_sha256': None, 'status': 'unavailable'}
            try:
                path = None if name.startswith('object:') else self.system.ex.path(name)
                if path is None or not path.is_file():
                    if is_content_addressed_basis(row) and self.system.ex.objects.has(row['sha256']):
                        size = row['bytes']
                        if size > MAX_PROFILE_BYTES:
                            observed['status'] = 'per-source-byte-bound'
                        elif used + size > MAX_BASIS_BYTES:
                            observed['status'] = 'total-source-byte-bound'
                        else:
                            raw = self.system.ex.objects.read(row['sha256'], retained=True, expected_bytes=size)
                            used += len(raw)
                            observed['current_sha256'] = hashlib.sha256(raw).hexdigest()
                            observed['current_bytes'] = len(raw)
                            observed['status'] = ('current' if row.get('sha256') == observed['current_sha256']
                                                  else 'changed')
                            observed['custody'] = 'exchange-object'
                    else:
                        observed['status'] = 'missing'
                elif path.stat().st_size > MAX_PROFILE_BYTES:
                    observed['status'] = 'per-source-byte-bound'
                elif used + path.stat().st_size > MAX_BASIS_BYTES:
                    observed['status'] = 'total-source-byte-bound'
                else:
                    with path.open('rb') as source:
                        raw = source.read(MAX_PROFILE_BYTES + 1)
                    if len(raw) > MAX_PROFILE_BYTES or used + len(raw) > MAX_BASIS_BYTES:
                        observed['status'] = 'source-byte-bound'
                    else:
                        used += len(raw)
                        observed['current_sha256'] = hashlib.sha256(raw).hexdigest()
                        observed['current_bytes'] = len(raw)
                        observed['status'] = ('current' if row.get('sha256') == observed['current_sha256']
                                              else 'changed')
            except (OSError, ValueError) as exc:
                observed['error'] = str(exc)
            captured[name] = observed
        return captured, used

    @staticmethod
    def _support(names, basis):
        observed = [basis.get(name, {'path': name, 'status': 'unbound'}) for name in names]
        return {'status': ('no-declared-evidence' if not observed else
                           'current' if all(row['status'] == 'current' for row in observed) else
                           'needs-qualification'),
                'sources': observed,
                'scope': 'Byte correspondence of nominated evidence; not assessment of its meaning.'}

    def interaction_map(self, record_id, limit=20):
        """Factor declared positive interactions by resource or explicit conflict."""
        _integer(limit, 'interaction group limit', 200)
        record = self.system.show(record_id)
        _, rows, _, references = self.profile(record)
        opportunities = rows['opportunities']
        resources = {}
        explicit = {}
        unknown = unresolved = self_refs = 0
        for ident, row in opportunities.items():
            unknown += row.get('footprint', 'unknown') == 'unknown'
            for mode in ('reads', 'writes'):
                for name in row.get(mode, []):
                    resources.setdefault(name, {'reads': [], 'writes': []})[mode].append(ident)
            for other in row.get('conflicts', []):
                if other not in opportunities:
                    unresolved += 1
                elif other == ident:
                    self_refs += 1
                else:
                    left, right = sorted((ident, other))
                    explicit.setdefault(left, set()).add(right)

        def groups():
            for name, users in resources.items():
                writers = users['writes']
                if writers and (len(writers) > 1 or any(reader != writers[0] for reader in users['reads'])):
                    yield {'kind': 'resource', 'resource': name, **users}
            for left, others in explicit.items():
                yield {'kind': 'explicit-conflict', 'left': left, 'others': sorted(others)}

        coverage = dict(opportunities=len(opportunities), declared_references=references,
                        unknown_footprints=unknown, unresolved_conflict_refs=unresolved,
                        self_conflict_refs=self_refs, total_groups=0, omitted_by_limit=0, omitted_by_bytes=0)
        result = {'format': 'g60-interaction-map/1',
                  'record': {key: record[key] for key in ('id', 'revision', 'sha256')},
                  'groups': [], 'coverage': coverage,
                  'scope': 'Current declarations only. Resource groups relate each writer to every other '
                           'writer and reader of that name. Explicit edges are unordered. This view does '
                           'not check physical identity, evidence currency, hidden costs or authority. '
                           'Recover omitted groups and original opportunities with show.'}
        budget = _ViewBudget()
        if not budget.include(result):
            raise ValueError('interaction map header exceeds the view bound')
        for group in groups():
            coverage['total_groups'] += 1
            if len(result['groups']) >= limit:
                coverage['omitted_by_limit'] += 1
            elif budget.include({'groups': [group]}):
                result['groups'].append(group)
            else:
                coverage['omitted_by_bytes'] += 1
        return result

    def inspect(self, record_id, focus=None, limit=20, pair_limit=200):
        _integer(limit, 'composition limit', 200)
        _integer(pair_limit, 'composition pair_limit', 20000, minimum=0)
        if focus is not None:
            _text(focus, 'composition focus')
        record = self.system.show(record_id)
        body, rows, input_bytes, reference_count = self.profile(record)
        basis, basis_bytes = self._basis(record)
        known = {PREFIXES[c] + ':' + ident for c in COLLECTIONS for ident in rows[c]}
        unresolved, unresolved_keys = [], set()

        def missing(ref, owner):
            item = {'ref': ref, 'declared_in': owner}
            if (ref, owner) not in unresolved_keys:
                unresolved_keys.add((ref, owner))
                unresolved.append(item)
            return ref

        if focus is not None and focus not in known:
            missing(focus, 'requested-focus')
        premises = {}
        for ident, row in rows['premises'].items():
            support = self._support(row.get('basis', []), basis)
            declared = row.get('disposition', 'unassessed')
            availability = 'unassessed' if support['status'] == 'needs-qualification' else declared
            premises[ident] = dict(row, ref='premise:' + ident, disposition=declared,
                                  availability=availability, support=support,
                                  scope='Caller-supplied availability, qualified by nominated current source bindings.')
        relations = {}
        for ident, row in rows['relations'].items():
            ref = 'relation:' + ident
            endpoints = ['element:' + value for key in ('from', 'to') for value in row.get(key, [])]
            unknown = [missing(value, ref) for value in endpoints if value not in known]
            relations[ident] = dict(row, ref=ref, observation=row.get('observation', 'unassessed'),
                                   support=self._support(row.get('basis', []), basis),
                                   unresolved_refs=unknown,
                                   observation_scope='Caller-authored observation; no truth or alignment judgment inferred.')
        opportunities = {}
        for ident, row in rows['opportunities'].items():
            ref = 'opportunity:' + ident
            unknown = [missing(value, ref) for value in row.get('at', [])
                       if value not in known or value.partition(':')[0] not in ('element', 'relation')]
            for other in row.get('conflicts', []):
                if other not in rows['opportunities']:
                    missing('opportunity:' + other, ref)
            alternatives = []
            for group in row.get('requires', []):
                inputs = []
                for premise in group:
                    if premise not in premises:
                        missing('premise:' + premise, ref)
                        inputs.append({'ref': 'premise:' + premise, 'availability': 'unresolved-reference'})
                    else:
                        known_premise = premises[premise]
                        inputs.append({'ref': known_premise['ref'], 'availability': known_premise['availability'],
                                       'support_status': known_premise['support']['status']})
                states = [value['availability'] for value in inputs]
                status = ('available' if all(s == 'available' for s in states) else
                          'unavailable' if 'unavailable' in states else 'unassessed')
                alternatives.append({'premises': inputs, 'availability': status})
            states = [alternative['availability'] for alternative in alternatives]
            availability = ('available-under-declared-premises' if not states or 'available' in states else
                            'unavailable-under-declared-premises' if all(s == 'unavailable' for s in states) else
                            'needs-qualification')
            if unknown:
                availability = 'needs-qualification'
            opportunities[ident] = dict(row, ref=ref, disposition=row.get('disposition', 'open'),
                                       phase=row.get('phase', 'unknown'), footprint=row.get('footprint', 'unknown'),
                                       availability=availability, premise_alternatives=alternatives,
                                       unresolved_refs=unknown,
                                       footprint_scope='Declared resources are partial claims; implicit context/publication dependencies are not inferred.')

        focus_refs = {focus} if focus is not None else set()
        if focus is not None and focus.startswith('element:'):
            element = focus.partition(':')[2]
            focus_refs.update('relation:' + ident for ident, relation in rows['relations'].items()
                              if element in relation.get('from', []) or element in relation.get('to', []))
        all_opportunities = sorted(opportunities.values(), key=lambda row:
                         (row['ref'] != focus,
                          not (row['ref'] in focus_refs or bool(set(row.get('at', [])) & focus_refs))))
        displayed_opportunities = all_opportunities[:limit]
        related_refs = set(focus_refs)
        for row in displayed_opportunities:
            related_refs.update(row.get('at', []))
            related_refs.update('premise:' + name for group in row.get('requires', []) for name in group)
        for relation in relations.values():
            if relation['ref'] in related_refs:
                related_refs.update('element:' + value for key in ('from', 'to') for value in relation.get(key, []))

        collections = {'opportunities': displayed_opportunities}
        complete = {'elements': [dict(row, ref='element:' + ident) for ident, row in rows['elements'].items()],
                    'relations': list(relations.values()), 'premises': list(premises.values()),
                    'opportunities': all_opportunities}
        coverage = {}
        for collection, items in complete.items():
            if collection != 'opportunities':
                # An explicitly requested row precedes related rows at the same
                # detail bound. Related rows alone must not crowd out that focus.
                collections[collection] = sorted(
                    items, key=lambda row: (row['ref'] != focus,
                                            row['ref'] not in related_refs))[:limit]
            shown = {row['ref'] for row in collections[collection]}
            coverage[collection] = {'total': len(items), 'shown': len(shown),
                                    'omitted_refs': [row['ref'] for row in items if row['ref'] not in shown]}

        pairs = []
        count = len(all_opportunities) * (len(all_opportunities) - 1) // 2
        result = {'format': FORMAT, 'profile': PROFILE,
                  'record': {key: record[key] for key in ('id', 'revision', 'sha256')},
                  'purpose': body['purpose'], 'focus': focus,
                  **collections, 'basis': list(basis.values()), 'interactions': pairs,
                  'unresolved_refs': unresolved, 'unknowns': body.get('unknowns', []),
                  'authored_coverage': body.get('coverage'),
                  'coverage': {'collections': coverage, 'total_declared_rows': sum(map(len, rows.values())),
                               'declared_reference_count': reference_count,
                               'profile_bytes': input_bytes, 'examined_basis_bytes': basis_bytes,
                               'limits': {'detail_per_collection': limit, 'pair_limit': pair_limit,
                                          'total_rows': MAX_ROWS, 'total_references': MAX_REFERENCES,
                                          'profile_bytes': MAX_PROFILE_BYTES, 'basis_bytes': MAX_BASIS_BYTES,
                                          'view_bytes': MAX_VIEW_BYTES},
                               'pairs': {'total': count, 'examined': 0, 'omitted': count, 'saturated': [],
                                         'ordering': 'Focus-first opportunity order, then unordered pairs in that order.'},
                               'recovery': {'record': record_id,
                                            'account': 'Show the exact captured composition body; a current compose call is a new observation and may use changed basis. Focus opportunity:ID to bring its pairs first, or increase declared bounds when useful.'},
                               'scope': 'Complete bounded profile evaluated; detail and pair omissions are explicit. Cycles in authored relations are allowed, not treated as task deadlocks. No outside composition or semantic relation census.'},
                  'scope': SCOPE}
        budget = _ViewBudget()
        if not budget.include(result):
            raise ValueError('composition base view exceeds the 1 MiB returned-view byte bound; '
                             'reduce limit, narrow the nominated composition, or read its exact body with show')
        # Pair comparisons concern the whole nominated opportunity collection,
        # ordered focus-first, not just the displayed detail rows. Stop before
        # accumulation would exceed either the count or returned byte bound.
        byte_saturated = False
        for index, left in enumerate(all_opportunities):
            if len(pairs) >= pair_limit or byte_saturated:
                break
            for right in all_opportunities[index + 1:]:
                if len(pairs) >= pair_limit:
                    break
                reasons = []
                if right['id'] in left.get('conflicts', []) or left['id'] in right.get('conflicts', []):
                    reasons.append({'kind': 'explicit-conflict'})
                for kind, first, second in [('write-write', 'writes', 'writes'),
                                            ('left-write-right-read', 'writes', 'reads'),
                                            ('left-read-right-write', 'reads', 'writes')]:
                    common = sorted(set(left.get(first, [])) & set(right.get(second, [])))
                    if common:
                        reasons.append({'kind': kind, 'resources': common})
                unknown = left['footprint'] == 'unknown' or right['footprint'] == 'unknown'
                pair = {'left': left['ref'], 'right': right['ref'],
                        'phase_refs': [{'opportunity': left['ref'], 'field': 'phase'},
                                       {'opportunity': right['ref'], 'field': 'phase'}],
                        'interactions': reasons,
                        'status': ('declared-interaction' if reasons else
                                   'unknown-footprint' if unknown else 'no-declared-interaction'),
                        'unknown_footprint': unknown,
                        'scope': 'No declared interaction is not actual independence; context and effect contracts still apply.'}
                if not budget.include({'interactions': [pair]}):
                    byte_saturated = True
                    break
                pairs.append(pair)
        pair_coverage = result['coverage']['pairs']
        pair_coverage.update(examined=len(pairs), omitted=count - len(pairs))
        if byte_saturated:
            pair_coverage['saturated'].append('view-byte-bound')
        if len(pairs) == pair_limit and len(pairs) < count:
            pair_coverage['saturated'].append('pair-limit')
        return result
