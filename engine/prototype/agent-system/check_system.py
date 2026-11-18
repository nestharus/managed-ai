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
        received = self.system.receive('ordered-argv', None, 'actual ordered-argv fixture command', [], wait=15)
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
