"""Compact receipts retain exactly the Collector evidence used at acceptance."""
import base64
from copy import deepcopy
import json
import os
from pathlib import Path
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3] / 'engine'
sys.path.insert(0, str(ROOT))
from core.exchange.transport.collector import material, worker_active
from core.exchange import Exchange, carrier_receipts
from core import storage
from core.storage import MAX_BYTES, encode, sha, write_json


def until(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class TransportReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='transport-receipts-')
        self.base = Path(self.temporary.name)
        self.work = self.base / 'work'
        self.work.mkdir()
        self.root = self.base / 'exchange'
        (self.work / 'brief.md').write_text('Use this contribution under the current brief.\n')
        Exchange.initialize(self.root, self.work)
        self.ex = Exchange(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, packet='one', *, materials=None, targets=None, returns=None, artifacts=None):
        return self.ex.prepare(packet, 'reader', 'brief.md', [], materials or [],
                               targets or [], returns or [], artifacts)

    def collect(self, code, packet='one', proposals=None):
        self.ex.dispatch(packet, [sys.executable, '-B', '-c', code, '{context}'])
        captured = {}
        original = self.ex.transport.collect_marked

        def observe(*args):
            data, view = original(*args)
            captured['view'] = deepcopy(view)
            return data, view

        with patch.object(self.ex.transport, 'collect_marked', side_effect=observe):
            record = self.ex.receive(packet, None, 'actual test command', proposals or [], wait=8)
        return record, captured['view']

    def test_completed_carrier_view_survives_receiving(self):
        self.prepare()
        record, collected = self.collect('import os; os.write(1, b"normal contribution\\n")')
        self.assertEqual(self.ex.get(record['reply_sha256']), b'normal contribution\n')
        self.assertEqual(self.ex.recover_transport('one'), collected)

    def rewrite_return(self, record, packet='one'):
        record['return_sha256'] = sha(encode({k: v for k, v in record.items() if k != 'return_sha256'}))
        write_json(self.ex.directory(packet) / 'return.json', record)

    def test_exact_binary_recovery_after_carrier_removal_and_source_change(self):
        self.prepare(targets=['result.bin'])
        proposal = self.base / 'proposal.bin'
        proposal.write_bytes(b'proposal\x00\xff')
        code = ('import os,pathlib,sys\n'
                'with pathlib.Path("invocations").open("a") as stream: stream.write("once\\n")\n'
                'os.write(1,b"reply\\x00\\xff\\n"); sys.exit(7)')
        record, captured = self.collect(code, proposals=['result.bin=' + str(proposal)])
        self.assertEqual(self.ex.recover_transport('one'), captured)
        self.assertEqual(captured['result']['returncode'], 7)
        self.assertIsNone(captured['result']['signal'])
        self.assertIsNone(captured['streams']['stdout']['utf8_text'])
        self.assertEqual(captured['streams']['stderr']['utf8_text'], '')
        self.assertEqual(self.ex.get(record['reply_sha256']), b'reply\x00\xff\n')
        self.assertEqual(self.ex.get(record['proposals'][0]['sha256']), proposal.read_bytes())
        shutil.rmtree(self.root / 'carrier')
        (self.work / 'brief.md').write_text('A fresh receiving purpose.\n')
        process = subprocess.run([sys.executable, '-B', str(ROOT / 'core/cli/exchange_packets.py'), '--root', str(self.root),
                                  'recover-transport', 'one'], capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), captured)
        self.assertFalse((self.root / 'carrier').exists())
        self.assertEqual((self.work / 'invocations').read_text(), 'once\n')
        with self.assertRaisesRegex(ValueError, 'basis changed'):
            self.ex.publish('one', 'result.bin', 'Original source has changed.')
        self.prepare('qualified', returns=['one'], artifacts=['one:result.bin'], targets=['qualified.bin'])
        context = self.ex.context('qualified')['context']
        self.assertIn('A fresh receiving purpose.', context)
        self.assertIn('No original target authority is inherited.', context)
        imported = self.ex.receive('qualified', proposal, 'current caller interpretation',
                                   ['qualified.bin=' + str(proposal)])
        self.assertEqual(imported['transport']['kind'], 'caller-import')
        self.assertIsNone(self.ex.recover_transport('qualified'))
        self.ex.publish('qualified', 'qualified.bin', 'Current recipient qualifies this exact contribution.')
        self.assertEqual((self.work / 'qualified.bin').read_bytes(), proposal.read_bytes())
        self.assertFalse((self.work / 'result.bin').exists())

    def test_fixed_output_sweep_counts_all_added_objects_and_receipt(self):
        stdout = b'o' * 33000
        stderr = b'diagnostic\n'
        measurements = []
        for index, context_size in enumerate((64 * 1024, 512 * 1024, 3 * 1024 * 1024)):
            with self.subTest(context_size=context_size):
                self.work = self.base / f'work-{index}'
                self.work.mkdir()
                (self.work / 'brief.md').write_text('Fixed brief.\n')
                self.root = self.base / f'exchange-{index}'
                Exchange.initialize(self.root, self.work)
                self.ex = Exchange(self.root)
                # Equal-length packet names keep rendering overhead identical.
                probe, packet = f'probe-{index}', f'final-{index}'
                for name in ('a.txt', 'b.txt'):
                    (self.work / name).write_bytes(b'')
                self.prepare(probe, materials=['a.txt', 'b.txt'])
                overhead = len((self.ex.directory(probe) / 'context.md').read_bytes())
                payload_size = context_size - overhead
                first_size = payload_size // 2
                (self.work / 'a.txt').write_bytes(b'a' * first_size)
                (self.work / 'b.txt').write_bytes(b'b' * (payload_size - first_size))
                self.prepare(packet, materials=['a.txt', 'b.txt'])
                context = (self.ex.directory(packet) / 'context.md').read_bytes()
                self.assertEqual(len(context), context_size)
                before = {path.name for path in (self.root / 'objects').iterdir()}
                record, captured = self.collect(
                    'import os; os.write(1,b"o"*33000); os.write(2,b"diagnostic\\n")', packet)
                receipt_bytes = (self.ex.directory(packet) / 'return.json').stat().st_size
                added_objects = [path for path in (self.root / 'objects').iterdir() if path.name not in before]
                object_bytes = sum(path.stat().st_size for path in added_objects)
                expected_objects = {sha(context): len(context), sha(stdout): len(stdout), sha(stderr): len(stderr)}
                expected_added = sum(size for key, size in expected_objects.items() if key not in before)
                self.assertEqual(object_bytes, expected_added)
                self.assertLess(receipt_bytes, 16 * 1024)
                metadata = json.dumps(record['transport'])
                self.assertNotIn('"base64"', metadata)
                self.assertNotIn('"utf8_text"', metadata)
                self.assertEqual(self.ex.recover_transport(packet), captured)
                measurements.append({'context_bytes': len(context), 'receipt_bytes': receipt_bytes,
                                     'added_object_bytes': object_bytes,
                                     'total_added_bytes': receipt_bytes + object_bytes})
        print('Transport storage sweep ' + json.dumps(measurements, sort_keys=True))

    def test_large_stderr_and_original_reply_bound(self):
        self.prepare()
        size = 3 * 1024 * 1024 + 19
        record, captured = self.collect(f'import os; os.write(1,b"ok"); os.write(2,b"\\xff"*{size})')
        self.assertEqual(self.ex.recover_transport('one'), captured)
        self.assertEqual(captured['streams']['stderr']['byte_count'], size)
        self.assertIsNone(captured['streams']['stderr']['utf8_text'])
        self.assertLess(len(encode(record)), 16 * 1024)
        self.prepare('at-limit')
        accepted, captured = self.collect(f'import os; os.write(1,b"x"*{MAX_BYTES})', 'at-limit')
        self.assertEqual(accepted['reply_bytes'], MAX_BYTES)
        self.assertEqual(self.ex.recover_transport('at-limit'), captured)
        self.prepare('too-large')
        with self.assertRaisesRegex(ValueError, 'per-object limit'):
            self.collect(f'import os; os.write(1,b"x"*{MAX_BYTES + 1})', 'too-large')
        self.assertFalse((self.ex.directory('too-large') / 'return.json').exists())

    def test_material_shapes_and_all_non_body_observations_survive(self):
        self.prepare()
        _, captured = self.collect('print("captured")')
        current = self.base / 'current.bin'
        for body in (b'', b'changed current context', b'\xff\x00'):
            with self.subTest(body=body):
                current.write_bytes(body)
                view = deepcopy(captured)
                view['current_brief'] = material(current)
                view['interpretation'] = 'current-interpretation-required'
                view['extra_observation'] = {'uncertainty': None, 'flags': [False, 0, '']}
                retained = carrier_receipts.retain_transport(self.ex, view)
                self.assertEqual(carrier_receipts.transport_view(self.ex, retained, recover=True), view)
                stored = self.root / 'objects' / retained['carrier']['current_brief']['sha256']
                stored.unlink()
                with self.assertRaises((ValueError, OSError)):
                    carrier_receipts.transport_view(self.ex, retained)
                stored.write_bytes(body)
        current.unlink()
        missing = deepcopy(captured)
        missing['current_brief'] = material(current)
        missing['interpretation'] = 'current-context-unavailable'
        missing['current_context_error'] = 'captured missing context'
        self.assertEqual(carrier_receipts.transport_view(self.ex, carrier_receipts.retain_transport(self.ex, missing), recover=True), missing)
        del missing['current_brief']
        self.assertEqual(carrier_receipts.transport_view(self.ex, carrier_receipts.retain_transport(self.ex, missing), recover=True), missing)

    def test_old_inline_record_remains_unchanged_and_recoverable(self):
        self.prepare()
        record, captured = self.collect('import os; os.write(1,b"old reply"); os.write(2,b"old diagnostic")')
        record['transport'] = {'kind': 'local-command', 'carrier': captured}
        self.rewrite_return(record)
        original_record = (self.ex.directory('one') / 'return.json').read_bytes()
        for key in (captured['origin_request']['origin_brief']['sha256'], captured['streams']['stderr']['sha256']):
            (self.root / 'objects' / key).unlink()
        shutil.rmtree(self.root / 'carrier')
        self.assertEqual(Exchange(self.root).recover_transport('one'), captured)
        self.assertEqual(self.ex.inspect('one')['return'], record)
        self.assertEqual((self.ex.directory('one') / 'return.json').read_bytes(), original_record)

    def test_missing_corrupt_and_wrong_length_bodies_refuse_intact_use(self):
        self.prepare(targets=['result.txt'])
        proposal = self.base / 'proposal.txt'
        proposal.write_text('proposal')
        record, captured = self.collect('import os; os.write(1,b"reply"); os.write(2,b"diagnostic")',
                                        proposals=['result.txt=' + str(proposal)])
        keys = {row['sha256'] for row in carrier_receipts.carrier_materials(record['transport']['carrier'])}
        keys.add(record['proposals'][0]['sha256'])
        for key in keys:
            path = self.root / 'objects' / key
            saved = path.read_bytes()
            for replacement in (None, b'corruption'):
                with self.subTest(key=key, replacement=replacement):
                    if replacement is None:
                        path.unlink()
                    else:
                        path.write_bytes(replacement)
                    with self.assertRaises((ValueError, OSError)):
                        self.ex.recover_transport('one')
                    with self.assertRaises((ValueError, OSError)):
                        self.ex.inspect('one')
                    self.assertEqual(self.ex.list()[0]['transport'], 'record-unresolved')
                    with self.assertRaises((ValueError, OSError)):
                        self.ex.publish('one', 'result.txt', 'Body must be intact before publication.')
                    path.write_bytes(saved)
        damaged = deepcopy(record)
        damaged['transport']['carrier']['streams']['stderr']['byte_count'] += 1
        self.rewrite_return(damaged)
        with self.assertRaisesRegex(ValueError, 'length changed'):
            self.ex.recover_transport('one')
        self.rewrite_return(record)
        self.assertEqual(self.ex.recover_transport('one'), captured)

    def test_retention_failure_prevents_publication_and_retry_reuses_capture(self):
        self.prepare()
        code = ('import os\n'
                'from pathlib import Path\n'
                'with Path("invocations").open("a") as stream: stream.write("once\\n")\n'
                'os.write(1, b"one captured result\\n")')
        self.ex.dispatch('one', [sys.executable, '-B', '-c', code, '{context}'])
        original = self.ex.objects.put_retained
        context = (self.ex.directory('one') / 'context.md').read_bytes()

        def fail_context(data, *, max_bytes):
            if data == context:
                raise OSError('injected durable retention failure')
            return original(data, max_bytes=max_bytes)

        with patch.object(self.ex.objects, 'put_retained', side_effect=fail_context):
            with self.assertRaisesRegex(OSError, 'retention failure'):
                self.ex.receive('one', None, 'test command', [], wait=8)
        self.assertFalse((self.ex.directory('one') / 'return.json').exists())
        self.ex.receive('one', None, 'test command', [], wait=8)
        self.assertEqual(self.ex.recover_transport('one')['streams']['stdout']['utf8_text'], 'one captured result\n')
        self.assertEqual((self.work / 'invocations').read_text(), 'once\n')

    def test_material_counts_require_nonnegative_integers(self):
        self.prepare()
        original, _ = self.collect('import os; os.write(1,b"x"); os.write(2,b"y")')
        for count in (True, 1.0, -1, '1', None):
            with self.subTest(count=count):
                record = deepcopy(original)
                record['transport']['carrier']['streams']['stdout']['byte_count'] = count
                self.rewrite_return(record)
                with self.assertRaises(ValueError):
                    self.ex.recover_transport('one')
        for count in (True, 1.0):
            with self.subTest(reply_count=count):
                record = deepcopy(original)
                record['reply_bytes'] = count
                self.rewrite_return(record)
                with self.assertRaises(ValueError):
                    self.ex.recover_transport('one')

    def test_required_materials_cannot_hide_missing_objects(self):
        self.prepare()
        original, _ = self.collect('import os; os.write(1,b"x"); os.write(2,b"y")')
        for index in range(3):
            for present in (False, 'yes', 1, None):
                with self.subTest(index=index, present=present):
                    record = deepcopy(original)
                    material = list(carrier_receipts.carrier_materials(record['transport']['carrier']))[index]
                    material['present'] = present
                    self.rewrite_return(record)
                    with self.assertRaises(ValueError):
                        self.ex.recover_transport('one')
        record = deepcopy(original)
        record['transport']['carrier']['streams']['stderr']['present'] = False
        (self.root / 'objects' / sha(b'y')).unlink()
        self.rewrite_return(record)
        with self.assertRaises(ValueError):
            self.ex.recover_transport('one')

    def test_caller_import_and_proposal_counts_are_required(self):
        self.prepare(targets=['result.txt'])
        reply = self.base / 'reply.txt'
        reply.write_bytes(b'x')
        original = self.ex.receive('one', reply, 'caller', ['result.txt=' + str(reply)])
        for field in ('reply_bytes', 'proposal_bytes'):
            for count in (None, True, 1.0, -1, '1'):
                with self.subTest(field=field, count=count):
                    record = deepcopy(original)
                    if field == 'reply_bytes':
                        record[field] = count
                    else:
                        record['proposals'][0]['bytes'] = count
                    self.rewrite_return(record)
                    with self.assertRaises(ValueError):
                        self.ex.recover_transport('one')

    def test_optional_material_presence_is_boolean(self):
        self.prepare()
        original, _ = self.collect('print("reply")')
        for present in ('yes', 1, 0, None):
            with self.subTest(present=present):
                record = deepcopy(original)
                record['transport']['carrier']['current_brief']['present'] = present
                self.rewrite_return(record)
                with self.assertRaises(ValueError):
                    self.ex.recover_transport('one')

    def test_corrupt_retained_reads_use_recorded_length(self):
        self.prepare()
        self.collect('import os; os.write(1,b"x"); os.write(2,b"y")')
        target = self.root / 'objects' / sha(b'y')
        actual_read = storage.read_bytes
        calls = []

        def growing_read(path, limit=storage.MAX_BYTES):
            if path == target:
                calls.append(limit)
                target.write_bytes(b'damaged' * 1000)
            return actual_read(path, limit)

        with patch.object(storage, 'read_bytes', side_effect=growing_read):
            with self.assertRaises(ValueError):
                self.ex.recover_transport('one')
        self.assertEqual(calls, [1])
        with patch.object(storage, 'read_bytes', wraps=actual_read) as observed:
            with self.assertRaisesRegex(ValueError, 'length changed'):
                self.ex.recover_transport('one')
        self.assertFalse(any(call.args[0] == target for call in observed.call_args_list))

    def test_compact_material_refuses_inline_body(self):
        self.prepare()
        record, _ = self.collect('print("reply")')
        record['transport']['carrier']['streams']['stdout']['base64'] = 'cmVwbHkK'
        self.rewrite_return(record)
        with self.assertRaisesRegex(ValueError, 'inline material'):
            self.ex.recover_transport('one')

    def test_transport_stdout_must_equal_retained_reply(self):
        self.prepare()
        record, _ = self.collect('import os; os.write(1,b"x"); os.write(2,b"y")')
        record['transport']['carrier']['streams']['stdout']['sha256'] = sha(b'y')
        self.rewrite_return(record)
        with self.assertRaisesRegex(ValueError, 'does not match the reply'):
            self.ex.recover_transport('one')

    def test_launch_failure_and_signal_meanings_are_preserved(self):
        self.prepare('launch')
        argv = [str(self.base / 'absent-executable'), '{context}']
        self.ex.dispatch('launch', argv)
        pending = self.ex.receive('launch', None, 'failed launch', [], wait=8)
        self.assertFalse(pending['received'])
        self.assertEqual(pending['carrier']['status'], 'launch-failed')
        self.assertIsNone(pending['carrier']['result']['returncode'])
        self.assertIsNone(pending['carrier']['result']['signal'])
        self.assertFalse((self.ex.directory('launch') / 'return.json').exists())
        repeated = self.ex.dispatch('launch', argv)
        self.assertEqual(repeated['dispatches'][0]['dispatch'], 'already-attempted')
        self.prepare('signalled')
        _, captured = self.collect('import os,signal; os.write(1,b"before signal"); os.kill(os.getpid(),signal.SIGTERM)',
                                   'signalled')
        self.assertEqual(self.ex.recover_transport('signalled'), captured)
        if os.name == 'posix':
            self.assertEqual(captured['result']['returncode'], -signal.SIGTERM)
            self.assertEqual(captured['result']['signal'], signal.SIGTERM)
        else:
            self.assertNotEqual(captured['result']['returncode'], 0)

    def test_worker_interruption_retains_partial_bytes_and_refuses_redispatch(self):
        self.prepare()
        code = ('import os,pathlib,time\n'
                'with pathlib.Path("invocations").open("a") as stream: stream.write("once\\n")\n'
                'os.write(1,b"partial\\x00"); os.write(2,b"diagnostic\\xff")\n'
                'pathlib.Path("ready").touch()\n'
                'deadline=time.monotonic()+8\n'
                'while not pathlib.Path("release").exists() and time.monotonic()<deadline: time.sleep(0.01)\n'
                'pathlib.Path("finished").touch()\n')
        argv = [sys.executable, '-B', '-c', code, '{context}']
        dispatch = self.ex.dispatch('one', argv)
        job = self.root / 'carrier' / 'jobs' / sha(b'exchange-one')
        try:
            self.assertTrue(until(lambda: (self.work / 'ready').exists()))
            kill_sig = getattr(signal, 'SIGKILL', signal.SIGTERM)
            os.kill(dispatch['dispatches'][0]['worker_pid_hint'], kill_sig)
            self.assertTrue(until(lambda: not worker_active(job)))
            returned = self.ex.receive('one', None, 'interrupted worker', [])
            self.assertFalse(returned['received'])
            self.assertEqual(returned['carrier']['status'], 'uncertain')
            self.assertNotIn('result', returned['carrier'])
            self.assertEqual(base64.b64decode(returned['carrier']['streams']['stdout']['base64']), b'partial\x00')
            self.assertEqual(base64.b64decode(returned['carrier']['streams']['stderr']['base64']), b'diagnostic\xff')
            self.assertEqual(self.ex.dispatch('one', argv)['dispatches'][0]['dispatch'], 'already-attempted')
            self.assertFalse((self.ex.directory('one') / 'return.json').exists())
            self.assertEqual((self.work / 'invocations').read_text(), 'once\n')
        finally:
            (self.work / 'release').touch()
            self.assertTrue(until(lambda: (self.work / 'finished').exists()))
            if os.name == 'nt':
                def released():
                    target = job / 'stderr.bin'
                    if not target.exists():
                        return True
                    probe = job / 'stderr.bin.probe'
                    try:
                        target.rename(probe)
                        probe.rename(target)
                        return True
                    except OSError:
                        return False
                until(released)


