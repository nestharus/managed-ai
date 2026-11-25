#!/usr/bin/env python3
"""Wave 1 regression tests: LOCK_SH, admission backoff, single code_identity, HMAC index, put-batch, L5(1), AU1, SC3."""
if __package__:
    from .native_test_paths import APP, ENGINE, PRODUCT
else:
    from native_test_paths import APP, ENGINE, PRODUCT
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from system import System
from controller import Deployment, DeploymentSelection, code_identity, encode, exclusive, sha, shared


def _write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    return path


class Wave1RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='managed-ai-wave1-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / 'store'
        self.dep = self.root / 'deployment'
        System.initialize(self.store, {'contribution': 'Verify Wave 1 concurrency, indexing and batch updates.'},
                          instance_id='wave1-store')
        Deployment.initialize(self.dep, self.store, ENGINE)
        self.select_cli = [sys.executable, '-B', str(ENGINE / 'runtime/select_deployment.py'),
                           '--deployment', str(self.dep)]
        self.store_cli = [sys.executable, '-B', str(ENGINE / 'runtime/manage_store.py'),
                          '--root', str(self.store)]

    def run_cli(self, cmd, *, env=None, expected_rc=0):
        merged = os.environ.copy()
        if env:
            merged.update(env)
        proc = subprocess.run(cmd, capture_output=True, text=True, env=merged, timeout=40)
        self.assertEqual(proc.returncode, expected_rc, f'cmd={cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}')
        return proc

    def test_shared_locks_allow_concurrent_readonly_operations(self):
        dep_lock = self.dep / 'deployment.lock'
        owner_lock = self.store / 'exchange/owner.lock'
        discover_req = _write_json(self.root / 'discover.json', {'query': 'Wave 1', 'scopes': ['records']})
        with shared(dep_lock), shared(owner_lock):
            for args in (
                ['status'],
                ['verify'],
                ['system', 'show', 'goal'],
                ['system', 'status'],
                ['system', 'catalogue', '--limit', '1'],
                ['system', 'history', 'goal'],
                ['system', 'discover', '--request', str(discover_req)],
            ):
                with self.subTest(deployment_args=args):
                    proc = self.run_cli([*self.select_cli, *args],
                                        env={'MANAGED_AI_DOMAIN_ADMISSION_SECONDS': '1'})
                    self.assertTrue(json.loads(proc.stdout))
            for args in (['show', 'goal'], ['status'], ['history', 'goal']):
                with self.subTest(store_args=args):
                    proc = self.run_cli([*self.store_cli, *args],
                                        env={'MANAGED_AI_DOMAIN_ADMISSION_SECONDS': '1'})
                    self.assertTrue(json.loads(proc.stdout))

    def test_shared_lock_falls_back_to_readonly_open_on_eacces(self):
        lock = self.store / 'exchange/owner.lock'
        lock.touch()
        real_open = Path.open
        modes = []

        def restricted_open(path, mode='r', *args, **kwargs):
            if path == lock:
                modes.append(mode)
                if mode == 'a+b':
                    raise PermissionError(errno.EACCES, 'Permission denied', str(path))
            return real_open(path, mode, *args, **kwargs)

        with patch.object(Path, 'open', restricted_open):
            with shared(lock):
                pass
        self.assertEqual(modes, ['a+b', 'rb'])

    def test_contended_cli_commands_honor_admission_deadline_and_exit_75(self):
        owner_lock = self.store / 'exchange/owner.lock'
        rec_file = _write_json(self.root / 'rec.json', {'id': 'r1', 'kind': 'note', 'content': 'hello'})
        with exclusive(owner_lock):
            for cmd in (
                [*self.select_cli, 'system', 'status'],
                [*self.select_cli, 'system', 'show', 'goal'],
                [*self.select_cli, 'system', 'put', '--record', str(rec_file), '--expected', '0', '--no-status'],
                [*self.store_cli, 'status'],
                [*self.store_cli, 'put', '--record', str(rec_file), '--expected', '0', '--no-status'],
            ):
                with self.subTest(cmd=cmd):
                    proc = self.run_cli(cmd, env={'MANAGED_AI_DOMAIN_ADMISSION_SECONDS': '0.1'}, expected_rc=75)
                    self.assertEqual(proc.stdout, '')
                    err = json.loads(proc.stderr)
                    self.assertEqual(err['status'], 'contended')
                    self.assertEqual(err['lock'], str(owner_lock))

    def test_single_code_identity_per_cli_invocation(self):
        probe = (
            "import fcntl, sys\n"
            "from pathlib import Path\n"
            "from unittest.mock import patch\n"
            "import controller\n"
            "calls = 0\n"
            "locked_during_hash = []\n"
            "owner_lock = Path(sys.argv[1])\n"
            "orig = controller.code_identity\n"
            "def counted(engine):\n"
            "    global calls\n"
            "    calls += 1\n"
            "    with owner_lock.open('rb') as fh:\n"
            "        try:\n"
            "            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)\n"
            "            locked_during_hash.append(False)\n"
            "        except BlockingIOError:\n"
            "            locked_during_hash.append(True)\n"
            "    return orig(engine)\n"
            "with patch.object(controller, 'code_identity', side_effect=counted):\n"
            "    sys.argv = ['controller.py', *sys.argv[2:]]\n"
            "    controller.main()\n"
            "print(f'CODE_IDENTITY_CALLS={calls} LOCKED={locked_during_hash}', file=sys.stderr)\n"
        )
        owner_lock = self.store / 'exchange/owner.lock'
        owner_lock.touch()
        env = os.environ.copy()
        env['PYTHONPATH'] = str(ENGINE / 'runtime/deployment')
        for subcmd in (['status'], ['verify'], ['system', 'show', 'goal'], ['system', 'status']):
            with self.subTest(subcmd=subcmd):
                proc = self.run_cli([sys.executable, '-B', '-c', probe, str(owner_lock),
                                     '--deployment', str(self.dep), *subcmd], env=env)
                self.assertIn('CODE_IDENTITY_CALLS=1', proc.stderr)
                if subcmd[0] == 'system':
                    self.assertIn('LOCKED=[True]', proc.stderr)

    def test_cli_system_and_direct_api_do_not_read_store_before_lock(self):
        from continuation_support.storage import AdmissionContended
        owner_lock = self.store / 'exchange/owner.lock'
        system_json = self.store / 'system.json'
        workspace_json = self.store / 'exchange/workspace.json'
        orig_system = system_json.read_bytes()
        orig_workspace = workspace_json.read_bytes()

        # C6-3(a): CLI `system` waits on owner.lock BEFORE reading system.json (returns exit 75, not JSON parse error)
        with exclusive(owner_lock):
            try:
                system_json.write_bytes(b'{transient-write-in-progress')
                for args in (['system', 'status'], ['system', 'show', 'goal']):
                    with self.subTest(cli_args=args):
                        proc = self.run_cli([*self.select_cli, *args],
                                            env={'MANAGED_AI_DOMAIN_ADMISSION_SECONDS': '0.1'}, expected_rc=75)
                        self.assertEqual(proc.stdout, '')
                        err = json.loads(proc.stderr)
                        self.assertEqual(err['status'], 'contended')
                        self.assertEqual(err['lock'], str(owner_lock))
            finally:
                system_json.write_bytes(orig_system)

        # C6-3(b): Direct Deployment.system() acquires owner.lock BEFORE constructing Exchange / reading workspace.json
        deployment = Deployment(self.dep)
        with exclusive(owner_lock):
            try:
                workspace_json.write_bytes(b'{transient-write-in-progress')
                with patch.dict(os.environ, {'MANAGED_AI_DOMAIN_ADMISSION_SECONDS': '0.05'}):
                    with self.assertRaises(AdmissionContended):
                        deployment.system(['status'])
            finally:
                workspace_json.write_bytes(orig_workspace)

    def test_command_parser_rejects_abbreviated_options(self):
        proc = self.run_cli([sys.executable, '-B', str(ENGINE / 'runtime/manage_store.py'),
                             '--roo', str(self.store), 'show', 'goal'], expected_rc=2)
        self.assertIn('--root', proc.stderr)
        proc2 = self.run_cli([sys.executable, '-B', str(ENGINE / 'runtime/select_deployment.py'),
                              '--dep', str(self.dep), 'status'], expected_rc=2)
        self.assertIn('--deployment', proc2.stderr)
        proc3 = self.run_cli([*self.select_cli, 'system', 'put', '--rec', 'x.json', '--exp', '0'], expected_rc=2)
        self.assertIn('--record', proc3.stderr)

    def test_activate_hardening_rejects_symlink_duplicate_key_and_nonfinite(self):
        sel = DeploymentSelection(self.dep)
        original_sha = sel.descriptor_sha256
        staged = sel.stage('next', ENGINE)
        cand_path = self.dep / 'candidate.json'
        cand_bytes = cand_path.read_bytes()

        # Duplicate key in candidate.json
        dup = cand_bytes[:-2] + b',\n  "edition": "next"\n}\n'
        cand_path.write_bytes(dup)
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            sel.activate('next', original_sha, sha(dup), 'Reject duplicate key.')

        # Nonfinite JSON in candidate.json
        bad_nan = cand_bytes[:-2] + b',\n  "extra": NaN\n}\n'
        cand_path.write_bytes(bad_nan)
        with self.assertRaisesRegex(ValueError, 'nonfinite JSON'):
            sel.activate('next', original_sha, sha(bad_nan), 'Reject NaN.')

        # Symlink candidate.json
        real_cand = self.root / 'real-cand.json'
        real_cand.write_bytes(cand_bytes)
        cand_path.unlink()
        cand_path.symlink_to(real_cand)
        with self.assertRaisesRegex(ValueError, 'ordinary descriptor required'):
            sel.activate('next', original_sha, staged['candidate_sha256'], 'Reject symlink candidate.')
        cand_path.unlink()

        # Symlink deployment.json
        dep_path = self.dep / 'deployment.json'
        real_dep = self.root / 'real-dep.json'
        real_dep.write_bytes(dep_path.read_bytes())
        dep_path.unlink()
        dep_path.symlink_to(real_dep)
        cand_path.write_bytes(cand_bytes)
        with self.assertRaisesRegex(ValueError, 'ordinary descriptor required'):
            sel.activate('next', original_sha, staged['candidate_sha256'], 'Reject symlink deployment.')

    def test_no_status_and_status_basis_verification_scope(self):
        system = System(self.store)
        for name, body in (('sources/active.txt', 'v1'), ('sources/duty.txt', 'v1'), ('sources/cold.txt', 'v1')):
            p = system.work / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding='utf-8')
        r_active = _write_json(self.root / 'active.json', {
            'id': 'active-rec', 'kind': 'note', 'content': 'a', 'basis': ['sources/active.txt'], 'active': True})
        r_duty = _write_json(self.root / 'duty.json', {
            'id': 'inactive-duty', 'kind': 'obligation', 'content': {'text': 'keep owed'},
            'basis': ['sources/duty.txt'], 'active': False})
        r_cold = _write_json(self.root / 'cold.json', {
            'id': 'inactive-cold', 'kind': 'note', 'content': 'cold', 'basis': ['sources/cold.txt'], 'active': False})

        # put --no-status omits 'status'
        out = json.loads(self.run_cli([*self.store_cli, 'put', '--record', str(r_active),
                                       '--expected', '0', '--no-status']).stdout)
        self.assertIn('record', out)
        self.assertNotIn('status', out)

        # default put retains 'status'
        out2 = json.loads(self.run_cli([*self.store_cli, 'put', '--record', str(r_duty), '--expected', '0']).stdout)
        self.assertIn('status', out2)
        self.run_cli([*self.store_cli, 'put', '--record', str(r_cold), '--expected', '0', '--no-status'])

        # Mutate all 3 basis files
        for name in ('sources/active.txt', 'sources/duty.txt', 'sources/cold.txt'):
            (system.work / name).write_text('v2-changed', encoding='utf-8')

        default_status = json.loads(self.run_cli([*self.store_cli, 'status']).stdout)
        self.assertEqual(default_status['basis_verification_scope'], 'active-and-governing')
        self.assertEqual(default_status['deferred_inactive_basis_records'], 1)
        changed = set(default_status['gaps']['changed_record_basis'])
        self.assertEqual(changed, {'active-rec', 'inactive-duty'})

        full_status = json.loads(self.run_cli([*self.store_cli, 'status', '--verify-all-basis']).stdout)
        self.assertEqual(full_status['basis_verification_scope'], 'full')
        self.assertNotIn('deferred_inactive_basis_records', full_status)
        self.assertEqual(set(full_status['gaps']['changed_record_basis']),
                         {'active-rec', 'inactive-duty', 'inactive-cold'})

    def test_head_index_reindex_tamper_detection_and_readonly_no_key_create(self):
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            for i in range(20):
                system.put({'id': f'rec-{i:02d}', 'kind': 'note', 'content': {'title': f'Title {i}', 'body': 'original'}}, 0)
            receipt = system.reindex()
            self.assertEqual(receipt['indexed_records'], 21)
            self.assertTrue(system.record_store.index.path.is_file())
            self.assertTrue(system.record_store.receipts.key_path.is_file())

            # Incremental put keeps index synced
            system.put({'id': 'rec-00', 'kind': 'note', 'content': {'title': 'Title 0 revised', 'body': 'updated'}}, 1)
            summaries = system.record_store.indexed_summaries()
            self.assertIsNotNone(summaries)
            self.assertEqual(summaries['rec-00']['revision'], 2)

            # Tampered HMAC in head-index.json falls back cleanly to strict RecordStore reads
            raw_idx = system.record_store.index.path.read_bytes()
            system.record_store.index.path.write_bytes(raw_idx.replace(b'"note"', b'"fake"', 1))
            self.assertIsNone(system.record_store.index.load(system.record_store.format))
            self.assertIsNone(system.record_store.indexed_summaries())
            # status() still works cleanly by falling back
            st = system.status()
            self.assertEqual(len(st['records']), 21)
            system.record_store.index.path.write_bytes(raw_idx)

            # Same-size, same-mtime tamper on disk MUST be caught by scoped_heads() (TurnScope / context)
            target = system.record_path('rec-05')
            before_stat = target.stat()
            target.write_bytes(target.read_bytes().replace(b'original', b'modified'))
            os.utime(target, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
            with self.assertRaisesRegex(ValueError, 'digest'):
                list(system.record_store.scoped_heads(lambda ident, kind: ident == 'goal'))
            # Restore rec-05 and verify external mtime/size change is also caught by indexed_summaries()
            target.write_bytes(target.read_bytes().replace(b'modified', b'original'))
            os.utime(target, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
            self.assertIsNotNone(system.record_store.indexed_summaries())
            target.write_bytes(target.read_bytes().replace(b'original', b'tampered-longer'))
            with self.assertRaisesRegex(ValueError, 'digest'):
                system.record_store.indexed_summaries()
            # Restore via clean re-put
            target.write_bytes(target.read_bytes().replace(b'tampered-longer', b'original'))

            # Missing head-scope.key MUST NOT be recreated on read-only operations
            system.record_store.receipts.key_path.unlink()
            self.assertIsNone(system.record_store.indexed_summaries())
            system.status()
            list(system.record_store.scoped_heads(lambda ident, kind: ident == 'goal'))
            self.assertFalse(system.record_store.receipts.key_path.exists())

    def test_put_batch_50_records_patch_replace_pin_prevalidate_and_resume(self):
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            basis_file = system.work / 'sources/evidence.txt'
            basis_file.parent.mkdir(parents=True, exist_ok=True)
            basis_file.write_text('pinned evidence', encoding='utf-8')
            heads = {}
            for i in range(50):
                res = system.put({
                    'id': f'item-{i:02d}',
                    'kind': 'note',
                    'content': {'title': f'Item {i}', 'status': 'draft', 'drop_me': 'old'},
                    'relations': [{'relation': 'supports', 'target': 'goal'},
                                  {'relation': 'obsolete', 'target': 'old-ref'}],
                    'basis': ['sources/evidence.txt'],
                    'active': True,
                }, 0)
                heads[f'item-{i:02d}'] = res
            system.reindex()

        # 1. Pre-validation abort with ZERO mutations when op #49 has stale sha256
        bad_ops = [{
            'id': f'item-{i:02d}',
            'expected_revision': 1,
            'expected_sha256': heads[f'item-{i:02d}']['sha256'] if i < 49 else '0' * 64,
            'mode': 'patch',
            'set_active': False,
        } for i in range(50)]
        bad_spec = _write_json(self.root / 'bad-batch.json', {
            'format': 'managed-ai-batch-update/1',
            'id': 'batch-bad',
            'instance_id': 'wave1-store',
            'pin_predecessors': True,
            'operations': bad_ops,
        })
        proc = self.run_cli([*self.select_cli, 'system', 'put-batch', '--spec', str(bad_spec)], expected_rc=2)
        self.assertIn('stale record guard', proc.stderr)
        self.assertEqual(System(self.store).show('item-00')['revision'], 1)
        self.assertFalse((self.store / 'work/.system/batches/batch-bad.json').exists())

        # 2. Valid 50-record patch + 1 rev-0 replace with pin_predecessors=True
        ops = [{
            'id': f'item-{i:02d}',
            'expected_revision': 1,
            'expected_sha256': heads[f'item-{i:02d}']['sha256'],
            'mode': 'patch',
            'add_relations': [
                {'relation': 'supports', 'target': 'goal'},  # duplicate -> auto-deduped
                {'relation': 'refines', 'target': f'item-{(i + 1) % 50:02d}'},
            ],
            'remove_relations': [{'relation': 'obsolete', 'target': 'old-ref'}],
            'set_active': (i % 2 == 0),
            'replace_content_fields': {'status': 'published'},
            'remove_content_fields': ['drop_me'],
        } for i in range(50)]
        ops.append({
            'id': 'item-new',
            'expected_revision': 0,
            'mode': 'replace',
            'record': {'kind': 'note', 'content': {'title': 'Created in batch'}},
        })
        good_spec = _write_json(self.root / 'good-batch.json', {
            'format': 'managed-ai-batch-update/1',
            'id': 'batch-50',
            'instance_id': 'wave1-store',
            'pin_predecessors': True,
            'operations': ops,
        })
        out = json.loads(self.run_cli([*self.select_cli, 'system', 'put-batch', '--spec', str(good_spec)]).stdout)
        self.assertTrue(out['complete'])
        self.assertEqual(len(out['records']), 51)

        s = System(self.store)
        item0 = s.show('item-00')
        self.assertEqual(item0['revision'], 2)
        self.assertEqual(item0['content'], {'title': 'Item 0', 'status': 'published'})
        self.assertEqual(item0['relations'], [
            {'relation': 'supports', 'target': 'goal'},
            {'relation': 'refines', 'target': 'item-01'},
        ])
        self.assertEqual(len(item0['basis']), 1)  # preserved basis intact!
        self.assertEqual(s.show('item-01')['active'], False)
        self.assertEqual(s.show('item-new')['revision'], 1)

        # Predecessor pin recoverable from exchange/objects
        pin0 = out['records'][0]['pinned_object']
        self.assertIsNotNone(pin0)
        old0 = json.loads(s.ex.objects.read(pin0))
        self.assertEqual(old0['revision'], 1)
        self.assertEqual(old0['sha256'], heads['item-00']['sha256'])

        # HeadIndex stayed synced across batch
        summaries = s.record_store.indexed_summaries()
        self.assertEqual(summaries['item-00']['revision'], 2)
        self.assertEqual(summaries['item-new']['revision'], 1)

        # Idempotent retry returns exact receipt
        out_retry = json.loads(self.run_cli([*self.select_cli, 'system', 'put-batch', '--spec', str(good_spec)]).stdout)
        self.assertEqual(out_retry, out)

        # Changed spec under same batch ID refused
        conflict_spec = _write_json(self.root / 'conflict-batch.json', {
            'format': 'managed-ai-batch-update/1',
            'id': 'batch-50',
            'instance_id': 'wave1-store',
            'pin_predecessors': False,
            'operations': ops[:1],
        })
        self.run_cli([*self.select_cli, 'system', 'put-batch', '--spec', str(conflict_spec)], expected_rc=2)

    def test_put_batch_resumes_after_interruption(self):
        from native_runtime.record_batch import put_batch
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            r1 = system.put({'id': 'res-a', 'kind': 'note', 'content': {'v': 1}}, 0)
            r2 = system.put({'id': 'res-b', 'kind': 'note', 'content': {'v': 1}}, 0)
            spec = {
                'format': 'managed-ai-batch-update/1',
                'id': 'resume-batch',
                'instance_id': 'wave1-store',
                'pin_predecessors': True,
                'operations': [
                    {'id': 'res-a', 'expected_revision': 1, 'expected_sha256': r1['sha256'],
                     'mode': 'patch', 'replace_content_fields': {'v': 2}},
                    {'id': 'res-b', 'expected_revision': 1, 'expected_sha256': r2['sha256'],
                     'mode': 'patch', 'replace_content_fields': {'v': 2}},
                ],
            }
            orig_replace = system.record_store._replace

            def fail_on_b(record, expected, **kwargs):
                if record['id'] == 'res-b':
                    raise OSError('simulated crash before second op')
                return orig_replace(record, expected, **kwargs)

            with patch.object(system.record_store, '_replace', side_effect=fail_on_b):
                with self.assertRaisesRegex(OSError, 'simulated crash'):
                    put_batch(system, spec)
            receipt_path = system.work / '.system/batches/resume-batch.json'
            partial = json.loads(receipt_path.read_bytes())
            self.assertFalse(partial['complete'])
            self.assertEqual([r['id'] for r in partial['records']], ['res-a'])
            self.assertEqual(system.show('res-a')['revision'], 2)
            self.assertEqual(system.show('res-b')['revision'], 1)

            completed = put_batch(system, spec)
            self.assertTrue(completed['complete'])
            self.assertEqual([r['id'] for r in completed['records']], ['res-a', 'res-b'])
            self.assertEqual(system.show('res-b')['content'], {'v': 2})

    def test_put_batch_reconciles_crash_after_replace_before_receipt_for_patch_and_replace(self):
        from native_runtime import record_batch
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            p1 = system.put({'id': 'wal-patch', 'kind': 'note', 'content': {'v': 1}}, 0)
            r1 = system.put({'id': 'wal-rep1', 'kind': 'note', 'content': {'v': 10}}, 0)
            spec = {
                'format': 'managed-ai-batch-update/1',
                'id': 'wal-crash-batch',
                'instance_id': 'wave1-store',
                'pin_predecessors': True,
                'operations': [
                    {'id': 'wal-patch', 'expected_revision': 1, 'expected_sha256': p1['sha256'],
                     'mode': 'patch', 'replace_content_fields': {'v': 2}},
                    {'id': 'wal-rep1', 'expected_revision': 1, 'expected_sha256': r1['sha256'],
                     'mode': 'replace', 'record': {'kind': 'note', 'content': {'v': 20}}},
                    {'id': 'wal-rep0', 'expected_revision': 0,
                     'mode': 'replace', 'record': {'kind': 'note', 'content': {'v': 100}}},
                ],
            }
            orig_write = record_batch.write_json
            receipt_path = system.work / '.system/batches/wal-crash-batch.json'

            # Crash immediately after _replace() commits and before the clearing write_json for EVERY op mode
            for expected_id in ('wal-patch', 'wal-rep1', 'wal-rep0'):
                saw_pending = {'flag': False}

                def crash_after_commit(path, value):
                    if path == receipt_path and value.get('pending', {}).get('id') == expected_id:
                        saw_pending['flag'] = True
                        return orig_write(path, value)
                    if path == receipt_path and saw_pending['flag'] and 'pending' not in value:
                        raise OSError(f'simulated crash after commit of {expected_id}')
                    return orig_write(path, value)

                with patch.object(record_batch, 'write_json', side_effect=crash_after_commit):
                    with self.assertRaisesRegex(OSError, f'after commit of {expected_id}'):
                        record_batch.put_batch(system, spec)
                partial = json.loads(receipt_path.read_bytes())
                self.assertEqual(partial['pending']['id'], expected_id)

            completed = record_batch.put_batch(system, spec)
            self.assertTrue(completed['complete'])
            self.assertNotIn('pending', completed)
            self.assertEqual([r['id'] for r in completed['records']], ['wal-patch', 'wal-rep1', 'wal-rep0'])
            self.assertEqual(system.show('wal-patch')['content'], {'v': 2})
            self.assertEqual(system.show('wal-rep1')['content'], {'v': 20})
            self.assertEqual(system.show('wal-rep0')['content'], {'v': 100})
            # Verify rev-1 predecessors are pinned and intact
            self.assertEqual(json.loads(system.ex.objects.read(completed['records'][0]['pinned_object']))['sha256'],
                             p1['sha256'])
            self.assertEqual(json.loads(system.ex.objects.read(completed['records'][1]['pinned_object']))['sha256'],
                             r1['sha256'])

    def test_put_batch_refreshes_head_index_on_partial_failure_and_resume(self):
        from native_runtime.record_batch import put_batch
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            r1 = system.put({'id': 'idx-a', 'kind': 'note', 'content': {'v': 1}}, 0)
            r2 = system.put({'id': 'idx-b', 'kind': 'note', 'content': {'v': 1}}, 0)
            system.reindex()
            stale_index_bytes = system.record_store.index.path.read_bytes()
            spec = {
                'format': 'managed-ai-batch-update/1',
                'id': 'idx-resume-batch',
                'instance_id': 'wave1-store',
                'pin_predecessors': False,
                'operations': [
                    {'id': 'idx-a', 'expected_revision': 1, 'expected_sha256': r1['sha256'],
                     'mode': 'patch', 'replace_content_fields': {'v': 2}},
                    {'id': 'idx-b', 'expected_revision': 1, 'expected_sha256': r2['sha256'],
                     'mode': 'patch', 'replace_content_fields': {'v': 2}},
                ],
            }
            orig_replace = system.record_store._replace

            def fail_on_b(record, expected, **kwargs):
                if record['id'] == 'idx-b':
                    raise OSError('simulated failure on second op')
                return orig_replace(record, expected, **kwargs)

            with patch.object(system.record_store, '_replace', side_effect=fail_on_b):
                with self.assertRaisesRegex(OSError, 'simulated failure'):
                    put_batch(system, spec)

            # Even immediately after partial failure, try/finally refreshed idx-a in HeadIndex
            mid = system.record_store.indexed_summaries()
            self.assertIsNotNone(mid)
            self.assertEqual(mid['idx-a']['revision'], 2)
            self.assertEqual(mid['idx-b']['revision'], 1)

            # Simulate hard kill where HeadIndex was never flushed before process death, then resume
            system.record_store.index.path.write_bytes(stale_index_bytes)
            completed = put_batch(system, spec)
            self.assertTrue(completed['complete'])
            after = system.record_store.indexed_summaries()
            self.assertIsNotNone(after)
            self.assertEqual(after['idx-a']['revision'], 2)
            self.assertEqual(after['idx-b']['revision'], 2)

    def test_put_batch_pin_predecessors_supports_legacy_over_2mib_head(self):
        from continuation_support.storage import MAX_BYTES
        from native_runtime.record_batch import put_batch
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            big_payload = 'L' * (MAX_BYTES + 4096)
            body = {
                'format': system.record_store.format,
                'id': 'legacy-big',
                'kind': 'note',
                'revision': 1,
                'content': {'blob': big_payload},
                'relations': [],
                'basis': [],
                'active': True,
            }
            legacy_sha = sha(encode(body))
            raw = encode(dict(body, sha256=legacy_sha))
            self.assertGreater(len(raw), MAX_BYTES)
            system.record_path('legacy-big').write_bytes(raw)

            spec = {
                'format': 'managed-ai-batch-update/1',
                'id': 'legacy-pin-batch',
                'instance_id': 'wave1-store',
                'pin_predecessors': True,
                'operations': [{
                    'id': 'legacy-big',
                    'expected_revision': 1,
                    'expected_sha256': legacy_sha,
                    'mode': 'patch',
                    'replace_content_fields': {'blob': 'compacted'},
                }],
            }
            res = put_batch(system, spec)
            self.assertTrue(res['complete'])
            self.assertEqual(system.show('legacy-big')['revision'], 2)
            pinned_key = res['records'][0]['pinned_object']
            restored = json.loads(system.ex.objects.read(pinned_key, retained=True))
            self.assertEqual(restored['sha256'], legacy_sha)
            self.assertEqual(len(restored['content']['blob']), len(big_payload))

    def test_canonical_digest_rejects_noncanonical_raw_slice_forgery(self):
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            system.put({'id': 'canon', 'kind': 'note', 'content': {'k': 'value'}}, 0)
            rec = system.show('canon')
            path = system.record_path('canon')
            # Valid compact JSON with exact canonical sha256 must pass
            compact = json.dumps(rec, separators=(',', ':')).encode('utf-8')
            path.write_bytes(compact)
            self.assertEqual(system.show('canon')['sha256'], rec['sha256'])
            # Tampered content must fail
            path.write_bytes(compact.replace(b'value', b'WRONG'))
            with self.assertRaisesRegex(ValueError, 'record digest changed'):
                system.show('canon')
            # C6-5: Non-canonical pretty JSON with sha256 computed over non-canonical raw slice MUST be rejected
            canonical_without_sha = encode({k: v for k, v in rec.items() if k != 'sha256'})
            noncanonical_without_sha = canonical_without_sha.replace(b'"revision": 1', b'"revision":  1')
            self.assertNotEqual(noncanonical_without_sha, canonical_without_sha)
            forged_digest = sha(noncanonical_without_sha)
            idx = noncanonical_without_sha.rfind(b'\n}\n')
            forged_raw = (
                noncanonical_without_sha[:idx]
                + f',\n  "sha256": "{forged_digest}"\n}}\n'.encode('ascii')
            )
            # Also test with sha256 placed alphabetically right before "relations" (matching old slice layout)
            marker = b'\n  "relations": '
            pos = noncanonical_without_sha.find(marker)
            forged_alpha = (
                noncanonical_without_sha[:pos]
                + f'\n  "sha256": "{forged_digest}",'.encode('ascii')
                + noncanonical_without_sha[pos:]
            )
            for payload in (forged_raw, forged_alpha):
                path.write_bytes(payload)
                with self.assertRaisesRegex(ValueError, 'record digest changed'):
                    system.show('canon')


if __name__ == '__main__':
    unittest.main(verbosity=2)
