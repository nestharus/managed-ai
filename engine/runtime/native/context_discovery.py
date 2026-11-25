"""Bounded, source-qualified discovery for the cooperative local agent system.

The host supplies useful questions, relation meanings and applicability. This
module searches declared local scopes and follows authored record relations; it
does not infer authority, semantic completeness or a universal dependency graph.
No persistent index is maintained. The returned selectors are candidates for a
later context, not a claim that a receiving participant has read their bodies.
"""
from __future__ import annotations

from collections import deque
import hashlib
import json
import sys
from pathlib import Path, PurePosixPath


FORMAT = 'agent-system-discovery/1'
PROJECT_CONTEXT_BYTES = 2 * 1024 * 1024
SCOPES = ('records', 'project', 'techniques')
PREFIX_SCOPE = {'record': 'records', 'project': 'project', 'technique': 'techniques'}
SELECTOR = {'records': 'records', 'project': 'project_sources', 'techniques': 'techniques'}
FIELDS = {'query', 'content', 'scopes', 'anchors', 'relation_labels', 'direction',
          'hops', 'limit', 'scan_limit', 'max_bytes', 'include_inactive',
          'scan_order', 'seed', 'records_only', 'exclude_relation_labels',
          'include_content_links'}
SCAN_ORDERS = ('inventory', 'scope-round-robin', 'seeded')
SCOPE = ('Lexical matches and authored relation routes nominate inspection. '
         'Machine examination, a returned candidate, body delivery, semantic '
         'applicability, actual use and authority are different outcomes. '
         'No returned match establishes no adequate source outside this search.')


def digest(body):
    return hashlib.sha256(body).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer from {minimum} to {maximum}')
    return value


def strings(value, name):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(name + ' must be a list of nonempty strings')
    if len(set(value)) != len(value):
        raise ValueError(name + ' contains duplicates')
    return value


def request(value, deployed):
    if not isinstance(value, dict):
        raise ValueError('discovery request must be an object')
    if set(value) - FIELDS:
        raise ValueError('unknown discovery fields: ' + ', '.join(sorted(set(value) - FIELDS)))
    query = value.get('query', '')
    if not isinstance(query, str):
        raise ValueError('discovery query must be text')
    for flag in ('content', 'include_inactive', 'records_only', 'include_content_links'):
        if flag in value and type(value[flag]) is not bool:
            raise ValueError(flag + ' must be Boolean')
    scopes = strings(value.get('scopes', list(SCOPES if deployed else ('records', 'techniques'))), 'scopes')
    if not scopes or set(scopes) - set(SCOPES):
        raise ValueError('scopes must select records, project or techniques')
    if 'project' in scopes and not deployed:
        raise ValueError('project discovery requires an explicitly selected deployment')
    direction = value.get('direction', 'outgoing')
    if direction not in ('outgoing', 'incoming', 'both'):
        raise ValueError('direction must be outgoing, incoming or both')
    scan_order = value.get('scan_order', 'inventory')
    if scan_order not in SCAN_ORDERS:
        raise ValueError('scan_order must be inventory, scope-round-robin or seeded')
    if scan_order == 'seeded':
        seed = integer(value.get('seed'), 'seed', 0, 2**64 - 1)
    else:
        if 'seed' in value:
            raise ValueError('seed is only accepted with scan_order=seeded')
        seed = None
    normalized = {
        'query': query, 'content': value.get('content', False), 'scopes': scopes,
        'anchors': strings(value.get('anchors', []), 'anchors'),
        'relation_labels': strings(value.get('relation_labels', []), 'relation_labels'),
        'direction': direction,
        'hops': integer(value.get('hops', 1), 'hops', 0, 8),
        'limit': integer(value.get('limit', 25), 'limit', 1, 200),
        'scan_limit': integer(value.get('scan_limit', 1000), 'scan_limit', 1, 100000),
        'max_bytes': integer(value.get('max_bytes', 1048576), 'max_bytes', 1, 33554432),
        'include_inactive': value.get('include_inactive', False),
        'scan_order': scan_order,
    }
    if scan_order == 'seeded':
        normalized['seed'] = seed
    if value.get('records_only'):
        normalized['records_only'] = True
    exclude_labels = strings(value.get('exclude_relation_labels', []), 'exclude_relation_labels')
    if exclude_labels:
        normalized['exclude_relation_labels'] = exclude_labels
    if value.get('include_content_links'):
        normalized['include_content_links'] = True
    return normalized


