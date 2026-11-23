"""The source service exports only receipts that its serialized owner published."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from source_tools import encoded, identity
from workbench_agent import Workbench


class ReceiptPublication(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        corpus = self.root / 'corpus'
        corpus.mkdir()
        for name in ('one.md', 'two.md'):
            (corpus / name).write_text(name)
        self.config = dict(corpus_root=str(corpus), workspace=str(self.root / 'work'),
                           state_dir=str(self.root / 'controller'), task='Read both sources.')
        self.owner = Workbench(self.config)
        self.service = self.owner.source_service
        self.addCleanup(self.service.close)

    def read(self, name):
        return self.owner.source_read({'path': name})['sources'][0]['receipt_id']

    def test_failed_replace_cannot_seed_a_retry_or_another_receipt(self):
        observations = []
        def refuse(staging, destination):
            candidate = json.loads(staging.read_bytes())
            observations.append((next(iter(candidate['sources'])), self.service.receipts()))
            raise OSError('before replace')

        with patch('source_tools.os.replace', side_effect=refuse):
            with self.assertRaisesRegex(OSError, 'before replace'):
                self.read('one.md')
        failed, observed = observations[0]
        self.assertFalse(self.service.ledger_path.exists())
        self.assertNotIn(failed, observed['sources'])
        self.assertNotIn(failed, self.service.receipts()['sources'])
        self.assertEqual(self.owner.checkpoint()['source_receipt_ids'], [])

        other = self.read('two.md')
        self.assertEqual(set(json.loads(self.service.ledger_path.read_bytes())['sources']), {other})
        self.assertEqual(self.read('one.md'), failed)
        self.assert_reopens({failed, other})

    def test_identical_retry_writes_after_a_prepublication_fault(self):
        with patch('source_tools.os.replace', side_effect=OSError('before replace')):
            with self.assertRaisesRegex(OSError, 'before replace'):
                self.read('one.md')
        receipt = self.read('one.md')
        self.assert_reopens({receipt})

    def test_failed_refresh_keeps_the_published_inventory_and_warm_index(self):
        previous = self.service.inventory()['receipt_id']
        self.read('one.md')
        index = self.service.db
        (self.service.root / 'two.md').write_text('A new observation.')
        with patch('source_tools.os.replace', side_effect=OSError('before replace')):
            with self.assertRaisesRegex(OSError, 'before replace'):
                self.service.refresh()
        self.assertIs(self.service.db, index)
        self.assertTrue(self.service.read('one.md')['from_cache'])
        selection = self.service.discover('one')
        exported = json.loads(self.service.ledger_path.read_bytes())
        self.assertEqual(selection['inventory_id'], previous)
        self.assertEqual(set(exported['inventories']), {previous})
        self.assertIn(selection['receipt_id'], exported['selections'])

    def test_lost_replace_response_accepts_only_the_exact_postimage(self):
        self.read('one.md')
        replace = os.replace
        def lose_response(staging, destination):
            replace(staging, destination)
            raise OSError('lost replace response')

        with patch('source_tools.os.replace', side_effect=lose_response) as writes:
            receipt = self.read('two.md')
            self.assertEqual(self.read('two.md'), receipt)
            self.assertEqual(writes.call_count, 1)
        self.assert_reopens(set(self.owner.source_receipt_ids))

    def test_interrupt_after_replace_keeps_the_row_on_the_next_write(self):
        replace = os.replace
        def interrupt(staging, destination):
            replace(staging, destination)
            raise KeyboardInterrupt

        with patch('source_tools.os.replace', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.read('one.md')
        first, = json.loads(self.service.ledger_path.read_bytes())['sources']
        second = self.read('two.md')
        self.assert_reopens({first, second})

    def test_different_postimage_does_not_acknowledge_or_overwrite_it(self):
        self.read('one.md')
        replace = os.replace
        different = {'kind': 'source', 'path': 'a different serialized result'}
        key = identity(different)
        def publish_other_image(staging, destination):
            other = json.loads(destination.read_bytes())
            other['sources'][key] = different
            staging.write_bytes(encoded(other) + b'\n')
            replace(staging, destination)
            raise OSError('different postimage')

        with patch('source_tools.os.replace', side_effect=publish_other_image):
            with self.assertRaisesRegex(OSError, 'different postimage'):
                self.read('two.md')
        self.assertIn(key, self.service.receipts()['sources'])
        self.read('two.md')
        self.assert_reopens({key, *self.owner.source_receipt_ids})

    def test_unavailable_readback_reopens_before_a_different_write(self):
        replace = os.replace
        read_bytes = Path.read_bytes
        def lose_response(staging, destination):
            replace(staging, destination)
            raise OSError('lost replace response')

        def unavailable(path):
            if path == self.service.ledger_path:
                raise PermissionError('readback unavailable')
            return read_bytes(path)

        with patch('source_tools.os.replace', side_effect=lose_response), patch.object(Path, 'read_bytes', unavailable):
            with self.assertRaisesRegex(OSError, 'readback unavailable'):
                self.read('one.md')
        first, = json.loads(self.service.ledger_path.read_bytes())['sources']
        second = self.read('two.md')
        self.assert_reopens({first, second})

    def assert_reopens(self, expected):
        path = self.service.ledger_path
        exported = self.service.receipts()
        self.assertEqual(set(exported['sources']), expected)
        self.assertEqual(path.read_bytes(), encoded(exported) + b'\n')
        script = ("import json,sys; from workbench_agent import Workbench; "
                  "w=Workbench(json.loads(sys.argv[1]),resume=True); "
                  "w.restore(json.loads(sys.argv[2])); print(json.dumps(w.source_service.receipts()))")
        raw = subprocess.check_output([sys.executable, '-B', '-c', script,
                                       json.dumps(self.config), json.dumps(self.owner.checkpoint())],
                                      cwd=Path(__file__).parent)
        self.assertEqual(json.loads(raw), exported)
