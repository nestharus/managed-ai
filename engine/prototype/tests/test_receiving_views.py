"""Known-result access and originating-context recovery keep their actual scope."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from continuation_support import collector
from continuation_support.collector import Collector, encode, exclusive, sha, write_json
from continuation_support.exchange import Exchange


class ReceivingViewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.work = self.base / 'work'
        self.work.mkdir()
        self.root = self.base / 'exchange'
        (self.work / 'brief.md').write_text('ORIGINAL-BRIEF: explain this source.')
        (self.work / 'method.md').write_text('ORIGINAL-METHOD: preserve its actual scope.')
        (self.work / 'source.md').write_text('ORIGINAL-SOURCE: an authored premise.')
        Exchange.initialize(self.root, self.work)
        self.ex = Exchange(self.root)
        self.ex.prepare('origin', 'writer', 'brief.md', ['method.md'],
                        ['source.md'], ['answer.md'], [])

    def tearDown(self):
        self.temporary.cleanup()

    def retained_bytes(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def queued_carrier_fixture(self):
        """Retain the documented boundary before carrier dispatch, without running argv."""
        carrier = Collector(self.root / 'carrier')
        with exclusive(carrier.root / 'owner.lock'):
            for job_id in ('exchange-origin', 'unrelated-one', 'unrelated-two'):
                carrier.admit(job_id, [sys.executable, '-c', 'raise AssertionError("not dispatched")'],
                              self.work, self.work / 'brief.md', 'writer')
        request = self.ex.load('origin')[1]
        write_json(self.ex.directory('origin') / 'dispatch.json', {
            'job_id': 'exchange-origin', 'argv': ['not executed in this fixture'],
            'request_sha256': sha(encode(request))})
        return carrier, carrier.jobs / sha(b'exchange-origin')

    def test_target_receive_does_not_read_unrelated_job_history(self):
        carrier, target = self.queued_carrier_fixture()
        with patch.object(collector, 'material', wraps=collector.material) as material, \
                patch.object(collector, 'read_json', wraps=collector.read_json) as read:
            result = self.ex.receive('origin', None, 'test carrier reader', [], wait=0)
        self.assertFalse(result['received'])
        self.assertEqual(result['carrier']['status'], 'queued')
        self.assertEqual([c.args[0] for c in read.call_args_list], [target / 'request.json'])
        streams = [c.args[0] for c in material.call_args_list if c.args[0].suffix == '.bin']
        self.assertEqual(streams, [target / 'stdout.bin', target / 'stderr.bin'])
        self.assertFalse(any(carrier.jobs.glob('*/started.json')))
        self.assertFalse((self.ex.directory('origin') / 'return.json').exists())

    def test_malformed_target_keeps_collector_diagnostic(self):
        _, target = self.queued_carrier_fixture()
        (target / 'request.json').write_text('{unfinished-json')
        before = self.retained_bytes()
        result = self.ex.receive('origin', None, 'test carrier reader', [], wait=0)
        self.assertFalse(result['received'])
        view = result['carrier']
        self.assertEqual(view['status'], 'record-unresolved')
        self.assertEqual(view['directory'], str(target))
        self.assertEqual(view['requested_job_id'], 'exchange-origin')
        self.assertNotIn('job_id', view)
        self.assertIn('Expecting property name', view['error'])
        self.assertEqual(before, self.retained_bytes())

    def test_missing_target_is_an_explicit_unresolved_record(self):
        _, target = self.queued_carrier_fixture()
        shutil.rmtree(target)
        result = self.ex.receive('origin', None, 'test carrier reader', [], wait=0)
        self.assertFalse(result['received'])
        self.assertEqual(result['carrier']['status'], 'record-unresolved')
        self.assertEqual(result['carrier']['requested_job_id'], 'exchange-origin')
        self.assertFalse(target.exists())

    def test_nonstring_target_identity_remains_unresolved(self):
        _, target = self.queued_carrier_fixture()
        request = json.loads((target / 'request.json').read_text())
        request['job_id'] = None
        write_json(target / 'request.json', request)
        result = self.ex.receive('origin', None, 'test carrier reader', [], wait=0)
        self.assertFalse(result['received'])
        self.assertEqual(result['carrier']['status'], 'record-unresolved')
        self.assertEqual(result['carrier']['requested_job_id'], 'exchange-origin')
        self.assertIn('job identity', result['carrier']['error'])
        self.assertNotIn('job_id', result['carrier'])
        self.assertFalse((self.ex.directory('origin') / 'return.json').exists())

    def test_misbound_dispatch_is_refused_before_carrier_read(self):
        self.queued_carrier_fixture()
        path = self.ex.directory('origin') / 'dispatch.json'
        original = json.loads(path.read_text())
        for field, wrong in [('job_id', 'unrelated-one'), ('request_sha256', '0' * 64)]:
            with self.subTest(field=field):
                write_json(path, original | {field: wrong})
                with patch.object(Collector, 'views', side_effect=AssertionError('wrong carrier read')):
                    with self.assertRaisesRegex(ValueError, 'dispatch does not match'):
                        self.ex.receive('origin', None, 'test carrier reader', [], wait=0)
                self.assertFalse((self.ex.directory('origin') / 'return.json').exists())

    def test_context_survives_workspace_loss_with_exact_provenance(self):
        expected = (self.ex.directory('origin') / 'context.md').read_bytes()
        binding = json.loads((self.ex.directory('origin') / 'binding.json').read_text())
        before = self.retained_bytes()
        shutil.rmtree(self.work)
        result = Exchange(self.root).context('origin')
        self.assertEqual(result['context'].encode('utf-8'), expected)
        self.assertEqual(result['packet'], 'origin')
        self.assertEqual(result['consumer'], 'writer')
        self.assertEqual(result['request_sha256'], binding['request_sha256'])
        self.assertEqual(result['context_sha256'], hashlib.sha256(expected).hexdigest())
        self.assertEqual(result['context_bytes'], len(expected))
        self.assertEqual(before, self.retained_bytes())

    def test_context_rejects_corrupt_immutable_text_and_source(self):
        context = self.ex.directory('origin') / 'context.md'
        original = context.read_bytes()
        context.write_bytes(original + b'changed')
        with self.assertRaisesRegex(ValueError, 'frozen request/context changed'):
            self.ex.context('origin')
        context.write_bytes(original)
        request = self.ex.load('origin')[1]
        (self.root / 'objects' / request['methods'][0]['sha256']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'content object changed'):
            self.ex.context('origin')

    def test_context_does_not_reconcile_publication_intent(self):
        reply = self.base / 'reply.md'; reply.write_text('Actual caller-authored return.')
        self.ex.receive('origin', reply, 'test author', ['answer.md=' + str(reply)])
        intent = self.ex.directory('origin') / 'publish-observed.json'
        write_json(intent, {'phase': 'intent', 'path': 'answer.md'})
        before = self.retained_bytes()
        result = self.ex.context('origin')
        self.assertIn('ORIGINAL-METHOD', result['context'])
        self.assertEqual(before, self.retained_bytes())
        self.assertFalse((self.work / 'answer.md').exists())
        self.assertEqual(json.loads(intent.read_text())['phase'], 'intent')

    def test_cli_context_text_is_exact_and_json_retains_binding(self):
        command = [sys.executable, '-B', str(ROOT / 'work.py'), '--root', str(self.root),
                   'context', 'origin']
        before = self.retained_bytes()
        text = subprocess.run(command + ['--text'], capture_output=True, timeout=10)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertEqual(text.stdout, (self.ex.directory('origin') / 'context.md').read_bytes())
        structured = subprocess.run(command, capture_output=True, timeout=10)
        self.assertEqual(structured.returncode, 0, structured.stderr)
        result = json.loads(structured.stdout)
        self.assertEqual(result['context'].encode(), text.stdout)
        self.assertEqual(result['context_sha256'], sha(text.stdout))
        self.assertEqual(before, self.retained_bytes())


if __name__ == '__main__':
    unittest.main()
