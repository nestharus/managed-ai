"""Wave 7 architecture, security, performance, and protocol regression tests."""
from __future__ import annotations

import ast
import hmac
import io
import json
import os
from pathlib import Path
import platform as stdlib_platform
import subprocess
import sys
import tempfile
import tokenize
import unittest
from unittest.mock import patch

if __package__:
    from .native_test_paths import ENGINE, SRC
else:
    from native_test_paths import ENGINE, SRC

from managed_ai.core.attention import Attention
from managed_ai.core.exchange.packet_context import PacketContext
from managed_ai.core.exchange.transport import collector
from managed_ai.core.ownership import external_root, packet_id, relative_path
from managed_ai.core.storage import encode, sha
from managed_ai.deployment.contract import DeploymentService
from managed_ai.deployment.controller import Deployment
from managed_ai.deployment import isolation as dep_isolation
from managed_ai.library.catalogue import Library
from managed_ai.library.contract import LibraryReader, SearchBackend, SourceProducerService
from managed_ai.library.format import digest as lib_digest, encoded as lib_encoded
from managed_ai.library.index import PreparedIndex, SearchIndex
from managed_ai.library.lexical import search_query
from managed_ai.library.producer import Producer
from managed_ai.store.contract import CatalogueReader, ObjectRepository, RecordRepository, StoreService
from managed_ai.store.records import cold_archive, compaction, head_index, head_receipts
from managed_ai.store.records.object_audit import audit_objects
from managed_ai.store.system import System
from managed_ai.core.exchange import open_contact
from managed_ai.store.governance import audit_feedback

OWNED_PACKAGES = ('platform', 'deployment', 'core', 'store', 'library')


def _owned_python_files(*, include_platform: bool = True) -> list[Path]:
    files: list[Path] = []
    for pkg in OWNED_PACKAGES:
        if not include_platform and pkg == 'platform':
            continue
        pkg_dir = ENGINE / pkg
        if not pkg_dir.is_dir():
            continue
        for py in sorted(pkg_dir.rglob('*.py')):
            files.append(py)
    return files


