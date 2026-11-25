"""Regression tests for signal_field/queue.py guards, dispatch allowlist, and worker diagnostics (AU2, OE1)."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

QUEUE_PATH = Path(__file__).with_name('queue.py')
spec = importlib.util.spec_from_file_location('signal_field_queue_under_test', QUEUE_PATH)
queue_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue_mod)

PRODUCT_ROOT = QUEUE_PATH.resolve().parents[2]


def sample_use(ident='u1', mode='live'):
    return {
        'id': ident,
        'mode': mode,
        'question': 'What changed?',
        'purpose': 'Keep bounded observation current.',
        'sources': ['s1'],
        'observer': {'definition': 'test-observer'},
        'scope': {'coverage': 'unit'},
    }


class SignalFieldQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / 'queue.sqlite'

    def run_cli(self, request_obj, *, db=None, raw_bytes=None, extra_args=()):
        db = self.db_path if db is None else db
        payload = raw_bytes if raw_bytes is not None else json.dumps(request_obj).encode('utf-8')
        return subprocess.run(
            [sys.executable, '-B', str(QUEUE_PATH), '--db', str(db), *extra_args],
            input=payload, capture_output=True,
        )

    # --- AU2: Product-directory containment & ordinary-file/symlink guards ---
    def test_refuses_database_inside_product_directory(self):
        for candidate in (
            PRODUCT_ROOT / 'host/signal_field/forbidden.sqlite',
            PRODUCT_ROOT / 'runtime/forbidden.sqlite',
            PRODUCT_ROOT / 'forbidden.sqlite',
        ):
            with self.subTest(candidate=str(candidate)):
                with self.assertRaisesRegex(ValueError, 'outside the product directory'):
                    queue_mod.Field(candidate)
                proc = self.run_cli({'op': 'status'}, db=candidate)
                self.assertEqual(proc.returncode, 2)
                self.assertIn('outside the product directory', json.loads(proc.stderr)['reason'])
                self.assertFalse(candidate.exists())

    def test_refuses_symlink_and_directory_database_paths(self):
        # Directory as --db is refused.
        db_dir = self.root / 'as_dir'
        db_dir.mkdir()
        with self.assertRaisesRegex(ValueError, 'ordinary database file'):
            queue_mod.Field(db_dir)

        if os.name == 'nt':
            # Symlink creation on Windows may require elevated privileges; test if permitted.
            probe = self.root / 'probe_link'
            try:
                probe.symlink_to(self.root / 'probe_target')
            except OSError:
                return
            probe.unlink()

        # Direct symlink for --db is refused.
        real_target = self.root / 'real.sqlite'
        real_target.write_bytes(b'')
        link_db = self.root / 'link.sqlite'
        link_db.symlink_to(real_target)
        with self.assertRaisesRegex(ValueError, 'must not be a symlink'):
            queue_mod.Field(link_db)

        # Symlink parent escaping into product directory is refused.
        link_dir = self.root / 'escape_dir'
        link_dir.symlink_to(PRODUCT_ROOT / 'host')
        with self.assertRaisesRegex(ValueError, 'outside the product directory'):
            queue_mod.Field(link_dir / 'escaped.sqlite')

        # Symlinked journal sidecar is refused.
        good_db = self.root / 'sidecar_test.sqlite'
        wal_link = Path(str(good_db) + '-wal')
        wal_link.symlink_to(real_target)
        with self.assertRaisesRegex(ValueError, 'must not be a symlink'):
            queue_mod.Field(good_db)

        # Symlinked --request file is refused by CLI.
        req_real = self.root / 'req.json'
        req_real.write_text('{"op":"status"}')
        req_link = self.root / 'req_link.json'
        req_link.symlink_to(req_real)
        proc = subprocess.run(
            [sys.executable, '-B', str(QUEUE_PATH), '--db', str(self.db_path), '--request', str(req_link)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn('must not be a symlink', json.loads(proc.stderr)['reason'])

    # --- AU2: Explicit operation allowlist & parameter validation (no blind getattr) ---
    def test_explicit_dispatch_allowlist_and_parameter_validation(self):
        # Internal / private methods must be refused.
        for bad_op in ('execute', 'one', 'all', 'mutation', 'ticket', 'queue', 'close', '__init__', 'basis'):
            proc = self.run_cli({'op': bad_op})
            self.assertEqual(proc.returncode, 2)
            self.assertIn('unknown operation', json.loads(proc.stderr)['reason'])

        # Unexpected extra parameters on valid operations must be refused.
        for bad_req, expected in (
            ({'op': 'status', 'unexpected': 1}, 'unsupported parameters'),
            ({'op': 'pull', 'extra': True}, 'unsupported parameters'),
            ({'op': 'reconcile', 'force': True}, 'unsupported parameters'),
            ({'op': 'enroll'}, 'missing required parameters'),
            ({'op': 'change', 'source': 's1', 'epoch': 'e1', 'position': 1, 'value': 1, 'notify': 'yes'}, 'booleans'),
            ({'op': 'run', 'argv': [sys.executable, '-c', 'x\x00y'], 'budget': 1, 'timeout': 5}, 'explicit subprocess argv'),
            ({'op': 'run', 'argv': [sys.executable], 'budget': True, 'timeout': 5}, 'positive invocation budget'),
        ):
            with self.subTest(bad_req=bad_req):
                proc = self.run_cli(bad_req)
                self.assertEqual(proc.returncode, 2)
                self.assertIn(expected, json.loads(proc.stderr)['reason'])

        # Duplicate keys and non-finite JSON must be refused.
        proc_dup = self.run_cli(None, raw_bytes=b'{"op":"status","op":"pull"}')
        self.assertEqual(proc_dup.returncode, 2)
        self.assertIn('duplicate JSON key', json.loads(proc_dup.stderr)['reason'])

        proc_nan = self.run_cli(None, raw_bytes=b'{"op":"run","argv":["x"],"budget":1,"timeout":NaN}')
        self.assertEqual(proc_nan.returncode, 2)
        self.assertIn('nonfinite JSON', json.loads(proc_nan.stderr)['reason'])

        # Abbreviated CLI options must be refused (allow_abbrev=False).
        proc_abbrev = subprocess.run(
            [sys.executable, '-B', str(QUEUE_PATH), '--d', str(self.db_path)],
            input=b'{"op":"status"}', capture_output=True,
        )
        self.assertNotEqual(proc_abbrev.returncode, 0)

        # Valid end-to-end CLI operations work cleanly.
        self.assertEqual(self.run_cli({'op': 'enroll', 'spec': sample_use()}).returncode, 0)
        self.assertEqual(self.run_cli({'op': 'change', 'source': 's1', 'epoch': 'e1', 'position': 1, 'value': 'v1'}).returncode, 0)
        pulled = json.loads(self.run_cli({'op': 'pull'}).stdout)
        self.assertEqual(pulled['use'], 'u1')
        rec = json.loads(self.run_cli({'op': 'receive', 'packet': pulled,
                                       'result': {'state': 'complete', 'account': 'Observed v1.'}}).stdout)
        self.assertEqual(rec['status'], 'received')

    # --- OE1: Retain returncode, stdout, stderr in structured result and SQLite ---
    def test_worker_failure_diagnostics_retained_and_reconstructible(self):
        with queue_mod.Field(self.db_path) as field:
            field.enroll(sample_use('u1'))
            # 1. Non-zero exit retains returncode, stdout, stderr in return AND SQLite.
            fail_argv = [
                sys.executable, '-c',
                "import sys; sys.stdout.write('partial-out'); sys.stderr.write('structured-refusal: bad flag'); sys.exit(7)",
            ]
            res = field.run(fail_argv, budget=1, timeout=10)
            self.assertEqual(res['status'], 'worker-unresolved')
            self.assertEqual(res['worker']['kind'], 'non-zero-exit')
            self.assertEqual(res['worker']['returncode'], 7)
            self.assertEqual(res['worker']['stdout'], 'partial-out')
            self.assertEqual(res['worker']['stderr'], 'structured-refusal: bad flag')
            self.assertFalse(res['worker']['stdout_truncated'])
            self.assertFalse(res['worker']['stderr_truncated'])
            job_id = res['in_flight_job']

        # Reopen fresh connection (simulating restart) and inspect SQLite-retained worker_outcome!
        with queue_mod.Field(self.db_path) as reopened:
            inspected = reopened.inspect(job_id=job_id)
            self.assertEqual(inspected['worker_outcome']['kind'], 'non-zero-exit')
            self.assertEqual(inspected['worker_outcome']['returncode'], 7)
            self.assertEqual(inspected['worker_outcome']['stdout'], 'partial-out')
            self.assertEqual(inspected['worker_outcome']['stderr'], 'structured-refusal: bad flag')
            pending = reopened.pending()
            self.assertEqual(pending['items'][0]['worker_outcome']['returncode'], 7)

            # Retry clears worker_outcome and lets us test distinct failure modes cleanly.
            reopened.retry(job_id, inspected['token'])
            self.assertIsNone(reopened.inspect(job_id=job_id)['worker_outcome'])

            # 2. Launch failure (missing executable) is cleanly distinguishable from non-zero exit.
            missing = str(self.root / 'no-such-worker-binary')
            res_launch = reopened.run([missing], budget=1, timeout=10)
            self.assertEqual(res_launch['status'], 'worker-unresolved')
            self.assertEqual(res_launch['worker']['kind'], 'launch-error')
            self.assertIsNone(res_launch['worker']['returncode'])
            self.assertIsNotNone(res_launch['worker']['errno'])
            inspected_launch = reopened.inspect(job_id=job_id)
            self.assertEqual(inspected_launch['worker_outcome']['kind'], 'launch-error')

            # 3. Zero exit with invalid JSON stdout is distinguishable from non-zero exit.
            reopened.retry(job_id, inspected_launch['token'])
            res_badjson = reopened.run([sys.executable, '-c', "print('not-json')"], budget=1, timeout=10)
            self.assertEqual(res_badjson['status'], 'worker-unresolved')
            self.assertEqual(res_badjson['worker']['kind'], 'invalid-return')
            self.assertEqual(res_badjson['worker']['returncode'], 0)
            self.assertIn('not-json', res_badjson['worker']['stdout'])
            inspected_badjson = reopened.inspect(job_id=job_id)
            self.assertEqual(inspected_badjson['worker_outcome']['kind'], 'invalid-return')

            # 4. Timeout is cleanly distinguishable and retains timeout details.
            reopened.retry(job_id, inspected_badjson['token'])
            res_timeout = reopened.run([sys.executable, '-c', "import time; time.sleep(5)"], budget=1, timeout=0.2)
            self.assertEqual(res_timeout['status'], 'worker-unresolved')
            self.assertEqual(res_timeout['worker']['kind'], 'timeout')
            self.assertIsNone(res_timeout['worker']['returncode'])
            self.assertEqual(res_timeout['worker']['timeout'], 0.2)
            inspected_timeout = reopened.inspect(job_id=job_id)
            self.assertEqual(inspected_timeout['worker_outcome']['kind'], 'timeout')

            # 5. Oversized stderr on non-zero exit is bounded cleanly.
            reopened.retry(job_id, inspected_timeout['token'])
            big_size = queue_mod.MAX_WORKER_STREAM_BYTES + 4096
            res_big = reopened.run(
                [sys.executable, '-c', f"import sys; sys.stderr.write('E' * {big_size}); sys.exit(3)"],
                budget=1, timeout=10,
            )
            self.assertEqual(res_big['worker']['returncode'], 3)
            self.assertTrue(res_big['worker']['stderr_truncated'])
            self.assertEqual(res_big['worker']['stderr_bytes'], big_size)
            self.assertEqual(len(res_big['worker']['stderr'].encode('utf-8')), queue_mod.MAX_WORKER_STREAM_BYTES)

    def test_legacy_database_without_worker_outcome_column_migrates_cleanly(self):
        legacy_db = self.root / 'legacy.sqlite'
        conn = sqlite3.connect(legacy_db)
        conn.executescript('''
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE sources (id TEXT PRIMARY KEY, epoch TEXT NOT NULL, position INTEGER NOT NULL, value TEXT NOT NULL);
        CREATE TABLE uses (id TEXT PRIMARY KEY, spec TEXT NOT NULL, signature TEXT NOT NULL, generation INTEGER NOT NULL, latest TEXT, last_rejected TEXT);
        CREATE TABLE dependencies (source TEXT NOT NULL, use_id TEXT NOT NULL, PRIMARY KEY (source,use_id));
        CREATE TABLE jobs (id TEXT PRIMARY KEY, use_id TEXT NOT NULL, generation INTEGER NOT NULL, basis TEXT NOT NULL, spec TEXT NOT NULL, ticket INTEGER, token TEXT, gap TEXT, attempt TEXT);
        CREATE TABLE selected_history (name TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
        INSERT INTO meta VALUES ('format', 'g60-signal-field/1');
        INSERT INTO meta VALUES ('sequence', '0');
        ''')
        conn.commit()
        conn.close()

        with queue_mod.Field(legacy_db) as field:
            field.enroll(sample_use('migrated'))
            res = field.run([sys.executable, '-c', "import sys; sys.stderr.write('boom'); sys.exit(2)"],
                            budget=1, timeout=10)
            self.assertEqual(res['worker']['returncode'], 2)
            self.assertEqual(field.inspect(job_id=res['in_flight_job'])['worker_outcome']['stderr'], 'boom')


if __name__ == '__main__':
    unittest.main(verbosity=2)
