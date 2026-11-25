"""Exercise the installed adapter through fresh processes and reopened originals."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import subprocess
import sys
import unittest


class ReceivingChecks(unittest.TestCase):
    def setUp(self):
        self.entry = Path(os.environ['G60_READER']).parent / 'adapters/source-admission/admission.py'
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root / 'admissions.sqlite'
        self.expected = self.root / 'expected.json'
        self.expected.write_bytes(b'{"page":1}')
        self.call('init', '--epoch', 'edition-one', '--origin', 'original')

    def call(self, *args, accepted=True):
        result = subprocess.run([sys.executable, '-B', self.entry, '--db', self.db, *args],
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode == 0, accepted, result.stderr.decode())
        return json.loads(result.stdout) if accepted else result.stderr.decode()

    def ingest(self, job, previous):
        self.call('open', job, '--expected-epoch', 'edition-one', '--prior-history', 'empty')
        raw = self.root / (job + '-earlier.json')
        raw.write_bytes(previous)
        self.call('admit', job, 'earlier', '--source', str(raw))
        self.call('admit', job, 'selected', '--source', str(self.expected))
        self.call('close', job, '--selected', 'selected')

    def judge(self, job, receiver, requirement, use='current'):
        return self.call('judge', job, '--expected', str(self.expected), '--receiver', receiver,
                         '--requirement', requirement, '--equality', 'json-number-value',
                         '--use', use, '--expected-epoch', 'edition-one')

    def test_two_receivers_obtain_different_dispositions_from_one_retained_source(self):
        self.ingest('mixed', b'{"earlier":0,"earlier":0}')
        decisions = [self.judge('mixed', 'legacy', 'normalized'),
                     self.judge('mixed', 'strict', 'all-originals')]
        receiving = self.root / 'receiving'
        receiving.mkdir()
        for result in decisions:
            if result['eligible']:
                (receiving / (result['receiver'] + '.json')).write_bytes(self.expected.read_bytes())
        self.assertEqual(sorted(p.name for p in receiving.iterdir()), ['legacy.json'])
        self.assertEqual((receiving / 'legacy.json').read_bytes(), b'{"page":1}')
        original = subprocess.check_output([sys.executable, '-B', self.entry, '--db', self.db,
                                            'original', 'mixed', 'earlier'])
        pairs = json.loads(original, object_pairs_hook=list)
        self.assertEqual(pairs, [('earlier', 0), ('earlier', 0)])
        self.assertEqual(decisions[0]['selected_sha256'], decisions[1]['selected_sha256'])

    def test_installed_cutoff_and_source_change_survive_reentry(self):
        self.ingest('clean', b'{"earlier":"independent content"}')
        before = self.judge('clean', 'strict', 'all-originals')
        self.assertTrue(before['eligible'])
        self.call('admit', 'clean', 'late', '--source', str(self.expected), accepted=False)
        self.call('close', 'clean', '--selected', 'earlier', accepted=False)
        self.call('advance', '--epoch', 'edition-two', '--origin', 'derived', '--expected', 'edition-one')
        self.assertFalse(self.judge('clean', 'strict', 'all-originals')['eligible'])
        self.assertTrue(self.judge('clean', 'historian', 'all-originals', 'historical')['eligible'])
        self.assertEqual(self.call('show', 'clean')['selected'], 'selected')


if __name__ == '__main__':
    unittest.main()
