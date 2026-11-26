"""Regression tests for continuing_work & capture_native hardening (L2, L6, L7, OE6, SO2(2), A5)."""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / 'src'
HERE = SRC_ROOT / 'managed_ai/host/continuing_work'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from managed_ai.host.continuing_work import (
    capture_native,
    continuing_work,
    native_bridge,
    occurrence_files,
)
from managed_ai.host.continuing_work.continuing_work import (
    Host,
    MAX_EVENT_LINE_BYTES,
    MAX_EVENT_STREAM_BYTES,
)

ENGINE = (
    SRC_ROOT / 'managed_ai/store/manage_store.py'
    if (SRC_ROOT / 'managed_ai/store/manage_store.py').is_file()
    else REPO_ROOT / 'engine/runtime/manage_store.py'
)


def _native_engine_available() -> bool:
    if not (SRC_ROOT / 'managed_ai/store/manage_store.py').is_file():
        try:
            capture_native.reviewed_engine(ENGINE)
            return True
        except (ImportError, ModuleNotFoundError, ValueError):
            return False
    try:
        capture_native.reviewed_engine(ENGINE)
        return True
    except (ImportError, ModuleNotFoundError):
        return False


class ContinuingWorkCrossPlatformTests(unittest.TestCase):
    """Cross-platform host tests that run natively on both Windows and POSIX."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.host = Host(self.base / 'host')
        self.host.root.mkdir(parents=True, exist_ok=True)

    def test_cross_platform_file_lock_contention_and_shared_coexistence(self):
        lock_file = self.base / 'host.lock'
        ready = self.base / 'ready.flag'
        release = self.base / 'release.flag'
        holder = "\n".join([
            "import sys, time",
            "from pathlib import Path",
            "sys.path.insert(0, sys.argv[1])",
            "from managed_ai.host.continuing_work.occurrence_files import file_lock",
            "with file_lock(Path(sys.argv[2])):",
            "    Path(sys.argv[3]).write_text('ready', encoding='utf-8')",
            "    deadline = time.monotonic() + 15",
            "    while not Path(sys.argv[4]).exists() and time.monotonic() < deadline:",
            "        time.sleep(0.05)",
        ])
        proc = subprocess.Popen(
            [sys.executable, '-B', '-c', holder, str(SRC_ROOT),
             str(lock_file), str(ready), str(release)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.exists():
                out, err = proc.communicate(timeout=5)
                self.fail(f'Lock holder subprocess failed (rc={proc.returncode}): {err or out}')
            with self.assertRaises(BlockingIOError):
                with occurrence_files.file_lock(lock_file, nonblocking=True):
                    pass
            with self.assertRaises(BlockingIOError):
                with occurrence_files.file_lock(lock_file, shared=True, nonblocking=True):
                    pass
        finally:
            release.write_text('go', encoding='utf-8')
            proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0)

        # After release, shared locks coexist across threads and block nonblocking exclusive writes.
        with occurrence_files.file_lock(lock_file, shared=True, nonblocking=True):
            self.assertEqual(lock_file.stat().st_size, 0)
            thread_results = []
            def second_reader():
                try:
                    with occurrence_files.file_lock(lock_file, shared=True, nonblocking=True):
                        thread_results.append('shared-ok')
                except Exception as exc:
                    thread_results.append(exc)
            t = threading.Thread(target=second_reader)
            t.start()
            t.join(timeout=5)
            self.assertEqual(thread_results, ['shared-ok'])
            with self.assertRaises(BlockingIOError):
                with occurrence_files.file_lock(lock_file, shared=False, nonblocking=True):
                    pass

    def test_native_bridge_pin_event_and_strict_occurrence_io_cross_platform(self):
        sample = self.base / 'sample.txt'
        sample.write_bytes(b'cross-platform payload\n')
        pinned = self.host.pin(sample)
        self.assertEqual((self.host.root / pinned['object']).read_bytes(), b'cross-platform payload\n')

        # Concurrent multi-threaded and multi-process event writes via cross-platform file_lock.
        errors = []
        def worker(tid):
            try:
                for i in range(10):
                    self.host.event('xplat-thread', {'tid': tid, 'seq': i, 'pad': 'q' * 120})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(3)]
        child_code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from managed_ai.host.continuing_work.continuing_work import Host; h = Host(sys.argv[2]); "
            "[h.event('xplat-proc', {'seq': i, 'pad': 'p' * 120}) for i in range(15)]"
        )
        proc = subprocess.Popen([sys.executable, '-B', '-c', child_code, str(SRC_ROOT), str(self.host.root)])
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(proc.wait(), 0)
        self.assertEqual(errors, [])

        lines = (self.host.root / 'EVENTS.jsonl').read_text(encoding='utf-8').splitlines()
        events = [occurrence_files.loads_strict(line) for line in lines]
        self.assertEqual(sum(1 for e in events if e['action'] == 'xplat-thread'), 30)
        self.assertEqual(sum(1 for e in events if e['action'] == 'xplat-proc'), 15)
        self.assertIs(continuing_work.capture_error, native_bridge.capture_error)

    def test_capture_native_uses_canonical_runtime_without_prototype(self):
        native_path, programs = capture_native.native_sources(ENGINE)
        if ENGINE.parent.name == 'store':
            self.assertEqual(native_path, ENGINE.parent / 'system.py')
            self.assertTrue(all(key.startswith(('deployment/', 'core/', 'store/', 'platform/')) for key in programs))
        else:
            self.assertEqual(native_path, ENGINE.parent / 'native/system.py')
            self.assertTrue(all(key.startswith('runtime/') for key in programs))
        self.assertFalse(any(key.startswith('prototype/') for key in programs))


@unittest.skipUnless(_native_engine_available(), 'native runtime store engine unavailable on this platform')
class ContinuingWorkHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.commission = self.base / 'commission.md'
        self.commission.write_text('Keep host custody verifiable and recoverable.')
        self.host = Host(self.base / 'host')
        self.host.init({
            'commission': str(self.commission),
            'engine': str(ENGINE),
            'duties': [{'id': 'custody', 'text': 'Preserve exact receipts and outputs.'}],
        })

    def prepare(self, sid, outputs=('out.md',), sources=()):
        return self.host.prepare({
            'id': sid,
            'task': 'Perform bounded work.',
            'prediction': 'Outputs remain verifiable.',
            'duty_ids': ['custody'],
            'outputs': list(outputs),
            'sources': list(sources),
        })

    # --- L2: capture_native constructs System(root) AFTER acquiring owner.lock ---
    def test_capture_native_locks_before_constructing_system(self):
        self.prepare('turn1')
        lock_path = self.host.native / 'exchange' / 'owner.lock'
        sys_json = self.host.native / 'system.json'
        original_sys = sys_json.read_bytes()
        with occurrence_files.file_lock(lock_path, nonblocking=True):
            try:
                # Temporarily corrupt system.json while the exclusive lock is held.
                # If capture_native wrongly constructed System(root) before locking,
                # it would fail parsing JSON rather than failing immediately on owner.lock.
                sys_json.write_bytes(b'{corrupt-while-locked')
                proc = subprocess.run(
                    [sys.executable, '-B', str(HERE / 'capture_native.py'),
                     '--engine', str(ENGINE), '--root', str(self.host.native),
                     'inspect-turn', 'turn1'],
                    capture_output=True, text=True,
                )
            finally:
                sys_json.write_bytes(original_sys)
        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stderr)
        self.assertIn('another owner holds', payload['error'])

        # Once unlocked, capture_native succeeds cleanly.
        proc_ok = subprocess.run(
            [sys.executable, '-B', str(HERE / 'capture_native.py'),
             '--engine', str(ENGINE), '--root', str(self.host.native),
             'inspect-turn', 'turn1'],
            capture_output=True, text=True,
        )
        self.assertEqual(proc_ok.returncode, 0, proc_ok.stderr)
        self.assertEqual(json.loads(proc_ok.stdout)['turn']['packet'], 'turn1')

    # --- L6 + A5: occurrence_files strict JSON & atomic durable writes ---
    def test_occurrence_files_strict_json_and_atomic_write(self):
        target = self.base / 'doc.json'
        with self.assertRaises(ValueError):
            occurrence_files.write(target, {'bad': math.nan})
        with self.assertRaises(ValueError):
            occurrence_files.write(target, {'bad': math.inf})
        self.assertFalse(target.exists())

        fsync_calls = []
        real_fsync = os.fsync
        def track_fsync(fd):
            fsync_calls.append(fd)
            return real_fsync(fd)

        with patch.object(occurrence_files.os, 'fsync', side_effect=track_fsync):
            occurrence_files.write(target, {'ok': 1, 'text': 'café'})
        self.assertGreaterEqual(len(fsync_calls), 1)
        self.assertEqual(occurrence_files.read(target), {'ok': 1, 'text': 'café'})
        # No fixed .tmp file left behind.
        self.assertEqual([p.name for p in target.parent.iterdir() if p.name.startswith('.doc.json')], [])

        dup = self.base / 'dup.json'
        dup.write_text('{"a": 1, "a": 2}\n')
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            occurrence_files.read(dup)

        nan_file = self.base / 'nan.json'
        nan_file.write_text('{"x": NaN}\n')
        with self.assertRaisesRegex(ValueError, 'nonfinite JSON'):
            occurrence_files.read(nan_file)

    # --- L6 + OE6: Host.pin atomic + verifies existing object on collision ---
    def test_host_pin_refuses_corrupt_or_symlink_object_collision(self):
        sample = self.base / 'sample.txt'
        sample.write_bytes(b'exact payload bytes')
        pinned = self.host.pin(sample)
        obj = self.host.root / pinned['object']
        self.assertEqual(obj.read_bytes(), b'exact payload bytes')

        # Re-pinning identical bytes succeeds when object matches.
        self.assertEqual(self.host.pin(sample)['sha256'], pinned['sha256'])

        # Corrupting the retained object must be detected and refused, never reused.
        obj.write_bytes(b'corrupted bytes')
        with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
            self.host.pin(sample)

        # Replacing object with a symlink must be refused.
        other = self.base / 'other.txt'
        other.write_bytes(b'exact payload bytes')
        obj.unlink()
        obj.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'ordinary file'):
            self.host.pin(sample)

    # --- OE6: Host.event durability, framing safety, bounding, and concurrent writers ---
    def test_host_event_framing_bounding_and_concurrency(self):
        # Non-finite JSON rejected.
        with self.assertRaises(ValueError):
            self.host.event('bad-event', {'score': float('nan')})

        # Unicode line separators U+2028 / U+2029 / U+0085 must not tear JSONL lines.
        self.host.event('unicode-sep', {'stdout': 'a\u2028b\u2029c\u0085d\ne'})
        lines = (self.host.root / 'EVENTS.jsonl').read_text(encoding='utf-8').splitlines()
        parsed = [json.loads(line) for line in lines]
        self.assertEqual(parsed[-1]['stdout'], 'a\u2028b\u2029c\u0085d\ne')

        # Oversized stdout/stderr are bounded inline while preserving full bytes in objects/.
        big_out = 'X' * (MAX_EVENT_STREAM_BYTES + 5000)
        row = self.host.event('big-stream', {'stdout': big_out, 'exit': 0})
        self.assertTrue(row['stdout_truncated'])
        self.assertEqual(row['stdout_bytes'], len(big_out.encode('utf-8')))
        self.assertEqual((self.host.root / row['stdout_object']).read_bytes(), big_out.encode('utf-8'))

        # Oversized arbitrary event payload is bounded under MAX_EVENT_LINE_BYTES.
        huge_row = self.host.event('huge-event', {'id': 's1', 'blob': 'Y' * (MAX_EVENT_LINE_BYTES + 2048)})
        self.assertTrue(huge_row['truncated'])
        self.assertTrue((self.host.root / huge_row['event_object']).is_file())
        last_line = (self.host.root / 'EVENTS.jsonl').read_bytes().splitlines()[-1]
        self.assertLessEqual(len(last_line), MAX_EVENT_LINE_BYTES)

        # Concurrent multi-threaded and multi-process programmatic writers produce valid JSONL.
        errors = []
        def worker(tid):
            try:
                for i in range(15):
                    self.host.event('thread-write', {'tid': tid, 'seq': i, 'pad': 'z' * 200})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        child_code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from managed_ai.host.continuing_work.continuing_work import Host; h = Host(sys.argv[2]); "
            "[h.event('proc-write', {'seq': i, 'pad': 'p' * 200}) for i in range(20)]"
        )
        proc = subprocess.Popen([sys.executable, '-B', '-c', child_code, str(SRC_ROOT), str(self.host.root)])
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(proc.wait(), 0)
        self.assertEqual(errors, [])

        all_lines = (self.host.root / 'EVENTS.jsonl').read_text(encoding='utf-8').splitlines()
        events = [json.loads(line) for line in all_lines]
        self.assertEqual(sum(1 for e in events if e['action'] == 'thread-write'), 60)
        self.assertEqual(sum(1 for e in events if e['action'] == 'proc-write'), 20)

    # --- L7: Host.capture crash recovery & reconciliation ---
    def test_capture_crash_recovery_via_retry_and_resume(self):
        self.prepare('cap1')
        self.prepare('cap2')
        receipt1 = self.base / 'receipt1.json'
        receipt1.write_text(json.dumps({
            'id': 'rec-crash-1',
            'assessments': [{'item': 'continuing-commission', 'disposition': 'considered', 'account': 'Checked.'}],
        }))

        # Crash right before writing captures/cap1/rec-crash-1.json (after native Attention committed).
        real_write = continuing_work.write
        def crash_on_capture(path, value):
            if Path(path).name == 'rec-crash-1.json' and Path(path).parent.name == 'cap1':
                raise OSError('simulated crash before capture file write')
            return real_write(path, value)

        with patch.object(continuing_work, 'write', side_effect=crash_on_capture):
            with self.assertRaisesRegex(OSError, 'simulated crash'):
                self.host.capture('cap1', {'receipt': str(receipt1), 'producer': 'auditor'})

        # Native Attention already committed rec-crash-1, host capture file is absent, pending marker exists.
        self.assertFalse((self.host.root / 'captures/cap1/rec-crash-1.json').exists())
        self.assertTrue((self.host.root / 'captures/cap1/.pending-rec-crash-1.json').is_file())
        status_row = next(r for r in self.host.status()['scopes'] if r['id'] == 'cap1')
        self.assertEqual(status_row['pending_captures'], ['rec-crash-1'])

        # Security check: wrong producer, wrong body, or wrong turn MUST still fail with 'receipt identity already used'.
        with self.assertRaisesRegex(ValueError, 'receipt identity already used'):
            self.host.capture('cap1', {'receipt': str(receipt1), 'producer': 'impostor'})
        with self.assertRaisesRegex(ValueError, 'receipt identity already used'):
            self.host.capture('cap2', {'receipt': str(receipt1), 'producer': 'auditor'})
        altered = self.base / 'altered.json'
        altered.write_text(json.dumps({
            'id': 'rec-crash-1',
            'assessments': [{'item': 'continuing-commission', 'disposition': 'deferred', 'account': 'Different.'}],
        }))
        with self.assertRaisesRegex(ValueError, 'receipt identity already used'):
            self.host.capture('cap1', {'receipt': str(altered), 'producer': 'auditor'})
        self.assertFalse((self.host.root / 'captures/cap1/rec-crash-1.json').exists())
        # Impostor/cross-turn probes must neither overwrite cap1's pending record nor leave a stale pending record on cap2.
        self.assertEqual(occurrence_files.read(self.host.root / 'captures/cap1/.pending-rec-crash-1.json')['producer'], 'auditor')
        self.assertFalse((self.host.root / 'captures/cap2/.pending-rec-crash-1.json').exists())

        # Retrying with the exact same (turn, receipt, producer) reconciles cleanly!
        reconciled = self.host.capture('cap1', {'receipt': str(receipt1), 'producer': 'auditor'})
        self.assertEqual(reconciled['attention']['receipt'], 'rec-crash-1')
        self.assertEqual(set(reconciled), {'id', 'attention', 'turn', 'parent_duties_closed', 'scope'})
        self.assertTrue((self.host.root / 'captures/cap1/rec-crash-1.json').is_file())
        self.assertFalse((self.host.root / 'captures/cap1/.pending-rec-crash-1.json').exists())

        # Subsequent duplicate capture STILL refuses once host capture exists.
        with self.assertRaisesRegex(ValueError, 'Retain the existing candidate capture'):
            self.host.capture('cap1', {'receipt': str(receipt1), 'producer': 'auditor'})

        # Also test recovery via Host.resume() when crash happens on cap2.
        receipt2 = self.base / 'receipt2.json'
        receipt2.write_text(json.dumps({
            'id': 'rec-crash-2',
            'assessments': [{'item': 'continuing-commission', 'disposition': 'considered', 'account': 'Second.'}],
        }))
        def crash_on_cap2(path, value):
            if Path(path).name == 'rec-crash-2.json' and Path(path).parent.name == 'cap2':
                raise OSError('simulated crash 2')
            return real_write(path, value)
        with patch.object(continuing_work, 'write', side_effect=crash_on_cap2):
            with self.assertRaisesRegex(OSError, 'simulated crash 2'):
                self.host.capture('cap2', {'receipt': str(receipt2), 'producer': 'auditor'})
        resumed = self.host.resume('cap2', {'account': 'Reconcile interrupted candidate capture.'})
        self.assertEqual(resumed['state'], 'open-with-reconciled-captures')
        self.assertEqual(resumed['reconciled_captures'][0]['attention']['receipt'], 'rec-crash-2')
        self.assertTrue((self.host.root / 'captures/cap2/rec-crash-2.json').is_file())
        self.assertFalse((self.host.root / 'captures/cap2/.pending-rec-crash-2.json').exists())

    # --- SO2(2): Partial multi-target adopt failure & clean resume/settlement ---
    def test_partial_multitarget_adopt_failure_and_settlement(self):
        src = self.base / 'src.md'
        src.write_text('Original basis.')
        self.prepare('multi', outputs=('first.md', 'second.md'),
                     sources=[{'id': 'src', 'path': str(src)}])
        reply = self.base / 'reply.md'
        reply.write_text('Multi-target reply.')
        out1 = self.base / 'out1.md'
        out1.write_text('First output content.')
        out2 = self.base / 'out2.md'
        out2.write_text('Second output content.')

        # Intercept call() so that right after adopt of first.md succeeds, the basis of second.md changes!
        real_call = self.host.call
        def drift_before_second(*args, **kwargs):
            if len(args) >= 4 and args[0] == 'adopt' and args[3] == 'outputs/multi/second.md':
                # External change to target 2 causes native adopt to refuse with changed basis.
                (self.host.work / 'outputs/multi/second.md').write_text('External conflicting write.')
            return real_call(*args, **kwargs)

        with patch.object(self.host, 'call', side_effect=drift_before_second):
            with self.assertRaisesRegex(ValueError, 'declared basis changed'):
                self.host.receive('multi', {
                    'producer': 'builder',
                    'reply': str(reply),
                    'proposals': {'first.md': str(out1), 'second.md': str(out2)},
                    'account': 'Publish both targets.',
                })

        # Target 1 is published on disk AND durably tracked in scope['partial_publication']!
        self.assertEqual((self.host.work / 'outputs/multi/first.md').read_text(), 'First output content.')
        scope = occurrence_files.read(self.host.root / 'scopes/multi.json')
        self.assertNotIn('result', scope)
        partial = scope['partial_publication']
        self.assertEqual(partial['state'], 'partial-publication-needs-reconciliation')
        self.assertEqual([p['path'] for p in partial['publications']], ['outputs/multi/first.md'])
        self.assertEqual(partial['failed_target'], 'outputs/multi/second.md')
        self.assertEqual(partial['pending_targets'], ['outputs/multi/second.md'])

        # Host.status surfaces the partial publication cleanly.
        row = next(r for r in self.host.status()['scopes'] if r['id'] == 'multi')
        self.assertEqual(row['host_state'], 'partial-publication-needs-reconciliation')
        self.assertEqual(row['published_targets'], ['outputs/multi/first.md'])
        self.assertEqual(row['pending_targets'], ['outputs/multi/second.md'])

        # Ordinary resume without settle_partial reports partial publication and does not replay target 1.
        with self.assertRaisesRegex(ValueError, 'partial publication recorded.*settle_partial=true'):
            self.host.resume('multi', {'account': 'Retry remaining target.'})

        # Explicit settlement settles target 1 and marks target 2 unpublished.
        settled = self.host.resume('multi', {'account': 'Settle first.md; carry second.md to new scope.',
                                             'settle_partial': True})
        self.assertTrue(settled['partial'])
        self.assertEqual(settled['published_targets'], ['outputs/multi/first.md'])
        self.assertEqual(settled['unpublished_targets'], ['outputs/multi/second.md'])

        scope_after = occurrence_files.read(self.host.root / 'scopes/multi.json')
        self.assertNotIn('partial_publication', scope_after)
        self.assertTrue(scope_after['result']['partial'])
        self.assertEqual(scope_after['result']['unpublished_targets'], ['outputs/multi/second.md'])
        self.assertEqual(len(scope_after['result']['outputs']), 1)

        row_after = next(r for r in self.host.status()['scopes'] if r['id'] == 'multi')
        self.assertEqual(row_after['host_state'], 'partially-received')

        # Duty fulfillment refuses unpublished target 2, accepts actually published target 1!
        with self.assertRaisesRegex(ValueError, 'actual current returned outputs'):
            self.host.duty({'id': 'custody', 'disposition': 'fulfilled',
                            'account': 'Attempt citing unpublished target.',
                            'evidence': ['outputs/multi/second.md']})
        self.host.duty({'id': 'custody', 'disposition': 'fulfilled',
                        'account': 'Cite actually published first.md.',
                        'evidence': ['outputs/multi/first.md']})
        self.assertEqual(self.host.goal()['content']['obligations'][0]['disposition'], 'fulfilled')

        # Calling resume again on an already-settled partial scope stays idempotent.
        again = self.host.resume('multi', {'account': 'Recheck settled scope.'})
        self.assertTrue(again['already_received'])
        self.assertTrue(again['partial'])
        self.assertEqual(again['unpublished_targets'], ['outputs/multi/second.md'])

        # overlap_rows distinguishes partially-received scopes from fully returned ones (C4-5 minor).
        rows = continuing_work.overlap_rows(
            [scope_after], [], ['src'], [],
        )
        self.assertEqual(rows[0]['scope_state'], 'partially-received')

    # --- C4-4: Pre-commit capture refusal must not leave stale .pending that wedges resume ---
    def test_precommit_capture_failure_does_not_wedge_revised_retry_or_resume(self):
        self.prepare('cap-retry')
        bad_receipt = self.base / 'bad-account.json'
        # Empty account is rejected pre-commit by Attention.record and mapped to the generic fallback error.
        bad_receipt.write_text(json.dumps({
            'id': 'rec-revised',
            'assessments': [{'item': 'continuing-commission', 'disposition': 'considered', 'account': '   '}],
        }))
        with self.assertRaisesRegex(ValueError, 'Candidate capture could not be confirmed'):
            self.host.capture('cap-retry', {'receipt': str(bad_receipt), 'producer': 'auditor'})
        # Uncommitted pre-commit refusal cleans up pending marker immediately.
        self.assertFalse((self.host.root / 'captures/cap-retry/.pending-rec-revised.json').exists())

        # Also test the harder case: a stale uncommitted .pending exists (e.g. from OwnerBusy/crash pre-commit),
        # then a revised receipt under the same rid commits and crashes before writing captures/<sid>/<rid>.json!
        stale_obj, stale_sha = self.host._put_object_bytes(bad_receipt.read_bytes())
        occurrence_files.write(
            self.host.root / 'captures/cap-retry/.pending-rec-revised.json',
            {'id': 'cap-retry', 'receipt': 'rec-revised', 'producer': 'auditor',
             'receipt_object': stale_obj, 'receipt_sha256': stale_sha,
             'started_at_utc': occurrence_files.now()},
        )
        fixed_receipt = self.base / 'fixed-account.json'
        fixed_receipt.write_text(json.dumps({
            'id': 'rec-revised',
            'assessments': [{'item': 'continuing-commission', 'disposition': 'considered', 'account': 'Fixed account.'}],
        }))
        real_write = continuing_work.write
        def crash_on_final(path, value):
            if Path(path).name == 'rec-revised.json' and Path(path).parent.name == 'cap-retry':
                raise OSError('crash after revised commit')
            return real_write(path, value)
        with patch.object(continuing_work, 'write', side_effect=crash_on_final):
            with self.assertRaisesRegex(OSError, 'crash after revised commit'):
                self.host.capture('cap-retry', {'receipt': str(fixed_receipt), 'producer': 'auditor'})

        # resume() must reconcile the revised committed receipt cleanly rather than wedging on the stale pre-commit marker!
        resumed = self.host.resume('cap-retry', {'account': 'Reconcile revised capture.'})
        self.assertEqual(resumed['state'], 'open-with-reconciled-captures')
        self.assertEqual(resumed['reconciled_captures'][0]['attention']['receipt'], 'rec-revised')
        self.assertTrue((self.host.root / 'captures/cap-retry/rec-revised.json').is_file())
        self.assertFalse((self.host.root / 'captures/cap-retry/.pending-rec-revised.json').exists())

    # --- C4-5: Host.upgrade() succeeds even when account exceeds MAX_EVENT_LINE_BYTES ---
    def test_host_upgrade_succeeds_with_oversized_account_event(self):
        res = self.host.upgrade({
            'engine': str(ENGINE),
            'account': 'A' * (MAX_EVENT_LINE_BYTES + 2048),
        })
        self.assertTrue(res['native_programs_unchanged'])
        self.assertIn('sha256', res['host_producer'])
        self.assertIn('sha256', res['host_method'])
        self.assertEqual(len(res['host_dependencies']), 7)
        self.assertEqual(
            {Path(d['path']).name for d in res['host_dependencies']},
            {
                'contract.py', 'occurrence_files.py', 'repair_custody.py',
                'native_bridge.py', 'scope_prepare.py', 'scope_receive.py', 'scope_lifecycle.py',
            },
        )
        last_line = (self.host.root / 'EVENTS.jsonl').read_bytes().splitlines()[-1]
        self.assertLessEqual(len(last_line), MAX_EVENT_LINE_BYTES)
        parsed = json.loads(last_line)
        self.assertTrue(parsed['truncated'])
        self.assertEqual(parsed['action'], 'producing-release-selected')

    # --- W5-B5: Failed commission() must never mutate COMMISSION.md or corrupt goal basis ---
    def test_failed_commission_does_not_mutate_commission_md_or_goal_basis(self):
        comm_target = self.host.work / 'sources/host/COMMISSION.md'
        original_comm = comm_target.read_bytes()
        original_cfg = (self.host.root / 'host.json').read_bytes()
        original_goal = self.host.goal()

        revised = self.base / 'revised-commission.md'
        revised.write_text('Revised instructions that should not take effect on failure.\n', encoding='utf-8')

        # 1. Blank account fails BEFORE touching COMMISSION.md.
        with self.assertRaisesRegex(ValueError, 'commission continuation account'):
            self.host.commission({'path': str(revised), 'account': '   '})
        self.assertEqual(comm_target.read_bytes(), original_comm)
        self.assertEqual((self.host.root / 'host.json').read_bytes(), original_cfg)

        # 2. Conflicting duty text fails BEFORE touching COMMISSION.md.
        with self.assertRaisesRegex(ValueError, 'silently replace an earlier duty'):
            self.host.commission({
                'path': str(revised),
                'account': 'Try to alter duty.',
                'duties': [{'id': 'custody', 'text': 'Altered duty text.'}],
            })
        self.assertEqual(comm_target.read_bytes(), original_comm)

        # 3. Failing native put rolls back COMMISSION.md atomically so goal basis stays intact!
        real_call = self.host.call
        def fail_put(*args, **kwargs):
            if args and args[0] == 'put':
                raise ValueError('simulated native put failure')
            return real_call(*args, **kwargs)
        with patch.object(self.host, 'call', side_effect=fail_put):
            with self.assertRaisesRegex(ValueError, 'simulated native put failure'):
                self.host.commission({'path': str(revised), 'account': 'Valid account.'})
        self.assertEqual(comm_target.read_bytes(), original_comm)
        self.assertEqual((self.host.root / 'host.json').read_bytes(), original_cfg)
        self.assertEqual(self.host.goal()['sha256'], original_goal['sha256'])

    # --- W5-B5: Failed init() must not persist host.json; successful init writes atomically ---
    def test_failed_init_does_not_persist_host_json_and_init_writes_atomically(self):
        bad_host = Host(self.base / 'failed-init-host')
        real_call = bad_host.call
        def fail_track(*args, **kwargs):
            if args and args[0] == 'track':
                raise ValueError('simulated track failure during init')
            return real_call(*args, **kwargs)
        with patch.object(bad_host, 'call', side_effect=fail_track):
            with self.assertRaisesRegex(ValueError, 'simulated track failure'):
                bad_host.init({
                    'commission': str(self.commission),
                    'engine': str(ENGINE),
                    'duties': [{'id': 'd1', 'text': 'Duty one.'}],
                })
        self.assertFalse((bad_host.root / 'host.json').exists())
        self.assertIsNone(bad_host.config)

        # Verify successful init writes COMMISSION.md, OPERATION.md, FIELD.md, start-receipt.md via atomic_bytes.
        written = []
        real_atomic = continuing_work.atomic_bytes
        def record_atomic(path, data):
            written.append(Path(path).name)
            return real_atomic(path, data)
        good_host = Host(self.base / 'atomic-init-host')
        with patch.object(continuing_work, 'atomic_bytes', side_effect=record_atomic):
            good_host.init({
                'commission': str(self.commission),
                'engine': str(ENGINE),
                'duties': [{'id': 'd1', 'text': 'Duty one.'}],
            })
        self.assertTrue((good_host.root / 'host.json').is_file())
        for expected_name in ('COMMISSION.md', 'OPERATION.md', 'FIELD.md', 'start-receipt.md'):
            self.assertIn(expected_name, written)

    # --- W5-B5: NTFS junction / symlink refusal in occurrence_files & native_bridge ---
    def test_reparse_point_or_symlink_refused_in_occurrence_files_and_native_bridge(self):
        real_dir = self.base / 'real-dir'
        real_dir.mkdir()
        real_file = real_dir / 'data.json'
        real_file.write_text('{"ok": true}\n', encoding='utf-8')
        link_dir = self.base / 'link-dir'
        if os.name == 'nt':
            proc = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link_dir), str(real_dir)], capture_output=True)
            if proc.returncode != 0:
                return
            self.addCleanup(lambda: subprocess.run(['cmd', '/c', 'rmdir', str(link_dir)], capture_output=True))
        else:
            link_dir.symlink_to(real_dir)

        with self.assertRaisesRegex(ValueError, 'symlink or reparse point'):
            with occurrence_files.file_lock(link_dir / 'lock'):
                pass
        with self.assertRaisesRegex(ValueError, 'symlink or reparse point'):
            occurrence_files.atomic_bytes(link_dir / 'out.bin', b'abc')
        with self.assertRaisesRegex(ValueError, 'symlink or reparse point'):
            occurrence_files.read(link_dir / 'data.json')
        with self.assertRaisesRegex(ValueError, 'symlink or reparse point'):
            self.host.pin(link_dir / 'data.json')
        with self.assertRaisesRegex(ValueError, 'symlink or reparse point'):
            self.host.commission({'path': str(link_dir / 'data.json'), 'account': 'Refuse junction source.'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
