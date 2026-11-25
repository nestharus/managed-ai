"""Wave 4 regression tests for core, deployment, store, library, and tools."""
from __future__ import annotations

import ast
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from .native_test_paths import APP, ENGINE
else:
    from native_test_paths import APP, ENGINE

from core.attention import Attention
from core.exchange import Exchange
from core.ownership import external_root
from core.storage import AdmissionContended, encode, exclusive, sha, shared
from deployment.controller import Deployment
from store import domain_api
from store.cli.command_dispatch import emit
from store.composition.composition_change_notice import notice
from store.composition.composition_opportunities import guarded_inventory
from store.system import ENTRYPOINT, EngineEntrypoint, System
from tools import open_contact


class Wave4CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = self.base / 'store'
        System.initialize(self.store, {'contribution': 'Verify Wave 4 core contracts.'}, instance_id='w4-inst')
        self.dep = self.base / 'dep'
        Deployment.initialize(self.dep, self.store, ENGINE)

    def _run(self, argv, *, env=None, expected_rc=0):
        merged = dict(os.environ)
        if env:
            merged.update(env)
        proc = subprocess.run(argv, capture_output=True, text=True, env=merged, timeout=30)
        self.assertEqual(proc.returncode, expected_rc, f'rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}')
        return proc

    def test_w4_cb1_change_notice_cross_platform_git_reader(self):
        """W4-CB1: composition_change_notice.notice() works natively on Windows and POSIX without selectors pipe failure."""
        repo = self.base / 'repo'
        repo.mkdir()
        git_env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        git_env.update({
            'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.com',
            'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.com',
        })
        for cmd in (
            ['git', '-C', str(repo), 'init'],
            ['git', '-C', str(repo), 'config', 'core.autocrlf', 'false'],
        ):
            subprocess.run(cmd, check=True, capture_output=True, env=git_env)
        (repo / 'alpha.py').write_bytes(b'x = 1\r\n')
        (repo / 'beta.py').write_bytes(b'y = 2\n')
        subprocess.run(['git', '-C', str(repo), 'add', 'alpha.py', 'beta.py'], check=True, capture_output=True, env=git_env)
        subprocess.run(['git', '-C', str(repo), 'commit', '-m', 'base'], check=True, capture_output=True, env=git_env)
        base_rev = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                                  check=True, capture_output=True, text=True, env=git_env).stdout.strip()
        (repo / 'alpha.py').write_bytes(b'x = 2\r\n')
        (repo / 'untracked.txt').write_text('hello\n', encoding='utf-8')

        sys_obj = System(self.store)
        with exclusive(self.store / 'exchange/owner.lock'):
            sys_obj.put({
                'id': 'comp-notice',
                'kind': 'note',
                'content': {
                    'format': 'g60-composition/1',
                    'purpose': 'Cross-platform change notice fixture.',
                    'elements': [
                        {'id': 'a', 'account': 'alpha module', 'content': {'paths': ['alpha.py']}},
                        {'id': 'b', 'account': 'beta module', 'content': {'paths': ['beta.py']}},
                    ],
                    'relations': [
                        {'id': 'ab', 'from': ['a'], 'to': ['b'], 'prediction': 'check beta'},
                    ],
                },
            }, 0)
            report = notice(sys_obj, 'comp-notice', repo, base_rev, 50)
        self.assertEqual(report['format'], 'g60-change-notice/1')
        self.assertEqual(report['comparison'], 'changes-present')
        self.assertEqual(report['touched_elements'], ['a'])
        self.assertIn('alpha.py', report['changed'])
        self.assertIn('untracked.txt', report['unmapped_changes'])
        self.assertEqual(report['candidate_relations'], [{'id': 'ab', 'prediction': 'check beta'}])

    def test_w4_cb3_library_cli_and_audit_feedback_respect_owner_lock(self):
        """W4-CB3: engine/library/cli.py --store and engine/tools/audit_feedback.py acquire shared owner.lock."""
        lock_path = self.store / 'exchange/owner.lock'
        short_env = {'MANAGED_AI_DOMAIN_ADMISSION_SECONDS': '0.05'}

        # Under active exclusive lock, both must fail with AdmissionContended exit 75
        with exclusive(lock_path):
            p1 = self._run([sys.executable, '-B', str(ENGINE / 'library/cli.py'),
                            '--store', str(self.store), 'discover', '--query', 'guide'],
                           env=short_env, expected_rc=75)
            self.assertEqual(json.loads(p1.stderr)['status'], 'contended')

            p2 = self._run([sys.executable, '-B', str(ENGINE / 'tools/audit_feedback.py'),
                            str(ENGINE), str(self.store), 'goal', '--obligation'],
                           env=short_env, expected_rc=75)
            self.assertEqual(json.loads(p2.stderr)['status'], 'contended')

        # Under active shared lock, both succeed concurrently (non-mutating read admission)
        with shared(lock_path, allow_readonly_open=True):
            p3 = self._run([sys.executable, '-B', str(ENGINE / 'library/cli.py'),
                            '--store', str(self.store), 'discover', '--query', 'guide'],
                           env=short_env, expected_rc=0)
            self.assertIn('entries', json.loads(p3.stdout))

            p4 = self._run([sys.executable, '-B', str(ENGINE / 'tools/audit_feedback.py'),
                            str(ENGINE), str(self.store), 'goal', '--obligation'],
                           env=short_env, expected_rc=0)
            self.assertEqual(json.loads(p4.stdout)['record'], 'goal')

    def test_w4_cb4_library_cli_discover_query_does_not_hijack_library_flag(self):
        """W4-CB4: `library/cli.py discover --query --library` queries InstalledCatalogue instead of hijacking."""
        for argv in (
            [sys.executable, '-B', str(ENGINE / 'library/cli.py'), 'discover', '--query', '--library'],
            [sys.executable, '-B', str(ENGINE / 'library/cli.py'), 'discover', '--query=--library'],
        ):
            proc = self._run(argv, expected_rc=0)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload['query'], '--library')
            self.assertIn('entries', payload)

    def test_w4_cb5_exchange_and_attention_clis_use_shared_lock_and_exit_75(self):
        """W4-CB5: Read-only exchange/attention CLIs take shared owner.lock and return exit 75 under contention."""
        ex_root = self.store / 'exchange'
        lock_path = ex_root / 'owner.lock'
        short_env = {'MANAGED_AI_DOMAIN_ADMISSION_SECONDS': '0.05'}

        with exclusive(lock_path):
            System(self.store).attention.feedback('fb-cb5')

        # Under shared lock: list/inspect succeed concurrently!
        with shared(lock_path, allow_readonly_open=True):
            p_ex = self._run([sys.executable, '-B', str(ENGINE / 'core/cli/exchange_packets.py'),
                              '--root', str(ex_root), 'list'], env=short_env, expected_rc=0)
            self.assertIsInstance(json.loads(p_ex.stdout), list)

            p_att = self._run([sys.executable, '-B', str(ENGINE / 'core/cli/track_attention.py'),
                               '--root', str(ex_root), 'inspect', 'fb-cb5'], env=short_env, expected_rc=0)
            self.assertEqual(json.loads(p_att.stdout)['id'], 'fb-cb5')

        # Under exclusive lock: read-only CLIs exit 75 with structured contention JSON
        with exclusive(lock_path):
            p_ex2 = self._run([sys.executable, '-B', str(ENGINE / 'core/cli/exchange_packets.py'),
                               '--root', str(ex_root), 'list'], env=short_env, expected_rc=75)
            self.assertEqual(json.loads(p_ex2.stderr)['status'], 'contended')

            p_att2 = self._run([sys.executable, '-B', str(ENGINE / 'core/cli/track_attention.py'),
                                '--root', str(ex_root), 'inspect', 'fb-cb5'], env=short_env, expected_rc=75)
            self.assertEqual(json.loads(p_att2.stderr)['status'], 'contended')

    def test_w4_cb6_guarded_inventory_uses_shared_lock_and_respects_already_locked(self):
        """W4-CB6: guarded_inventory uses shared (not exclusive) on external proof store and skips when already_locked."""
        proof = self.base / 'proof-store'
        System.initialize(proof, {'contribution': 'External proof store.'}, instance_id='proof-inst')
        binding = {'store': os.path.relpath(proof, self.store).replace('\\', '/'),
                   'instance_id': 'proof-inst', 'id': 'inv'}
        sys_obj = System(self.store)

        # Concurrent shared lock on proof store MUST NOT block guarded_inventory!
        with shared(proof / 'exchange/owner.lock', allow_readonly_open=True):
            with guarded_inventory(sys_obj, binding, proof) as owner:
                self.assertEqual(owner.instance_id, 'proof-inst')

        # Active exclusive lock on proof store MUST raise AdmissionContended when already_locked=False
        with exclusive(proof / 'exchange/owner.lock'):
            with patch.dict(os.environ, {'MANAGED_AI_DOMAIN_ADMISSION_SECONDS': '0.05'}):
                with self.assertRaises(AdmissionContended):
                    with guarded_inventory(sys_obj, binding, proof, already_locked=False):
                        self.fail('should contend on proof store exclusive lock')
            # When caller already holds locks (already_locked=True), no nested re-lock occurs
            with guarded_inventory(sys_obj, binding, proof, already_locked=True) as owner:
                self.assertEqual(owner.instance_id, 'proof-inst')

    def test_w4_cb7_collect_returns_ownership_and_open_contact(self):
        """W4-CB7: collect_returns runs on Windows, external_root guards all of engine/, open_contact handles plain stderr."""
        p = self._run([sys.executable, '-B', str(ENGINE / 'core/cli/collect_returns.py'), '--help'], expected_rc=0)
        self.assertIn('admit', p.stdout)

        # external_root rejects paths inside engine/ (including engine/host, engine/core, engine/release)
        for sub in ('core/x', 'store/x', 'host/x', 'release/x', 'deployment/x'):
            with self.assertRaisesRegex(ValueError, 'outside the distributed package'):
                external_root(ENGINE / sub)

        # open_contact._error_code handles non-JSON stderr without raising JSONDecodeError
        self.assertIsNone(open_contact._error_code(open_contact.NativeError('plain traceback text')))
        self.assertEqual(
            open_contact._error_code(open_contact.NativeError('{"code": "feedback_revision_conflict"}')),
            'feedback_revision_conflict',
        )

    def test_w4_h3_attention_inspect_receipt_contract(self):
        """W4-H3: Attention.inspect_receipt(rid) returns None when absent or exact 4-key dict when recorded."""
        sys_obj = System(self.store)
        self.assertIsNone(sys_obj.attention.inspect_receipt('nonexistent-receipt'))
        with exclusive(self.store / 'exchange/owner.lock'):
            spec_path = sys_obj.work / 'att-spec.json'
            spec_path.write_text(json.dumps({
                'items': [{'id': 'q1', 'use': 'verify receipt', 'question': 'is receipt inspectable?'}],
            }), encoding='utf-8')
            sys_obj.attention.track('att-spec.json')
            fb = sys_obj.attention.feedback('fb-1', items=['q1'])
            rec_path = sys_obj.work / 'assessment.json'
            rec_path.write_text(json.dumps({
                'id': 'rec-1',
                'assessments': [{'item': 'q1', 'disposition': 'considered', 'account': 'checked'}],
            }), encoding='utf-8')
            recorded = sys_obj.attention.record('fb-1', 'assessment.json', 'tester')
            inspected = sys_obj.attention.inspect_receipt(recorded['receipt'])
        self.assertIsNotNone(inspected)
        self.assertEqual(set(inspected.keys()), {'id', 'object', 'record', 'feedback_object'})
        self.assertEqual(inspected['id'], recorded['receipt'])
        self.assertEqual(inspected['record']['feedback'], 'fb-1')
        self.assertEqual(inspected['feedback_object'], fb['feedback_object'])

    def test_w4_o8_entrypoint_domain_api_and_pure_emit(self):
        """W4-O8 / W4-O6: Typed EngineEntrypoint, curated domain_api exports, and pure emit() without disk reads."""
        self.assertIsInstance(ENTRYPOINT, EngineEntrypoint)
        self.assertEqual(ENTRYPOINT.deployment_api, 'agent-system-deployment/2')
        self.assertEqual(ENTRYPOINT.engine_api, 'managed-ai-runtime/1')
        for name in (
            'System', 'NativeError', 'TurnPreparation', 'Composition', 'ContextDiscovery',
            'DomainSelection', 'InstalledCatalogue', 'RecordStore', 'HEAD_INDEX_FORMAT',
            'is_ordinary_file_stat', 'check_ordinary_ancestors', 'strict_json_loads',
            'safe_relpath', 'admission_deadline', 'owner_admission', 'exclusive', 'shared',
            'OwnerBusy', 'ObjectStore', 'Deployment', 'DeploymentSelection',
            'AdmissionContended', 'code_identity', 'open_admitted_store', 'read_descriptor_bytes',
        ):
            self.assertTrue(hasattr(domain_api, name), f'missing domain_api.{name}')
        # emit() must not perform Path(...).read_text() disk I/O
        emit_src = inspect.getsource(emit)
        self.assertNotIn('read_text', emit_src)
        self.assertNotIn('read_bytes', emit_src)

    def test_c29_2_drive_relative_paths_and_materialize_escape_rejected(self):
        """C29-2 / C29-1: reject Windows drive-relative paths across ownership/store/library and emit Wave 4 exchange CLI path."""
        import zipfile
        from core.ownership import relative_path
        from core.exchange.publication import _publication_basis
        from store.protocol.inputs import artifact_target
        from store.selection.domain_selection import source_path
        from store.selection.installed_catalogue import InstalledCatalogue
        from library.reader.library_format import relative as lib_relative

        bad_paths = (
            'C:escape.txt',
            'a/C:escape.txt',
            'c:nested/file.txt',
            'Z:alias.txt',
            'foo\x00bar.txt',
            'a\\b.txt',
        )
        for bad in bad_paths:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    relative_path(bad)
                with self.assertRaises(ValueError):
                    artifact_target(bad)
                with self.assertRaises(ValueError):
                    source_path(self.base, bad)
                with self.assertRaises(ValueError):
                    lib_relative(bad)

        sys_obj = System(self.store)
        with self.assertRaises(ValueError):
            sys_obj.ex.path('C:alias.txt')

        # InstalledCatalogue.materialize must reject zip member slug/C:escape.txt without writing outside staging
        cat_dir = self.base / 'crafted-cat'
        (cat_dir / 'operations').mkdir(parents=True)
        payload = b'escape-attempt'
        archive = cat_dir / 'operations/demo.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('demo/C:escape.txt', payload)
        archive_bytes = archive.read_bytes()
        cat_doc = {
            'format': 'agent-system-catalogue/1',
            'entries': [],
            'assets': [{
                'kind': 'operation-archive',
                'operation_id': 'operation:demo',
                'path': 'operations/demo.zip',
                'sha256': sha(archive_bytes),
                'bytes': len(archive_bytes),
                'members': [{
                    'path': 'demo/C:escape.txt',
                    'sha256': sha(payload),
                    'bytes': len(payload),
                }],
            }],
        }
        (cat_dir / 'catalogue.json').write_text(json.dumps(cat_doc), encoding='utf-8')
        cat = InstalledCatalogue(cat_dir)
        with self.assertRaises(ValueError):
            cat.materialize('operation:demo', sys_obj.ex)
        self.assertFalse((self.base / 'escape.txt').exists())
        self.assertFalse((sys_obj.work / 'operations/demo').exists())

        # C29-1: _publication_basis emits canonical Wave 4 core/cli/exchange_packets.py entry
        basis = _publication_basis(
            'p1',
            'out.txt',
            {'packet': 'p1', 'targets': [{'path': 'out.txt', 'sha256': 'd' * 64}]},
            {'packet': 'p1', 'request_sha256': 'a' * 64, 'return_sha256': 'b' * 64},
            {'sha256': 'c' * 64, 'base_sha256': 'd' * 64},
            [],
        )
        self.assertIsNotNone(basis)
        self.assertEqual(basis['inspect']['entry'], 'core/cli/exchange_packets.py')


if __name__ == '__main__':
    unittest.main(verbosity=2)
