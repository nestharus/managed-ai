"""Qualify the owner operation for reading completed import acknowledgements."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

if __package__:
    from .native_test_paths import APP
else:
    from native_test_paths import APP

from continuation_support.storage import write_json
from native_runtime.record_import import import_records, read_receipt
from system import System


def specification(ident):
    return {'format': 'managed-ai-import/1', 'id': ident, 'records': [
        {'id': ident, 'kind': 'observation', 'content': ident}]}


class ImportReceiptTest(unittest.TestCase):
    def test_completed_acknowledgements_remain_origin_identities(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / 'store'
            System.initialize(root, {'contribution': 'Check import receipt custody.'})
            system = System(root)
            completed = specification('complete-import')
            interrupted = specification('interrupted-import')
            foreign = specification('foreign-import')

            accepted = import_records(system, completed)
            self.assertEqual(read_receipt(system, completed), accepted)
            with patch.object(system, 'put', side_effect=RuntimeError('interruption')):
                with self.assertRaisesRegex(RuntimeError, 'interruption'):
                    import_records(system, interrupted)
            self.assertFalse(read_receipt(system, interrupted)['complete'])
            other = import_records(system, foreign)
            write_json(system.work / '.system/imports/foreign-import.json', dict(other, instance_id='another-store'))
            with self.assertRaisesRegex(ValueError, 'another store or import identity'):
                read_receipt(system, foreign)

            system.put(dict(completed['records'][0], content='new head'), 1)
            self.assertEqual(read_receipt(system, completed), accepted)
            self.assertEqual(list(system.completed_import_records()), accepted['records'])