class Wave7ArchitectureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.store = self.base / 'store'
        System.initialize(self.store, {'contribution': 'Verify Wave 7 architecture.'}, instance_id='w7-inst')
        self.dep = self.base / 'dep'
        Deployment.initialize(self.dep, self.store, ENGINE)

    def _make_dir_link(self, link_path: Path, target_dir: Path) -> None:
        if os.name == 'nt':
            proc = subprocess.run(
                ['cmd', '/c', 'mklink', '/J', str(link_path), str(target_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.addCleanup(lambda p=link_path: subprocess.run(['cmd', '/c', 'rmdir', str(p)], capture_output=True))
        else:
            os.symlink(target_dir, link_path)

    def _make_library(self, name: str, bodies: dict[str, bytes]) -> Library:
        directory = self.base / name
        directory.mkdir()
        (directory / 'objects').mkdir()
        rows = []
        for ident, body in bodies.items():
            h = lib_digest(body)
            obj = 'objects/' + h + '.txt'
            (directory / obj).write_bytes(body)
            rows.append({
                'id': ident,
                'title': ident,
                'summary': 'Complete body',
                'object': obj,
                'sha256': h,
                'bytes': len(body),
                'source': {'path': ident, 'sha256': h},
                'link_base': ident,
                'references': [],
            })
        manifest = {
            'format': 'knowledge-library/1',
            'edition': name,
            'bodies': rows,
            'modules': [
                {
                    'id': 'base',
                    'title': 'Base',
                    'purpose': 'Base module',
                    'members': list(bodies.keys()),
                    'requires': [],
                    'parents': [],
                    'external_participation': ['Receiving host.'],
                },
            ],
            'provenance': {},
        }
        (directory / 'catalogue.json').write_bytes(lib_encoded(manifest))
        return Library(directory)

    def test_qa7_1_symlink_and_junction_ancestors_rejected(self) -> None:
        real_dir = self.base / 'real-workspace'
        real_dir.mkdir()
        linked_dir = self.base / 'linked-workspace'
        self._make_dir_link(linked_dir, real_dir)

        with self.assertRaises(ValueError):
            external_root(linked_dir, 'workspace')
        with self.assertRaises(ValueError):
            external_root(linked_dir / 'nested', 'workspace')
        with self.assertRaises(ValueError):
            PacketContext(linked_dir)

        # collector.py must reject linked exchange/workspace roots as well
        with self.assertRaises(ValueError):
            collector.Collector(linked_dir)

    def test_qa7_2_windows_reserved_names_and_trailing_dots_rejected(self) -> None:
        bad_ids = ['CON', 'con', 'NUL', 'AUX', 'PRN', 'COM1', 'com9', 'LPT1', 'lpt9', 'con.txt', 'foo.', 'bar ']
        for bad in bad_ids:
            with self.subTest(ident=bad):
                with self.assertRaises(ValueError):
                    packet_id(bad)

        bad_paths = ['CON', 'sub/NUL.json', 'dir/AUX/file.txt', 'a/COM1.log', 'dir/file.', 'dir/name ']
        for bad in bad_paths:
            with self.subTest(path=bad):
                with self.assertRaises(ValueError):
                    relative_path(bad)

        # Valid names that contain reserved substrings remain allowed
        self.assertEqual(packet_id('control-1'), 'control-1')
        self.assertEqual(relative_path('config/context.json'), 'config/context.json')

    def test_qa7_5_type_guards_raise_value_error(self) -> None:
        system = System(self.store)
        with self.assertRaises(ValueError):
            system.put(['not-a-dict'], 0)
        with self.assertRaises(ValueError):
            system.put('not-a-dict', 0)

        # Non-dict engine or store section in deployment.json must raise ValueError, not AttributeError
        dep_file = self.dep / 'deployment.json'
        original = json.loads(dep_file.read_text(encoding='utf-8'))

        bad_engine = dict(original, engine=['not', 'a', 'dict'])
        dep_file.write_text(json.dumps(bad_engine), encoding='utf-8')
        with self.assertRaises(ValueError):
            Deployment(self.dep)

        bad_store = dict(original, store='not-a-dict')
        dep_file.write_text(json.dumps(bad_store), encoding='utf-8')
        with self.assertRaises(ValueError):
            Deployment(self.dep)

        dep_file.write_text(json.dumps(original), encoding='utf-8')

    def test_qa7_6_exchange_cli_has_zero_runtime_warnings(self) -> None:
        env = dict(os.environ)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['PYTHONPATH'] = str(SRC)
        proc = subprocess.run(
            [sys.executable, '-B', '-W', 'error', '-m', 'managed_ai.core.exchange.cli', '--help'],
            capture_output=True,
            text=True,
            encoding='utf-8',
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f'stderr={proc.stderr}\nstdout={proc.stdout}')
        self.assertEqual(proc.stderr.strip(), '')

    def test_qa7_7_strict_json_rejects_duplicate_keys_and_non_finite_numbers(self) -> None:
        system = System(self.store)

        # 1) Attention._get rejects duplicate keys and NaN
        attention = Attention(system.ex)
        dup_key = system.ex.objects.put(b'{"a":1,"a":2}')
        nan_key = system.ex.objects.put(b'{"score":NaN}')
        with self.assertRaises(ValueError):
            attention._get(dup_key)
        with self.assertRaises(ValueError):
            attention._get(nan_key)

        # 2) HeadIndex.load_with_format rejects HMAC-signed duplicate-key / NaN payloads
        hi = system.record_store.index
        hi.directory.mkdir(parents=True, exist_ok=True)
        key = hi.receipts._key(create=True)
        self.assertIsNotNone(key)
        for bad_body in (b'{"a":1,"a":2}', b'{"v":NaN}'):
            mac = hmac.digest(key, head_index.INDEX_DOMAIN + bad_body, 'sha256').hex()
            signed = ('{"hmac":"' + mac + '",').encode('ascii') + bad_body[1:]
            hi.path.write_bytes(signed)
            self.assertIsNone(hi.load_with_format(system.record_store.format))

        # 3) HeadReceipts.projection rejects HMAC-signed duplicate-key payload
        hr = head_receipts.HeadReceipts(system.ex.root)
        hr._xattr_unsupported = False
        rkey = hr._key(create=True)
        self.assertIsNotNone(rkey)
        bad_payload = b'[{"format":"managed-ai-record/1","id":"goal","id":"goal"},"' + (b'0' * 64) + b'"]'
        tag = hmac.digest(rkey, head_receipts.DOMAIN + bad_payload, 'sha256')
        with patch.object(os, 'getxattr', create=True, return_value=tag + bad_payload):
            self.assertIsNone(hr.projection(system.record_store.path('goal'), 'goal', system.record_store.format))

        # 4) audit_objects rejects duplicate-key JSON record
        system.put({'id': 'audit-rec', 'kind': 'note', 'content': {'v': 1}}, 0)
        rec_path = system.record_store.path('audit-rec')
        hi.path.unlink(missing_ok=True)
        rec_path.write_bytes(b'{"id":"audit-rec","id":"audit-rec"}')
        with self.assertRaises(ValueError):
            audit_objects(system)

    def test_c45_1_isolation_preserves_stdlib_platform_module(self) -> None:
        self.assertNotIn('platform', dep_isolation._ISOLATED_ROOT_PACKAGES)
        self.assertNotIn('platform', audit_feedback._ISOLATED_ROOT_PACKAGES)

        before = sys.modules['platform']
        self.assertIs(before, stdlib_platform)
        with dep_isolation._isolated_engine(ENGINE):
            self.assertIs(sys.modules['platform'], before)
        self.assertIs(sys.modules['platform'], before)

    def test_v7_1_and_v7_2_controller_runpy_and_exports(self) -> None:
        import runpy

        ns = runpy.run_path(str(ENGINE / 'deployment' / 'controller.py'))
        for symbol in ('Deployment', 'AdmissionContended', 'admission_deadline', 'code_identity', 'shared', 'exclusive'):
            self.assertIn(symbol, ns, f'missing expected controller export: {symbol}')

    def test_p7_2_streaming_compaction_and_library_prepare_index(self) -> None:
        system = System(self.store)
        payload_a = ('compressible alpha block ' * 400).encode('utf-8')
        payload_b = ('compressible beta block ' * 500).encode('utf-8')
        key_a = system.ex.objects.put(payload_a)
        key_b = system.ex.objects.put(payload_b)

        plan = compaction.plan_compact_objects(system, min_savings_bytes=64)
        self.assertGreaterEqual(plan['candidate_count'], 2)
        receipt = compaction.execute_compact_objects(
            system,
            plan=plan,
            expected_plan=plan['plan_sha256'],
            reason='wave7 streaming check',
        )
        self.assertEqual(receipt['compacted_objects'], plan['candidate_count'])

        self.assertFalse((system.ex.objects.directory / key_a).exists())
        self.assertTrue((system.ex.objects.directory / (key_a + '.deflate')).is_file())
        self.assertEqual(system.ex.objects.read(key_a, retained=True), payload_a)
        self.assertEqual(system.ex.objects.read(key_b, retained=True), payload_b)

        after_plan = compaction.plan_compact_objects(system, min_savings_bytes=64)
        self.assertEqual(after_plan['candidate_count'], 0)

        # Library.prepare_index builds valid streaming-hashed FTS5 derivative
        lib = self._make_library('prep-lib', {'doc/one.md': b'Alpha streaming index test.\n'})
        idx_path = self.base / 'prepared.sqlite'
        prep = lib.prepare_index(idx_path)
        self.assertTrue(idx_path.is_file())
        with PreparedIndex(idx_path, prep['index']['sha256'], prep['basis']) as opened:
            hits = opened.search('body_docs', search_query('streaming', 'fts5'))
            self.assertIn('doc/one.md', hits)

    def test_p7_3_head_index_scatter_write_and_incremental_hmac(self) -> None:
        system = System(self.store)
        system.put({'id': 'rec-p73', 'kind': 'note', 'content': {'text': 'scatter write parity'}}, 0)
        system.reindex()

        hi = system.record_store.index
        loaded = hi.load_with_format(system.record_store.format)
        self.assertIsNotNone(loaded)
        entries, fmt = loaded
        self.assertIn('rec-p73', entries)
        self.assertEqual(fmt, head_index.INDEX_FORMAT)

        # Tampering 1 byte in the authenticated payload must fail HMAC verification
        raw = hi.path.read_bytes()
        tampered = raw[:-2] + (b' ' if raw[-2:-1] != b' ' else b'\t') + raw[-1:]
        hi.path.write_bytes(tampered)
        self.assertIsNone(hi.load_with_format(system.record_store.format))

    def test_p7_4_manage_store_help_avoids_heavy_imports(self) -> None:
        probe = "\n".join([
            "import json, sys",
            'sys.argv = ["manage_store.py", "--help"]',
            "from managed_ai.store import manage_store",
            "rc = 0",
            "try:",
            "    manage_store.main()",
            "except SystemExit as exc:",
            "    rc = exc.code",
            'print(json.dumps({"rc": rc, "system": "managed_ai.store.system" in sys.modules, '
            '"exchange": "managed_ai.core.exchange" in sys.modules, '
            '"futures": "concurrent.futures" in sys.modules}))',
        ])
        env = dict(os.environ)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['PYTHONPATH'] = str(SRC)
        proc = subprocess.run(
            [sys.executable, '-B', '-c', probe],
            capture_output=True,
            text=True,
            encoding='utf-8',
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f'stderr={proc.stderr}\nstdout={proc.stdout}')
        last_line = proc.stdout.strip().splitlines()[-1]
        observed = json.loads(last_line)
        self.assertEqual(observed['rc'], 0)
        self.assertFalse(observed['system'], 'manage_store --help must not import store.system')
        self.assertFalse(observed['exchange'], 'manage_store --help must not import core.exchange')
        self.assertFalse(observed['futures'], 'manage_store --help must not import concurrent.futures')

    def test_p7_9_audit_objects_streaming_parity_with_and_without_head_index(self) -> None:
        system = System(self.store)
        live_key = system.ex.objects.put(b'live referenced blob')
        orphan_key = system.ex.objects.put(b'unreferenced orphan blob')
        system.put(
            {'id': 'ref-rec', 'kind': 'note', 'content': {'blob_ref': live_key}},
            0,
        )
        system.reindex()

        with_index = audit_objects(system)
        unreach_with = {row['object'] for row in with_index['unreachable']}
        self.assertIn(orphan_key, unreach_with)
        self.assertNotIn(live_key, unreach_with)

        # Remove HeadIndex to force Layer-1 raw streaming scan and verify identical classification
        system.record_store.index.path.unlink(missing_ok=True)
        without_index = audit_objects(system)
        unreach_without = {row['object'] for row in without_index['unreachable']}
        self.assertEqual(unreach_without, unreach_with)
        self.assertEqual(without_index['reachable_count'], with_index['reachable_count'])

    def test_p7_10_discovery_lexical_short_circuits_on_body_byte_budget(self) -> None:
        system = System(self.store)
        for idx in range(80):
            system.put(
                {
                    'id': f'large-{idx:02d}',
                    'kind': 'note',
                    'content': {'text': f'searchable payload {idx} ' + ('x' * 2048)},
                },
                0,
            )

        res = system.discover({
            'query': 'searchable',
            'scopes': ['records'],
            'max_bytes': 1500,
        })
        coverage = res['coverage']
        self.assertIn('body-byte-budget', coverage['saturated'])
        self.assertGreater(coverage['unexamined_count'], 0)

    def test_c48_2_oversized_record_does_not_abort_smaller_records(self) -> None:
        system = System(self.store)
        system.put({'id': 'a-first', 'kind': 'note', 'content': {'text': 'small leading note'}}, 0)
        system.put(
            {'id': 'b-valid-1p1mb', 'kind': 'note', 'content': {'blob': 'z' * (1150 * 1024)}},
            0,
        )
        system.put({'id': 'c-needle', 'kind': 'note', 'content': {'text': 'find_me_default_budget'}}, 0)

        # Default 1 MiB budget: skip b-valid-1p1mb and still discover c-needle
        res_default = system.discover({
            'query': 'find_me_default_budget',
            'content': True,
            'scopes': ['records'],
        })
        self.assertIn('record:c-needle', [c['ref'] for c in res_default['candidates']])
        self.assertIn('body-byte-budget', res_default['coverage']['saturated'])

        # Tight explicit budget (4000 B): still finds c-needle after skipping oversized record
        res_small = system.discover({
            'query': 'find_me_default_budget',
            'content': True,
            'scopes': ['records'],
            'max_bytes': 4000,
        })
        self.assertIn('record:c-needle', [c['ref'] for c in res_small['candidates']])

    def test_p7_11_cold_archive_manifest_cache_and_invalidation(self) -> None:
        system = System(self.store)
        system.put({'id': 'cold-1', 'kind': 'note', 'active': False, 'content': {'text': 'archive me ' * 50}}, 0)
        plan = cold_archive.plan_archive_records(system, ids=['cold-1'])
        cold_archive.execute_archive_records(
            system,
            plan=plan,
            expected_plan=plan['plan_sha256'],
            reason='p7-11 cache test',
        )

        archive = system.record_store.cold_archive
        archive._manifest_cache = None

        with patch('managed_ai.store.records.cold_archive.read_json', wraps=cold_archive.read_json) as spy:
            self.assertTrue(archive.has('cold-1'))
            self.assertTrue(archive.has('cold-1'))
            self.assertFalse(archive.has('missing-id'))
            self.assertEqual(spy.call_count, 1)

            # Mutating via save_manifest must invalidate the stat-keyed cache
            entries = archive.entries()
            archive.save_manifest(system.instance_id, entries)
            self.assertTrue(archive.has('cold-1'))
            self.assertEqual(spy.call_count, 2)

    def test_protocol_conformance_across_domains(self) -> None:
        dep = Deployment(self.dep)
        system = System(self.store)
        lib = self._make_library('proto-lib', {'doc/a.md': b'Protocol conformance body.\n'})
        idx = SearchIndex(
            [{'id': 'doc/a.md', 'title': 'A', 'summary': 'S', 'text': 'Protocol conformance body.'}],
            'bm25',
        )

        self.assertIsInstance(dep, DeploymentService)
        self.assertIsInstance(system, StoreService)
        self.assertIsInstance(system.record_store, RecordRepository)
        self.assertIsInstance(system.ex.objects, ObjectRepository)
        self.assertIsInstance(system.techniques, CatalogueReader)
        self.assertIsInstance(lib, LibraryReader)
        self.assertIsInstance(idx, SearchBackend)

        with Producer(lib.directory) as prod:
            self.assertIsInstance(prod, SourceProducerService)

    def test_ast_and_hygiene_architecture_guards(self) -> None:
        os_violations: list[str] = []
        package_hack_violations: list[str] = []
        semicolon_violations: list[str] = []
        top_futures_violations: list[str] = []
        bare_json_loads_violations: list[str] = []
        library_sandbox_violations: list[str] = []
        crlf_violations: list[str] = []

        for py in _owned_python_files(include_platform=True):
            rel = py.relative_to(ENGINE).as_posix()
            raw = py.read_bytes()
            if b'\r\n' in raw:
                crlf_violations.append(rel)

            text = raw.decode('utf-8')
            tree = ast.parse(text, filename=str(py))

            # 1) Zero semicolons across owned production modules
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type == tokenize.OP and tok.string == ';':
                    semicolon_violations.append(f'{rel}:{tok.start[0]}')

            # 2) Zero top-level concurrent.futures imports
            for node in tree.body:
                if isinstance(node, ast.Import):
                    if any(a.name.startswith('concurrent.futures') for a in node.names):
                        top_futures_violations.append(f'{rel}:{node.lineno}')
                elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('concurrent.futures'):
                    top_futures_violations.append(f'{rel}:{node.lineno}')

            # 3) AST walks across owned code
            is_platform = rel.startswith('platform/')
            is_library_isolated = rel.startswith('library/') and rel != 'library/cli.py'
            is_strict_json_owner = rel == 'platform/_common.py'

            for node in ast.walk(tree):
                if not is_platform:
                    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                        if (node.value.id, node.attr) in {('os', 'name'), ('sys', 'platform'), ('os', 'uname')}:
                            os_violations.append(f'{rel}:{node.lineno}:{node.value.id}.{node.attr}')
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            if alias.name in {'fcntl', 'msvcrt'}:
                                os_violations.append(f'{rel}:{node.lineno}:import {alias.name}')
                    elif isinstance(node, ast.ImportFrom) and node.module in {'fcntl', 'msvcrt'}:
                        os_violations.append(f'{rel}:{node.lineno}:from {node.module}')
                    elif isinstance(node, ast.Name) and node.id == 'WinDLL':
                        os_violations.append(f'{rel}:{node.lineno}:WinDLL')

                if not is_strict_json_owner and isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                        if func.value.id == 'json' and func.attr == 'loads':
                            bare_json_loads_violations.append(f'{rel}:{node.lineno}')

                if isinstance(node, ast.If):
                    test = node.test
                    if (
                        isinstance(test, ast.UnaryOp)
                        and isinstance(test.op, ast.Not)
                        and isinstance(test.operand, ast.Name)
                        and test.operand.id == '__package__'
                    ):
                        package_hack_violations.append(f'{rel}:{node.lineno}')

                if is_library_isolated:
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            parts = alias.name.split('.')
                            root = parts[1] if parts[0] == 'managed_ai' and len(parts) > 1 else parts[0]
                            if root in {'core', 'store', 'deployment'}:
                                library_sandbox_violations.append(f'{rel}:{node.lineno}:import {alias.name}')
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        parts = node.module.split('.')
                        root = parts[1] if parts[0] == 'managed_ai' and len(parts) > 1 else parts[0]
                        if root in {'core', 'store', 'deployment'}:
                            library_sandbox_violations.append(f'{rel}:{node.lineno}:from {node.module}')

        self.assertEqual(os_violations, [], f'OS/platform branching outside platform/: {os_violations}')
        self.assertEqual(package_hack_violations, [], f'if not __package__ hacks: {package_hack_violations}')
        self.assertEqual(semicolon_violations, [], f'Semicolons found: {semicolon_violations}')
        self.assertEqual(top_futures_violations, [], f'Top-level concurrent.futures: {top_futures_violations}')
        self.assertEqual(bare_json_loads_violations, [], f'Bare json.loads calls: {bare_json_loads_violations}')
        self.assertEqual(library_sandbox_violations, [], f'Library sandbox leakage: {library_sandbox_violations}')
        self.assertEqual(crlf_violations, [], f'CRLF line endings found: {crlf_violations}')

    def test_w8_no_legacy_shims_orphaned_packages_or_grab_bags(self) -> None:
        retired_dirs = (
            'library/bundles',
            'library/reader',
            'library/search',
            'release',
            'core/cli',
            'tools',
        )
        still_present = [rel for rel in retired_dirs if (ENGINE / rel).exists()]
        self.assertEqual(still_present, [], f'Retired directories must be absent: {still_present}')

        # Zero orphaned code-less packages anywhere under src/managed_ai/
        codeless_packages: list[str] = []
        for init_py in sorted(ENGINE.rglob('__init__.py')):
            pkg_dir = init_py.parent
            if pkg_dir == ENGINE:
                continue
            has_other_py = any(p.name != '__init__.py' for p in pkg_dir.rglob('*.py'))
            if not has_other_py:
                tree = ast.parse(init_py.read_text(encoding='utf-8'), filename=str(init_py))
                non_doc_nodes = [
                    node for node in tree.body
                    if not (
                        isinstance(node, ast.Expr)
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)
                    )
                ]
                if not non_doc_nodes:
                    codeless_packages.append(pkg_dir.relative_to(ENGINE).as_posix())
        self.assertEqual(codeless_packages, [], f'Orphaned code-less packages found: {codeless_packages}')

        # open_contact uses a declarative command handler map (no if/elif dispatch chain in main)
        self.assertEqual(
            set(open_contact._COMMAND_HANDLERS),
            {'init', 'open', 'return', 'context', 'stages', 'status'},
        )

    def test_w8_no_single_child_meta_folders_or_vague_subbuckets(self) -> None:
        self.assertFalse((ENGINE / 'optional').exists(), 'Single-child optional/ meta-folder must be absent')
        self.assertFalse(
            (ENGINE / 'companions' / 'workbench' / 'controller').exists(),
            'companions/workbench/controller/ must be flattened into companions/workbench/',
        )
        self.assertFalse(
            (ENGINE / 'companions' / 'workbench' / 'core').exists(),
            'companions/workbench/core/ must be flattened into companions/workbench/',
        )

        # No top-level package under src/managed_ai/ may be a single-child meta-wrapper
        single_child_wrappers: list[str] = []
        for child in sorted(ENGINE.iterdir()):
            if not child.is_dir() or child.name.startswith('_') or not (child / '__init__.py').is_file():
                continue
            entries = [p for p in child.iterdir() if p.name not in {'__init__.py', '__pycache__'}]
            if len(entries) == 1 and entries[0].is_dir():
                single_child_wrappers.append(child.name)
        self.assertEqual(
            single_child_wrappers,
            [],
            f'Single-child meta-folder wrappers found: {single_child_wrappers}',
        )


if __name__ == '__main__':
    unittest.main()
