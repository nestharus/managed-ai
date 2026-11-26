"""Selected capture through the host and its qualified native exchange."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[4]
HOST_DIR = REPO_ROOT / 'engine/host/continuing_work'
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

import capture_native
from continuing_work import Host


def _default_engine():
    primary = REPO_ROOT / 'engine/store/manage_store.py'
    return primary if primary.is_file() else REPO_ROOT / 'engine/runtime/manage_store.py'


class SelectedCapture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        commission = root / 'commission.md'
        commission.write_text('Read the declared observation.', encoding='utf-8')
        self.host = Host(root / 'host')
        entry = _default_engine()
        self.host.init({'commission': str(commission), 'engine': str(entry),
                        'duties': [{'id': 'observe', 'text': 'Read the declared observation.'}]})
        self.host.prepare(dict(id='one', task='Read the source.', prediction='It remains available.',
                               duty_ids=['observe'], outputs=['answer.md'], sources=[]))
        self.receipt = root / 'receipt.json'
        self.supply('original')

    def supply(self, receipt_id):
        self.receipt.write_text(json.dumps({'id': receipt_id, 'assessments': [{
            'item': 'continuing-commission', 'disposition': 'considered', 'account': 'Read.'}]}))

    def capture(self):
        return self.host.capture('one', {'receipt': str(self.receipt), 'producer': 'reader'})

    def test_capture_uses_same_input_for_its_filename_and_native_receipt(self):
        assess = self.host.capture_native
        def change_caller_file(*args):
            self.supply('changed')
            return assess(*args)

        with patch.object(self.host, 'capture_native', side_effect=change_caller_file) as calls:
            captured = self.capture()
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(captured['attention']['receipt'], 'original')
        self.assertEqual(captured, json.loads((self.host.root / 'captures/one/original.json').read_bytes()))
        self.supply('original')
        with self.assertRaisesRegex(ValueError, 'Retain the existing candidate capture'):
            self.capture()

    def test_selected_capture_skips_unrelated_packet_and_refuses_returned_turn(self):
        other = self.host.native / 'exchange/packets/unrelated-private-fixture'
        other.mkdir()
        (other / 'request.json').write_bytes(b'damaged fixture')
        captured = self.capture()
        self.assertEqual(captured['turn'], {'id': 'one', 'transport': 'prepared', 'publication_count': 0})
        self.assertEqual(set(captured), {'id', 'attention', 'turn', 'parent_duties_closed', 'scope'})
        self.assertEqual(set(captured['attention']), {'receipt', 'object', 'recorded', 'boundary'})
        self.assertNotIn(other.name, json.dumps(captured))
        self.assertEqual(captured['attention']['recorded'], ['continuing-commission'])
        with self.assertRaisesRegex(ValueError, 'receipt identity already used'):
            self.host.capture_native('one', self.receipt, 'reader')
        self.host.call('receive', 'one', '--reply', self.receipt, '--producer', 'reader')
        self.supply('next')
        with self.assertRaisesRegex(ValueError, capture_native.RETURN_ERROR):
            self.capture()
        self.assertFalse((self.host.root / 'captures/one/next.json').exists())

    def test_damaged_return_blocks_assessment_before_any_credit(self):
        barrier = self.host.native / 'exchange/packets/one/return.json'
        barrier.write_bytes(b'{')
        with self.assertRaisesRegex(ValueError, 'Candidate capture could not be confirmed'):
            self.capture()
        self.assertFalse((self.host.root / 'captures/one/original.json').exists())
        barrier.unlink()
        self.assertEqual(self.capture()['attention']['receipt'], 'original')

    def test_terminal_transport_keeps_the_existing_attention_authority(self):
        native, _ = capture_native.reviewed_engine(Path(self.host.engine))
        system = native.System(self.host.native)
        with native.exclusive(system.ex.root / 'owner.lock'):
            system.run('one', [str(self.host.root / 'absent-command'), '{context}'])
            self.assertFalse(system.receive('one', None, 'reader', [], wait=8)['received'])
            system.ex.close_transport('one', 'The fixture executable was absent.')
        captured = self.capture()
        self.assertEqual(captured['turn']['transport'], 'terminal-without-return')
        self.assertEqual(captured['attention']['receipt'], 'original')
        self.assertFalse(captured['parent_duties_closed'])
