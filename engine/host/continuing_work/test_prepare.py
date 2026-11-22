"""Host preparation through its native store, on disposable working files."""
from pathlib import Path
import tempfile
import unittest

from continuing_work import Host


class PrepareScope(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.source = root / 'example.md'
        self.source.write_text('First source edition.')
        commission = root / 'commission.md'
        commission.write_text('Keep a recoverable source observation.')
        self.host = Host(root / 'host')
        engine = Path(__file__).resolve().parents[2] / 'runtime/manage_store.py'
        self.host.init({'commission': str(commission), 'engine': str(engine),
                        'duties': [{'id': 'observe', 'text': 'Observe the source.'}]})
        self.request = {'id': 'first', 'task': 'Inspect the observation.',
                        'prediction': 'The exact edition remains available.',
                        'duty_ids': ['observe'], 'outputs': ['answer.md'],
                        'sources': [{'id': 'example', 'path': str(self.source)}]}

    def host_files(self):
        return {p.relative_to(self.host.root): p.read_bytes()
                for p in self.host.root.rglob('*') if p.is_file()}

    def test_invalid_request_leaves_previous_resources_and_native_store_intact(self):
        self.host.prepare(self.request)
        self.source.write_text('Changed source, still waiting to be selected.')
        before = self.host_files()
        for fields, error in (({'outputs': ['same', 'same']}, 'distinct new output names'),
                              ({'overlap_limit': 0}, 'overlap_limit'),
                              ({'questions': [{'question': 'Which edition?', 'sources': ['absent']}]},
                               'unselected resource IDs')):
            with self.subTest(fields=fields):
                with self.assertRaisesRegex(ValueError, error):
                    self.host.prepare(dict(self.request, id='second', **fields))
                self.assertEqual(self.host_files(), before)

        result = self.host.prepare(dict(self.request, id='second'))
        self.assertEqual(result['changed_sources'], 1)
        self.assertEqual(result['targets'], ['outputs/second/answer.md'])
        self.assertEqual((self.host.work / 'sources/resources/example.md').read_bytes(),
                         self.source.read_bytes())
        self.assertTrue((self.host.root / 'scopes/second.json').is_file())

    def test_native_attention_refusal_retains_prepared_inputs_without_a_completed_scope(self):
        with self.assertRaisesRegex(ValueError, 'unknown active item or group'):
            self.host.prepare(dict(self.request, attention_items=['undelivered-question']))
        self.assertEqual((self.host.work / 'sources/resources/example.md').read_bytes(),
                         self.source.read_bytes())
        self.assertTrue((self.host.root / 'overlap-index/first.json').is_file())
        self.assertFalse((self.host.root / 'scopes/first.json').exists())
