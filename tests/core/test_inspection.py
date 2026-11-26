"""Read-side observations remain available while publication stays blocked."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from tests._paths import ENGINE as ROOT, SRC
from managed_ai.core.exchange import publication as module
from managed_ai.core.exchange import Exchange


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.work = self.base / 'work'; self.work.mkdir()
        self.root = self.base / 'runtime'
        (self.work / 'brief.md').write_text('Produce the authorized artifact.')
        (self.work / 'source.md').write_text('Current selected material.')
        (self.work / 'output.md').write_text('original')
        self.reply = self.base / 'reply.md'; self.reply.write_text('Retained actual return for this control.')
        self.proposal = self.base / 'proposal.md'; self.proposal.write_text('proposed')
        Exchange.initialize(self.root, self.work)
        self.ex = Exchange(self.root)
        self.ex.prepare('one', 'operator', 'brief.md', [], ['source.md'], ['output.md'], [])
        self.ex.receive('one', self.reply, 'authored test control', ['output.md=' + str(self.proposal)])

    def tearDown(self):
        self.temp.cleanup()

    def hashes(self):
        return {p.relative_to(self.base).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.base.rglob('*') if p.is_file() and not p.is_symlink()}

    def interrupt(self, after=False):
        atomic, write = module._atomic_bytes, module.write_json
        def interrupted_atomic(path, data):
            if path == self.work / 'output.md' and not after:
                raise OSError('controlled interruption before target')
            return atomic(path, data)
        def interrupted_receipt(path, data):
            if path.name.startswith('publish-') and data.get('phase') == 'complete' and after:
                raise OSError('controlled interruption after target')
            return write(path, data)
        with patch.object(module, '_atomic_bytes', interrupted_atomic), patch.object(module, 'write_json', interrupted_receipt):
            with self.assertRaises(OSError):
                self.ex.publish('one', 'output.md', 'test authority')
        return next(self.ex.directory('one').glob('publish-*.json'))

    def checked_view(self):
        before = self.hashes()
        result = self.ex.inspect('one')
        self.assertEqual(before, self.hashes())
        self.assertIn('Retained actual return', result['reply_text'])
        return result

    def test_before_effect_is_readable_without_replay(self):
        self.interrupt()
        view = self.checked_view()
        self.assertIsNone(view['basis'])
        self.assertIn('manual reconciliation', view['basis_error'])
        row = view['publications'][0]
        self.assertEqual(row['record']['phase'], 'intent')
        self.assertTrue(row['current_target']['matches_base'])
        self.assertFalse(row['current_target']['matches_proposal'])
        with self.assertRaisesRegex(ValueError, 'already attempted'):
            self.ex.publish('one', 'output.md', 'still forbidden')

    def test_after_effect_does_not_invent_completion(self):
        self.interrupt(after=True)
        row = self.checked_view()['publications'][0]
        self.assertEqual(row['record']['phase'], 'intent')
        self.assertTrue(row['current_target']['matches_proposal'])
        self.assertFalse(row['current_target']['matches_base'])
        with self.assertRaisesRegex(ValueError, 'already attempted'):
            self.ex.publish('one', 'output.md', 'still forbidden')

    def test_equal_base_and_proposal_are_not_a_historical_decision(self):
        self.ex.prepare('equal', 'operator', 'brief.md', [], [], ['output.md'], [])
        self.ex.receive('equal', self.reply, 'control', ['output.md=' + str(self.work / 'output.md')])
        intent = self.ex.directory('equal') / 'publish-control.json'
        proposal = json.loads((self.ex.directory('equal') / 'return.json').read_text())['proposals'][0]
        intent.write_text(json.dumps(dict(phase='intent', **proposal)))
        row = self.ex.inspect('equal')['publications'][0]
        self.assertTrue(row['current_target']['matches_base'])
        self.assertTrue(row['current_target']['matches_proposal'])
        self.assertEqual(row['record']['phase'], 'intent')

    def test_unreadable_current_material_does_not_hide_origin(self):
        (self.work / 'source.md').unlink(); (self.work / 'source.md').mkdir()
        result = self.checked_view()
        self.assertIsNone(result['basis'])
        self.assertIn('ordinary file required', result['basis_error'])
        self.assertIsNotNone(result['return'])
        with self.assertRaises(ValueError):
            self.ex.publish('one', 'output.md', 'not an adequate effect basis')

    def test_missing_target_is_observed_as_missing(self):
        self.interrupt()
        (self.work / 'output.md').unlink()
        row = self.checked_view()['publications'][0]['current_target']
        self.assertFalse(row['exists']); self.assertIsNone(row['sha256'])
        self.assertFalse(row['matches_base']); self.assertFalse(row['matches_proposal'])

    def test_different_target_is_not_relabelled_as_either_occurrence(self):
        self.interrupt(after=True)
        (self.work / 'output.md').write_text('a later unrelated occurrence')
        row = self.checked_view()['publications'][0]['current_target']
        self.assertFalse(row['matches_base']); self.assertFalse(row['matches_proposal'])

    def test_symlink_target_is_not_followed(self):
        self.interrupt()
        (self.work / 'output.md').unlink()
        (self.work / 'output.md').symlink_to(self.reply)
        row = self.checked_view()['publications'][0]
        self.assertNotIn('current_target', row)
        self.assertIn('symlink', row['observation_error'])

    def test_malformed_publication_retains_independently_valid_return(self):
        p = self.interrupt(); p.write_text('{unfinished-json')
        result = self.checked_view()
        self.assertIn('basis_error', result)
        self.assertIn('observation_error', result['publications'][0])
        self.assertNotIn('current_target', result['publications'][0])

    def test_undeclared_publication_path_is_not_read(self):
        p = self.interrupt()
        record = json.loads(p.read_text()); record['path'] = '../reply.md'
        p.write_text(json.dumps(record))
        row = self.checked_view()['publications'][0]
        self.assertNotIn('current_target', row)
        self.assertIn('not a declared target', row['observation_error'])

    def test_corrupt_return_still_fails_closed(self):
        self.interrupt()
        p = self.ex.directory('one') / 'return.json'
        record = json.loads(p.read_text()); record['producer'] = 'altered'
        p.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, 'return record changed'):
            self.ex.inspect('one')

    def test_corrupt_context_still_fails_closed(self):
        self.interrupt()
        p = self.ex.directory('one') / 'context.md'
        p.write_bytes(p.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'frozen request/context changed'):
            self.ex.inspect('one')

    def test_corrupt_object_still_fails_closed(self):
        self.interrupt()
        record = json.loads((self.ex.directory('one') / 'return.json').read_text())
        (self.root / 'objects' / record['reply_sha256']).write_text('changed')
        with self.assertRaisesRegex(ValueError, 'content object changed'):
            self.ex.inspect('one')

    def test_complete_receipt_remains_complete_not_reconciled_again(self):
        self.ex.publish('one', 'output.md', 'normal completed publication')
        result = self.checked_view()
        self.assertNotIn('basis_error', result)
        self.assertTrue(all(row['unchanged'] for row in result['basis']))
        self.assertEqual(result['publications'][0]['record']['phase'], 'complete')

    def test_fresh_cli_can_receive_then_continue_without_repairing_old_intent(self):
        intent = self.interrupt(after=True)
        before = intent.read_bytes()
        p = subprocess.run([sys.executable, '-B', str(ROOT / 'core/cli/exchange_packets.py'), '--root', str(self.root),
                            'inspect', 'one'], capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)['publications'][0]['record']['phase'], 'intent')
        self.ex.prepare('next', 'explanation', 'brief.md', [], [], ['note.md'], ['one'], ['one:output.md'])
        self.assertIn('proposed', (self.ex.directory('next') / 'context.md').read_text())
        self.ex.receive('next', self.reply, 'separate authored explanatory control', ['note.md=' + str(self.reply)])
        self.ex.publish('next', 'note.md', 'authority for this different contribution only')
        self.assertEqual(intent.read_bytes(), before)
        self.assertEqual((self.work / 'output.md').read_text(), 'proposed')
        self.assertTrue((self.work / 'note.md').is_file())


if __name__ == '__main__':
    unittest.main()
