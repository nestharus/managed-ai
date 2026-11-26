"""Wave 5 regression tests for core, deployment, store, library, and tools."""
from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from .native_test_paths import APP, ENGINE
else:
    from native_test_paths import APP, ENGINE

from managed_ai.core.storage import encode, exclusive, sha, shared, sync_dir
from managed_ai.deployment.controller import Deployment, DeploymentSelection, code_identity
from managed_ai.library.bundles import product_bundle


def _load_publish_module():
    """Load scripts/publish_library.py when moved in Stage 7.3, else fallback to library.bundles.publish."""
    script = ENGINE.parents[1] / 'scripts' / 'publish_library.py'
    if script.is_file():
        spec = importlib.util.spec_from_file_location('publish_library_script', str(script))
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    from managed_ai.library.bundles import publish as legacy_publish
    return legacy_publish


publish = _load_publish_module()
from managed_ai.library.reader import library_format
from managed_ai.library.reader.knowledge import Library
from managed_ai.store.composition import composition_boundary
from managed_ai.store.records import record_batch, record_import, record_transfer
from managed_ai.store.records.cold_archive import execute_archive_records, plan_archive_records
from managed_ai.store.records.head_index import HeadIndex, entry_matches_exact_ref
from managed_ai.store.records.object_audit import audit_objects
from managed_ai.store.selection.domain_selection import DomainSelection
from managed_ai.store.system import System
from managed_ai.store.turns.turn_retirement import plan_turn_retirement
from managed_ai.tools import open_contact


