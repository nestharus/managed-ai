#!/usr/bin/env python3
"""Focused discovery checks using isolated caller-owned fixture runtimes.

These checks cover source identity, scoped retrieval and context handoff. They do
not measure semantic relevance, agent reading, useful uptake or broad coverage.
"""
from __future__ import annotations

if __package__:
    from ..native_test_paths import APP, ENGINE, PRODUCT
else:
    from pathlib import Path as _Path
    import sys as _sys
    _repo = str(_Path(__file__).resolve().parents[3])
    if _repo not in _sys.path:
        _sys.path.insert(0, _repo)
    from tests.store.native_test_paths import APP, ENGINE, PRODUCT
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from managed_ai.store.query.context_discovery import Discovery
from managed_ai.store.system import System


def sha(body):
    return hashlib.sha256(body).hexdigest()


class DiscoveryChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='agent-discovery-check-')
        self.base = Path(self.temporary.name)
        self.root = self.base / 'runtime'
        System.initialize(self.root, {'contribution': 'Use the actual current representation guidance.'})
        self.system = System(self.root)
        self.project = self.base / 'project'
        self.project.mkdir()
        (self.project / 'current-note.md').write_text('Current receiver migration note with newly accepted information.\n')
        (self.project / 'chart.png').write_bytes(b'\x89PNG\r\n\x00\xff')
        self.system.deployment = {
            'project_root': str(self.project), 'project_files': [],
            'selection': {'edition': 'fixture-current', 'selection_id': 'selection-one',
                          'manifest_sha256': 'fixture-manifest-one'},
        }
        self.bind_project()
        old = b'Historical explanation before the later receiving task.\n'
        self.knowledge = {'id': 'document:older.md', 'title': 'Earlier explanation',
                          'kind': 'source-document', 'family': 'fixture',
                          'source_path': 'older.md', 'source_root': 'fixture-predecessor',
                          'sha256': sha(old), 'bytes': len(old), 'body_path': 'unused.txt'}
        self.system.techniques.index = lambda: {
            'format': 'agent-system-catalogue/1',
            'snapshot': {'edition': 'fixture-predecessor'}, 'entries': [self.knowledge],
        }
        self.system.techniques.body = lambda entry, **kwargs: old

    def tearDown(self):
        self.temporary.cleanup()

    def bind_project(self):
        self.system.deployment['project_files'] = [
            {'path': p.relative_to(self.project).as_posix(), 'bytes': p.stat().st_size,
             'sha256': sha(p.read_bytes()), 'mode': 0o644}
            for p in sorted(self.project.rglob('*')) if p.is_file()
        ]

    def record(self, ident, targets=(), active=True):
        return self.system.put({'id': ident, 'kind': 'landmark', 'content': {'concern': ident},
                                'relations': [{'relation': 'may-help', 'target': t,
                                               'status': 'proposed', 'reason': 'Fixture declared association'}
                                              for t in targets], 'active': active}, 0)

    def test_current_source_absent_from_frozen_repertoire_reaches_context(self):
        result = Discovery(self.system).search({
            'query': 'receiver migration', 'content': True, 'scopes': ['project', 'techniques'],
        })
        self.assertEqual([c['ref'] for c in result['candidates']], ['project:current-note.md'])
        self.assertEqual(result['context_request'], {'project_sources': ['current-note.md']})
        source = result['candidates'][0]['source']
        self.assertEqual(source['selection']['selection_id'], 'selection-one')
        self.assertEqual(source['sha256'], sha((self.project / 'current-note.md').read_bytes()))
        self.assertEqual(result['coverage']['catalogue_snapshot'], {'edition': 'fixture-predecessor'})
        context = self.system.context('discovery-use', dict(result['context_request'], task='Read the discovered current note.'))
        received = Path(context['context']).read_text()
        self.assertIn('Current receiver migration note with newly accepted information.', received)
        self.assertNotIn('Historical explanation before the later receiving task.', received)

    def test_authored_cycle_is_bounded_and_labels_remain_proposals(self):
        self.record('entry', ['neighbor', 'project:current-note.md'])
        self.record('neighbor', ['entry'])
        result = Discovery(self.system).search({'anchors': ['entry'], 'scopes': ['records', 'project'], 'hops': 6})
        refs = [c['ref'] for c in result['candidates']]
        self.assertEqual(len(refs), len(set(refs)))
        self.assertEqual(set(refs), {'record:entry', 'record:neighbor', 'project:current-note.md'})
        relation = next(m for c in result['candidates'] for m in c['matches'] if m['kind'] == 'declared-relation')
        self.assertEqual(relation['edge']['relation'], 'may-help')
        self.assertEqual(relation['edge']['properties']['status'], 'proposed')
        self.assertEqual(result['coverage']['examined_relation_rows'], 3)

    def test_inactive_and_unknown_endpoints_are_explicit(self):
        self.record('entry', ['parked', 'record:missing', 'invented endpoint'])
        self.record('parked', active=False)
        result = Discovery(self.system).search({'anchors': ['record:entry'], 'scopes': ['records'], 'hops': 2})
        self.assertEqual([c['ref'] for c in result['candidates']], ['record:entry'])
        self.assertIn({'ref': 'record:parked', 'reason': 'inactive-record'}, result['coverage']['skipped'])
        unresolved = {r['target']: r['reason'] for r in result['coverage']['unresolved_references']}
        self.assertEqual(unresolved['record:missing'], 'absent-from-selected-inventory')
        self.assertIn('invented endpoint', unresolved)
        widened = Discovery(self.system).search({'anchors': ['record:entry'], 'scopes': ['records'],
                                                 'include_inactive': True, 'hops': 2})
        self.assertIn('record:parked', [c['ref'] for c in widened['candidates']])

    def test_result_limit_scan_limit_and_byte_limit_have_different_evidence(self):
        self.record('entry', ['neighbor', 'record:goal'])
        self.record('neighbor')
        shown = Discovery(self.system).search({'anchors': ['record:entry'], 'scopes': ['records'], 'hops': 2, 'limit': 1})
        self.assertEqual(len(shown['candidates']), 1)
        self.assertEqual(set(shown['coverage']['omitted_candidate_refs']), {'record:neighbor', 'record:goal'})
        self.assertNotIn('entry-scan-budget', shown['coverage']['saturated'])
        bounded = Discovery(self.system).search({'anchors': ['record:entry'], 'scopes': ['records'], 'hops': 2, 'scan_limit': 1})
        self.assertEqual(bounded['coverage']['examined_entries'], 1)
        self.assertIn('entry-scan-budget', bounded['coverage']['saturated'])
        self.assertGreater(bounded['coverage']['unexamined_count'], 0)
        tiny = Discovery(self.system).search({'query': 'current-note', 'scopes': ['project'], 'max_bytes': 1})
        self.assertEqual(tiny['candidates'][0]['ref'], 'project:current-note.md')
        self.assertIsNone(tiny['candidates'][0]['selector'])
        self.assertEqual(tiny['context_request'], {})
        self.assertEqual(tiny['coverage']['examined_body_bytes'], 0)
        self.assertIn('body-byte-budget', tiny['coverage']['saturated'])

    def test_shared_source_fits_once_without_collapsing_reference_meanings(self):
        shared = b'Needle in a shared complete source body.\n'
        other = b'Needle elsewhere.\n'
        entries = [
            dict(self.knowledge, id='document:a', title='First use', body_path='shared.txt',
                 sha256=sha(shared), bytes=len(shared)),
            dict(self.knowledge, id='document:b', title='Another use', body_path='shared.txt',
                 sha256=sha(shared), bytes=len(shared)),
            dict(self.knowledge, id='document:c', body_path='other.txt',
                 sha256=sha(other), bytes=len(other)),
        ]
        self.system.techniques.index = lambda: {'entries': entries}
        calls = []
        def acquire(entry, *, byte_limit=None):
            self.assertEqual(byte_limit, entry['bytes'])
            calls.append(entry['body_path'])
            return {'shared.txt': shared, 'other.txt': other}[entry['body_path']]
        self.system.techniques.body = acquire
        result = Discovery(self.system).search({
            'query': 'needle', 'content': True, 'scopes': ['techniques'],
            'max_bytes': len(shared) + len(other),
        })
        self.assertEqual(result['context_request']['techniques'], ['document:a', 'document:b', 'document:c'])
        self.assertEqual([c['title'] for c in result['candidates'][:2]], ['First use', 'Another use'])
        self.assertEqual(calls, ['shared.txt', 'other.txt'])
        coverage = result['coverage']
        self.assertEqual(coverage['examined_body_bytes'], len(shared) + len(other))
        self.assertEqual(coverage['examined_body_reference_bytes'], 2 * len(shared) + len(other))
        self.assertEqual(coverage['examined_body_sources'], 2)
        self.assertEqual(coverage['reused_body_references'], 1)
        self.assertEqual(coverage['skipped'], [])
        self.assertNotEqual(result['candidates'][0]['source']['id'], result['candidates'][1]['source']['id'])

    def test_equal_digest_on_another_path_does_not_bypass_that_source(self):
        body = b'Original complete source.\n'
        entries = [
            dict(self.knowledge, id='document:a', body_path='present.txt', sha256=sha(body), bytes=len(body)),
            dict(self.knowledge, id='document:b', body_path='changed.txt', sha256=sha(body), bytes=len(body)),
        ]
        self.system.techniques.index = lambda: {'entries': entries}
        self.system.techniques.body = lambda entry, **kwargs: body if entry['body_path'] == 'present.txt' else b'changed'
        with self.assertRaisesRegex(ValueError, 'source binding changed: technique:document:b'):
            Discovery(self.system).search({'query': 'absent', 'content': True, 'scopes': ['techniques']})

    def test_shared_path_with_incompatible_declared_binding_is_not_reused(self):
        body = b'Original complete source.\n'
        original = dict(self.knowledge, id='document:a', sha256=sha(body), bytes=len(body))
        for changed in ({'bytes': len(body) - 1}, {'sha256': sha(b'changed')}):
            with self.subTest(changed=changed):
                entries = [original, dict(original, id='document:b', **changed)]
                self.system.techniques.index = lambda: {'entries': entries}
                self.system.techniques.body = lambda entry, **kwargs: body
                with self.assertRaisesRegex(ValueError, 'source binding changed: technique:document:b'):
                    Discovery(self.system).search({'query': 'absent', 'content': True, 'scopes': ['techniques']})

    def test_body_observation_is_reacquired_on_the_next_query(self):
        body = b'Original complete source.\n'
        self.knowledge.update(sha256=sha(body), bytes=len(body))
        self.system.techniques.body = lambda entry, **kwargs: body
        discovery = Discovery(self.system)
        request = {'query': 'original', 'content': True, 'scopes': ['techniques']}
        self.assertEqual(len(discovery.search(request)['candidates']), 1)
        self.system.techniques.body = lambda entry, **kwargs: b'changed after the first query'
        with self.assertRaisesRegex(ValueError, 'source binding changed'):
            discovery.search(request)

    def test_shared_binary_observation_keeps_each_unavailable_text_route(self):
        body = b'\xff\x00'
        entries = [dict(self.knowledge, id=ident, sha256=sha(body), bytes=len(body))
                   for ident in ('document:a', 'document:b')]
        self.system.techniques.index = lambda: {'entries': entries}
        self.system.techniques.body = lambda entry, **kwargs: body
        result = Discovery(self.system).search({'scopes': ['techniques'], 'max_bytes': len(body)})
        self.assertEqual(len(result['candidates']), 2)
        self.assertEqual(result['context_request'], {})
        self.assertEqual([c['acquisition_needed'] for c in result['candidates']], ['not-utf8-text'] * 2)
        self.assertEqual(result['coverage']['examined_body_bytes'], len(body))
        self.assertEqual(result['coverage']['examined_body_reference_bytes'], 2 * len(body))
        self.assertEqual(result['coverage']['reused_body_references'], 1)

    def test_scope_round_robin_reaches_a_later_scope_without_changing_default(self):
        for index in range(12):
            (self.project / f'a{index:02d}.md').write_text('Ordinary project material.\n')
        self.bind_project()
        self.knowledge['title'] = 'Distinctive technical guide'
        request = {'query': 'technical', 'scopes': ['project', 'techniques'], 'scan_limit': 3}
        before = {p.relative_to(self.base): sha(p.read_bytes())
                  for p in self.base.rglob('*') if p.is_file()}
        ordinary = Discovery(self.system).search(request)
        spread = Discovery(self.system).search(dict(request, scan_order='scope-round-robin'))
        self.assertEqual(ordinary['candidates'], [])
        self.assertEqual(ordinary['coverage']['examined_entries_by_scope'], {'project': 3, 'techniques': 0})
        self.assertEqual([c['ref'] for c in spread['candidates']], ['technique:document:older.md'])
        self.assertEqual(spread['coverage']['examined_entries_by_scope'], {'project': 2, 'techniques': 1})
        self.assertEqual(spread['coverage']['examined_entries'], 3)
        self.assertTrue(spread['candidates'][0]['body_identity']['verified'])
        after = {p.relative_to(self.base): sha(p.read_bytes())
                 for p in self.base.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_seeded_nomination_is_repeatable_changes_scope_and_keeps_exact_pins(self):
        entries = [dict(self.knowledge, id=f'document:{i:02d}') for i in range(16)]
        self.system.techniques.index = lambda: {'entries': entries}
        request = {'scopes': ['techniques'], 'scan_limit': 3, 'limit': 3, 'scan_order': 'seeded', 'seed': 0}
        first = Discovery(self.system).search(request)
        repeated = Discovery(self.system).search(request)
        other = Discovery(self.system).search(dict(request, seed=1))
        self.assertEqual(first, repeated)
        self.assertNotEqual([c['ref'] for c in first['candidates']], [c['ref'] for c in other['candidates']])
        self.assertNotEqual(first['coverage']['lexical_nomination']['order_sha256'],
                            other['coverage']['lexical_nomination']['order_sha256'])
        self.assertEqual(first['coverage']['examined_entries'], 3)
        self.assertEqual(first['coverage']['unexamined_count'], 13)
        for result in (first, other):
            pins = [{'ref': c['ref'], 'source': c['source']} for c in result['candidates']]
            verified = Discovery(self.system).verify_selection(pins)
            self.assertEqual(verified['verified'], pins)

    def test_seeded_order_preserves_anchor_priority_and_separate_bounds(self):
        body = b'Complete source for a nominated route.\n'
        entries = [dict(self.knowledge, id=f'document:{i:02d}', title='Selected route',
                        body_path=f'{i:02d}.txt', sha256=sha(body), bytes=len(body))
                   for i in range(16)]
        self.system.techniques.index = lambda: {'entries': entries}
        self.system.techniques.body = lambda entry, **kwargs: body
        result = Discovery(self.system).search({
            'query': 'selected', 'scopes': ['techniques'], 'anchors': ['technique:document:15'],
            'hops': 0, 'scan_limit': 3, 'limit': 1, 'max_bytes': len(body),
            'scan_order': 'seeded', 'seed': 0})
        self.assertEqual([c['ref'] for c in result['candidates']], ['technique:document:15'])
        self.assertEqual(result['coverage']['examined_entries'], 3)
        self.assertEqual(result['coverage']['matched_candidates'], 3)
        self.assertEqual(len(result['coverage']['omitted_candidate_refs']), 2)
        self.assertEqual(result['coverage']['examined_body_bytes'], len(body))
        self.assertEqual(result['coverage']['examined_body_sources'], 1)
        limited = Discovery(self.system).search({
            'scopes': ['techniques'], 'scan_limit': 3, 'limit': 3,
            'max_bytes': len(body)-1, 'scan_order': 'seeded', 'seed': 0})
        self.assertEqual(limited['coverage']['examined_entries'], 3)
        self.assertEqual(limited['coverage']['examined_body_sources'], 0)
        self.assertEqual(limited['context_request'], {})
        self.assertTrue(all(c['acquisition_needed'] == 'body-byte-budget' for c in limited['candidates']))

    def test_seeded_scan_requires_an_explicit_valid_seed(self):
        invalid = [{'scan_order': 'seeded'}, {'scan_order': 'random'}, {'seed': 0},
                   {'scan_order': 'scope-round-robin', 'seed': 0}]
        invalid += [{'scan_order': 'seeded', 'seed': seed}
                    for seed in (True, -1, 2**64, 0.5, '0', None)]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                Discovery(self.system).search(request)
        for seed in (0, 2**64 - 1):
            result = Discovery(self.system).search({'scopes': ['techniques'], 'scan_order': 'seeded', 'seed': seed})
            self.assertEqual(result['coverage']['lexical_nomination']['seed'], seed)

    def test_returned_request_can_be_replayed_for_every_scan_policy(self):
        base = {'query': 'historical', 'content': True, 'scopes': ['techniques']}
        for supplied in (base, dict(base, scan_order='inventory'),
                         dict(base, scan_order='scope-round-robin'),
                         dict(base, scan_order='seeded', seed=0)):
            with self.subTest(supplied=supplied):
                first = Discovery(self.system).search(supplied)
                self.assertEqual('seed' in first['request'], supplied.get('scan_order') == 'seeded')
                replayed = Discovery(self.system).search(first['request'])
                self.assertEqual(replayed, first)

    def test_changed_source_requires_new_binding_and_old_result_stays_exact(self):
        first = Discovery(self.system).search({'anchors': ['project:current-note.md'], 'scopes': ['project'], 'hops': 0})
        captured = json.dumps(first, sort_keys=True)
        (self.project / 'current-note.md').write_text('A later edition changes the receiving explanation.\n')
        with self.assertRaisesRegex(ValueError, 'source binding changed'):
            Discovery(self.system).search({'anchors': ['project:current-note.md'], 'scopes': ['project'], 'hops': 0})
        self.bind_project()
        self.system.deployment['selection'] = {'edition': 'fixture-next', 'selection_id': 'selection-two',
                                                'manifest_sha256': 'fixture-manifest-two'}
        second = Discovery(self.system).search({'anchors': ['project:current-note.md'], 'scopes': ['project'], 'hops': 0})
        self.assertNotEqual(first['source_bindings'], second['source_bindings'])
        self.assertEqual(json.dumps(first, sort_keys=True), captured)

    def test_binary_and_oversized_project_entries_do_not_offer_text_selectors(self):
        (self.project / 'large.txt').write_bytes(b'a' * (2 * 1024 * 1024 + 1))
        self.bind_project()
        result = Discovery(self.system).search({'anchors': ['project:chart.png', 'project:large.txt'],
                                                'scopes': ['project'], 'hops': 0})
        self.assertEqual(len(result['candidates']), 2)
        self.assertEqual(result['context_request'], {})
        reasons = {c['ref']: c['acquisition_needed'] for c in result['candidates']}
        self.assertEqual(reasons['project:chart.png'], 'not-utf8-text')
        self.assertEqual(reasons['project:large.txt'], 'exceeds-project-context-body-bound')

    def test_scope_type_guards_and_no_match_do_not_claim_absence(self):
        for request in ({'max_bytes': True}, {'limit': 0}, {'scan_limit': 1.5}, {'content': 1},
                        {'anchors': ['a', 'a']}, {'unknown': 1}, {'scopes': []}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                Discovery(self.system).search(request)
        missed = Discovery(self.system).search({'query': 'not-present-in-fixture', 'scopes': ['project']})
        self.assertEqual(missed['candidates'], [])
        self.assertIn('outside this search', missed['coverage']['scope'])
        self.system.deployment = None
        with self.assertRaisesRegex(ValueError, 'explicitly selected deployment'):
            Discovery(self.system).search({'scopes': ['project']})
        default = Discovery(self.system).search({})
        self.assertEqual(default['coverage']['unavailable_default_scopes'], ['project'])
        self.assertNotIn('project', default['coverage']['requested_scopes'])

    def test_incoming_routes_retain_the_original_asserting_record(self):
        self.record('entry', ['project:current-note.md'])
        result = Discovery(self.system).search({'anchors': ['project:current-note.md'],
                                                'scopes': ['project', 'records'], 'direction': 'incoming', 'hops': 1})
        reached = next(c for c in result['candidates'] if c['ref'] == 'record:entry')
        match = next(m for m in reached['matches'] if m['kind'] == 'declared-relation')
        self.assertEqual(match['direction'], 'incoming')
        self.assertEqual(match['edge']['declared_in']['ref'], 'record:entry')
        self.assertEqual(match['edge']['from'], 'record:entry')
        self.assertEqual(match['edge']['to'], 'project:current-note.md')

    def test_selection_pins_refuse_record_or_edition_drift_before_context(self):
        self.record('entry')
        discovery = Discovery(self.system)
        result = discovery.search({'anchors': ['record:entry', 'project:current-note.md'],
                                   'scopes': ['records', 'project'], 'hops': 0})
        pins = [{'ref': c['ref'], 'source': c['source']} for c in result['candidates']]
        verified = discovery.verify_selection(pins)
        self.assertEqual(verified['verified'], pins)
        self.assertEqual(verified['context_request'], {'records': ['entry'], 'project_sources': ['current-note.md']})
        self.system.put({'id': 'entry', 'kind': 'landmark', 'content': {'concern': 'changed interpretation'}}, 1)
        with self.assertRaisesRegex(ValueError, 'source identity changed: record:entry'):
            discovery.verify_selection(pins)
        project_pin = [p for p in pins if p['ref'].startswith('project:')]
        self.system.deployment['selection']['selection_id'] = 'selection-two'
        self.assertEqual(project_pin[0]['source']['selection']['selection_id'], 'selection-one')
        with self.assertRaisesRegex(ValueError, 'source identity changed: project:current-note.md'):
            discovery.verify_selection(project_pin)
        self.assertEqual(discovery.verify_selection([]), {'verified': [], 'context_request': {}})
        with self.assertRaisesRegex(ValueError, 'duplicate source pin'):
            discovery.verify_selection(project_pin + project_pin)

    def test_context_pins_guard_actual_selection_and_preserve_original_recovery(self):
        self.record('earlier-item')
        self.record('selected-item')
        result = Discovery(self.system).search({
            'anchors': ['record:selected-item', 'project:current-note.md'],
            'scopes': ['records', 'project'], 'hops': 0,
        })
        pins = [{'ref': c['ref'], 'source': c['source']} for c in result['candidates']]
        record_pin = next(p for p in pins if p['ref'] == 'record:selected-item')
        with self.assertRaisesRegex(ValueError, 'pinned discovery body is not selected'):
            self.system.context('pin-outside-record-limit', {
                'records': ['earlier-item', 'selected-item'], 'record_limit': 1,
                'source_pins': [record_pin],
            })
        self.assertFalse((self.system.work / '.system/turns/pin-outside-record-limit').exists())
        project_pin = next(p for p in pins if p['ref'] == 'project:current-note.md')
        with self.assertRaisesRegex(ValueError, 'pinned discovery body is not selected'):
            self.system.context('pin-without-project-body', {'source_pins': [project_pin]})
        request = dict(result['context_request'], source_pins=pins, task='Use these exact discovered bodies.')
        prepared = self.system.context('pinned-current', request)
        original = Path(prepared['context']).read_bytes().decode('utf-8')
        self.assertIn('Current receiver migration note with newly accepted information.', original)
        self.assertEqual(self.system.view('pinned-current')['source_pins'], pins)
        self.assertEqual(self.system.recover('pinned-current')['context'], original)
        self.system.put({'id': 'selected-item', 'kind': 'landmark',
                         'content': {'concern': 'A later changed interpretation'}}, 1)
        with self.assertRaisesRegex(ValueError, 'source identity changed: record:selected-item'):
            self.system.context('stale-pinned-current', request)
        self.assertFalse((self.system.work / '.system/turns/stale-pinned-current').exists())
        self.assertEqual(self.system.recover('pinned-current')['context'], original)
        self.assertEqual(self.system.view('pinned-current')['source_pins'], pins)

    def test_p9_7_head_index_v3_discovery_parity_and_domain_api_view(self):
        """P9-7 & P9-4: HeadIndex v3 (exact-base + delta) discovery matches unindexed discovery identically."""
        from managed_ai.deployment.identity import code_identity
        from managed_ai.host.continuing_work.capture_native import _engine_code_identity
        from managed_ai.store import domain_api

        # P9-4: parallelized code_identity matches capture_native._engine_code_identity byte-for-byte.
        self.assertEqual(code_identity(ENGINE), _engine_code_identity(ENGINE))

        self.record('alpha', ['beta', 'record:gamma', 'project:current-note.md'])
        self.record('beta', ['gamma'])
        self.system.put({
            'id': 'gamma',
            'kind': 'claim',
            'content': {'summary': 'Distinctive quantum cadence finding', 'parent_id': 'alpha'},
            'relations': [{'relation': 'supports', 'target': 'alpha'}],
            'active': True,
        }, 0)
        self.record('dormant', ['alpha'], active=False)

        queries = [
            {'query': 'claim', 'content': False, 'scopes': ['records']},
            {'query': 'quantum cadence', 'content': True, 'scopes': ['records']},
            {'scopes': ['records', 'project'], 'scan_order': 'scope-round-robin'},
            {'scopes': ['records'], 'scan_order': 'seeded', 'seed': 42},
            {'anchors': ['record:alpha'], 'scopes': ['records', 'project'], 'direction': 'both', 'hops': 2},
            {'anchors': ['project:current-note.md'], 'scopes': ['records', 'project'], 'direction': 'incoming', 'hops': 1},
            {'anchors': ['record:alpha'], 'scopes': ['records'], 'direction': 'incoming', 'hops': 1, 'include_content_links': True},
            {'anchors': ['record:alpha'], 'scopes': ['records'], 'direction': 'both', 'hops': 2, 'include_inactive': True},
        ]

        # 1. Baseline unindexed discovery results (no HeadIndex present).
        self.assertFalse(self.system.record_store.index.exists())
        unindexed_results = [Discovery(self.system).search(q) for q in queries]

        # 2. Build HeadIndex v3 and verify public domain_api.open_verified_index_view works & closes cleanly.
        self.system.record_store.reindex()
        with domain_api.open_verified_index_view(self.system.record_store) as verified:
            self.assertIsNotNone(verified)
            self.assertIsInstance(verified, domain_api.VerifiedIndexView)
            self.assertTrue(verified.is_exact_base)

        indexed_results = [Discovery(self.system).search(q) for q in queries]
        for idx, (q, expected, actual) in enumerate(zip(queries, unindexed_results, indexed_results)):
            with self.subTest(mode='exact-base', idx=idx, query=q):
                if q.get('direction') in ('incoming', 'both'):
                    # Reverse CSR inspects only incoming sources rather than scanning every unrelated record.
                    self.assertEqual(actual['candidates'], expected['candidates'])
                    self.assertEqual(actual['context_request'], expected['context_request'])
                    self.assertEqual(actual['source_bindings'], expected['source_bindings'])
                else:
                    self.assertEqual(actual, expected)

        # 3. Mutate via delta sidecar (without full reindex) and confirm exact parity with a freshly unindexed scan!
        self.system.put({
            'id': 'delta-link',
            'kind': 'claim',
            'content': {'summary': 'Fresh delta quantum note', 'ref_id': 'beta'},
            'relations': [{'relation': 'cites', 'target': 'gamma'}],
            'active': True,
        }, 0)
        delta_queries = [
            {'query': 'delta quantum', 'content': True, 'scopes': ['records']},
            {'anchors': ['record:gamma'], 'scopes': ['records'], 'direction': 'incoming', 'hops': 1},
            {'anchors': ['record:beta'], 'scopes': ['records'], 'direction': 'incoming', 'hops': 1, 'include_content_links': True},
        ]
        delta_indexed = [Discovery(self.system).search(q) for q in delta_queries]

        # Remove HeadIndex files to compare against pure unindexed discovery on the exact same store state.
        sys_dir = self.system.ex.path('.system')
        for name in ('head-index.current.json', 'head-index.delta.jsonl'):
            (sys_dir / name).unlink(missing_ok=True)
        for gen_file in sys_dir.glob('head-index.gen*.bin'):
            gen_file.unlink(missing_ok=True)

        delta_unindexed = [Discovery(self.system).search(q) for q in delta_queries]
        for idx, (q, expected, actual) in enumerate(zip(delta_queries, delta_unindexed, delta_indexed)):
            with self.subTest(mode='v3-delta', idx=idx, query=q):
                if q.get('direction') in ('incoming', 'both'):
                    self.assertEqual(actual['candidates'], expected['candidates'])
                    self.assertEqual(actual['context_request'], expected['context_request'])
                    self.assertEqual(actual['source_bindings'], expected['source_bindings'])
                else:
                    self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