class NativeTransportUseTests(unittest.TestCase):
    def test_fresh_native_cli_recovery_and_qualification(self):
        api = runpy.run_path(str(ROOT / 'store' / 'system.py'))
        System = api['System']
        with tempfile.TemporaryDirectory(prefix='native-transport-use-') as temporary:
            base = Path(temporary)
            root = base / 'runtime'
            System.initialize(root, {'contribution': 'Qualify captured work against a new source.'})
            system = System(root)
            source = system.work / 'sources' / 'current.txt'
            source.write_text('Original source')
            system.put({'id': 'source', 'kind': 'note', 'content': 'Current receiving dependency',
                        'basis': ['sources/current.txt']}, 0)
            system.context('original', {'records': ['source'], 'targets': ['outputs/result.txt']})
            proposal = base / 'proposal.txt'
            proposal.write_text('Retained contribution')
            system.run('original', [sys.executable, '-B', '-c', 'print("actual worker reply")', '{context}'])
            record = system.receive('original', None, 'actual worker', ['outputs/result.txt=' + str(proposal)], wait=8)
            captured = system.recover_transport('original')
            self.assertLess(len(encode(record)), 16 * 1024)
            shutil.rmtree(system.ex.root / 'carrier')
            source.write_text('Current source requires qualification')
            process = subprocess.run([sys.executable, '-B', str(ROOT / 'store' / 'system.py'),
                                      '--root', str(root), 'recover-transport', 'original'],
                                     capture_output=True, timeout=15)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout), captured)
            with self.assertRaisesRegex(ValueError, 'basis changed'):
                system.adopt('original', 'outputs/result.txt', 'Original basis has changed.')
            system.context('qualified', {'records': ['source'], 'returns': ['original'],
                                         'artifacts': ['original:outputs/result.txt'],
                                         'targets': ['outputs/qualified.txt']})
            fresh = system.recover('qualified')['context']
            self.assertIn('Current source requires qualification', fresh)
            self.assertIn('Retained contribution', fresh)
            system.receive('qualified', proposal, 'caller-authored current qualification',
                           ['outputs/qualified.txt=' + str(proposal)])
            system.adopt('qualified', 'outputs/qualified.txt', 'Current recipient accepts the contribution.')
            self.assertEqual((system.work / 'outputs/qualified.txt').read_text(), 'Retained contribution')
            self.assertFalse((system.work / 'outputs/result.txt').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
