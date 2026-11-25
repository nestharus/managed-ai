"""A selected observation uses one packet and preserves Exchange transport precedence."""
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

from capture_native import reviewed_engine, turn_summary

ENGINE = (
    REPO_ROOT / 'engine/store/manage_store.py'
    if (REPO_ROOT / 'engine/store/manage_store.py').is_file()
    else REPO_ROOT / 'engine/runtime/manage_store.py'
)


class PacketSummary(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        native, _ = reviewed_engine(ENGINE)
        store = self.root / 'store'
        native.System.initialize(store, {'contribution': 'Observe the selected transport.'})
        self.system = native.System(store)
        self.ex = self.system.ex
        self.packet = self.ex.directory('start')
        other = self.packet.with_name('another-private-packet')
        other.mkdir()
        (other / 'request.json').write_bytes(b'invalid fixture')
        self.reply = self.root / 'reply.md'
        self.reply.write_text('Retained answer.')

    def compare(self, status):
        listed = {r['packet']: r for r in self.ex.list()}
        self.assertEqual(listed['another-private-packet']['transport'], 'record-unresolved')
        self.assertEqual(listed['start']['transport'], status)
        iterdir = Path.iterdir
        def selected_only(directory):
            self.assertNotEqual(directory, self.packet.parent, 'selected route listed the packet root')
            return iterdir(directory)

        with patch.object(self.ex, 'load', wraps=self.ex.load) as loads, \
             patch.object(Path, 'iterdir', selected_only), \
             patch.object(self.ex, 'list', side_effect=AssertionError('selected route enumerated packets')):
            row = turn_summary(self.system, 'start')
        self.assertEqual([call.args[0] for call in loads.call_args_list], ['start'])
        self.assertEqual(row, listed['start'])
        self.assertNotIn('another-private-packet', json.dumps(row))
        return row

    def test_prepared_returned_and_changed_basis(self):
        self.compare('prepared')
        _, request = self.ex.load('start')
        source = self.system.work / request['brief']['path']
        source.write_text('A different current working copy.')
        self.compare('prepared')
        self.system.receive('start', self.reply, 'reader', [])
        self.compare('returned')

    def test_actual_failed_launch_attempt_terminal_account_and_damaged_barrier(self):
        self.system.run('start', [str(self.root / 'absent-command'), '{context}'])
        self.compare('dispatch-attempted')
        observed = self.system.receive('start', None, 'reader', [], wait=8)
        self.assertFalse(observed['received'])
        with self.system._selected_exchange() as owner:
            outcome = owner.close_transport('start', 'The executable was absent.')
        row = self.compare('terminal-without-return')
        self.assertEqual(row['transport_outcome'], {k: outcome[k] for k in ('reason', 'account', 'outcome_sha256')})
        (self.packet / 'transport-outcome.json').write_bytes(b'{')
        self.compare('record-unresolved')

    def test_unreadable_publication_and_missing_packet_are_unresolved(self):
        self.compare('prepared')
        (self.packet / 'publish-damaged.json').write_bytes(b'{')
        self.compare('record-unresolved')
        row = turn_summary(self.system, 'missing')
        self.assertEqual((row['packet'], row['transport']), ('missing', 'record-unresolved'))

    def test_return_and_completed_target_keep_their_separate_status(self):
        target = 'answer.md'
        self.system.context('delivery', {'targets': [target]})
        self.system.receive('delivery', self.reply, 'reader', [target+'='+str(self.reply)])
        self.system.adopt('delivery', target, 'Accept the observed reply.')
        row = turn_summary(self.system, 'delivery')
        self.assertEqual(row, next(r for r in self.ex.list() if r['packet'] == 'delivery'))
        self.assertEqual(row['transport'], 'returned')
        self.assertEqual(row['publications'], [{'path': target, 'phase': 'complete'}])
