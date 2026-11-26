"""Test the native history boundary and historical diagnostics across restarts."""
if __package__:
    from .native_test_paths import APP, ENGINE, PRODUCT
else:
    from native_test_paths import APP, ENGINE, PRODUCT
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from managed_ai.store.system import System, encode, sha



class HistoryChecks(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'runtime'
        System.initialize(self.root, {'contribution': 'Recover recorded history.'}, record_retention='append-history')
        self.system = System(self.root)
        self.store = self.system.record_store

    def command(self, executable, *args, success=True):
        result = subprocess.run([sys.executable, '-B', str(PRODUCT / executable), *map(str, args)],
                                capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(result.stdout.strip())
        return result.stderr

    def test_cursor_binds_exact_head_and_detects_broken_prefix(self):
        note = {'id': 'reading', 'kind': 'note', 'content': 'Read the scale.'}
        for revision in range(5):
            self.system.put(note, revision)
        args = ('--root', self.root, 'history', 'reading', '--limit', 2)
        first = self.command('store/manage_store.py', *args)
        self.assertEqual([r['revision'] for r in first['records']], [5, 4])
        self.assertFalse(first['complete'])
        self.system.put(note, 5)
        cursor = self.root / 'cursor.json'
        cursor.write_text(json.dumps(first['cursor']))
        second = self.command('store/manage_store.py', *args, '--cursor', cursor)
        self.assertEqual(second['head'], first['head'])
        self.assertEqual([r['revision'] for r in second['records']], [3, 2])
        cursor.write_text(json.dumps(second['cursor']))
        last = self.command('store/manage_store.py', *args, '--cursor', cursor)
        self.assertEqual([r['revision'] for r in last['records']], [1])
        self.assertTrue(last['complete'])
        self.assertIsNone(last['cursor'])
        self.assertEqual(self.store.show('reading')['revision'], 6)
        cursor.write_text(json.dumps(dict(first['cursor'], next_object=second['cursor']['next_object'])))
        self.assertIn('off the captured chain', self.command('store/manage_store.py', *args, '--cursor', cursor, success=False))
        cursor.write_text(json.dumps(second['cursor']))
        (self.store.objects.directory / first['records'][0]['previous_object']).write_text('{}')
        self.assertIn('object changed', self.command('store/manage_store.py', *args, '--cursor', cursor, success=False))

    def install_history(self, ident, rows):
        # Old writers permitted transitions rejected today. Encode the original
        # native envelope and link its exact bytes, without relaxing present writes.
        previous = None
        for revision, fields in enumerate(rows, 1):
            record = dict(format=self.store.format, id=ident, revision=revision, relations=[],
                          basis=[], active=True, updated_at='2026-01-01T00:00:00Z', previous_object=previous)
            record.update(fields)
            record['sha256'] = sha(encode(record))
            raw = encode(record)
            if revision < len(rows):
                previous = self.store.objects.put(raw)
            else:
                self.store.path(ident).write_bytes(raw)

    def test_feedback_repaired_and_historical_dropped_returns(self):
        pin = dict(id='evidence', revision=1, sha256='b'*64, object='a'*64)
        old_return = dict(observation=pin, interpretation='Door stayed locked.', route='hold', next='Repair latch.')
        def feedback(**fields):
            return dict(kind='feedback-use', content=dict(returns=[], **fields))
        adverse = feedback(state='hold')
        adverse['content']['returns'] = [old_return]
        # Pre-carry revision reset, then a modern retained return and accounted repair.
        rows = [adverse, feedback(state='open', revision_reason='Earlier plan reset.'), adverse,
                feedback(state='open', carried_returns=[dict(return_number=1, **old_return)],
                         return_offset=1, revision_reason='Repeat at another door.')]
        fixed = dict(rows[-1]['content'], state='review', returns=[dict(route='review', resolutions=[
            dict(return_number=1, account='The door now opens under load.')])])
        rows.extend([dict(kind='feedback-use', content=fixed), feedback(state='open', revision_reason='Next plan.')])
        self.install_history('trial', rows)
        result = self.command('store/governance/audit_feedback.py', PRODUCT, self.root, 'trial')
        self.assertEqual(result['unexplained_drops'], [dict(revision=2, prior_revision=1,
            returns=[dict(number=1, observation=pin, interpretation='Door stayed locked.')])])
        self.assertEqual(result['through_revision'], 6)
        parent = self.store.show('trial')['previous_object']
        (self.store.objects.directory / parent).unlink()
        self.command('store/governance/audit_feedback.py', PRODUCT, self.root, 'trial', success=False)

    def test_obligation_tracks_retyping_and_inactive_history(self):
        basis = [dict(path='proof', sha256='b'*64)]
        self.install_history('duty', [dict(kind='obligation', content={}, basis=basis),
            dict(kind='obligation', content={}, active=False),
            dict(kind='obligation', content={'disposition':'released'}), dict(kind='note', content='Moved')])
        result = self.command('store/governance/audit_feedback.py', PRODUCT, self.root, 'duty', '--obligation')
        self.assertEqual(result['inactive_for_navigation'], [2])
        self.assertEqual([(r['revision'], r['observations']) for r in result['history']], [
            (2, ['earlier evidence removed or replaced without account']), (3, ['closure had no account']),
            (4, ['retyped before accounted closure'])])
        self.assertEqual(self.store.show('duty')['kind'], 'note')

    def test_hash_valid_wrong_id_revision_format_or_inner_digest_fails(self):
        original = {'id':'item','kind':'note','content':'Value'}
        self.system.put(original, 0)
        record = self.store.show('item')
        self.system.put(original, 1)
        head = self.store.show('item')
        for change in ({'id':'other'}, {'revision':True}, {'format':'unknown'}, {'sha256':'0'*64},
                       {'previous_object':'b'*64}):
            with self.subTest(change=change):
                wrong = dict(record, **change)
                if 'sha256' not in change:
                    del wrong['sha256']
                    wrong['sha256'] = sha(encode(wrong))
                replacement = dict(head, previous_object=self.store.objects.put(encode(wrong)))
                del replacement['sha256']
                replacement['sha256'] = sha(encode(replacement))
                self.store.path('item').write_bytes(encode(replacement))
                with self.assertRaises(ValueError):
                    list(self.store.history_pages('item'))


if __name__ == '__main__':
    unittest.main()
