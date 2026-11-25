#!/usr/bin/env python3
"""Wave 3 regression tests: cross-platform locking/HeadIndex, Pillars 1-5, LB-1..5, LB-7, LB-9, LB-10, LB-13..15."""
if __package__:
    from .native_test_paths import APP, ENGINE, PRODUCT
else:
    from native_test_paths import APP, ENGINE, PRODUCT

import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from continuation_support.exchange.publication_receipts import publication_inventory
from continuation_support.storage import (
    OwnerBusy,
    encode,
    exclusive,
    read_json,
    sha,
    shared,
)
from controller import Deployment, DeploymentSelection, code_identity, read_descriptor_bytes
from native_runtime.cold_archive import plan_archive_records
from native_runtime.formats import (
    HEAD_INDEX_FORMAT,
    HEAD_INDEX_FORMAT_V1,
    PACKET_BINDING_FORMAT,
    TURN_EXPOSURE_FORMAT,
    TURN_PREPARATION_FORMAT,
    TURN_SELECTION_FORMAT,
    TURN_VIEW_FORMAT,
)
from native_runtime.selection.domain_selection import (
    DomainSelection,
    validate_payload,
)
from native_runtime.loading import Loading
from system import System


def _write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    return path


class Wave3RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='managed-ai-wave3-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / 'store'
        self.dep = self.root / 'deployment'
        System.initialize(
            self.store,
            {'contribution': 'Verify Wave 3 cross-platform, boundary, schema, and DAG guarantees.'},
            instance_id='wave3-store',
        )
        Deployment.initialize(self.dep, self.store, ENGINE)
        self.select_cli = [sys.executable, '-B', str(ENGINE / 'runtime/select_deployment.py'),
                           '--deployment', str(self.dep)]
        self.store_cli = [sys.executable, '-B', str(ENGINE / 'runtime/manage_store.py'),
                          '--root', str(self.store)]

    def run_cli(self, cmd, *, env=None, expected_rc=0):
        merged = os.environ.copy()
        if env:
            merged.update(env)
        proc = subprocess.run(cmd, capture_output=True, text=True, env=merged, timeout=60)
        self.assertEqual(proc.returncode, expected_rc, f'cmd={cmd}\nstdout={proc.stdout}\nstderr={proc.stderr}')
        return proc

    def test_pillar1_cross_platform_locks_and_head_index_parity(self):
        """Cross-platform shared/exclusive locks (LockFileEx on Windows, flock on POSIX) & HeadIndex v2."""
        lock = self.store / 'exchange/owner.lock'
        with shared(lock):
            with shared(lock):
                pass
            with self.assertRaises(OwnerBusy):
                with exclusive(lock):
                    self.fail('exclusive acquired while shared held')
        with exclusive(lock):
            with self.assertRaises(OwnerBusy):
                with shared(lock):
                    self.fail('shared acquired while exclusive held')
            proc = self.run_cli([*self.store_cli, 'status'],
                                env={'MANAGED_AI_DOMAIN_ADMISSION_SECONDS': '0.1'}, expected_rc=75)
            self.assertEqual(json.loads(proc.stderr)['status'], 'contended')

        # Build HeadIndex v2 and confirm indexed_summaries_with_format() works on BOTH Windows and POSIX
        with exclusive(lock):
            sys_obj = System(self.store)
            for i in range(6):
                sys_obj.put({'id': f'rec-{i}', 'kind': 'note', 'content': {'title': f'Record {i}'}}, 0)
            sys_obj.reindex()
            summaries, fmt = sys_obj.record_store.indexed_summaries_with_format()
            self.assertEqual(fmt, HEAD_INDEX_FORMAT)
            self.assertIsNotNone(summaries)
            self.assertEqual(len(summaries), 7)

    def test_lb1_lb2_lb3_domain_selection_strictness_and_dependency_source_ids(self):
        """LB-1 (nonfinite number), LB-2 (strict JSON), LB-3 (version & allowed keys), LB-10 (dependency_source_ids)."""
        # LB-1: number schema rejects NaN / Inf
        validate_payload(3.5, {'type': 'number'})
        validate_payload(4, {'type': 'number'})
        for bad in (float('nan'), float('inf'), float('-inf')):
            with self.assertRaisesRegex(ValueError, 'wrong payload type'):
                validate_payload(bad, {'type': 'number'})

        dom_dir = self.root / 'dom'
        dom_dir.mkdir()
        tool_src = dom_dir / 'tool.py'
        tool_src.write_text('# tool\n', encoding='utf-8')
        dep_src = dom_dir / 'helper.py'
        dep_src.write_text('# helper\n', encoding='utf-8')
        contract_src = dom_dir / 'CONTRACT.md'
        contract_src.write_text('# contract\n', encoding='utf-8')
        cat_bytes = encode({
            'format': 'managed-ai-source-catalogue/1',
            'sources': [
                {'id': 'dom/tool.py', 'path': 'tool.py', 'sha256': sha(tool_src.read_bytes()),
                 'bytes': len(tool_src.read_bytes()), 'title': 'Tool', 'exposure': 'available'},
                {'id': 'dom/helper.py', 'path': 'helper.py', 'sha256': sha(dep_src.read_bytes()),
                 'bytes': len(dep_src.read_bytes()), 'title': 'Helper', 'exposure': 'available'},
                {'id': 'dom/CONTRACT.md', 'path': 'CONTRACT.md', 'sha256': sha(contract_src.read_bytes()),
                 'bytes': len(contract_src.read_bytes()), 'title': 'Contract', 'exposure': 'available'},
            ],
        })
        (dom_dir / 'catalogue.json').write_bytes(cat_bytes)
        base_manifest = {
            'format': 'managed-ai-domain/1',
            'id': 'sample-dom',
            'version': '1',
            'engine_api': 'managed-ai-runtime/1',
            'source_root': '.',
            'catalogue': {'path': 'catalogue.json', 'sha256': sha(cat_bytes)},
            'schemas': {},
            'entry_sources': [],
            'protected_record_ids': ['custom-governed'],
            'content_addressed_basis_prefixes': ['custom/blobs/'],
            'tools': [{
                'id': 'my-tool',
                'source_id': 'dom/tool.py',
                'entry_source_id': 'dom/tool.py',
                'contract_source_id': 'dom/CONTRACT.md',
                'dependency_source_ids': ['dom/helper.py'],
            }],
        }
        dom_file = dom_dir / 'domain.json'
        dom_file.write_bytes(encode(base_manifest))
        store = self.root / 'dom-store'
        System.initialize(store, {'contribution': 'Verify LB-1/2/3/10.'}, instance_id='dom-store', domains=[dom_file])
        sel = System(store).domain_selection
        self.assertEqual(sel.protected_record_ids, frozenset({'custom-governed'}))
        self.assertEqual(sel.content_addressed_basis_prefixes, ('custom/blobs/',))
        self.assertEqual(sel.tools[0]['dependency_source_ids'], ['dom/helper.py'])

        # LB-3: Reject bogus domain version & unknown top-level key; allow valid semantic version string '2'
        v2_dom = dom_dir / 'v2-domain.json'
        v2_dom.write_bytes(encode(dict(base_manifest, version='2')))
        self.assertEqual(
            DomainSelection(store, {'domains': [{'path': str(v2_dom), 'sha256': sha(v2_dom.read_bytes())}]}).domains,
            ['sample-dom'],
        )
        for mutated, pattern in (
            (dict(base_manifest, version=99999), 'unsupported domain manifest version'),
            (dict(base_manifest, version=True), 'unsupported domain manifest version'),
            (dict(base_manifest, version=''), 'unsupported domain manifest version'),
            (dict(base_manifest, unexpected_key='boom'), 'unknown domain manifest fields'),
        ):
            bad_dom = self.root / 'bad-dom.json'
            bad_dom.write_bytes(encode(mutated))
            with self.assertRaisesRegex(ValueError, pattern):
                DomainSelection(store, {'domains': [{'path': str(bad_dom), 'sha256': sha(bad_dom.read_bytes())}]})

        # LB-2: Duplicate key / NaN in domain.json rejected
        dup_dom = self.root / 'dup-dom.json'
        dup_dom.write_text('{"id": "x", "id": "y"}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            DomainSelection(store, {'domains': [{'path': str(dup_dom), 'sha256': sha(dup_dom.read_bytes())}]})

        # LB-10: Unknown dependency_source_ids entry rejected during catalogue validation
        bad_dep_manifest = dict(base_manifest, tools=[dict(base_manifest['tools'][0],
                                                           dependency_source_ids=['dom/missing.py'])])
        bad_dep_dom = dom_dir / 'bad-dep-domain.json'
        bad_dep_dom.write_bytes(encode(bad_dep_manifest))
        bad_sel = DomainSelection(store, {'domains': [{'path': str(bad_dep_dom), 'sha256': sha(bad_dep_dom.read_bytes())}]})
        with self.assertRaisesRegex(ValueError, 'available selected sources'):
            bad_sel.validate_catalogue({e['id']: e for e in bad_sel.entries})

    def test_lb4_lb5_lb7_strict_json_and_allowed_keys(self):
        """LB-4 (store_configuration allowed keys), LB-5 (publication_receipts strict JSON), LB-7 (run --argv-json)."""
        # LB-4: Legacy configure path rejects unknown fields
        sys_obj = System(self.store)
        with exclusive(sys_obj.ex.root / 'owner.lock'):
            cur_sha = sha((self.store / 'system.json').read_bytes())
            with self.assertRaisesRegex(ValueError, 'unknown configure specification fields'):
                Loading(sys_obj).configure({'expected_sha256': cur_sha, 'account': 'test', 'rogue_key': 1})

        # LB-5: Corrupt / NaN / duplicate-key publication receipt flagged as unreadable
        pkt_dir = self.store / 'exchange/packets/lb5-pkt'
        pkt_dir.mkdir(parents=True, exist_ok=True)
        (pkt_dir / 'publish-foo.json').write_text('{"phase": "complete", "phase": "intent"}\n', encoding='utf-8')
        (pkt_dir / 'reconcile-foo.json').write_text('{"x": NaN}\n', encoding='utf-8')
        pubs, recs, unreadable = publication_inventory(pkt_dir)
        self.assertEqual(pubs, {})
        self.assertEqual(recs, {})
        self.assertEqual(len(unreadable), 2)

        # LB-7: `system run --argv-json` rejects NaN / duplicate keys and symlinks
        bad_argv = self.root / 'bad-argv.json'
        bad_argv.write_text('[NaN]\n', encoding='utf-8')
        proc = self.run_cli([*self.store_cli, 'run', 'turn-lb7', '--argv-json', str(bad_argv)], expected_rc=2)
        self.assertIn('nonfinite', proc.stderr)

    def test_lb9_code_identity_excludes_tests_and_prototype_retired(self):
        """LB-9: code_identity() ignores tests/examples/patterns/prototype and engine/prototype/ is retired."""
        self.assertFalse((ENGINE / 'prototype').exists(), 'physical engine/prototype/ must be retired')

        eng_copy = self.root / 'eng-copy'
        ignore = shutil.ignore_patterns('__pycache__')
        shutil.copytree(ENGINE / 'runtime', eng_copy / 'runtime', ignore=ignore)
        before = code_identity(eng_copy)

        # Adding files under tests/, examples/, patterns/, prototype/, or test_*.py MUST NOT change code_identity()!
        (eng_copy / 'runtime/tests/new_test_only.py').write_text('x = 1\n', encoding='utf-8')
        (eng_copy / 'examples').mkdir(exist_ok=True)
        (eng_copy / 'examples/demo.py').write_text('y = 2\n', encoding='utf-8')
        (eng_copy / 'patterns').mkdir(exist_ok=True)
        (eng_copy / 'patterns/pat.py').write_text('z = 3\n', encoding='utf-8')
        (eng_copy / 'prototype').mkdir(exist_ok=True)
        (eng_copy / 'prototype/legacy.py').write_text('w = 4\n', encoding='utf-8')
        (eng_copy / 'runtime/native/test_stray.py').write_text('t = 5\n', encoding='utf-8')
        self.assertEqual(code_identity(eng_copy), before)

        # Modifying an executable runtime module MUST change code_identity()!
        target = eng_copy / 'runtime/native/native_runtime/formats.py'
        target.write_text(target.read_text(encoding='utf-8') + '\n# changed\n', encoding='utf-8')
        self.assertNotEqual(code_identity(eng_copy), before)

    def test_lb13_lb14_lb15_controller_locking_goals_scan_and_extra_stores(self):
        """LB-13 (direct Deployment.system lock, explain-context 1x code_identity, command_prefix), LB-14, LB-15."""
        # LB-13: command_prefix emits canonical runtime/select_deployment.py
        from types import SimpleNamespace
        dep = Deployment(self.dep)
        with exclusive(self.store / 'exchange/owner.lock'):
            bound_sys = dep._open_selected_store({'System': System}, SimpleNamespace(action='status'))
        self.assertIn('select_deployment.py', ' '.join(bound_sys.command_prefix))
        self.assertNotIn('prototype', ' '.join(bound_sys.command_prefix))

        # LB-13: Direct Deployment.system() takes deployment.lock (fails AdmissionContended if exclusive held)
        from continuation_support.storage import AdmissionContended
        with exclusive(self.dep / 'deployment.lock'):
            with patch.dict(os.environ, {'MANAGED_AI_DOMAIN_ADMISSION_SECONDS': '0.05'}):
                with self.assertRaises(AdmissionContended):
                    dep.system(['status'])

        # LB-13: explain-context CLI invokes code_identity() at most ONCE
        with exclusive(self.store / 'exchange/owner.lock'):
            System(self.store).context('turn-exp', {'task': 'Test explain-context single code_identity.'})
        probe = (
            "import sys\n"
            "from unittest.mock import patch\n"
            "import controller\n"
            "calls = 0\n"
            "orig = controller.code_identity\n"
            "def counted(engine):\n"
            "    global calls\n"
            "    calls += 1\n"
            "    return orig(engine)\n"
            "with patch.object(controller, 'code_identity', side_effect=counted):\n"
            "    sys.argv = ['controller.py', *sys.argv[1:]]\n"
            "    controller.main()\n"
            "print(f'CALLS={calls}', file=sys.stderr)\n"
        )
        env = dict(os.environ, PYTHONPATH=str(ENGINE / 'runtime/deployment'))
        proc = self.run_cli([sys.executable, '-B', '-c', probe, '--deployment', str(self.dep),
                             'system', 'explain-context', 'turn-exp'], env=env)
        self.assertIn('CALLS=1', proc.stderr)

        # LB-14: goals.interpret() calls record_store.all() at most ONCE
        sys_obj = System(self.store)
        acct = self.root / 'acct.txt'
        acct.write_text('Single scan interpretation.\n', encoding='utf-8')
        orig_all = sys_obj.record_store.all
        all_calls = []

        def counted_all():
            all_calls.append(1)
            return orig_all()

        with exclusive(self.store / 'exchange/owner.lock'):
            with patch.object(sys_obj.record_store, 'all', side_effect=counted_all):
                sys_obj.interpret(1, 'continue', acct, ['records/goal.json'])
        self.assertEqual(len(all_calls), 1)

        # LB-15: Deployment.admitted(..., extra_stores=(other_store,)) locks both stores cleanly
        other_store = self.root / 'other-store'
        System.initialize(other_store, {'contribution': 'Second store.'}, instance_id='other-store')
        with exclusive(other_store / 'exchange/owner.lock'):
            with patch.dict(os.environ, {'MANAGED_AI_DOMAIN_ADMISSION_SECONDS': '0.05'}):
                with self.assertRaises(AdmissionContended):
                    with Deployment.admitted(self.dep, extra_stores=(other_store,), read_only=True):
                        self.fail('should contend on extra_store owner.lock')
        with Deployment.admitted(self.dep, extra_stores=(other_store,), read_only=False) as admitted:
            with self.assertRaises(OwnerBusy):
                with shared(self.store / 'exchange/owner.lock'):
                    pass
            with self.assertRaises(OwnerBusy):
                with shared(other_store / 'exchange/owner.lock'):
                    pass
            # deployment.lock stays SHARED during store writes so concurrent readers can still inspect status
            with shared(self.dep / 'deployment.lock'):
                pass
            self.assertEqual(admitted.runtime, self.store.resolve())

    def test_pillar3_domain_neutrality_cold_archive_and_dedup_prefix(self):
        """Pillar 3: zero hardcoded research strings in engine/runtime/; domain-configured protection & prefixes."""
        # Static lint across engine/runtime/{continuation,deployment,native}
        for sub in ('continuation', 'deployment', 'native'):
            for py_file in (ENGINE / 'runtime' / sub).rglob('*.py'):
                text = py_file.read_text(encoding='utf-8')
                self.assertNotIn('research-current', text, f'domain leak in {py_file}')
                self.assertNotIn('evidence/research-originals', text, f'domain leak in {py_file}')

        # Store WITHOUT domain config: 'research-current' is NOT protected by default, and dedup without --prefix fails
        sys_obj = System(self.store)
        with exclusive(self.store / 'exchange/owner.lock'):
            sys_obj.put({'id': 'research-current', 'kind': 'note', 'content': {'v': 1}, 'active': False}, 0)
            plan = plan_archive_records(sys_obj, ids=['research-current'])
        self.assertEqual([r['id'] for r in plan['eligible']], ['research-current'])
        proc = self.run_cli([*self.store_cli, 'deduplicate-originals', 'plan'], expected_rc=2)
        self.assertIn('content_addressed_basis_prefixes', proc.stderr)

    def test_pillar4_versioned_artifacts_legacy_compat_and_head_index_v1_fallback(self):
        """Pillar 4: explicit 'format' on newly written turn/exchange artifacts + v1 HeadIndex fallback."""
        with exclusive(self.store / 'exchange/owner.lock'):
            sys_obj = System(self.store)
            sys_obj.context('turn-fmt', {'task': 'Check explicit format versions.'})
            turn_dir = self.store / 'work/.system/turns/turn-fmt'
            self.assertEqual(read_json(turn_dir / 'preparation.json')['format'], TURN_PREPARATION_FORMAT)
            self.assertEqual(read_json(turn_dir / 'selection.json')['format'], TURN_SELECTION_FORMAT)
            self.assertEqual(read_json(turn_dir / 'exposure.json')['format'], TURN_EXPOSURE_FORMAT)
            self.assertEqual(read_json(turn_dir / 'view.json')['format'], TURN_VIEW_FORMAT)
            self.assertEqual(read_json(self.store / 'exchange/packets/turn-fmt/binding.json')['format'],
                             PACKET_BINDING_FORMAT)

            # HeadIndex v2 written by reindex, AND explicit v1 HeadIndex loads with HEAD_INDEX_FORMAT_V1
            sys_obj.reindex()
            idx_path = sys_obj.record_store.index.path
            parsed = json.loads(idx_path.read_bytes())
            self.assertEqual(parsed['format'], HEAD_INDEX_FORMAT)
            import hmac as hmac_mod
            from native_runtime.head_index import INDEX_DOMAIN
            body_v1 = {k: v for k, v in parsed.items() if k != 'hmac'}
            body_v1['format'] = HEAD_INDEX_FORMAT_V1
            payload = json.dumps(body_v1, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            key = sys_obj.record_store.receipts._key(create=False)
            tag = hmac_mod.digest(key, INDEX_DOMAIN + payload, 'sha256').hex()
            idx_path.write_bytes(b'{"hmac":"' + tag.encode('ascii') + b'",' + payload[1:])
            summaries, fmt = sys_obj.record_store.indexed_summaries_with_format()
            self.assertEqual(fmt, HEAD_INDEX_FORMAT_V1)
            self.assertIsNotNone(summaries)

    def test_pillar5_ast_import_dag_no_inverted_native_imports(self):
        """Pillar 5: AST boundary check — ZERO modules under native_runtime/ import moved top-level modules."""
        moved = {
            'composition', 'composition_boundary', 'composition_change_notice',
            'composition_opportunities', 'composition_revision',
            'context_discovery', 'feedback', 'information',
        }
        native_dir = ENGINE / 'runtime/native'
        for name in moved:
            self.assertFalse((native_dir / f'{name}.py').exists(), f'legacy top-level {name}.py should be moved')
            self.assertTrue((native_dir / 'native_runtime' / f'{name}.py').is_file(),
                            f'native_runtime/{name}.py missing')
        violations = []
        for py_file in (native_dir / 'native_runtime').rglob('*.py'):
            tree = ast.parse(py_file.read_text(encoding='utf-8'), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split('.', 1)[0]
                        if top in moved:
                            violations.append(f'{py_file.name}: import {alias.name}')
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    top = node.module.split('.', 1)[0]
                    if top in moved:
                        violations.append(f'{py_file.name}: from {node.module}')
        self.assertEqual(violations, [], f'Inverted native_runtime imports found: {violations}')


    def test_c19_o2_catalogue_rejects_unknown_top_level_fields(self):
        """C19-O2: Unknown top-level key in catalogue.json rejected by DomainSelection.add_catalogue()."""
        dom_dir = self.root / 'dom-cat-o2'
        dom_dir.mkdir()
        bad_cat = dom_dir / 'bad-catalogue.json'
        bad_cat.write_bytes(encode({
            'format': 'managed-ai-source-catalogue/1',
            'sources': [],
            'rogue_catalogue_key': True,
        }))
        bad_cat_dom = dom_dir / 'bad-cat-domain.json'
        bad_cat_dom.write_bytes(encode({
            'format': 'managed-ai-domain/1',
            'id': 'cat-o2-dom',
            'version': '1',
            'engine_api': 'managed-ai-runtime/1',
            'source_root': '.',
            'catalogue': {'path': 'bad-catalogue.json', 'sha256': sha(bad_cat.read_bytes())},
            'schemas': {},
            'entry_sources': [],
            'tools': [],
        }))
        with self.assertRaisesRegex(ValueError, 'unknown reference source catalogue fields'):
            DomainSelection(self.store, {'domains': [{'path': str(bad_cat_dom), 'sha256': sha(bad_cat_dom.read_bytes())}]})

    def test_c19_1_tampered_external_command_parser_never_executes(self):
        """C19-1: External engine code must NEVER execute before code_identity(engine) verification."""
        ext_engine = self.root / 'ext-engine'
        shutil.copytree(ENGINE / 'runtime', ext_engine / 'runtime', ignore=shutil.ignore_patterns('tests', '__pycache__'))
        ext_dep = self.root / 'ext-dep'
        Deployment.initialize(ext_dep, self.store, ext_engine)
        marker = self.root / 'EXECUTED_UNVERIFIED.txt'
        parser_path = ext_engine / 'runtime/native/native_runtime/command_parser.py'
        parser_path.write_text(
            parser_path.read_text(encoding='utf-8')
            + f'\nfrom pathlib import Path as _P\n_P({str(marker)!r}).write_text("pwned", encoding="utf-8")\n',
            encoding='utf-8',
        )
        proc = self.run_cli([
            sys.executable, '-B', str(ENGINE / 'runtime/select_deployment.py'),
            '--deployment', str(ext_dep), 'system', 'status',
        ], expected_rc=2)
        self.assertIn('shared engine code changed', proc.stderr)
        self.assertFalse(marker.exists(), 'unverified external command_parser.py executed before code_identity!')

    def test_c19_2_require_domain_enforces_pin_and_rejects_symlink_or_junction(self):
        """C19-2: --require-domain verifies exact binding sha256 and rejects symlink/junction ancestors."""
        dom_dir = self.root / 'dom-other'
        dom_dir.mkdir()
        cat_bytes = encode({'format': 'managed-ai-source-catalogue/1', 'sources': []})
        (dom_dir / 'catalogue.json').write_bytes(cat_bytes)
        dom_path = dom_dir / 'domain.json'
        orig_manifest = {
            'format': 'managed-ai-domain/1', 'id': 'other', 'version': '1',
            'engine_api': 'managed-ai-runtime/1', 'source_root': '.',
            'catalogue': {'path': 'catalogue.json', 'sha256': sha(cat_bytes)},
            'schemas': {}, 'entry_sources': [], 'tools': [],
        }
        dom_path.write_bytes(encode(orig_manifest))
        store = self.root / 'c19-2-store'
        dep = self.root / 'c19-2-dep'
        System.initialize(store, {'contribution': 'Verify C19-2.'}, instance_id='c19-2-inst', domains=[dom_path])
        Deployment.initialize(dep, store, ENGINE)
        cli = [sys.executable, '-B', str(ENGINE / 'runtime/select_deployment.py'),
               '--deployment', str(dep), '--require-domain', 'research', 'status']

        # Baseline: 'other' domain refused when 'research' required
        proc0 = self.run_cli(cli, expected_rc=2)
        self.assertIn('requires a research-domain deployment', proc0.stderr)

        # Attack (a): tamper domain.json id to 'research' without updating system.json sha256 pin
        forged_manifest = dict(orig_manifest, id='research')
        dom_path.write_bytes(encode(forged_manifest))
        proc_a = self.run_cli(cli, expected_rc=2)
        self.assertIn('selected domain manifest changed', proc_a.stderr)

        # Attack (b): replace dom-other/ with a symlink (POSIX) or NTFS junction (Windows) to forged dir
        forged_dir = self.root / 'dom-forged'
        forged_dir.mkdir()
        (forged_dir / 'catalogue.json').write_bytes(cat_bytes)
        (forged_dir / 'domain.json').write_bytes(encode(forged_manifest))
        shutil.rmtree(dom_dir)
        if os.name == 'nt':
            mk = subprocess.run(['cmd', '/c', 'mklink', '/J', str(dom_dir), str(forged_dir)],
                                capture_output=True, text=True)
            self.assertEqual(mk.returncode, 0, mk.stderr)
        else:
            dom_dir.symlink_to(forged_dir, target_is_directory=True)
        proc_b = self.run_cli(cli, expected_rc=2)
        self.assertIn('ordinary selected domain required', proc_b.stderr)

    def test_c21_o1_external_engine_precheck_suppresses_spurious_usage(self):
        """C21-O1: External engine precheck does not leak bogus argparse usage/help onto stdout or stderr."""
        ext_engine = self.root / 'ext-engine-c21'
        shutil.copytree(ENGINE / 'runtime', ext_engine / 'runtime', ignore=shutil.ignore_patterns('tests', '__pycache__'))
        ext_dep = self.root / 'ext-dep-c21'
        Deployment.initialize(ext_dep, self.store, ext_engine)
        # system status --help against a verified external engine must emit ONLY ONE usage block
        proc = self.run_cli([
            sys.executable, '-B', str(ENGINE / 'runtime/select_deployment.py'),
            '--deployment', str(ext_dep), 'system', 'status', '--help',
        ], expected_rc=0)
        self.assertEqual(proc.stdout.count('usage:'), 1, proc.stdout)
        self.assertEqual(proc.stderr, '')

    def test_posix_relative_paths_serialized_in_descriptors(self):
        """Wave 3.5: relative paths written to deployment.json, system.json, and host.json always use POSIX '/'."""
        from continuation_support._platform import safe_relpath

        base = self.root / 'posix-check'
        eng_copy = base / 'trunk/engine'
        shutil.copytree(ENGINE / 'runtime', eng_copy / 'runtime', ignore=shutil.ignore_patterns('tests', '__pycache__'))
        dom_dir = base / 'trunk/domains/sample'
        dom_dir.mkdir(parents=True)
        cat_bytes = encode({'format': 'managed-ai-source-catalogue/1', 'sources': []})
        (dom_dir / 'catalogue.json').write_bytes(cat_bytes)
        dom_path = dom_dir / 'domain.json'
        dom_path.write_bytes(encode({
            'format': 'managed-ai-domain/1', 'id': 'sample', 'version': '1',
            'engine_api': 'managed-ai-runtime/1', 'source_root': '.',
            'catalogue': {'path': 'catalogue.json', 'sha256': sha(cat_bytes)},
            'schemas': {}, 'entry_sources': [], 'tools': [],
        }))
        extra_cat = base / 'workspaces/sample/stores/extra/sources.json'
        extra_cat.parent.mkdir(parents=True)
        extra_cat.write_bytes(cat_bytes)
        dep_dir = base / 'workspaces/sample'
        store_dir = dep_dir / 'stores/main'
        System.initialize(store_dir, {'contribution': 'Verify POSIX relative paths.'},
                          instance_id='posix-relpath-inst', domains=[dom_path], catalogues=[extra_cat])
        sys_doc = json.loads((store_dir / 'system.json').read_text(encoding='utf-8'))
        for binding in (*sys_doc['domains'], *sys_doc['catalogues']):
            self.assertNotIn('\\', binding['path'], f'backslash in system.json: {binding}')
            self.assertIn('/', binding['path'])

        Deployment.initialize(dep_dir, store_dir, eng_copy)
        dep_doc = json.loads((dep_dir / 'deployment.json').read_text(encoding='utf-8'))
        self.assertEqual(dep_doc['engine']['path'], '../../trunk/engine')
        self.assertEqual(dep_doc['store']['path'], 'stores/main')

        # Stage also normalizes any pre-existing Windows backslash store path and writes POSIX engine path.
        dep_doc['store']['path'] = 'stores\\main'
        (dep_dir / 'deployment.json').write_bytes(encode(dep_doc))
        prior_sha = sha((dep_dir / 'deployment.json').read_bytes())
        sel = DeploymentSelection(dep_dir)
        staged = sel.stage('posix-ed', eng_copy)
        cand = json.loads((dep_dir / 'candidate.json').read_text(encoding='utf-8'))
        self.assertEqual(cand['engine']['path'], '../../trunk/engine')
        self.assertEqual(cand['store']['path'], 'stores/main')
        sel.activate('posix-ed', prior_sha, staged['candidate_sha256'], 'Normalize POSIX paths.')
        act_doc = json.loads((dep_dir / 'deployment.json').read_text(encoding='utf-8'))
        self.assertEqual(act_doc['engine']['path'], '../../trunk/engine')
        self.assertEqual(act_doc['store']['path'], 'stores/main')
        self.assertEqual(safe_relpath(eng_copy, dep_dir), '../../trunk/engine')

        # Host continuing_work config also serializes POSIX relative paths for engine, native_root, and object pins.
        cw_dir = str(ENGINE / 'host/continuing_work')
        if cw_dir not in sys.path:
            sys.path.insert(0, cw_dir)
        import continuing_work
        self.assertEqual(continuing_work._safe_relpath(eng_copy / 'runtime/manage_store.py', dep_dir / 'host'),
                         '../../../trunk/engine/runtime/manage_store.py')
        host_root = dep_dir / 'host'
        comm = base / 'commission.md'
        comm.write_text('Keep host paths portable.\n', encoding='utf-8')
        h = continuing_work.Host(host_root)
        h.root.mkdir(parents=True)
        pinned = h.pin(comm)
        self.assertNotIn('\\', pinned['object'])
        self.assertTrue(pinned['object'].startswith('objects/'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
