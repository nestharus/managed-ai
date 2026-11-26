"""Owner-accounted terminal outcomes and publication observations."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from tests._paths import ENGINE as ROOT, SRC, cleanup_tempdir

from managed_ai.core.exchange.transport.collector import Collector
from managed_ai.core.exchange import Exchange
from managed_ai.core.storage import MAX_BYTES, encode, read_json, sha, write_json


class LifecycleOutcomes(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_tempdir, temporary)
        self.base = Path(temporary.name)
        self.work = self.base / 'work'
        self.work.mkdir()
        for path, body in {'brief.md': 'Continue the artifact work.',
                           'source.txt': 'Current evidence.', 'one.txt': 'old one',
                           'two.txt': 'old two'}.items():
            (self.work / path).write_text(body)
        self.root = self.base / 'exchange'
        Exchange.initialize(self.root, self.work)
        self.ex = Exchange(self.root)
        self.ex.prepare('one', 'reader', 'brief.md', [], ['source.txt'], ['one.txt', 'two.txt'], [])
        self.reply = self.base / 'reply.md'
        self.reply.write_text('The owner supplied this contribution.')
        self.proposal = self.base / 'proposal.txt'
        self.proposal.write_text('new content')
        self.account = 'The owner records this terminal observation and will use a fresh occasion.'

    def dispatch(self, code):
        return self.ex.dispatch('one', [sys.executable, '-B', '-c', code, '{context}'])

    def collect(self):
        directory, request = self.ex.load('one')
        return self.ex.transport.collect('one', directory, request, wait=8)

    def failed_launch(self):
        self.ex.dispatch('one', [str(self.base / 'absent-command'), '{context}'])
        result = self.ex.receive('one', None, 'local command', [], wait=8)
        self.assertFalse(result['received'])
        self.assertEqual(result['carrier']['status'], 'launch-failed')
        return result['carrier']

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file() and path.name != 'owner.lock'}

    def assert_preserved(self, before):
        for relative, body in before.items():
            self.assertEqual((self.root / relative).read_bytes(), body, relative)

    def receive_proposals(self):
        return self.ex.receive('one', self.reply, 'owner',
                               ['one.txt=' + str(self.proposal), 'two.txt=' + str(self.proposal)])

    def interrupted_publication(self, *, after_target):
        self.receive_proposals()

        def completion_write(path, value):
            if path.name.startswith('publish-') and value.get('phase') == 'complete':
                raise OSError('controlled completion receipt error')
            return write_json(path, value)

        if after_target:
            with patch('managed_ai.core.exchange.publication.write_json', side_effect=completion_write):
                with self.assertRaises(OSError):
                    self.ex.publish('one', 'one.txt', 'owner attempts publication')
        else:
            with patch('managed_ai.core.exchange.publication._atomic_bytes', side_effect=OSError('controlled write error')):
                with self.assertRaises(OSError):
                    self.ex.publish('one', 'one.txt', 'owner attempts publication')
        return self.ex.directory('one') / ('publish-' + sha(b'one.txt') + '.json')

    def test_launch_failure_retains_exact_carrier_without_contribution(self):
        view = self.failed_launch()
        before = self.snapshot()
        record = self.ex.close_transport('one', self.account)
        self.assertEqual(record['reason'], 'launch-failed')
        self.assertEqual(record['transport']['carrier']['result']['returncode'], None)
        self.assertFalse((self.ex.directory('one') / 'return.json').exists())
        self.assertEqual(self.ex.list()[0]['transport'], 'terminal-without-return')
        self.assertIsNone(self.ex.inspect('one')['return'])
        self.assert_preserved(before)
        recovered = self.ex.recover_transport('one')
        for field in ['origin_request', 'streams', 'result', 'stream_integrity', 'status']:
            self.assertEqual(recovered[field], view[field])
        shutil.rmtree(self.root / 'carrier')
        process = subprocess.run([sys.executable, '-B', str(ROOT / 'core/exchange/cli.py'), '--root', str(self.root),
                                  'recover-transport', 'one'], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), recovered)

    def test_oversized_completed_output_retains_evidence_with_bound_effective(self):
        self.dispatch("import sys; sys.stdout.buffer.write(b'x' * (2 * 1024 * 1024 + 1))")
        with self.assertRaisesRegex(ValueError, 'per-object limit'):
            self.ex.receive('one', None, 'local command', [], wait=8)
        before = self.snapshot()
        record = self.ex.close_transport('one', self.account)
        self.assertEqual(record['reason'], 'reply-limit-exceeded')
        view = self.ex.recover_transport('one')
        self.assertEqual(view['result']['returncode'], 0)
        self.assertEqual(view['streams']['stdout']['byte_count'], MAX_BYTES + 1)
        self.assertEqual(view['streams']['stdout']['sha256'], sha(b'x' * (MAX_BYTES + 1)))
        self.assertFalse((self.ex.directory('one') / 'return.json').exists())
        self.assert_preserved(before)

    def test_nonzero_short_reply_keeps_actual_exit_and_explicit_adoption(self):
        self.dispatch("import sys; print('partial result'); sys.exit(7)")
        self.collect()
        with self.assertRaisesRegex(ValueError, 'launch failure or an oversized'):
            self.ex.close_transport('one', self.account)
        record = self.ex.receive('one', None, 'local command', ['one.txt=' + str(self.proposal)])
        self.assertEqual(record['transport']['carrier']['result']['returncode'], 7)
        self.assertEqual((self.work / 'one.txt').read_text(), 'old one')
        self.ex.publish('one', 'one.txt', 'owner accepts the bounded proposal')
        self.assertEqual((self.work / 'one.txt').read_text(), 'new content')

    def test_running_command_refuses_closure_and_then_receives(self):
        gate = self.work / 'gate'
        self.addCleanup(lambda: gate.touch(exist_ok=True) if gate.parent.exists() else None)
        self.dispatch("import os,pathlib,time; gate=pathlib.Path('gate'); deadline=time.monotonic()+30\n"
                      "while not gate.exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
                      "if not gate.exists(): raise SystemExit('test did not release the worker')\n"
                      "os.write(1, b'finished\\n')")
        with self.assertRaisesRegex(ValueError, 'verified terminal'):
            self.ex.close_transport('one', self.account)
        gate.touch()
        record = self.ex.receive('one', None, 'local command', [], wait=30)
        self.assertIn('reply_sha256', record, record)
        self.assertEqual(self.ex.get(record['reply_sha256']), b'finished\n')

    def test_uncertain_fixture_retains_attempt_and_refuses_terminal_closure(self):
        directory, request = self.ex.load('one')
        carrier = Collector(self.root / 'carrier')
        argv = [sys.executable, '-B', '-c', 'print("unused fixture command")', str(directory / 'context.md')]
        admission = carrier.admit('exchange-one', argv, self.work, directory / 'context.md', 'reader')
        carrier_directory = carrier.jobs / admission['key']
        original = read_json(carrier_directory / 'request.json')
        write_json(carrier_directory / 'dispatch.json',
                   {'attempt_id': 'declared-unstarted-fixture', 'request_sha256': sha(encode(original))})
        write_json(directory / 'dispatch.json',
                   {'job_id': 'exchange-one', 'argv': argv, 'request_sha256': sha(encode(request))})
        before = self.snapshot()
        result = self.ex.receive('one', None, 'owner inspecting uncertainty', [])
        self.assertFalse(result['received'])
        self.assertEqual(result['carrier']['status'], 'uncertain')
        with self.assertRaisesRegex(ValueError, 'verified terminal'):
            self.ex.close_transport('one', self.account)
        self.assert_preserved(before)

    def test_terminal_closure_is_idempotent_and_keeps_dispatch_guard(self):
        self.failed_launch()
        record = self.ex.close_transport('one', self.account)
        before = self.snapshot()
        self.assertEqual(self.ex.close_transport('one', self.account), record)
        self.assert_preserved(before)
        with self.assertRaisesRegex(ValueError, 'different account'):
            self.ex.close_transport('one', 'A different owner account.')
        with self.assertRaisesRegex(ValueError, 'terminal transport outcome'):
            self.dispatch('print("another attempt")')
        with self.assertRaisesRegex(ValueError, 'terminal transport outcome'):
            self.ex.receive('one', self.reply, 'replacement', [])
        with self.assertRaises(OSError):
            self.ex.publish('one', 'one.txt', 'no received proposal')
        self.assert_preserved(before)

    def test_bad_proposal_argument_can_be_corrected_without_closing_transport(self):
        self.dispatch('import os; os.write(1, b"valid bounded result\\n")')
        with self.assertRaisesRegex(ValueError, 'unique declared target'):
            self.ex.receive('one', None, 'local command', ['undeclared=' + str(self.proposal)], wait=8)
        self.assertFalse((self.ex.directory('one') / 'transport-outcome.json').exists())
        received = self.ex.receive('one', None, 'local command', ['one.txt=' + str(self.proposal)], wait=30)
        self.assertIn('reply_bytes', received, received)
        self.assertEqual(received['reply_bytes'], len(b'valid bounded result\n'))

    def test_reconcile_base_observation_preserves_intent_and_allows_sibling(self):
        intent = self.interrupted_publication(after_target=False)
        original_intent = intent.read_bytes()
        expected = sha((self.work / 'one.txt').read_bytes())
        record = self.ex.reconcile_publication('one', 'one.txt', expected, self.account)
        self.assertEqual(record['historical_completion'], 'unestablished')
        self.assertEqual(intent.read_bytes(), original_intent)
        self.assertEqual((self.work / 'one.txt').read_text(), 'old one')
        self.ex.publish('one', 'two.txt', 'owner accepts the unaffected sibling')
        self.assertEqual((self.work / 'two.txt').read_text(), 'new content')
        with self.assertRaisesRegex(ValueError, 'already attempted'):
            self.ex.publish('one', 'one.txt', 'repeat original target')

    def test_reconcile_proposal_observation_preserves_history_and_is_idempotent(self):
        intent = self.interrupted_publication(after_target=True)
        before = self.snapshot()
        expected = sha(self.proposal.read_bytes())
        with patch('managed_ai.core.exchange.publication._atomic_bytes', side_effect=AssertionError('target replay')):
            record = self.ex.reconcile_publication('one', 'one.txt', expected, self.account)
            self.assertEqual(self.ex.reconcile_publication('one', 'one.txt', expected, self.account), record)
        self.assert_preserved(before)
        self.assertEqual(read_json(intent)['phase'], 'intent')
        self.assertEqual(record['historical_completion'], 'unestablished')
        self.assertEqual(self.ex.inspect('one')['publications'][0]['reconciliation'], record)
        with self.assertRaisesRegex(ValueError, 'different account or observation'):
            self.ex.reconcile_publication('one', 'one.txt', expected, 'Changed account.')
        self.ex.publish('one', 'two.txt', 'owner accepts the unaffected sibling')

    def test_reconciliation_refuses_divergence_and_wrong_expected_identity(self):
        intent = self.interrupted_publication(after_target=False)
        original_intent = intent.read_bytes()
        with self.assertRaisesRegex(ValueError, 'expected reconciliation observation'):
            self.ex.reconcile_publication('one', 'one.txt', '0' * 64, self.account)
        (self.work / 'one.txt').write_text('later owner edit')
        expected = sha((self.work / 'one.txt').read_bytes())
        with self.assertRaisesRegex(ValueError, 'fresh receiving occasion'):
            self.ex.reconcile_publication('one', 'one.txt', expected, self.account)
        self.assertEqual(intent.read_bytes(), original_intent)
        self.assertEqual((self.work / 'one.txt').read_text(), 'later owner edit')
        self.assertEqual(list(self.ex.directory('one').glob('reconcile-*.json')), [])

    def test_reconciliation_requires_intact_binding_to_original_intent(self):
        intent = self.interrupted_publication(after_target=False)
        expected = sha((self.work / 'one.txt').read_bytes())
        self.ex.reconcile_publication('one', 'one.txt', expected, self.account)
        intent.write_bytes(intent.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            self.ex.publish('one', 'two.txt', 'continue after intent changed')
        inspected = self.ex.inspect('one')
        self.assertIsNotNone(inspected['return'])
        self.assertIn('binding changed', inspected['publications'][0]['observation_error'])

    def test_terminal_outcome_corruption_is_visible(self):
        self.failed_launch()
        self.ex.close_transport('one', self.account)
        path = self.ex.directory('one') / 'transport-outcome.json'
        record = read_json(path)
        record['account'] = 'Accidentally changed account.'
        write_json(path, record)
        self.assertEqual(self.ex.list()[0]['transport'], 'record-unresolved')
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            self.ex.recover_transport('one')


if __name__ == '__main__':
    unittest.main()
