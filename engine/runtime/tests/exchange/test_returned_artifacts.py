"""Exact receiving behavior for explicitly selected returned proposals."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'continuation'))
from continuation_support.exchange import Exchange
from continuation_support.collector import encode, sha


class ReturnedArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.work = self.base / 'work'; self.work.mkdir()
        self.root = self.base / 'exchange'
        (self.work / 'brief.md').write_text('Develop the new use without adopting the earlier proposal.')
        self.reply = self.base / 'reply.md'; self.reply.write_text('A separately returned proposal is available.')
        self.proposal = self.base / 'proposal.md'; self.proposal.write_text('EXACT-UNPUBLISHED-BODY')
        Exchange.initialize(self.root, self.work)
        self.ex = Exchange(self.root)
        self.ex.prepare('origin', 'author', 'brief.md', [], [], ['method.md'], [])
        self.ex.receive('origin', self.reply, 'authored test input', ['method.md=' + str(self.proposal)])

    def tearDown(self):
        self.temp.cleanup()

    def current(self, name='next', artifacts=None, returns=None, targets=None):
        return self.ex.prepare(name, 'different-use', 'brief.md', [], [], targets or [],
                               returns or [], ['origin:method.md'] if artifacts is None else artifacts)

    def context(self, name='next'):
        return (self.root / 'packets' / name / 'context.md').read_text()

    def test_unpublished_body_survives_source_removal(self):
        self.proposal.unlink()
        self.current()
        self.assertIn('EXACT-UNPUBLISHED-BODY', self.context())
        self.assertFalse((self.work / 'method.md').exists())
        self.assertFalse(list((self.root / 'packets/origin').glob('publish-*.json')))

    def test_changed_workspace_target_is_not_substituted(self):
        (self.work / 'method.md').write_text('A DIFFERENT CURRENT METHOD')
        self.current()
        self.assertIn('EXACT-UNPUBLISHED-BODY', self.context())
        self.assertNotIn('A DIFFERENT CURRENT METHOD', self.context())
        self.assertTrue(all(r['unchanged'] for r in self.ex.inspect('next')['basis']))

    def test_reply_only_exposes_available_paths_not_proposal_body(self):
        self.current(artifacts=[], returns=['origin'])
        self.assertIn('origin:method.md', self.context())
        self.assertNotIn('EXACT-UNPUBLISHED-BODY', self.context())
        self.assertFalse(self.ex.load('next')[1]['artifacts'])

    def test_two_interests_share_object_not_authority(self):
        self.current('one'); self.current('two')
        one = self.ex.load('one')[1]; two = self.ex.load('two')[1]
        self.assertEqual(one['artifacts'][0]['sha256'], two['artifacts'][0]['sha256'])
        self.assertEqual(one['targets'], [])
        with self.assertRaisesRegex(ValueError, 'declared target'):
            self.ex.receive('one', self.reply, 'test', ['method.md=' + str(self.proposal)])
        self.assertFalse((self.work / 'method.md').exists())

    def test_binary_body_retained_without_text_claim(self):
        self.proposal.write_bytes(b'\x00\xff')
        self.ex.prepare('binary', 'author', 'brief.md', [], [], ['bytes.bin'], [])
        self.ex.receive('binary', self.reply, 'test', ['bytes.bin=' + str(self.proposal)])
        self.current(artifacts=['binary:bytes.bin'])
        row = self.ex.load('next')[1]['artifacts'][0]
        self.assertEqual(self.ex.get(row['sha256']), b'\x00\xff')
        self.assertIn('Binary material', self.context())

    def test_invalid_or_duplicate_selections_are_rejected(self):
        for i, selections in enumerate([['origin'], [':method.md'], ['../origin:method.md'],
                                       ['origin:../method.md'], ['origin:absent.md'],
                                       ['origin:method.md', 'origin:method.md']]):
            with self.subTest(selections=selections):
                with self.assertRaises((ValueError, OSError)):
                    self.current('bad' + str(i), artifacts=selections)
                self.assertFalse((self.root / 'packets' / ('bad' + str(i))).exists())

    def test_tampered_origin_return_is_rejected(self):
        self.current()
        p = self.root / 'packets/origin/return.json'
        r = json.loads(p.read_text()); r['producer'] = 'changed'; p.write_text(json.dumps(r))
        with self.assertRaisesRegex(ValueError, 'return record changed'):
            self.ex.load('next')

    def test_tampered_proposed_content_is_rejected(self):
        self.current()
        row = self.ex.load('next')[1]['artifacts'][0]
        (self.root / 'objects' / row['sha256']).write_text('REPLACED')
        with self.assertRaisesRegex(ValueError, 'content object changed'):
            self.ex.load('next')

    def test_v1_packet_remains_readable(self):
        d = self.root / 'packets/origin'
        r = json.loads((d / 'request.json').read_text())
        r.pop('artifacts'); r['version'] = 1
        binding = json.loads((d / 'binding.json').read_text())
        binding['request_sha256'] = sha(encode(r))
        (d / 'request.json').write_bytes(encode(r))
        (d / 'binding.json').write_bytes(encode(binding))
        old = json.loads((d / 'return.json').read_text())
        old['request_sha256'] = binding['request_sha256']
        old['return_sha256'] = sha(encode({k: v for k, v in old.items() if k != 'return_sha256'}))
        (d / 'return.json').write_bytes(encode(old))
        self.current()
        self.assertEqual(self.ex.load('origin')[1]['version'], 1)
        self.assertIn('EXACT-UNPUBLISHED-BODY', self.context())

    def test_proposed_code_is_not_executed(self):
        marker = self.base / 'must-not-exist'
        self.proposal.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
        self.ex.prepare('code', 'author', 'brief.md', [], [], ['method.py'], [])
        self.ex.receive('code', self.reply, 'test', ['method.py=' + str(self.proposal)])
        self.current(artifacts=['code:method.py'])
        self.assertFalse(marker.exists())
        self.assertIn('material only', self.context())

    def test_only_current_targets_can_be_published(self):
        self.current(targets=['answer.md'])
        self.ex.receive('next', self.reply, 'test', ['answer.md=' + str(self.proposal)])
        self.ex.publish('next', 'answer.md', 'Explicit current authority.')
        self.assertTrue((self.work / 'answer.md').exists())
        self.assertFalse((self.work / 'method.md').exists())

    def test_missing_return_does_not_become_artifact(self):
        self.ex.prepare('pending', 'author', 'brief.md', [], [], ['method.md'], [])
        with self.assertRaises(OSError):
            self.current(artifacts=['pending:method.md'])

    def test_context_budget_counts_artifact(self):
        self.proposal.write_bytes(b'x' * 5000)
        self.ex.prepare('large', 'author', 'brief.md', [], [], ['large.txt'], [])
        self.ex.receive('large', self.reply, 'test', ['large.txt=' + str(self.proposal)])
        with patch('continuation_support.exchange.packet_context.MAX_PACKET_BYTES', 2048):
            with self.assertRaisesRegex(ValueError, 'total packet limit'):
                self.current(artifacts=['large:large.txt'])

    def test_cli_selects_artifact(self):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'exchange_packets.py'), '--root', str(self.root),
                                 'prepare', 'cli', '--consumer', 'recipient', '--brief', 'brief.md',
                                 '--artifact', 'origin:method.md'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('EXACT-UNPUBLISHED-BODY', self.context('cli'))


if __name__ == '__main__':
    unittest.main()
