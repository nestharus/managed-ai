"""Wave 4 regression tests for companions/representation (W4-B2, W4-B6)."""
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
REP_DIR = REPO_ROOT / 'companions/representation'
if str(REP_DIR) not in sys.path:
    sys.path.insert(0, str(REP_DIR))

import representation


class RepresentationWave4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_run_cli_action_acquires_owner_lock_before_constructing_system(self):
        events = []
        store_root = self.root / 'store'
        work = store_root / 'work'
        exchange = store_root / 'exchange'
        work.mkdir(parents=True)
        exchange.mkdir(parents=True)
        (work / 'basis.md').write_text('UTF-8 café basis.\n', encoding='utf-8')

        @contextmanager
        def fake_exclusive(lock_path):
            events.append(('lock_enter', Path(lock_path).name))
            try:
                yield
            finally:
                events.append(('lock_exit', Path(lock_path).name))

        class FakeLayout:
            def __init__(self, root):
                self.exchange = Path(root) / 'exchange'

        class FakeExchange:
            def __init__(self):
                self.root = exchange
                self.workspace = work

            def path(self, rel):
                return work / rel

        class FakeSystem:
            def __init__(self, root):
                events.append(('system_init', str(root)))
                self.root = Path(root)
                self.ex = FakeExchange()

        args = Namespace(
            engine=self.root / 'engine',
            root=store_root,
            action='pin',
            path='basis.md',
            accepted_in=None,
        )
        fake_loader = lambda engine: (FakeSystem, fake_exclusive, FakeLayout, lambda x: x, lambda x: x)
        with patch.object(representation, '_artifact_target_fn', return_value=lambda x: x):
            result = representation.run_cli_action(args, engine_loader=fake_loader)
        self.assertEqual(result['path'], 'basis.md')
        self.assertEqual(
            [e[0] for e in events],
            ['lock_enter', 'system_init', 'lock_exit'],
        )
        self.assertEqual(events[0][1], 'owner.lock')

    def test_prepare_cleans_up_directory_on_context_failure_so_retry_succeeds(self):
        store = self.root / 'store'
        work = store / 'work'
        work.mkdir(parents=True)
        src = work / 'basis.md'
        src.write_text('UTF-8 café basis.\n', encoding='utf-8')
        attempts = {'n': 0}

        class FakeExchange:
            def __init__(self):
                self.workspace = work

            def path(self, rel):
                return work / rel

        class FakeSystem:
            def __init__(self):
                self.root = store
                self.ex = FakeExchange()

            def context(self, packet_id, spec):
                attempts['n'] += 1
                if attempts['n'] == 1:
                    raise RuntimeError('transient context failure')
                return {'packet': packet_id, 'spec': spec}

        sys_obj = FakeSystem()
        ops = representation.RepresentationOperations(sys_obj)
        src_pin = {'path': 'basis.md', 'sha256': representation.digest(src.read_bytes())}
        spec = {
            'format': representation.OPERATION,
            'turn': 'rep-1',
            'mode': 'develop',
            'task': 'Describe basis.',
            'consumer': 'reviewer',
            'representations': [],
            'sources': [src_pin],
            'targets': ['out.json'],
            'applications': [{
                'id': 'app-1',
                'sources': ['basis.md'],
                'representations': [],
                'operation': 'Summarize basis.',
                'prediction': 'Produces out.json.',
            }],
            'interactions': {'account': 'Independent unit test.', 'unknowns': []},
        }
        with patch.object(representation, '_packet_id_fn', return_value=lambda x: x), \
             patch.object(representation, '_artifact_target_fn', return_value=lambda x: x):
            with self.assertRaisesRegex(RuntimeError, 'transient context failure'):
                ops.prepare(spec)
            rep_dir = work / 'representation-operations/rep-1'
            # Directory was cleaned up so immediate retry with same turn is NOT wedged!
            self.assertFalse(rep_dir.exists())

            res = ops.prepare(spec)
            self.assertEqual(res['packet'], 'rep-1')
            self.assertTrue((rep_dir / 'operation.json').is_file())
            self.assertTrue((rep_dir / 'contract.md').is_file())

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers(self):
        dup = self.root / 'dup.json'
        dup.write_text('{"id":"a","id":"b"}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            representation.read_json(dup)

        nan_file = self.root / 'nan.json'
        nan_file.write_text('{"val":NaN}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'nonfinite JSON'):
            representation.read_json(nan_file)

    # --- W5-B11: NTFS junction / symlink refusal in pin(), check_pin(), and _atomic_bytes() ---
    def test_pin_check_pin_and_atomic_bytes_refuse_reparse_or_symlink(self):
        import os
        import subprocess
        store = self.root / 'store'
        work = store / 'work'
        work.mkdir(parents=True)
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'secret.json').write_text('{"k": 1}\n', encoding='utf-8')
        link_dir = work / 'linked'
        if os.name == 'nt':
            proc = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link_dir), str(outside)], capture_output=True)
            if proc.returncode != 0:
                return
            self.addCleanup(lambda: subprocess.run(['cmd', '/c', 'rmdir', str(link_dir)], capture_output=True))
        else:
            link_dir.symlink_to(outside)

        class FakeExchange:
            def __init__(self):
                self.workspace = work

            def path(self, rel):
                return work / rel

        class FakeSystem:
            def __init__(self):
                self.ex = FakeExchange()

        ops = representation.RepresentationOperations(FakeSystem())
        with patch.object(representation, '_artifact_target_fn', return_value=lambda x: x):
            with self.assertRaisesRegex(ValueError, 'ordinary file'):
                ops.pin('linked/secret.json')
            with self.assertRaisesRegex(ValueError, 'ordinary file'):
                ops.check_pin({
                    'path': 'linked/secret.json',
                    'sha256': representation.digest((outside / 'secret.json').read_bytes()),
                })
        with self.assertRaisesRegex(ValueError, 'symlink or reparse point'):
            representation._atomic_bytes(link_dir / 'out.json', b'{}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