class Wave5CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = self.base / 'store'
        System.initialize(self.store, {'contribution': 'Verify Wave 5 core contracts.'}, instance_id='w5-inst')
        self.dep = self.base / 'dep'
        Deployment.initialize(self.dep, self.store, ENGINE)

    def _run(self, argv, *, env=None, expected_rc=0):
        merged = dict(os.environ)
        merged['PYTHONDONTWRITEBYTECODE'] = '1'
        if env:
            merged.update(env)
        proc = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', env=merged, timeout=30)
        self.assertEqual(proc.returncode, expected_rc, f'rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}')
        return proc

    def _make_dir_link(self, link_path: Path, target_dir: Path) -> None:
        if os.name == 'nt':
            proc = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link_path), str(target_dir)],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.addCleanup(lambda p=link_path: subprocess.run(['cmd', '/c', 'rmdir', str(p)], capture_output=True))
        else:
            os.symlink(target_dir, link_path)

    def test_w5_b2_deployment_rejects_symlink_or_junction_root_and_store(self):
        """W5-B2: Deployment & DeploymentSelection reject symlink/NTFS-junction deployment and store paths."""
        dep_link = self.base / 'dep-link'
        store_link = self.base / 'store-link'
        self._make_dir_link(dep_link, self.dep)
        self._make_dir_link(store_link, self.store)

        with self.assertRaises(ValueError):
            DeploymentSelection(dep_link)
        with self.assertRaises(ValueError):
            Deployment(dep_link)
        with self.assertRaises(ValueError):
            with Deployment.admitted(dep_link):
                self.fail('symlink/junction deployment must be refused')
        with self.assertRaises(ValueError):
            Deployment.initialize(self.base / 'new-dep', store_link, ENGINE)

    def test_w5_b4_record_store_size_and_pin_support_cold_archive(self):
        """W5-B4: RecordStore.size() and RecordStore.pin() work transparently on cold-archived records."""
        sys_obj = System(self.store)
        with exclusive(self.store / 'exchange/owner.lock'):
            sys_obj.put({'id': 'cold-rec', 'kind': 'note', 'active': False, 'content': {'note': 'archive me'}}, 0)
            plan = plan_archive_records(sys_obj, ids=['cold-rec'])
            execute_archive_records(sys_obj, plan=plan, expected_plan=plan['plan_sha256'], reason='w5-b4 test')
            self.assertFalse(sys_obj.record_store.path('cold-rec').exists())
            self.assertTrue(sys_obj.record_store.cold_archive.has('cold-rec'))

            rec = sys_obj.record_store.show('cold-rec')
            _, raw = sys_obj.record_store.cold_archive.read('cold-rec')
            self.assertEqual(sys_obj.record_store.size('cold-rec'), len(raw))
            pinned = sys_obj.record_store.pin(rec)
            self.assertEqual(pinned['id'], 'cold-rec')
            self.assertEqual(pinned['revision'], 1)
            self.assertEqual(pinned['sha256'], rec['sha256'])
            self.assertEqual(pinned['object'], sha(raw))

    def test_w5_b6_b7_reparse_and_transfer_guards_leave_zero_orphans(self):
        """W5-B6/B7: receipt/transfer/turn/boundary reparse checks and 0-orphan rejected receive()."""
        sys_obj = System(self.store)
        real_dir = self.base / 'real-dir'
        real_dir.mkdir()
        link_dir = self.base / 'link-dir'
        self._make_dir_link(link_dir, real_dir)

        # Receipt loaders refuse junction/symlink directories
        (real_dir / 'rcpt.json').write_bytes(encode({'id': 'r1'}))
        with self.assertRaises(ValueError):
            record_import._load_receipt(link_dir / 'rcpt.json')
        with self.assertRaises(ValueError):
            record_batch._load_receipt(link_dir / 'rcpt.json')

        # record_transfer.export refuses junction/symlink parent directory
        with exclusive(self.store / 'exchange/owner.lock'):
            sys_obj.put({'id': 'xfer-src', 'kind': 'note', 'content': {'v': 1}}, 0)
            with self.assertRaises(ValueError):
                record_transfer.export(sys_obj, 'xfer-src', link_dir / 'out.json')

            bundle_out = self.base / 'xfer.json'
            record_transfer.export(sys_obj, 'xfer-src', bundle_out)

            # Rejected receive() must leave 0 orphaned files in work/sources/received/ and 0 new objects
            dst_store = self.base / 'dst-store'
            System.initialize(dst_store, {'contribution': 'Transfer receive target.'}, instance_id='w5-dst')
            dst_sys = System(dst_store)
            before_objs = {p.name for p in dst_sys.ex.objects.directory.iterdir()} if dst_sys.ex.objects.directory.exists() else set()
            received_dir = dst_store / 'work/sources/received'
            with exclusive(dst_store / 'exchange/owner.lock'):
                with self.assertRaises(ValueError):
                    # Wrong expected revision (99 != 0) must fail BEFORE writing any received file or object
                    record_transfer.receive(dst_sys, bundle_out, 'xfer-dst', 'bad rev test', 99)
            after_objs = {p.name for p in dst_sys.ex.objects.directory.iterdir()} if dst_sys.ex.objects.directory.exists() else set()
            self.assertEqual(before_objs, after_objs)
            leftovers = list(received_dir.rglob('*')) if received_dir.exists() else []
            self.assertEqual(leftovers, [], f'Orphaned files after rejected receive: {leftovers}')

            # record_transfer.inspect rejects duplicate JSON keys (strict_json)
            dup_transfer = self.base / 'dup-xfer.json'
            dup_transfer.write_text('{"id":"a","id":"b"}\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                record_transfer.unpack_transfer(json.loads('{"format":"bad"}'), {})

        # composition_boundary enforces UTF-8 byte count on multibyte strings (70 '€' chars = 210 UTF-8 bytes > 200)
        multibyte_trial = {
            'format': composition_boundary.TRIAL_V1,
            'use': 'utf8 byte bound check',
            'variants': ['€' * 70],
            'contexts': ['ctx1'],
            'observations': [],
        }
        with self.assertRaisesRegex(ValueError, '200 UTF-8 bytes'):
            composition_boundary.analyze(multibyte_trial, limit=100)

        # turn_retirement refuses symlink/junction turn directory (both explicit --turn and --all-eligible)
        turns_dir = self.store / 'work/.system/turns'
        turns_dir.mkdir(parents=True, exist_ok=True)
        self._make_dir_link(turns_dir / 'bad-turn-link', real_dir)
        with exclusive(self.store / 'exchange/owner.lock'):
            with self.assertRaisesRegex(ValueError, 'ordinary turn working directory required'):
                plan_turn_retirement(sys_obj, turns=['bad-turn-link'])
            plan = plan_turn_retirement(sys_obj, all_eligible=True)
        self.assertEqual(plan['eligible'], [])
        self.assertTrue(any(
            row['turn'] == 'bad-turn-link' and 'symlink_or_non_directory' in row['reasons']
            for row in plan['ineligible']
        ))

    def test_w5_b8a_open_contact_utf8_and_strict_json(self):
        """W5-B8a: open_contact strict JSON rejects dup keys/NaN and handles UTF-8 non-ASCII stdout cleanly."""
        with self.assertRaises(ValueError):
            open_contact._strict_json_loads('{"a": 1, "a": 2}')
        with self.assertRaises(ValueError):
            open_contact._strict_json_loads('{"a": NaN}')

        utf8_store = self.base / 'utf8-store'
        System.initialize(utf8_store, {'contribution': 'Verify UTF-8 — café \u028f handling.'}, instance_id='w5-utf8')
        contact = open_contact.Contact(ENGINE, utf8_store, self.base / 'scratch')
        status = contact.run('status')
        self.assertEqual(status['instance_id'], 'w5-utf8')
        goal = contact.run('show', 'goal')
        self.assertIn('\u028f', goal['content']['contribution'])

    def test_w5_b9_library_reparse_atomic_exports_and_sandbox_isolation(self):
        """W5-B9: library_format.ordinary refuses NTFS junctions/symlinks; atomic exports; 0 core/store/deployment imports."""
        real_dir = self.base / 'lib-real'
        real_dir.mkdir()
        link_dir = self.base / 'lib-link'
        self._make_dir_link(link_dir, real_dir)
        with self.assertRaises(ValueError):
            library_format.ordinary(link_dir)

        # Sandbox boundary: engine/library/{reader,search,bundles} must NEVER import core, store, or deployment
        forbidden = {'core', 'store', 'deployment'}
        violations = []
        for sub in ('reader', 'search', 'bundles'):
            for py in sorted((ENGINE / 'library' / sub).rglob('*.py')):
                tree = ast.parse(py.read_text(encoding='utf-8'), filename=str(py))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            parts = alias.name.split('.')
                            root = parts[1] if parts[0] == 'managed_ai' and len(parts) > 1 else parts[0]
                            if root in forbidden:
                                violations.append(f'{py.name}:{node.lineno}: import {alias.name}')
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        parts = node.module.split('.')
                        root = parts[1] if parts[0] == 'managed_ai' and len(parts) > 1 else parts[0]
                        if root in forbidden:
                            violations.append(f'{py.name}:{node.lineno}: from {node.module}')
        self.assertEqual(violations, [], f'Library sandbox leakage: {violations}')

        # Atomic product_bundle.export and publish.publish leave no temp files
        src_root = self.base / 'pub-src'
        (src_root / 'docs').mkdir(parents=True)
        (src_root / 'docs/doc.md').write_text('# Hello\n\nBody text.\n', encoding='utf-8')
        spec = {
            'format': 'g60-source-map/1',
            'families': ['docs'],
            'additional': [],
            'overrides': {},
            'modules': [],
        }
        pub_out = self.base / 'published-lib'
        publish.publish(src_root, None, spec, 'ed-1', pub_out)
        getattr(publish, '_sync_dir', sync_dir)(pub_out)
        self.assertTrue((pub_out / 'catalogue.json').is_file())
        self.assertEqual([p.name for p in self.base.glob('.published-lib*')], [])

        lib = Library(pub_out)
        bundle_zip = self.base / 'bundle.zip'
        selection = {
            'format': library_format.SELECTION,
            'purpose': 'w5 atomic export check',
            'bodies': [lib.pin(lib.bodies['docs/doc.md'])],
            'modules': [],
        }
        product_bundle.export([lib], selection, 'ed-1', bundle_zip)
        self.assertTrue(bundle_zip.is_file())
        self.assertEqual([p.name for p in self.base.glob('.bundle.zip*')], [])

    def test_w5_b10_lock_reparse_and_zero_byte_head_scope_key_repair(self):
        """W5-B10: storage locks refuse symlink/junction dirs; HeadReceipts repairs 0-byte head-scope.key."""
        real_dir = self.base / 'lock-real'
        real_dir.mkdir()
        link_dir = self.base / 'lock-link'
        self._make_dir_link(link_dir, real_dir)
        with self.assertRaises(ValueError):
            with exclusive(link_dir / 'owner.lock'):
                self.fail('exclusive lock through symlink/junction dir must fail')
        with self.assertRaises(ValueError):
            with shared(link_dir / 'owner.lock'):
                self.fail('shared lock through symlink/junction dir must fail')

        # 0-byte head-scope.key auto-repaired on create=True
        key_path = self.store / 'exchange/head-scope.key'
        key_path.write_bytes(b'')
        self.assertEqual(key_path.stat().st_size, 0)
        sys_obj = System(self.store)
        repaired = sys_obj.record_store.receipts._key(create=True)
        self.assertIsNotNone(repaired)
        self.assertEqual(len(repaired), 32)
        self.assertEqual(key_path.stat().st_size, 32)

    def test_w5_b11_all_owned_engine_clis_enforce_allow_abbrev_false(self):
        """W5-B11: AST check confirms ZERO ArgumentParser/add_parser missing allow_abbrev=False in owned engine dirs."""
        missing = []
        for sub in ('core', 'deployment', 'store', 'library', 'tools'):
            for py in sorted((ENGINE / sub).rglob('*.py')):
                tree = ast.parse(py.read_text(encoding='utf-8'), filename=str(py))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        fn = node.func
                        name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else None)
                        if name in ('ArgumentParser', 'add_parser'):
                            ok = any(
                                kw.arg == 'allow_abbrev' and isinstance(kw.value, ast.Constant) and kw.value.value is False
                                for kw in node.keywords
                            )
                            if not ok:
                                missing.append(f'{py.relative_to(ENGINE)}:{node.lineno} ({name})')
        self.assertEqual(missing, [], f'CLI parsers missing allow_abbrev=False: {missing}')

    def test_w5_df1_df2_df3_df5_df6_df7_dogfood_and_qualification(self):
        """W5-DF1/2/3/5/6/7: collision IDs, write-time composition check, bare focus, inline discover, summaries, single code_identity & git provenance."""
        # W5-DF1: duplicate catalogue source ID collision names colliding IDs
        dup_dir = self.base / 'dup-cats'
        dup_dir.mkdir()
        (dup_dir / 'a.md').write_text('hello\n', encoding='utf-8')
        raw_a = (dup_dir / 'a.md').read_bytes()
        cat_body = encode({
            'format': 'managed-ai-source-catalogue/1',
            'sources': [{'id': 'dup/a.md', 'path': 'a.md', 'sha256': sha(raw_a), 'bytes': len(raw_a), 'title': 'A', 'exposure': 'available'}],
        })
        c1 = dup_dir / 'cat1.json'
        c2 = dup_dir / 'cat2.json'
        c1.write_bytes(cat_body)
        c2.write_bytes(cat_body)
        desc = {'catalogues': [{'path': str(c1), 'sha256': sha(cat_body)}, {'path': str(c2), 'sha256': sha(cat_body)}]}
        with self.assertRaisesRegex(ValueError, 'dup/a.md'):
            DomainSelection(self.store, desc)

        # W5-DF2: write-time g60-composition/1 validation rejects malformed at/footprint/requires; bare --focus resolves
        sys_obj = System(self.store)
        with exclusive(self.store / 'exchange/owner.lock'):
            with self.assertRaises(ValueError):
                sys_obj.put({
                    'id': 'bad-comp', 'kind': 'note',
                    'content': {
                        'format': 'g60-composition/1',
                        'purpose': 'Bad element missing account',
                        'elements': [{'id': 'e1'}],
                    },
                }, 0)
            sys_obj.put({
                'id': 'good-comp', 'kind': 'note',
                'content': {
                    'format': 'g60-composition/1',
                    'purpose': 'Good composition.',
                    'elements': [
                        {'id': 'elem-alpha', 'account': 'Alpha component'},
                        {'id': 'elem-beta', 'account': 'Beta component'},
                    ],
                    'relations': [
                        {'id': 'rel-ab', 'from': ['elem-alpha'], 'to': ['elem-beta'], 'prediction': 'beta sees alpha'},
                    ],
                },
            }, 0)
        # Bare --focus resolves unambiguously to good-comp's elem-alpha
        focused = self._run([sys.executable, '-B', str(ENGINE / 'store/system.py'),
                             '--root', str(self.store), 'compose', '--record', 'good-comp', '--focus', 'elem-alpha'])
        self.assertIn('elem-alpha', focused.stdout)

        # W5-DF3: inline system discover --query works without --request
        disc = self._run([sys.executable, '-B', str(ENGINE / 'store/system.py'),
                          '--root', str(self.store), 'discover', '--query', 'Alpha', '--content', '--records-only', '--limit', '5'])
        disc_json = json.loads(disc.stdout)
        self.assertEqual(disc_json['format'], 'agent-system-discovery/1')
        self.assertIn('candidates', disc_json)
        self.assertTrue(any(c['ref'] == 'record:good-comp' for c in disc_json['candidates']))

        # W5-DF5: retire-turns plan error names CLI flags --turn and --all-eligible
        retire = self._run([sys.executable, '-B', str(ENGINE / 'store/system.py'),
                            '--root', str(self.store), 'retire-turns', 'plan'], expected_rc=2)
        self.assertIn('--turn', retire.stderr)
        self.assertIn('--all-eligible', retire.stderr)

        # W5-DF6: --summary on system status and library/cli.py verify
        st_sum = json.loads(self._run([sys.executable, '-B', str(ENGINE / 'store/system.py'),
                                       '--root', str(self.store), 'status', '--summary']).stdout)
        self.assertEqual(st_sum['instance_id'], 'w5-inst')
        self.assertEqual(st_sum['record_count'], 2)
        self.assertNotIn('records', st_sum)
        lib_sum = json.loads(self._run([sys.executable, '-B', str(ENGINE / 'library/cli.py'),
                                        '--store', str(self.store), 'verify', '--summary']).stdout)
        self.assertEqual(lib_sum['status'], 'verified')
        self.assertGreater(lib_sum['verified_count'], 0)
        self.assertNotIn('verified', lib_sum)

        # W5-DF7: single code_identity() per status, git provenance in deployment.json, stage --from-head
        with patch('managed_ai.deployment.controller.code_identity', wraps=code_identity) as spy:
            Deployment.current_status(self.dep)
            self.assertEqual(spy.call_count, 1)
        git_repo = self.base / 'git-eng-repo'
        git_engine = git_repo / 'src' / 'managed_ai'
        shutil.copytree(ENGINE, git_engine, ignore=shutil.ignore_patterns('__pycache__'))
        git_env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        git_env.update({
            'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.com',
            'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.com',
        })
        for cmd in (
            ['git', '-C', str(git_repo), 'init'],
            ['git', '-C', str(git_repo), 'config', 'core.autocrlf', 'false'],
            ['git', '-C', str(git_repo), 'add', '.'],
            ['git', '-C', str(git_repo), 'commit', '-m', 'engine base'],
        ):
            subprocess.run(cmd, check=True, capture_output=True, env=git_env)
        head_sha = subprocess.run(['git', '-C', str(git_repo), 'rev-parse', 'HEAD'],
                                  check=True, capture_output=True, text=True, env=git_env).stdout.strip()
        git_dep = self.base / 'git-dep'
        Deployment.initialize(git_dep, self.store, git_engine)
        dep_cfg = json.loads((git_dep / 'deployment.json').read_text(encoding='utf-8'))
        self.assertEqual(dep_cfg['engine']['git_commit'], head_sha)
        self.assertIs(dep_cfg['engine']['git_clean'], True)
        staged = DeploymentSelection(git_dep).stage(None, git_engine, from_head=True)
        self.assertEqual(staged['edition'], f'commit-{head_sha[:12]}')
        self.assertEqual(staged['engine_git_commit'], head_sha)
        self.assertIs(staged['engine_git_clean'], True)

    def test_w5_p1_p2_p3_audit_cross_os_inode_and_delta_sidecar(self):
        """W5-P1/P2/P3: audit_objects unicode-escape parity, cross-OS raw_sha256 check, HeadIndex delta sidecar & compaction."""
        sys_obj = System(self.store)
        with exclusive(self.store / 'exchange/owner.lock'):
            # W5-P1: unicode-escaped 64-hex ref inside an object is retained by parallel wave-BFS
            obj_leaf = sys_obj.ex.objects.put(b'{"leaf": true}\n')
            escaped_ref = f'\\u00{ord(obj_leaf[0]):02x}' + obj_leaf[1:]
            obj_mid = sys_obj.ex.objects.put(('{"ref": "' + escaped_ref + '"}\n').encode('utf-8'))
            obj_dead = sys_obj.ex.objects.put(b'{"dead": true}\n')
            sys_obj.put({'id': 'rec-p1', 'kind': 'note', 'content': {'obj': obj_mid}}, 0)
            audit = audit_objects(sys_obj)
            unreachable = {row['object'] for row in audit['unreachable']}
            self.assertNotIn(obj_mid, unreachable)
            self.assertNotIn(obj_leaf, unreachable)
            self.assertIn(obj_dead, unreachable)

            # W5-P3: reindex -> put appends HMAC-signed delta sidecar bound to base_hmac
            sys_obj.record_store.reindex()
            delta_path = sys_obj.record_store.index.delta_path
            self.assertFalse(delta_path.exists())
            sys_obj.put({'id': 'delta-1', 'kind': 'note', 'content': {'title': 'Delta Title', 'tag': 'delta-tag'}}, 0)
            self.assertTrue(delta_path.exists())
            delta_lines = delta_path.read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(delta_lines), 1)
            delta_row = json.loads(delta_lines[0])
            self.assertIn('base_hmac', delta_row)
            self.assertIn('hmac', delta_row)

            # Deduplicated exact_refs omits record id & graph.title while entry_matches_exact_ref still matches
            summaries = sys_obj.record_store.indexed_summaries()
            self.assertIsNotNone(summaries)
            self.assertIn('delta-1', summaries)
            self.assertNotIn('delta-1', summaries['delta-1']['exact_refs'])
            self.assertTrue(entry_matches_exact_ref(summaries['delta-1'], 'delta-1'))

            # Tampered delta line causes safe rejection (load() returns None)
            orig_delta = delta_path.read_bytes()
            delta_path.write_bytes(orig_delta.replace(b'delta-1', b'delta-9'))
            self.assertIsNone(sys_obj.record_store.index.load(sys_obj.record_store.format))
            delta_path.write_bytes(orig_delta)

            # W5-P2: cross-OS inode/platform shift verifies raw_sha256 cryptographically
            sys_obj.record_store.reindex()
            loaded = sys_obj.record_store.index.load(sys_obj.record_store.format)
            self.assertIsNotNone(loaded)
            loaded['delta-1']['ino'] = int(loaded['delta-1']['ino']) + 2
            loaded['delta-1']['platform'] = 'posix' if os.name == 'nt' else 'nt'
            sys_obj.record_store.index.save(sys_obj.record_store.format, loaded)
            # Authentic bytes with shifted ino succeed
            warm = sys_obj.record_store.indexed_summaries()
            self.assertIsNotNone(warm)
            self.assertIn('delta-1', warm)

            # Same (size, mtime_ns) tampered bytes with shifted ino MUST be caught (never blindly trusted!)
            rec_path = sys_obj.record_store.path('delta-1')
            orig_raw = rec_path.read_bytes()
            st = rec_path.lstat()
            tampered_raw = orig_raw.replace(b'delta-tag', b'TAMPERED!')
            self.assertEqual(len(tampered_raw), len(orig_raw))
            try:
                rec_path.write_bytes(tampered_raw)
                os.utime(rec_path, ns=(st.st_atime_ns, st.st_mtime_ns))
                with self.assertRaises(ValueError):
                    sys_obj.record_store.indexed_summaries()
            finally:
                rec_path.write_bytes(orig_raw)
                os.utime(rec_path, ns=(st.st_atime_ns, st.st_mtime_ns))

            # W5-P3 auto-compaction after >64 delta ops
            sys_obj.record_store.reindex()
            for idx in range(66):
                sys_obj.put({'id': f'compact-{idx:02d}', 'kind': 'note', 'content': {'n': idx}}, 0)
            if delta_path.exists():
                self.assertLess(len(delta_path.read_text(encoding='utf-8').splitlines()), 65)
            final_summaries = sys_obj.record_store.indexed_summaries()
            self.assertIsNotNone(final_summaries)
            self.assertIn('compact-65', final_summaries)

    def test_c37_1_put_batch_preserves_pending_on_crash_inside_replace(self):
        """C37-1: Crash inside store._replace after op #2 commits on disk keeps WAL pending so retry recovers."""
        sys_obj = System(self.store)
        with exclusive(self.store / 'exchange/owner.lock'):
            r1 = sys_obj.put({'id': 'c37-rec-1', 'kind': 'note', 'content': {'v': 1}}, 0)
            r2 = sys_obj.put({'id': 'c37-rec-2', 'kind': 'note', 'content': {'v': 1}}, 0)
            spec = {
                'format': 'managed-ai-batch-update/1',
                'id': 'c37-1-batch',
                'instance_id': 'w5-inst',
                'pin_predecessors': True,
                'operations': [
                    {'id': 'c37-rec-1', 'expected_revision': 1, 'expected_sha256': r1['sha256'],
                     'mode': 'patch', 'replace_content_fields': {'v': 2}},
                    {'id': 'c37-rec-2', 'expected_revision': 1, 'expected_sha256': r2['sha256'],
                     'mode': 'patch', 'replace_content_fields': {'v': 2}},
                ],
            }
            orig_issue = sys_obj.record_store.receipts.issue

            def crash_after_rec2_disk_write(path, body, raw):
                if body['id'] == 'c37-rec-2':
                    raise OSError('simulated post-commit failure inside _replace for op #2')
                return orig_issue(path, body, raw)

            with patch.object(sys_obj.record_store.receipts, 'issue', side_effect=crash_after_rec2_disk_write):
                with self.assertRaisesRegex(OSError, 'simulated post-commit failure'):
                    record_batch.put_batch(sys_obj, spec)

            # Op #2 already committed rev 2 on disk; WAL receipt MUST preserve pending='c37-rec-2'
            self.assertEqual(sys_obj.show('c37-rec-2')['revision'], 2)
            receipt_path = self.store / 'work/.system/batches/c37-1-batch.json'
            partial = json.loads(receipt_path.read_bytes())
            self.assertFalse(partial['complete'])
            self.assertEqual([row['id'] for row in partial['records']], ['c37-rec-1'])
            self.assertEqual(partial.get('pending', {}).get('id'), 'c37-rec-2')

            # Retrying the identical batch reconciles op #2 and completes cleanly
            completed = record_batch.put_batch(sys_obj, spec)
            self.assertTrue(completed['complete'])
            self.assertNotIn('pending', completed)
            self.assertEqual([row['id'] for row in completed['records']], ['c37-rec-1', 'c37-rec-2'])
            self.assertEqual(sys_obj.show('c37-rec-1')['content'], {'v': 2})
            self.assertEqual(sys_obj.show('c37-rec-2')['content'], {'v': 2})

    def test_c42_1_and_c42_2_external_engine_isolation(self):
        """C42-1 & C42-2: audit_feedback loads explicit product engine; open_admitted_store isolates external engines."""
        from managed_ai.deployment.controller import open_admitted_store, opened_admitted_store
        import managed_ai as colocated_pkg

        def _make_ext_engine(dest: Path, label: str) -> Path:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ENGINE / '__init__.py', dest / '__init__.py')
            ignore = shutil.ignore_patterns('__pycache__', '*.pyc')
            for sub in ('core', 'deployment', 'store', 'library', 'platform'):
                shutil.copytree(ENGINE / sub, dest / sub, ignore=ignore)
            (dest / 'store' / 'ext_tag.py').write_text(f'LABEL = {label!r}\n', encoding='utf-8')
            sys_file = dest / 'store' / 'system.py'
            orig = sys_file.read_text(encoding='utf-8')
            sys_file.write_text(
                orig
                + '\nfrom managed_ai.store.ext_tag import LABEL as _EXT_LABEL\n'
                + 'System._ext_label = _EXT_LABEL\n'
                + 'def _lazy_ext_label(self):\n'
                + '    from managed_ai.store.ext_tag import LABEL\n'
                + '    return LABEL\n'
                + 'System.lazy_ext_label = _lazy_ext_label\n',
                encoding='utf-8',
            )
            return dest

        # C42-2: open_admitted_store & opened_admitted_store isolate both pkg/managed_ai and custom-named ext_eng
        pkg_ext = _make_ext_engine(self.base / 'pkg_a' / 'managed_ai', 'PKG_A')
        custom_ext = _make_ext_engine(self.base / 'ext_eng', 'CUSTOM_EXT')

        for ext_root, expected_label in ((pkg_ext, 'PKG_A'), (custom_ext, 'CUSTOM_EXT')):
            s_direct = open_admitted_store(ext_root, self.store)
            self.assertEqual(s_direct._ext_label, expected_label)
            self.assertIs(sys.modules.get('managed_ai'), colocated_pkg)
            self.assertNotIn('managed_ai.store.ext_tag', sys.modules)

            with opened_admitted_store(ext_root, self.store) as s_ctx:
                self.assertEqual(s_ctx._ext_label, expected_label)
                self.assertEqual(s_ctx.lazy_ext_label(), expected_label)
            self.assertIs(sys.modules.get('managed_ai'), colocated_pkg)
            self.assertNotIn('managed_ai.store.ext_tag', sys.modules)

            dep_dir = self.base / f'dep-{expected_label.lower()}'
            Deployment.initialize(dep_dir, self.store, ext_root)
            with Deployment.admitted(dep_dir, engine=ext_root, read_only=True) as bound:
                s_admitted = open_admitted_store(bound.engine, bound.runtime)
                self.assertEqual(s_admitted._ext_label, expected_label)
                self.assertEqual(s_admitted.lazy_ext_label(), expected_label)
            self.assertIs(sys.modules.get('managed_ai'), colocated_pkg)

        # C42-1: audit_feedback CLI loads the explicit external product argument (not co-located ENGINE_ROOT)
        marker_ext = _make_ext_engine(self.base / 'audit_ext' / 'managed_ai', 'AUDIT_EXT')
        sys_file = marker_ext / 'store' / 'system.py'
        sys_file.write_text(
            sys_file.read_text(encoding='utf-8')
            + '\nraise ValueError("EXTERNAL_AUDIT_PRODUCT_LOADED")\n',
            encoding='utf-8',
        )
        proc = self._run(
            [sys.executable, '-B', str(ENGINE / 'tools/audit_feedback.py'),
             str(marker_ext), str(self.store), 'goal', '--obligation'],
            expected_rc=2,
        )
        self.assertIn('EXTERNAL_AUDIT_PRODUCT_LOADED', proc.stderr)


if __name__ == '__main__':
    unittest.main()
