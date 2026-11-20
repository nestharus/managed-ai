#!/usr/bin/env python3
"""Focused checks for the app's composition boundaries, using external state.

The replies below are explicitly authored fixtures. They verify transport and
guards, not an independent agent's semantic competence or whole-goal success.
"""
from __future__ import annotations

import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

API = runpy.run_path(str(Path(__file__).with_name('system.py')))
System = API['System']
exclusive = API['exclusive']
sha = API['sha']


class SystemChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='agent-system-check-')
        self.base = Path(self.temporary.name)
        self.root = self.base / 'runtime'
        self.initial = System.initialize(self.root, {
            'contribution': 'Exercise the actual app boundaries and retain unresolved duties.',
            'obligations': [{'id': 'report', 'text': 'Report the actual limits of these checks.'}]})
        self.system = System(self.root)
        self.work = self.system.work
        self.lock = exclusive(self.system.ex.root / 'owner.lock')
        self.lock.__enter__()

    def tearDown(self):
        self.lock.__exit__(None, None, None)
        self.temporary.cleanup()

    def write(self, relative, value):
        path = self.work / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')
        return path

    def fixture(self, name, value):
        path = self.base / name
        path.write_text(json.dumps(value) if isinstance(value, dict) else value, encoding='utf-8')
        return path

    def proposal(self, turn, target='outputs/result.txt'):
        reply = self.fixture(turn + '-reply.txt', 'Caller-authored integration fixture: useful result.')
        proposal = self.fixture(turn + '-proposal.txt', 'Retained proposed contribution from ' + turn)
        return self.system.receive(turn, reply, 'caller-authored integration fixture',
                                   [target + '=' + str(proposal)])

    def test_recomposition_rejects_different_basis_captured_by_put(self):
        from composition import Composition
        from composition_revision import revise
        from native_runtime.identities import record_identity

        source = self.write('sources/contract', 'Both obligations apply.')
        profile = {'format': 'g60-composition/1', 'purpose': 'Check an allocation.'}
        self.system.put({'id': 'plan', 'kind': 'note', 'content': profile, 'basis': ['sources/contract']}, 0)
        target = record_identity(self.system.show('plan'))
        goal = record_identity(self.system.show('goal'))
        pin = dict(instance_id=self.system.instance_id, **goal, pointer='/content/contribution')
        proposal = dict(format='g60-recomposition/1', instance_id=self.system.instance_id, target=target,
                        fields={'purpose': 'Preserve each named obligation.'}, rows={}, sources=[pin],
                        bounds={'scan_limit': 1, 'max_bytes': 8192}, judgment='Compare the complete allocation.')
        review = revise(self.system, proposal)
        original_projection = Composition.inspect

        def change_after_projection(instance, *args, **options):
            projection = original_projection(instance, *args, **options)
            source.write_text('One obligation applies.', encoding='utf-8')
            return projection

        with patch.object(Composition, 'inspect', change_after_projection):
            with self.assertRaisesRegex(ValueError, 'basis changed at publication'):
                revise(self.system, proposal, accept=review['review_sha256'], reason='Use the contract.')
        self.assertEqual(record_identity(self.system.show('plan')), target)
        self.assertFalse((self.system.ex.objects.directory / sha(source.read_bytes())).exists())
        source.write_text('Both obligations apply.', encoding='utf-8')
        accepted = revise(self.system, proposal, accept=review['review_sha256'], reason='Use the contract.')
        self.assertEqual(self.system.show('plan')['basis'][0]['sha256'], sha(source.read_bytes()))
        self.assertEqual(accepted['head']['revision'], 2)

    def test_selected_receiver_reuses_one_guarded_read_and_pays_both_charges(self):
        from composition_revision import revise
        from native_runtime.identities import record_identity

        store = self.system.record_store
        target = self.system.put(dict(id='plan', kind='note', content=dict(
            format='g60-composition/1', purpose='Check the selected receiver.')), 0)
        target = record_identity(target)
        goal = record_identity(store.show('goal'))
        pin = dict(goal, instance_id=self.system.instance_id, pointer='/content')
        size = store.size('goal')
        q = dict(format='g60-recomposition/1', instance_id=self.system.instance_id, target=target,
                 fields={}, rows={}, sources=[pin, dict(pin, pointer='/kind')], consumer_scope=['goal'],
                 bounds=dict(scan_limit=2, max_bytes=2 * size), judgment='Check both uses of the goal.')
        with patch.object(store, 'read', wraps=store.read) as reads:
            review = revise(self.system, q)
            self.assertEqual([call.args[0] for call in reads.call_args_list], ['plan', 'goal'])
        self.assertEqual(review['read_set']['native_bytes'], size)
        self.assertEqual(review['read_set']['native_heads'], 1)
        self.assertEqual(review['consumer_scope'], dict(examined=[goal], bytes=size, candidates=[]))
        for bounds in (dict(scan_limit=1, max_bytes=2 * size), dict(scan_limit=2, max_bytes=2 * size - 1)):
            with self.assertRaisesRegex(ValueError, 'scan limit|byte bound'):
                revise(self.system, dict(q, bounds=bounds))
        self.system.put(dict(id='goal', kind='goal', content=dict(contribution='A later goal.')), goal['revision'])
        with self.assertRaisesRegex(ValueError, 'native evidence head revision conflict'):
            revise(self.system, q, accept=review['review_sha256'], reason='Use the checked goal.')

    def test_delayed_consumer_uses_only_a_verified_selected_capture(self):
        from composition_revision import revise
        from native_runtime.identities import record_identity
        from native_runtime.loading import Loading

        store = self.system.record_store
        def put(ident, content, rev=0, basis=()):
            self.system.put(dict(id=ident, kind='note', content=content, basis=list(basis)), rev)
            return record_identity(store.show(ident))

        def pin(head, path):
            return dict(head, instance_id=self.system.instance_id, pointer=path)

        def element(ident, value):
            return dict(id=ident, account=ident, content=value)

        original = dict(format='g60-composition/1', purpose='Check the grant.',
                        elements=[element('grant', True), element('other', False)])
        first = put('service', original)
        holder1 = put('first-capture', {}, basis=['records/service.json'])
        old = put('service', original, 1)
        holder2 = put('second-capture', {}, basis=['records/service.json'])
        transfer_path = self.base / 'service-transfer.json'
        Loading(self.system).export('service', transfer_path)
        selected = pin(holder2, '/basis/0')
        path = '/content/elements/0/content'
        use = pin(old, path)
        consumer = dict(original, elements=[element('use', {'composition_use': {'sources': [use]}})])
        put('reader', consumer)
        new = put('service', dict(original, elements=[element('other', True), element('grant', False)]), 2)
        q = dict(format='g60-recomposition/1', instance_id=self.system.instance_id, target=new,
                 fields={}, rows={}, sources=[selected], consumer_scope=['reader'],
                 bounds=dict(scan_limit=3, max_bytes=100000), judgment='Compare the earlier grant.')
        def binding(req):
            return revise(self.system, req)['consumer_scope']['candidates'][0]['bindings'][0]

        review = revise(self.system, q)
        scope = review['consumer_scope']
        item, = scope['retained_objects']
        self.assertEqual(item['record'], old)
        found = binding(q)
        self.assertEqual((found['source_row'], found['proposed_pointer'], found['status']),
                         ('element:grant', '/content/elements/1/content', 'relocated-and-changed'))
        self.assertEqual(found['basis_source'], dict(holder2, pointer='/basis/0'))
        self.assertEqual(binding(dict(q, rows={'element:grant': None}))['status'], 'removed')
        self.assertEqual(binding(dict(q, sources=[pin(holder1, '/basis/0')]))['reason'],
                         'captured basis holds a different record edition')
        self.assertEqual(binding(dict(q, sources=[pin(holder2, '/content')]))['reason'],
                         'no selected basis or native transfer supplies this local record edition')
        ambiguous = dict(q, sources=[pin(holder1, '/basis/0'), selected],
                         bounds=dict(scan_limit=5, max_bytes=100000))
        self.assertEqual(binding(ambiguous), found)
        transfer = json.loads(transfer_path.read_bytes())
        filed = put('filed', {'export': transfer})
        offered = pin(filed, '/content/export')
        located = binding(dict(q, sources=[offered]))
        self.assertEqual((located['source_row'], located['status'], located['transfer_source']),
                         ('element:grant', 'relocated-and-changed', offered))
        other = dict(transfer, source=dict(transfer['source'], instance_id='outside'))
        filed = put('filed', {'export': other}, 1)
        self.assertEqual(binding(dict(q, sources=[pin(filed, '/content/export')]))['status'], 'unresolved-edition')
        filed = put('filed', {'export': dict(transfer, object_sha256='0' * 64)}, 2)
        with self.assertRaisesRegex(ValueError, 'transferred source bytes differ'):
            binding(dict(q, sources=[pin(filed, '/content/export')]))
        # Wrong current-edition and future pins cannot select the body at the old array index.
        for wrong in (dict(use, revision=new['revision']), dict(use, revision=new['revision']+1),
                      dict(use, sha256=first['sha256'])):
            bad = dict(consumer, elements=[element('use', {'composition_use': {'sources': [wrong]}})])
            put('reader', bad, store.show('reader')['revision'])
            unresolved = binding(q)
            self.assertEqual(unresolved['status'], 'unresolved-edition')
            self.assertNotIn('source_row', unresolved)
        put('reader', consumer, store.show('reader')['revision'])
        with self.assertRaisesRegex(ValueError, 'reviewed composition'):
            revise(self.system, q, retain=review['review_sha256'], reason='Old receiver head.')
        total = review['read_set']['native_bytes'] + store.size('reader') + item['bytes']
        self.assertEqual(binding(dict(q, bounds=dict(scan_limit=3, max_bytes=total))), found)
        for bounds in (dict(scan_limit=2, max_bytes=total), dict(scan_limit=3, max_bytes=total-1)):
            with self.assertRaisesRegex(ValueError, 'scan limit|byte bound'):
                binding(dict(q, bounds=bounds))

        # Real object read and integrity check, while the proposed writer is between
        # normalization and replacement. Preserve the incumbent on loss or corruption.
        claim = dict(q, fields={'purpose': 'Publish the checked grant.'})
        decision = revise(self.system, claim)
        captured = store.objects.directory / item['sha256']
        raw = captured.read_bytes()
        prepare = store._prepare_write
        def damage_after_prepare(*args, **kwargs):
            prepared = prepare(*args, **kwargs)
            captured.write_bytes(b' ' * len(raw))
            return prepared
        with patch.object(store, '_prepare_write', damage_after_prepare):
            with self.assertRaisesRegex(ValueError, 'retained composition evidence changed'):
                revise(self.system, claim, accept=decision['review_sha256'], reason='Use this edition.')
        self.assertEqual(record_identity(store.show('service')), new)
        self.assertEqual(binding(q)['reason'], 'captured record object failed verification')
        captured.rename(captured.with_suffix('.held'))
        missing = binding(q)
        self.assertEqual(missing['reason'], 'captured record object is missing')
        self.assertNotIn('source_row', missing)
        captured.write_bytes(raw)
        self.assertEqual(binding(q), found)

    def test_recomposition_locates_consumers_by_edition_and_typed_row(self):
        from composition_revision import revise
        from native_runtime.identities import record_identity

        def put(name, content, revision=0):
            self.system.put(dict(id=name, kind='note', content=content), revision)
            return record_identity(self.system.show(name))

        profile = dict(format='g60-composition/1', purpose='Compare service grants.')
        source = dict(profile, elements=[{'id': name, 'account': name, 'content': {'value': value}}
                                         for name, value in [('grant', 1), ('other', 0)]])
        target = put('service', source)
        pin = dict(target, instance_id=self.system.instance_id, pointer='/content/elements/0/content/value')
        bound = dict(id='menu', account='Uses the named grant.',
                     content={'composition_use': {'sources': [pin, dict(pin, pointer='')]}})
        parent = dict(profile, elements=[bound], opportunities=[dict(id='release', contribution='Release L',
                                                                    at=['element:menu'])])
        put('named', parent)
        put('other-owner', dict(parent, elements=[dict(bound, content={'composition_use': {
            'sources': [dict(pin, instance_id='another-owner')]}})]))
        put('remarks', dict(source=pin))
        goal = dict(record_identity(self.system.show('goal')), instance_id=self.system.instance_id, pointer='/content')
        q = dict(format='g60-recomposition/1', instance_id=self.system.instance_id, target=target,
                 fields={}, rows={}, sources=[goal], bounds=dict(scan_limit=4, max_bytes=100000),
                 judgment='Check the named grant.', consumer_scope=['named', 'other-owner', 'remarks'])
        steady = revise(self.system, q)['consumer_scope']['candidates']
        self.assertEqual([r['target']['id'] for r in steady], ['named'])
        self.assertEqual(steady[0]['affected'], [])
        self.assertEqual([b['status'] for b in steady[0]['bindings']], ['unchanged', 'unchanged'])
        q['order'] = {'elements': ['other', 'grant']}
        review = revise(self.system, q)
        candidate, = review['consumer_scope']['candidates']
        self.assertEqual(candidate['affected'], ['element:menu', 'opportunity:release'])
        binding, envelope = candidate['bindings']
        self.assertEqual(envelope['status'], 'unprojected-envelope')
        self.assertEqual((binding['source_row'], binding['proposed_pointer'], binding['status']),
                         ('element:grant', '/content/elements/1/content/value', 'relocated'))
        with self.assertRaisesRegex(ValueError, 'scan limit'):
            revise(self.system, dict(q, bounds=dict(q['bounds'], scan_limit=3)))
        with self.assertRaisesRegex(ValueError, 'byte bound'):
            revise(self.system, dict(q, bounds=dict(q['bounds'], max_bytes=self.system.record_store.size('goal') + 1)))
        with patch('composition_revision.MAX_VIEW_BYTES', 128):
            with self.assertRaisesRegex(ValueError, 'view byte bound'):
                revise(self.system, q)
        put('remarks', parent, 1)
        with self.assertRaisesRegex(ValueError, 'reviewed composition'):
            revise(self.system, q, accept=review['review_sha256'], reason='Use the old review.')
        fresh = revise(self.system, q)
        result = revise(self.system, q, accept=fresh['review_sha256'], reason='Accept the order.')
        self.assertEqual(result['head']['revision'], 2)
        stale = revise(self.system, dict(q, target=result['head']))['consumer_scope']['candidates']
        self.assertEqual(stale[0]['bindings'][0]['status'], 'unresolved-edition')
        self.assertEqual(self.system.show('named')['revision'], 1)

    def test_recomposition_rechecks_external_original_at_reviewed_write(self):
        from composition_revision import revise
        from native_runtime.identities import record_identity

        self.system.put(dict(id='plan', kind='note', content=dict(format='g60-composition/1', purpose='Read grant.')), 0)
        target = record_identity(self.system.show('plan'))
        source = self.fixture('grant', 'A')
        pin = dict(owner='desk', path='grant', pointer='', bytes=1, sha256=sha(b'A'))
        proposal = dict(format='g60-recomposition/1', instance_id=self.system.instance_id, target=target,
                        fields={'purpose': 'Use the grant.'}, rows={}, sources=[pin],
                        bounds=dict(scan_limit=1, max_bytes=1), judgment='Accept the witnessed grant.')

        def read_original(pins, bounds):
            if pins[0]['sha256'] != sha(source.read_bytes()):
                raise ValueError('changed source edition')
            return 1, 1

        review = revise(self.system, proposal, external_reader=read_original)
        prepare = self.system.record_store._prepare_write

        def prepare_then_change(*args, **kwargs):
            result = prepare(*args, **kwargs)
            source.write_bytes(b'B')
            return result

        with patch.object(self.system.record_store, '_prepare_write', prepare_then_change):
            with self.assertRaisesRegex(ValueError, 'changed source edition'):
                revise(self.system, proposal, external_reader=read_original,
                       accept=review['review_sha256'], reason='Use the grant.')
        self.assertEqual(record_identity(self.system.show('plan')), target)

    def test_context_captures_the_canonical_source_guide(self):
        guide = Path(__file__).with_name('GUIDE.md').read_bytes()
        request = self.system.ex.load('start')[1]
        method = request['methods'][0]
        self.assertEqual(method['sha256'], sha(guide))
        self.assertEqual(self.system.ex.get(method['sha256']), guide)

    def test_missing_guide_refuses_instead_of_substituting_another_body(self):
        with patch.dict(System.context.__globals__, {'APP': self.base / 'incomplete-engine'}):
            with self.assertRaises(FileNotFoundError):
                self.system.context('missing-guide', {'task': 'Capture the installed guide.'})
        self.assertFalse(self.system.ex.directory('missing-guide').exists())

    def test_manual_use_and_unrelated_revision(self):
        self.write('sources/design.txt', 'Design source v1')
        self.system.put({'id': 'design', 'kind': 'hypothesis', 'content': {'use': 'Review this design'},
                         'basis': ['sources/design.txt']}, 0)
        self.system.context('review', {'records': ['design'], 'targets': ['outputs/result.txt']})
        self.proposal('review')
        self.assertFalse((self.work / 'outputs/result.txt').exists())
        self.system.put({'id': 'other', 'kind': 'note', 'content': 'An unrelated work item'}, 0)
        self.system.put({'id': 'other', 'kind': 'note', 'content': 'Its unrelated revision'}, 1)
        result = self.system.adopt('review', 'outputs/result.txt', 'Fixture owner accepts this actual body.')
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual((self.work / 'outputs/result.txt').read_text(), 'Retained proposed contribution from review')
        status = self.system.status()
        self.assertTrue(status['current_use']['needs_interpretation'])
        self.assertEqual([o['id'] for o in status['owed_goal_obligations']], ['report'])
        self.assertIn('Design source v1', self.system.recover('review')['context'])

    def test_relevant_source_revision_and_reuse(self):
        self.write('sources/design.txt', 'Old actual source')
        self.system.put({'id': 'design', 'kind': 'design', 'content': 'Specific declared dependency',
                         'basis': ['sources/design.txt']}, 0)
        self.system.context('old', {'records': ['design'], 'targets': ['outputs/result.txt']})
        self.proposal('old')
        self.write('sources/design.txt', 'New actual source')
        with self.assertRaisesRegex(ValueError, 'basis changed'):
            self.system.adopt('old', 'outputs/result.txt', 'Should be refused against changed source.')
        self.assertFalse((self.work / 'outputs/result.txt').exists())
        self.assertIn('integration fixture', self.system.ex.inspect('old')['reply_text'])
        self.assertIn('design', self.system.status()['gaps']['changed_record_basis'])
        self.system.context('new-use', {'records': ['design'], 'returns': ['old'],
                                       'artifacts': ['old:outputs/result.txt'],
                                       'targets': ['outputs/result.txt']})
        current = self.system.recover('new-use')['context']
        self.assertIn('New actual source', current)
        self.assertIn('Retained proposed contribution from old', current)
        self.assertIn('no original target authority', current.lower())
        self.proposal('new-use')
        self.assertEqual(self.system.adopt('new-use', 'outputs/result.txt', 'A new current receiving judgment.')['phase'], 'complete')

    def test_selected_record_and_new_governing_duty(self):
        self.system.put({'id': 'process', 'kind': 'process', 'content': 'First process'}, 0)
        self.system.context('record-basis', {'records': ['process'], 'targets': ['outputs/record.txt']})
        self.proposal('record-basis', 'outputs/record.txt')
        self.system.put({'id': 'process', 'kind': 'process', 'content': 'Revised process'}, 1)
        with self.assertRaisesRegex(ValueError, 'basis changed'):
            self.system.adopt('record-basis', 'outputs/record.txt', 'Stale selected record.')
        self.system.context('new-duty', {'targets': ['outputs/duty.txt']})
        self.proposal('new-duty', 'outputs/duty.txt')
        self.system.put({'id': 'owed', 'kind': 'obligation', 'content': {'text': 'Preserve a newly owed effect'}}, 0)
        # A mutable convenience sidecar must not be able to rewrite captured authority.
        selection = self.work / '.system/turns/new-duty/selection.json'
        row = json.loads(selection.read_text())
        row['view']['governing_records'].append('owed')
        selection.write_text(json.dumps(row))
        with self.assertRaisesRegex(ValueError, 'membership changed'):
            self.system.adopt('new-duty', 'outputs/duty.txt', 'Newly governing work needs current interpretation.')

    def test_missing_and_attention_source_changes_block_actual_writer(self):
        self.system.put({'id': 'awaited', 'kind': 'dependency', 'content': 'This source is currently absent',
                         'basis': ['sources/late.txt']}, 0)
        self.system.context('absence', {'records': ['awaited'], 'targets': ['outputs/absence.txt']})
        self.proposal('absence', 'outputs/absence.txt')
        self.write('sources/late.txt', 'The declared absent source has appeared.')
        with self.assertRaisesRegex(ValueError, 'additional basis changed'):
            self.system.adopt('absence', 'outputs/absence.txt', 'Requires current use of the newly available source.')
        self.write('sources/consideration.txt', 'Original source')
        self.system.context('consideration', {'attention': {'items': [
            {'id': 'source-use', 'question': 'What follows?', 'use': 'Source-relative proposal',
             'basis': ['sources/consideration.txt']}]}, 'targets': ['outputs/consideration.txt']})
        self.proposal('consideration', 'outputs/consideration.txt')
        self.write('sources/consideration.txt', 'The delivered consideration source changed.')
        with self.assertRaisesRegex(ValueError, 'additional basis changed'):
            self.system.adopt('consideration', 'outputs/consideration.txt', 'The actual writer must check this source too.')
        self.assertFalse((self.work / 'outputs/absence.txt').exists())
        self.assertFalse((self.work / 'outputs/consideration.txt').exists())

    def test_record_cas_and_reserved_writer_paths(self):
        self.system.put({'id': 'note', 'kind': 'free-role', 'content': ['arbitrary', {'shape': 2}]}, 0)
        with self.assertRaisesRegex(ValueError, 'revision conflict'):
            self.system.put({'id': 'note', 'kind': 'free-role', 'content': 'wrong revision'}, 0)
        before = (self.work / 'records/goal.json').read_bytes()
        for number, target in enumerate(('records/goal.json', './records/goal.json',
                                         '.system/turns/start/view.json', 'interpretations/claim.json',
                                         '../exchange/workspace.json')):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.system.context('reserved-' + str(number), {'targets': [target]})
            with self.subTest(writer=target), self.assertRaises(ValueError):
                self.system.adopt('start', target, 'Even bypassing preparation cannot grant this writer.')
        self.assertEqual(before, (self.work / 'records/goal.json').read_bytes())

    def test_strict_lists_and_delivered_attention(self):
        for number, request in enumerate(({'records': 'goal'}, {'techniques': 'method:01'},
                                         {'record_limit': True}, {'attention': []},
                                         {'attention': {'items': [{'id': 'bad', 'question': 'Q?',
                                           'use': 'U', 'groups': 'single-string'}]}})):
            with self.subTest(request=request), self.assertRaises((ValueError, TypeError)):
                self.system.context('bad-' + str(number), request)
        spec = {'items': [{'id': i, 'question': 'Does ' + i + ' contribute?', 'use': 'Actual review',
                           'groups': ['review'], 'basis': []} for i in ('first', 'second')]}
        self.system.track(self.fixture('attention.json', spec))
        self.system.context('attention', {'attention_limit': 1})
        view = self.system.view('attention')
        self.assertEqual(len(view['attention_delivered']), 1)
        self.assertEqual(len(view['attention_omitted']), 1)
        omitted = view['attention_omitted'][0]
        bad = self.fixture('bad-account.json', {'id': 'not-delivered', 'assessments': [
            {'item': omitted, 'disposition': 'considered', 'account': 'Must not be credited.'}]})
        result = self.system.receive('attention', self.fixture('reply.txt', 'Actual fixture reply'),
                                     'fixture host', [], bad)
        self.assertEqual(result['attention_account']['status'], 'unresolved')
        self.assertEqual(self.system.ex.inspect('attention')['reply_text'], 'Actual fixture reply')
        good = self.fixture('good-account.json', {'id': 'actually-delivered', 'assessments': [
            {'item': view['attention_delivered'][0], 'disposition': 'considered',
             'account': 'A public fixture account of the item actually delivered.'}]})
        self.assertEqual(self.system.assess('attention', good, 'fixture host')['recorded'], view['attention_delivered'])
        self.assertTrue(self.system.current_use()['needs_interpretation'])

    def test_goal_interpretation_evidence_and_surviving_obligations(self):
        self.write('outputs/evidence.txt', 'Actual evidence version one')
        account = self.fixture('account.txt', 'This fixture states a whole-goal judgment with limited evidence.')
        self.system.interpret(1, 'met', account, ['outputs/evidence.txt'])
        self.assertFalse(self.system.current_use()['needs_interpretation'])
        self.assertEqual(len(self.system.status()['owed_goal_obligations']), 1)
        self.write('outputs/evidence.txt', 'Changed evidence needs a fresh account')
        self.assertTrue(self.system.current_use()['needs_interpretation'])
        self.assertEqual(self.system.current_use()['evidence_changes'][0]['path'], 'outputs/evidence.txt')
        old_goal = (self.work / 'records/goal.json').read_bytes()
        changed = self.system.put({'id': 'goal', 'kind': 'goal', 'content': {'contribution': 'A revised actual use'}}, 1)
        self.assertIsNone(changed['previous_object'])
        self.assertEqual(self.system.ex.get(API['sha'](old_goal)), old_goal)
        self.assertIsNone(self.system.current_use()['interpretation'])
        self.assertEqual([o['id'] for o in self.system.status()['owed_goal_obligations']], ['report'])
        with self.assertRaisesRegex(ValueError, 'another current goal'):
            self.system.interpret(1, 'continue', account, [])
        with self.assertRaisesRegex(ValueError, 'wait condition'):
            self.system.interpret(2, 'wait', account, [])

    def test_goal_use_tracks_declared_obligation_revisions_only(self):
        account = self.fixture('account.txt', 'A fixture whole-goal judgment about the currently declared duties.')
        self.system.interpret(1, 'met', account, [])
        original = {p: p.read_bytes() for p in (self.work / 'interpretations').glob('*.json')}
        self.system.put({'id': 'unrelated', 'kind': 'note', 'content': 'Independent useful material'}, 0)
        self.system.put({'id': 'unrelated', 'kind': 'note', 'content': 'Revised independent material'}, 1)
        self.system.put({'id': 'field', 'kind': 'field', 'content': 'Optional wider alternatives'}, 0)
        self.system.put({'id': 'field', 'kind': 'field', 'content': 'Revised optional alternatives'}, 1)
        self.assertFalse(self.system.current_use()['needs_interpretation'])
        duty = {'id': 'followup', 'kind': 'obligation', 'content': {'text': 'A newly owed goal-serving effect'},
                'relations': [{'relation': 'serves', 'target': 'goal'}]}
        self.system.put(duty, 0)
        use = self.system.current_use()
        self.assertTrue(use['needs_interpretation'])
        self.assertIn('separate-obligations-changed', use['reassessment_reasons'])
        self.assertEqual([(r['id'], r['change']) for r in use['obligation_changes']], [('followup', 'added')])
        self.system.interpret(1, 'continue', account, [])
        self.assertFalse(self.system.current_use()['needs_interpretation'])
        duty['content']['text'] = 'The corrected goal-serving effect needs different participation'
        self.system.put(duty, 1)
        revised = self.system.current_use()
        self.assertTrue(revised['needs_interpretation'])
        self.assertEqual(revised['obligation_changes'][0]['change'], 'revised')
        self.assertEqual(revised['obligation_changes'][0]['captured']['revision'], 1)
        self.assertEqual(revised['obligation_changes'][0]['current']['revision'], 2)
        self.system.interpret(1, 'continue', account, [])
        note = {'id': 'followup', 'kind': 'note', 'content': 'Archive of the completed followup'}
        with self.assertRaisesRegex(ValueError, 'before reclassification'):
            self.system.put(note, 2)
        duty['content'].update(disposition='fulfilled', account='The new participant received the contribution.')
        self.system.put(duty, 2)
        self.system.interpret(1, 'continue', account, [])
        self.system.put(note, 3)
        self.assertEqual(self.system.current_use()['obligation_changes'][0]['change'], 'removed')
        for path, body in original.items():
            self.assertEqual(path.read_bytes(), body)

    def test_legacy_goal_use_is_explicitly_unbound(self):
        goal = self.system.show('goal')
        legacy = {'format': API['FORMAT'], 'id': 'legacy-fixture', 'created_at': API['now'](),
                  'goal_revision': 1, 'goal_sha256': goal['sha256'], 'disposition': 'met',
                  'account': 'A controlled fixture in the original unbound interpretation format.',
                  'evidence': [], 'condition': None, 'scope': 'Caller-authored fixture.'}
        path = self.write('interpretations/legacy.json', json.dumps(legacy))
        original = path.read_bytes()
        use = self.system.current_use()
        self.assertEqual(use['obligation_binding'], 'unbound')
        self.assertTrue(use['needs_interpretation'])
        self.assertIn('separate-obligations-unbound', use['reassessment_reasons'])
        self.assertEqual(use['obligation_changes'], [])
        self.assertNotIn('obligation_basis', use['interpretation'])
        self.assertEqual(path.read_bytes(), original)

    def test_complete_bodies_and_captured_compact_view(self):
        selected = self.system.catalogue('prototype/agent-system/GUIDE.md')
        self.system.context('knowledge', {'techniques': ['prototype/agent-system/GUIDE.md', 'host/CONSTRUCTION.md']})
        captured = self.system.recover('knowledge')['context']
        self.assertIn(selected['body'], captured)
        self.assertIn('complete-current-source-reference', captured)
        view = self.system.view('knowledge')
        self.assertIn('prototype/agent-system/COMPOSITION.md', view['outside_techniques'])
        self.assertNotIn('"outside_techniques":', captured)
        self.assertIn('view knowledge', captured)
        original = view
        self.write('.system/turns/knowledge/view.json', '{}')
        self.assertEqual(self.system.view('knowledge'), original)
        page = self.system.catalogue(query='', limit=2)
        self.assertEqual(page['shown'], 2)
        self.assertEqual(page['next_offset'], 2)
        second = self.system.catalogue(query='', limit=2, offset=2)
        self.assertNotEqual(page['entries'], second['entries'])
        initial = self.system.recover('start')
        self.assertLess(initial['context_bytes'], 15000)
        self.assertIn('recover start --text', self.initial['first_context']['next']['read_context'])

    def test_materialization_only_acquires_exact_source(self):
        operation = 'operation:observer-model'
        catalogue = self.base / 'materialization-fixture'
        catalogue.mkdir()
        archive = catalogue / 'observer-model.zip'
        members = []
        with zipfile.ZipFile(archive, 'w') as zipped:
            for name, body, mode in (
                ('observer-model/README.md', b'Authored materialization fixture.\n', 0o644),
                ('observer-model/bin/observe.py', b'raise RuntimeError("must not execute")\n', 0o755),
            ):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (0o100000 | mode) << 16
                zipped.writestr(info, body)
                members.append({'path': name, 'bytes': len(body), 'sha256': sha(body), 'mode': mode})
        raw = archive.read_bytes()
        asset = {'kind': 'operation-archive', 'operation_id': operation, 'path': archive.name,
                 'bytes': len(raw), 'sha256': sha(raw), 'members': members}
        (catalogue / 'catalogue.json').write_text(json.dumps({'assets': [asset]}), encoding='utf-8')
        with patch.object(self.system.techniques, 'root', catalogue):
            result = self.system.materialize(operation)
            self.assertEqual(result['files'], len(asset['members']))
            for member in asset['members']:
                installed = self.work / 'operations' / member['path']
                self.assertEqual(sha(installed.read_bytes()), member['sha256'])
                self.assertEqual(installed.stat().st_mode & 0o777, member['mode'] & 0o777)
            self.assertFalse((self.root / 'exchange/carrier').exists())
            with self.assertRaisesRegex(ValueError, 'already exists'):
                self.system.materialize(operation)

    def test_actual_trusted_command_capture(self):
        self.system.context('command', {'task': 'Read the actual supplied context and emit its identity.'})
        code = ('from pathlib import Path; import hashlib,json,sys; '
                'p=Path(sys.argv[1]); b=p.read_bytes(); '
                'print(json.dumps({"context_sha256":hashlib.sha256(b).hexdigest(),"bytes":len(b)}))')
        self.system.run('command', [sys.executable, '-B', '-c', code, '{context}'])
        received = self.system.receive('command', None, 'actual finite Python fixture command', [], wait=15)
        self.assertEqual(received['transport']['kind'], 'local-command')
        observed = json.loads(self.system.ex.inspect('command')['reply_text'])
        expected = self.system.recover('command')
        self.assertEqual(observed['context_sha256'], expected['context_sha256'])
        self.assertEqual(observed['bytes'], expected['context_bytes'])
        with self.assertRaisesRegex(ValueError, 'received return'):
            self.system.run('command', [sys.executable, '-c', code, '{context}'])
        self.assertTrue(self.system.current_use()['needs_interpretation'])

    def test_ordered_argv_preserves_repeated_and_empty_arguments(self):
        self.system.context('ordered-argv', {'task': 'Receive the exact ordered argument list.'})
        for invalid in ('not-a-list', [], ['', '{context}'], [sys.executable, 3, '{context}']):
            with self.subTest(argv=invalid), self.assertRaises(ValueError):
                self.system.run('ordered-argv', invalid)
        code = 'import json,sys; print(json.dumps(sys.argv[1:]))'
        self.system.run('ordered-argv', [sys.executable, '-B', '-c', code, '{context}', 'same', 'same', ''])
        received = self.system.receive('ordered-argv', None, 'actual ordered-argv fixture command', [], wait=30)
        self.assertIn('transport', received, received)
        self.assertEqual(received['transport']['kind'], 'local-command')
        actual = json.loads(self.system.ex.inspect('ordered-argv')['reply_text'])
        self.assertEqual(actual, [str(self.system.ex.directory('ordered-argv') / 'context.md'), 'same', 'same', ''])

    def test_public_cli_run_collects_in_one_live_invocation(self):
        cli_root = self.base / 'cli-runtime'
        command = [sys.executable, '-B', str(API['PRODUCT'] / 'system.py'), '--root', str(cli_root)]
        def invoke(*arguments):
            return subprocess.run(command + list(arguments), text=True, capture_output=True, timeout=20)
        initialized = invoke('init', '--goal-text', 'Verify bounded same-invocation command collection.')
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        code = ('from pathlib import Path; import json,sys,time; time.sleep(0.1); '
                'print(json.dumps({"context":Path(sys.argv[1]).read_text(),"arguments":sys.argv[2:]}))')
        argv = self.fixture('cli-argv.json', json.dumps([sys.executable, '-B', '-c', code,
                                                     '{context}', 'same', 'same', '']))
        for invalid in ('-1', 'nan', 'inf'):
            result = invoke('run', 'start', '--argv-json', str(argv), '--wait', invalid, '--producer', 'fixture')
            self.assertEqual(result.returncode, 2)
        missing_producer = invoke('run', 'start', '--argv-json', str(argv), '--wait', '1')
        self.assertEqual(missing_producer.returncode, 2)
        self.assertFalse((cli_root / 'exchange/packets/start/dispatch.json').exists())
        completed = invoke('run', 'start', '--argv-json', str(argv), '--wait', '5',
                           '--producer', 'actual same-invocation CLI fixture')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        supplied = json.loads(completed.stdout)
        self.assertEqual(supplied['return']['transport']['kind'], 'local-command')
        actual_system = System(cli_root)
        actual = json.loads(actual_system.ex.inspect('start')['reply_text'])
        self.assertEqual(actual['arguments'], ['same', 'same', ''])
        self.assertEqual(actual['context'], actual_system.recover('start')['context'])
        self.assertTrue(supplied['status']['current_use']['needs_interpretation'])
        prepared = invoke('context', 'bounded-wait', '--task', 'Keep a timed-out carrier unresolved without redispatch.')
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        marker, release = self.base / 'invocations.txt', self.base / 'release.txt'
        gated = ('from pathlib import Path\nimport sys,time\n'
                 'with Path(sys.argv[2]).open("a") as f: f.write("invoked\\n")\n'
                 'deadline=time.monotonic()+5\n'
                 'while not Path(sys.argv[3]).exists() and time.monotonic()<deadline: time.sleep(0.01)\n'
                 'print("same carrier completed")\n')
        pending_argv = self.fixture('pending-argv.json', json.dumps([sys.executable, '-B', '-c', gated,
                                                                 '{context}', str(marker), str(release)]))
        pending = invoke('run', 'bounded-wait', '--argv-json', str(pending_argv), '--wait', '0.01',
                         '--producer', 'actual bounded CLI fixture')
        self.assertEqual(pending.returncode, 0, pending.stderr)
        self.assertFalse(json.loads(pending.stdout)['return']['received'])
        dispatch_path = cli_root / 'exchange/packets/bounded-wait/dispatch.json'
        dispatched = dispatch_path.read_bytes()
        release.write_text('Allow the same admitted command to finish.')
        collected = invoke('receive', 'bounded-wait', '--wait', '5', '--producer', 'actual bounded CLI fixture')
        self.assertEqual(collected.returncode, 0, collected.stderr)
        self.assertEqual(json.loads(collected.stdout)['return']['transport']['kind'], 'local-command')
        self.assertEqual(marker.read_text().splitlines(), ['invoked'])
        self.assertEqual(dispatch_path.read_bytes(), dispatched)


if __name__ == '__main__':
    unittest.main(verbosity=2)