class Discovery:
    def __init__(self, system):
        self.records = system.record_store
        self.techniques = system.techniques
        self.projects = system.projects

    def verify_selection(self, pins):
        """Resolve exact candidate pins before a later context uses their selectors.

        A pin is exactly {ref, source}, copied from a candidate. Verification is
        deliberately strict about its original edition and record revision. The
        caller can obtain a new discovery or reason about a fresh selection; this
        method never silently rebases a prior applicability claim.
        """
        if not isinstance(pins, list) or len(pins) > 200:
            raise ValueError('source pins must be a list of at most 200 {ref, source} objects')
        refs, scopes = [], set()
        for pin in pins:
            if not isinstance(pin, dict) or set(pin) != {'ref', 'source'}:
                raise ValueError('each source pin must contain exactly ref and source')
            ref = pin['ref']
            if not isinstance(ref, str) or not ref or not isinstance(pin['source'], dict):
                raise ValueError('a source pin requires a reference and source identity object')
            scope = PREFIX_SCOPE.get(ref.partition(':')[0])
            if scope is None or ':' not in ref:
                raise ValueError('source pin must use a declared reference namespace')
            if ref in refs:
                raise ValueError('duplicate source pin: ' + ref)
            canonical(pin)
            refs.append(ref)
            scopes.add(scope)
        if not pins:
            return {'verified': [], 'context_request': {}}
        current = self.search({'anchors': refs, 'scopes': [s for s in SCOPES if s in scopes],
                               'hops': 0, 'limit': 200, 'scan_limit': 100000,
                               'max_bytes': 33554432, 'include_inactive': True})
        candidates = {c['ref']: c for c in current['candidates']}
        for pin in pins:
            candidate = candidates.get(pin['ref'])
            if candidate is None:
                raise ValueError('selected discovery source is missing or exceeds verification bounds: ' + pin['ref'])
            if candidate['source'] != pin['source']:
                raise ValueError('selected discovery source identity changed: ' + pin['ref'])
            if candidate.get('selector') is None or not candidate.get('body_identity', {}).get('verified'):
                raise ValueError('selected discovery source is unavailable as verified context text: ' + pin['ref'])
        return {'verified': json.loads(canonical(pins)), 'context_request': current['context_request']}

    def search(self, supplied):
        """Return a bounded discovery report without writing or invoking effects.

        With anchors and a query, return the union of the anchor neighborhood and
        lexical matches. Empty query without anchors lists the selected inventory.
        scan_limit independently bounds distinct examined entries and declared
        relation rows. max_bytes bounds admitted source-byte counts, including
        record JSON and bodies verified for context selectors. Source reads
        stop at the admitted size plus one excess byte on growth, then refuse;
        metadata and deployment verification are outside this source-byte
        budget. References to the same
        bound catalogue object reuse its verified observation within this query;
        logical reference bytes are reported separately. Metadata inventories are
        already owner-visible and are reported separately. Optional scan ordering
        changes lexical nomination, after anchor traversal, without changing the
        source, entry, body or result bounds. A seeded order is reproducible
        pseudorandom exploration, not an inclusion-probability warranty.
        """
        self.req = request(supplied, self.projects.current is not None)
        self.entries = {}
        self.by_scope = {scope: [] for scope in SCOPES}
        self.catalogue = None
        self.examined, self.loaded, self.bodies = set(), {}, {}
        self.body_info, self.body_failures = {}, {}
        self.body_objects, self.reused_body_references = {}, 0
        self.examined_bytes = 0
        self.skipped, self.unresolved, self.frontier = [], [], []
        self._skips, self._unresolved = set(), set()
        self.relation_rows, self.edges, self.edge_keys = set(), [], set()
        self.edge_cache = {}
        self.candidates = {}
        self.incoming = None
        self.lexical_nomination = None
        self.saturated = set()
        self._inventory()
        if self.req['anchors']:
            self._traverse()
        if self.req['query'].strip() or not self.req['anchors']:
            self._lexical()
        all_candidates = list(self.candidates.values())
        shown = all_candidates[:self.req['limit']]
        selectors = {key: [] for key in SELECTOR.values()}
        for candidate in shown:
            self._selector(candidate)
            for key, values in (candidate.get('selector') or {}).items():
                selectors[key].extend(v for v in values if v not in selectors[key])
        omitted = [c['ref'] for c in all_candidates[self.req['limit']:]]
        if omitted:
            self.saturated.add('result-limit')
        unexamined = {scope: [ref for ref in self.by_scope[scope] if ref not in self.examined]
                      for scope in self.req['scopes']}
        project = self.projects.current
        coverage = {
            'requested_scopes': self.req['scopes'],
            'unavailable_default_scopes': [] if project else ['project'],
            'inventory_counts': {s: len(self.by_scope[s]) for s in self.req['scopes']},
            'inventory': {s: self.by_scope[s] for s in self.req['scopes']},
            'examined_entries': len(self.examined),
            'examined_entries_by_scope': {
                s: len(self.by_scope[s]) - len(unexamined[s]) for s in self.req['scopes']},
            'lexical_nomination': self.lexical_nomination,
            'examined_relation_rows': len(self.relation_rows),
            'examined_body_bytes': self.examined_bytes,
            'examined_body_reference_bytes': sum(row['bytes'] for row in self.body_info.values()),
            'examined_body_sources': len(self.body_info) - self.reused_body_references,
            'reused_body_references': self.reused_body_references,
            'body_accounting': ('examined_body_bytes charges each acquired bound source object once '
                                'within this query. examined_body_reference_bytes counts its bytes '
                                'for each verified reference. Reuse requires the same catalogue '
                                'body path, expected digest and byte count; no cross-query cache.'),
            'unexamined': unexamined,
            'unexamined_count': sum(map(len, unexamined.values())),
            'skipped': self.skipped, 'unresolved_references': self.unresolved,
            'traversal_frontier': self.frontier,
            'omitted_candidate_refs': omitted,
            'matched_candidates': len(all_candidates),
            'shown_candidates': len(shown),
            'limits': {k: self.req[k] for k in ('hops', 'limit', 'scan_limit', 'max_bytes')},
            'saturated': sorted(self.saturated),
            'search_mode': ('anchor-neighborhood-and-lexical' if self.req['anchors'] and self.req['query'].strip()
                            else 'anchor-neighborhood' if self.req['anchors'] else
                            'lexical' if self.req['query'].strip() else 'inventory'),
            'project_selection': None if project is None else project.get('selection'),
            'project_manifest_sha256': None if project is None else project.get('selection', {}).get('manifest_sha256'),
            'catalogue_snapshot': None if self.catalogue is None else self.catalogue.get('snapshot'),
            'catalogue_metadata_sha256': None if self.catalogue is None else digest(canonical(self.catalogue)),
            'record_membership_sha256': digest(canonical(self.by_scope['records'])),
            'binding_scope': ('Catalogue metadata digest uses sorted compact UTF-8 JSON. Record membership '
                              'binds visible IDs only; revisions of unexamined records were not read. '
                              'Project identity comes from the selected verified deployment manifest. '
                              'Complete examined body identities are reported separately.'),
            'scope': SCOPE,
        }
        result = {
            'format': FORMAT, 'request': self.req, 'candidates': shown,
            'context_request': {k: v for k, v in selectors.items() if v},
            'context_request_scope': ('Candidate selectors only: not automatically applicable, read by a '
                                      'participant, or authorized for an effect. Retain this discovery '
                                      'report and compare each expected source identity when preparing '
                                      'a later context. A changed source needs an explicit current use.'),
            'source_bindings': {c['ref']: c['source'] for c in shown},
            'examined_bodies': self.body_info,
            'coverage': coverage,
            'ordering': ('Requested anchors and bounded breadth-first relations, then lexical '
                         + self.req['scan_order'] + ' order; not a relevance ranking.'),
        }
        if not shown and coverage['search_mode'] == 'lexical' and 'techniques' in self.req['scopes']:
            # A released app and an authoring checkout place the trusted reader differently.
            here = Path(__file__).resolve().parent
            reader = here.parent / 'source_library' / 'knowledge.py'
            if reader.is_file() and (reader.parent / 'library' / 'catalogue.json').is_file():
                query = self.req['query']
                recovery = {
                    'operation': 'ranked-published-knowledge',
                    'scope': 'Optional separate, read-only BM25 search of the installed knowledge library. '
                             'Local records and project files stay in the app search. Inspect the returned '
                             'source owner before using a nomination.',
                }
                # Preserve the query as a single argv value. The ranked reader validates its own syntax.
                if len(query.encode('utf-8')) <= 2048 and '\x00' not in query:
                    recovery['argv'] = [
                        sys.executable, '-B', str(reader), 'discover', '--backend', 'bm25',
                        '--query=' + query, '--limit', str(min(self.req['limit'], 100)),
                        '--scan-limit', str(min(self.req['scan_limit'], 2048)),
                        '--max-bytes', str(self.req['max_bytes']),
                    ]
                else:
                    recovery['input_needed'] = 'Supply a ranked query of at most 2048 UTF-8 bytes without NUL.'
                result['recovery'] = recovery
        # The occurrence must not share mutable identity dictionaries with the
        # live deployment/catalogue or caller's subsequent edits.
        return json.loads(canonical(result))

    def _inventory(self):
        if 'records' in self.req['scopes']:
            for ident in self.records.ids():
                ref = 'record:' + ident
                self.entries[ref] = {'scope': 'records', 'id': ident}
                self.by_scope['records'].append(ref)
        if 'project' in self.req['scopes']:
            for row in sorted(self.projects.current['project_files'], key=lambda r: r['path']):
                name = row['path']
                parts = PurePosixPath(name)
                if not isinstance(name, str) or not name or parts.is_absolute() or '..' in parts.parts or '\\' in name:
                    raise ValueError('invalid accepted-project member path')
                ref = 'project:' + name
                if ref in self.entries:
                    raise ValueError('duplicate accepted-project member')
                self.entries[ref] = {'scope': 'project', 'id': name, 'row': row}
                self.by_scope['project'].append(ref)
        if 'techniques' in self.req['scopes']:
            self.catalogue = self.techniques.index()
            entries = self.catalogue.get('entries', [])
            if not isinstance(entries, list):
                raise ValueError('catalogue entries must be a list')
            for row in sorted(entries, key=lambda r: r['id']):
                ref = 'technique:' + row['id']
                if ref in self.entries:
                    raise ValueError('duplicate catalogue entry')
                self.entries[ref] = {'scope': 'techniques', 'id': row['id'], 'row': row}
                self.by_scope['techniques'].append(ref)

    def _skip(self, ref, reason):
        key = (ref, reason)
        if key not in self._skips:
            self._skips.add(key)
            self.skipped.append({'ref': ref, 'reason': reason})

    def _unresolve(self, target, reason, declared_in=None):
        key = (target, reason, declared_in)
        if key not in self._unresolved:
            self._unresolved.add(key)
            self.unresolved.append({'target': target, 'reason': reason, 'declared_in': declared_in})

    def _resolve(self, target, declared_in=None):
        if not isinstance(target, str) or not target:
            self._unresolve(str(target), 'nonempty-reference-required', declared_in)
            return None
        if ':' not in target and 'record:' + target in self.entries:
            target = 'record:' + target
        prefix = target.partition(':')[0]
        scope = PREFIX_SCOPE.get(prefix)
        if scope is None:
            self._unresolve(target, 'unknown-reference-or-undeclared-bare-record', declared_in)
            return None
        if scope not in self.req['scopes']:
            self._unresolve(target, 'outside-requested-scopes', declared_in)
            return None
        if target not in self.entries:
            self._unresolve(target, 'absent-from-selected-inventory', declared_in)
            return None
        return target

    def _budget_bytes(self, ref, count):
        if type(count) is not int or count < 0:
            raise ValueError('source byte count must be a nonnegative integer')
        if self.examined_bytes + count > self.req['max_bytes']:
            self.saturated.add('body-byte-budget')
            self._skip(ref, 'body-byte-budget')
            return False
        self.examined_bytes += count
        return True

    def _inspect(self, ref):
        if ref in self.loaded:
            return self.loaded[ref]
        if ref in self.examined:
            return None
        if len(self.examined) >= self.req['scan_limit']:
            self.saturated.add('entry-scan-budget')
            self._skip(ref, 'entry-scan-budget')
            return None
        self.examined.add(ref)
        entry = self.entries[ref]
        scope, ident = entry['scope'], entry['id']
        if scope == 'records':
            byte_limit = self.records.size(ident)
            if not self._budget_bytes(ref, byte_limit):
                return None
            record, raw = self.records.read(ident, byte_limit=byte_limit)
            self.bodies[ref] = raw.decode('utf-8')
            self.body_info[ref] = {'bytes': len(raw), 'sha256': digest(raw), 'verified': True, 'text': True}
            if not self.req['include_inactive'] and record.get('active') is False:
                self._skip(ref, 'inactive-record')
                return None
            source = {'scope': scope, 'id': ident, 'format': record.get('format'),
                      'revision': record['revision'], 'record_sha256': record['sha256'],
                      'body_sha256': digest(raw), 'bytes': len(raw)}
            metadata = ' '.join(str(record.get(k, '')) for k in ('id', 'kind'))
            value = {'ref': ref, 'scope': scope, 'id': ident, 'source': source,
                     'title': ident, 'metadata': metadata, 'record': record}
        elif scope == 'project':
            row = entry['row']
            source = {'scope': scope, 'path': ident, 'sha256': row['sha256'], 'bytes': row['bytes'],
                      'selection': self.projects.current.get('selection')}
            value = {'ref': ref, 'scope': scope, 'id': ident, 'source': source,
                     'title': ident, 'metadata': ident}
        else:
            row = entry['row']
            source = {'scope': scope, 'id': ident, 'sha256': row['sha256'], 'bytes': row['bytes'],
                      'catalogue_snapshot': self.catalogue.get('snapshot'),
                      'source_root': row.get('source_root'), 'source_path': row.get('source_path')}
            metadata = ' '.join(str(row.get(k, '')) for k in ('id', 'title', 'kind', 'family', 'source_path', 'purpose'))
            value = {'ref': ref, 'scope': scope, 'id': ident, 'source': source,
                     'title': row.get('title', ident), 'metadata': metadata}
        self.loaded[ref] = value
        return value

    def _body(self, ref):
        if ref in self.bodies:
            return self.bodies[ref]
        if ref in self.body_failures:
            return None
        entry = self.entries[ref]
        row, scope = entry.get('row'), entry['scope']
        if scope == 'records':
            return None
        if scope == 'project' and row['bytes'] > PROJECT_CONTEXT_BYTES:
            self.body_failures[ref] = 'exceeds-project-context-body-bound'
            self._skip(ref, self.body_failures[ref])
            return None
        object_key = None
        if scope == 'techniques':
            if row.get('availability') == 'sealed' and ref not in self.req['anchors']:
                self.body_failures[ref] = 'sealed-source'
                self._skip(ref, 'sealed-source')
                return None
            # The first acquisition validates the physical body path and bytes.
            # A different path or binding must be acquired independently, even
            # when another entry advertises the same content hash.
            if not isinstance(row.get('body_path'), str) or not isinstance(row.get('sha256'), str):
                raise ValueError('catalogue body path and digest must be text')
            if type(row.get('bytes')) is not int or row['bytes'] < 0:
                raise ValueError('source byte count must be a nonnegative integer')
            object_key = (row['body_path'], row['sha256'], row['bytes'])
            if object_key in self.body_objects:
                self.reused_body_references += 1
                return self._retain_body(ref, *self.body_objects[object_key])
        if not self._budget_bytes(ref, row['bytes']):
            self.body_failures[ref] = 'body-byte-budget'
            return None
        if scope == 'project':
            raw = self.projects.body(row)
        else:
            raw = self.techniques.body(row, byte_limit=row['bytes'])
        if len(raw) != row['bytes'] or digest(raw) != row['sha256']:
            raise ValueError('discovery source binding changed: ' + ref)
        info = {'bytes': len(raw), 'sha256': digest(raw), 'verified': True, 'text': False}
        failure = None
        try:
            body = raw.decode('utf-8')
        except UnicodeDecodeError:
            body, failure = None, 'not-utf8-text'
        if body is not None and '\x00' in body:
            body, failure = None, 'contains-nul-not-context-text'
        info['text'] = failure is None
        if object_key is not None:
            self.body_objects[object_key] = (info, body, failure)
        return self._retain_body(ref, info, body, failure)

    def _retain_body(self, ref, info, body, failure):
        self.body_info[ref] = dict(info)
        if failure is not None:
            self.body_failures[ref] = failure
            self._skip(ref, failure)
            return None
        self.bodies[ref] = body
        return body

    def _candidate(self, item, match):
        ref = item['ref']
        if ref not in self.candidates:
            self.candidates[ref] = {k: item[k] for k in ('ref', 'title', 'source')}
            self.candidates[ref]['matches'] = []
        if match not in self.candidates[ref]['matches']:
            self.candidates[ref]['matches'].append(match)

    def _selector(self, candidate):
        ref = candidate['ref']
        entry = self.entries[ref]
        body = self._body(ref)
        if body is None:
            candidate['selector'] = None
            candidate['acquisition_needed'] = self.body_failures.get(ref, 'source-not-available-as-context-text')
        else:
            candidate['selector'] = {SELECTOR[entry['scope']]: [entry['id']]}
            candidate['body_identity'] = self.body_info[ref]
            candidate['acquisition_needed'] = None

    def _lexical_order(self):
        scopes = [self.by_scope[s] for s in self.req['scopes']]
        mode = self.req['scan_order']
        if mode == 'scope-round-robin':
            refs = [rows[index] for index in range(max(map(len, scopes), default=0))
                    for rows in scopes if index < len(rows)]
        else:
            refs = [ref for rows in scopes for ref in rows]
            if mode == 'seeded':
                # Stable across Python versions; independent of process-global
                # random state. Hash keys nominate order, never semantic merit.
                refs.sort(key=lambda ref: (
                    digest(canonical({'seed': self.req['seed'], 'ref': ref})), ref))
        self.lexical_nomination = {
            'policy': mode,
            'algorithm': ('sha256-canonical-seed-ref-sort/1' if mode == 'seeded'
                          else 'scope-round-robin/1' if mode == 'scope-round-robin'
                          else 'requested-scopes-sorted-identities/1'),
            'seed': self.req.get('seed'), 'entries': len(refs),
            'order_sha256': digest(canonical(refs)),
            'scope': ('Order nominates the full declared lexical inventory after anchors. '
                      'Entry examination, body acquisition and returned candidates remain separately bounded. '
                      'A fixed seed repeats the same order; changed membership can change its prefix. '
                      'Seeded hash sorting is pseudorandom exploration, not a uniform-sampling, '
                      'independent-trial, semantic-relevance or complete-gap-detection guarantee. '
                      'All policies enumerate metadata; seeded sorting costs O(N log N).'),
        }
        return refs

    def _lexical(self):
        terms = self.req['query'].casefold().split()
        for ref in self._lexical_order():
            item = self._inspect(ref)
            if item is None:
                continue
            metadata = item['metadata'].casefold()
            metadata_match = all(t in metadata for t in terms)
            body = None
            if terms and not metadata_match and self.req['content']:
                body = self._body(ref)
            searchable = metadata + ('\n' + body.casefold() if body is not None else '')
            if all(term in searchable for term in terms):
                self._candidate(item, {'kind': 'lexical' if terms else 'inventory', 'terms': terms,
                                       'channel': 'metadata' if metadata_match else 'metadata-and-complete-body'})

    def _record_edges(self, item):
        ref = item['ref']
        if ref in self.edge_cache:
            return self.edge_cache[ref]
        result = []
        excluded = set(self.req.get('exclude_relation_labels', ()))
        records_only = bool(self.req.get('records_only', False))
        record = item.get('record', {})
        for index, relation in enumerate(record.get('relations', [])):
            key = (ref, index)
            if key not in self.relation_rows:
                if len(self.relation_rows) >= self.req['scan_limit']:
                    self.saturated.add('relation-scan-budget')
                    self.frontier.append({'ref': ref, 'reason': 'relation-scan-budget', 'next_relation_index': index})
                    break
                self.relation_rows.add(key)
            if not isinstance(relation, dict) or not isinstance(relation.get('relation'), str):
                self._unresolve(str(relation), 'malformed-declared-relation', ref)
                continue
            label = relation['relation']
            if label in excluded:
                continue
            if self.req['relation_labels'] and label not in self.req['relation_labels']:
                continue
            raw_target = relation.get('target')
            if records_only:
                if not isinstance(raw_target, str) or not raw_target:
                    continue
                candidate_ref = raw_target if raw_target.startswith('record:') else ('record:' + raw_target if ':' not in raw_target else None)
                if candidate_ref is None or candidate_ref not in self.entries:
                    continue
                target = candidate_ref
            else:
                target = self._resolve(raw_target, ref)
                if target is None:
                    continue
            edge = {'from': ref, 'to': target, 'relation': label,
                    'declared_in': {'ref': ref, 'revision': item['source']['revision'],
                                    'record_sha256': item['source']['record_sha256']},
                    'properties': {k: v for k, v in relation.items() if k not in ('relation', 'target')}}
            result.append(edge)
            if key not in self.edge_keys:
                self.edge_keys.add(key)
                self.edges.append(edge)
        if self.req.get('include_content_links') and isinstance(record.get('content'), dict):
            from native_runtime.head_index import extract_content_record_links
            content_links = extract_content_record_links(record['content'])
            syn_index = len(record.get('relations', []))
            budget_hit = False
            for field_name, vals in sorted(content_links.items()):
                label = 'content:' + field_name
                if label in excluded or (self.req['relation_labels'] and label not in self.req['relation_labels']):
                    continue
                for val in vals:
                    target = 'record:' + val
                    if target not in self.entries:
                        continue
                    key = (ref, syn_index)
                    syn_index += 1
                    if key not in self.relation_rows:
                        if len(self.relation_rows) >= self.req['scan_limit']:
                            self.saturated.add('relation-scan-budget')
                            self.frontier.append({'ref': ref, 'reason': 'relation-scan-budget',
                                                  'next_relation_index': syn_index - 1})
                            budget_hit = True
                            break
                        self.relation_rows.add(key)
                    edge = {'from': ref, 'to': target, 'relation': label,
                            'declared_in': {'ref': ref, 'revision': item['source']['revision'],
                                            'record_sha256': item['source']['record_sha256']},
                            'properties': {'channel': 'content-link', 'field': field_name}}
                    result.append(edge)
                    if key not in self.edge_keys:
                        self.edge_keys.add(key)
                        self.edges.append(edge)
                if budget_hit:
                    break
        self.edge_cache[ref] = result
        return result

    def _incoming(self):
        if self.incoming is None:
            self.incoming = {}
            for ref in self.by_scope['records']:
                item = self._inspect(ref)
                if item:
                    for edge in self._record_edges(item):
                        self.incoming.setdefault(edge['to'], []).append(edge)
        return self.incoming

    def _traverse(self):
        queue = deque()
        visited = set()
        for anchor in self.req['anchors']:
            ref = self._resolve(anchor)
            if ref is not None:
                item = self._inspect(ref)
                if item is None:
                    self._unresolve(anchor, 'anchor-not-examined-or-inactive')
                    continue
                self._candidate(item, {'kind': 'anchor', 'requested': anchor})
                if ref not in visited:
                    queue.append((ref, 0))
                    visited.add(ref)
        while queue:
            ref, depth = queue.popleft()
            item = self.loaded[ref]
            if depth >= self.req['hops']:
                self.frontier.append({'ref': ref, 'reason': 'hop-bound', 'depth': depth,
                                      'scope': 'Not expanded; this does not assert that further relations exist.'})
                continue
            edges = []
            if self.req['direction'] in ('outgoing', 'both') and item['scope'] == 'records':
                edges.extend((e, e['to'], 'outgoing') for e in self._record_edges(item))
            if self.req['direction'] in ('incoming', 'both'):
                edges.extend((e, e['from'], 'incoming') for e in self._incoming().get(ref, []))
            for edge, target, direction in edges:
                other = self._inspect(target)
                if other is None:
                    self._unresolve(target, 'endpoint-not-examined-or-inactive', edge['from'])
                    continue
                self._candidate(other, {'kind': 'declared-relation', 'edge': edge,
                                        'direction': direction, 'depth': depth + 1})
                if target not in visited:
                    visited.add(target)
                    queue.append((target, depth + 1))
