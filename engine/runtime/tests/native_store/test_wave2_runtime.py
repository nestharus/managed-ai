#!/usr/bin/env python3
"""Wave 2 regression tests: G2S-04 (dedup + DEFLATE), G2S-06 (cold archive + 9-layer audit/GC), G2S-07 (graph CLI + discovery), L3, L4, L5(2), A5."""
if __package__:
    from .native_test_paths import APP, ENGINE, PRODUCT
else:
    from native_test_paths import APP, ENGINE, PRODUCT
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zlib

from composition import Composition
from context_discovery import Discovery
from continuation_support.storage import (
    MAX_BYTES,
    ObjectStore,
    encode,
    exclusive,
    read_json,
    sha,
    shared,
)
from controller import Deployment
from native_runtime.record_batch import put_batch
from native_runtime.record_import import import_records, read_receipt
from native_runtime.selection.domain_selection import DomainSelection, validate_payload, validate_schema
from system import System


def _write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    return path


class Wave2RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='managed-ai-wave2-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / 'store'
        self.dep = self.root / 'deployment'

        # Create a domain declaring relation_vocabulary and one sealed source for L3 + G2S-06 + G2S-07 tests
        dom_dir = self.root / 'domain'
        dom_dir.mkdir()
        avail_doc = dom_dir / 'available.md'
        avail_doc.write_text('# Available Guide\nsearchable alpha token\n', encoding='utf-8')
        sealed_doc = dom_dir / 'sealed-d12.md'
        sealed_doc.write_text('TOP SECRET SEALED BODY MUST NEVER BE OPENED\n', encoding='utf-8')
        self.sealed_path = sealed_doc
        cat_bytes = encode({
            'format': 'managed-ai-source-catalogue/1',
            'sources': [
                {
                    'id': 'w2/available',
                    'path': 'available.md',
                    'sha256': sha(avail_doc.read_bytes()),
                    'bytes': len(avail_doc.read_bytes()),
                    'title': 'Available Guide',
                    'exposure': 'available',
                },
                {
                    'id': 'w2/sealed-d12',
                    'path': 'sealed-d12.md',
                    'sha256': sha(sealed_doc.read_bytes()),
                    'bytes': len(sealed_doc.read_bytes()),
                    'title': 'Sealed D12 Fixture',
                    'exposure': 'sealed',
                },
            ],
        })
        (dom_dir / 'catalogue.json').write_bytes(cat_bytes)
        dom_manifest = dom_dir / 'domain.json'
        dom_manifest.write_bytes(encode({
            'format': 'managed-ai-domain/1',
            'id': 'wave2-domain',
            'version': '1',
            'engine_api': 'managed-ai-runtime/1',
            'source_root': '.',
            'catalogue': {'path': 'catalogue.json', 'sha256': sha(cat_bytes)},
            'schemas': {},
            'entry_sources': [],
            'tools': [],
            'relation_vocabulary': {
                'predicates': ['supports', 'contradicts', 'refines', 'answers'],
                'provenance_self_loop_relations': ['revises native navigation from', 'custom-self-pin'],
            },
        }))

        System.initialize(
            self.store,
            {'contribution': 'Verify Wave 2 storage compaction, cold archive, transitive GC, graph CLI, and fixes.'},
            instance_id='wave2-store',
            domains=[dom_manifest],
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

    def test_a5_storage_strict_json_and_nonfinite_rejection(self):
        with self.assertRaises(ValueError):
            encode({'bad': float('nan')})
        with self.assertRaises(ValueError):
            encode({'bad': float('inf')})

        nan_file = self.root / 'nan.json'
        nan_file.write_text('{"x": NaN}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'nonfinite'):
            read_json(nan_file)

        dup_file = self.root / 'dup.json'
        dup_file.write_text('{"a": 1, "a": 2}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            read_json(dup_file)

        real_file = self.root / 'real.json'
        real_file.write_text('{"ok": true}\n', encoding='utf-8')
        link_file = self.root / 'link.json'
        link_file.symlink_to(real_file)
        with self.assertRaisesRegex(ValueError, 'ordinary JSON file'):
            read_json(link_file)

    def test_l4_schema_keyword_type_scoping_and_relation_vocabulary(self):
        # minLength/maxLength apply ONLY to strings, minItems/maxItems ONLY to lists
        mixed_schema = {'type': ['string', 'array'], 'minLength': 3, 'maxLength': 5, 'minItems': 2, 'maxItems': 4}
        validate_schema(mixed_schema)
        validate_payload('abcd', mixed_schema)  # len 4 (passes minLength/maxLength; ignores minItems/maxItems)
        validate_payload('a' * 5, mixed_schema)
        validate_payload([1, 2], mixed_schema)  # 2 items < minLength 3 MUST PASS because minLength is str-only!
        validate_payload(['x'], {'type': ['string', 'array'], 'minLength': 10})
        validate_payload('x', {'type': ['string', 'array'], 'minItems': 10})
        with self.assertRaisesRegex(ValueError, 'minLength violated'):
            validate_payload('ab', mixed_schema)
        with self.assertRaisesRegex(ValueError, 'minItems violated'):
            validate_payload([1], mixed_schema)

        # validate_schema rejects malformed schema parameter types
        for bad in (
            {'minLength': -1},
            {'minLength': True},
            {'minLength': 5, 'maxLength': 2},
            {'minItems': '2'},
            {'minItems': 4, 'maxItems': 1},
            {'minimum': float('nan')},
            {'minimum': 10, 'maximum': 5},
            {'pattern': '[unclosed'},
            {'required': 'not-a-list'},
            {'properties': []},
            {'anyOf': []},
            {'enum': []},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_schema(bad)

        # DomainSelection exposes validated relation_vocabulary
        system = System(self.store)
        self.assertEqual(system.domain_selection.relation_predicates,
                         ['answers', 'contradicts', 'refines', 'supports'])
        self.assertEqual(system.domain_selection.provenance_self_loop_relations,
                         ['custom-self-pin', 'revises native navigation from'])

    def test_l5_2_fresh_import_refuses_preexisting_revision_ge_2_head(self):
        system = System(self.store)
        row = {'id': 'evolved-rec', 'kind': 'note', 'content': {'v': 1}}
        with exclusive(system.ex.root / 'owner.lock'):
            system.put(row, 0)
            system.put({'id': 'evolved-rec', 'kind': 'note', 'content': {'v': 2}}, 1)
            # Even if a fresh import supplies exact matching rev-2 content, rev >= 2 MUST be refused
            spec = {
                'format': 'managed-ai-import/1',
                'id': 'fresh-coopt-attempt',
                'records': [{'id': 'evolved-rec', 'kind': 'note', 'content': {'v': 2}}],
            }
            with self.assertRaisesRegex(ValueError, 'pre-existing head at revision 2'):
                import_records(system, spec)
            self.assertIsNone(read_receipt(system, spec))

    def test_g2s04_objectstore_deflate_security_compaction_and_originals_dedup(self):
        system = System(self.store)
        obj_dir = self.root / 'standalone-objects'
        obj_dir.mkdir()
        os_store = ObjectStore(obj_dir)

        # Compressible payload writes .deflate; incompressible random payload writes raw
        compressible = (b'context geometry repeated block\n' * 300)
        k_comp = os_store.put(compressible, compress=True)
        self.assertTrue((obj_dir / (k_comp + '.deflate')).is_file())
        self.assertFalse((obj_dir / k_comp).exists())
        self.assertTrue(os_store.has(k_comp))
        self.assertEqual(os_store.read(k_comp, expected_bytes=len(compressible)), compressible)

        tiny = b'abc'
        k_tiny = os_store.put(tiny, compress=True)
        self.assertTrue((obj_dir / k_tiny).is_file())
        self.assertFalse((obj_dir / (k_tiny + '.deflate')).exists())

        # Symlink .deflate refused
        fake_key = 'a' * 64
        (obj_dir / (fake_key + '.deflate')).symlink_to(obj_dir / (k_comp + '.deflate'))
        with self.assertRaisesRegex(ValueError, 'ordinary file required'):
            os_store.read(fake_key)
        (obj_dir / (fake_key + '.deflate')).unlink()

        # Trailing garbage after valid zlib stream refused
        (obj_dir / (k_comp + '.deflate')).write_bytes(zlib.compress(compressible) + b'EXTRA')
        with self.assertRaisesRegex(ValueError, 'trailing data'):
            os_store.read(k_comp)
        (obj_dir / (k_comp + '.deflate')).write_bytes(zlib.compress(compressible))

        # Zip-bomb defense: compressed payload expanding past MAX_BYTES refused on non-retained read
        bomb_raw = b'Z' * (MAX_BYTES + 65536)
        bomb_key = sha(bomb_raw)
        (obj_dir / (bomb_key + '.deflate')).write_bytes(zlib.compress(bomb_raw, level=9))
        with self.assertRaisesRegex(ValueError, 'file exceeds'):
            os_store.read(bomb_key, retained=False)
        # Retained read still recovers > MAX_BYTES historical blobs safely
        self.assertEqual(len(os_store.read(bomb_key, retained=True, expected_bytes=len(bomb_raw))), len(bomb_raw))

        # Full end-to-end store test: originals dedup + object compaction + status/compose/put parity
        payload = (b'{"evidence": "' + b'E' * 4000 + b'"}\n')
        p_sha = sha(payload)
        orig_rel = f'evidence/research-originals/{p_sha[:2]}/{p_sha}.json'
        orig_file = system.work / orig_rel
        orig_file.parent.mkdir(parents=True, exist_ok=True)
        orig_file.write_bytes(payload)

        # Also add a non-content-addressed file under the prefix (must be skipped!)
        stray = system.work / 'evidence/research-originals/notes.txt'
        stray.write_text('not content addressed', encoding='utf-8')

        comp_content = {
            'format': 'g60-composition/1',
            'purpose': 'Verify object-backed basis in Composition._basis()',
            'elements': [{'id': 'e1', 'account': 'element one'}],
            'premises': [{'id': 'p1', 'account': 'backed by original', 'disposition': 'available',
                          'basis': [orig_rel]}],
        }
        with exclusive(system.ex.root / 'owner.lock'):
            system.put({'id': 'comp-dedup', 'kind': 'note', 'content': comp_content, 'basis': [orig_rel]}, 0)

        # Plan dedup under LOCK_SH!
        with shared(self.store / 'exchange/owner.lock'):
            dedup_plan = json.loads(self.run_cli([*self.store_cli, 'deduplicate-originals', 'plan']).stdout)
        self.assertEqual(dedup_plan['reclaimable_files'], 1)
        self.assertEqual(dedup_plan['candidates'][0]['path'], orig_rel)
        self.assertEqual(len(dedup_plan['skipped']), 1)

        dedup_plan_file = _write_json(self.root / 'dedup-plan.json', dedup_plan)
        # Wrong expected-plan refused
        self.run_cli([*self.store_cli, 'deduplicate-originals', 'execute',
                      '--plan', str(dedup_plan_file), '--expected-plan', '0' * 64,
                      '--reason', 'bad sha'], expected_rc=2)
        self.assertTrue(orig_file.is_file())

        # Execute dedup
        dedup_rcpt = json.loads(self.run_cli([
            *self.store_cli, 'deduplicate-originals', 'execute',
            '--plan', str(dedup_plan_file), '--expected-plan', dedup_plan['plan_sha256'],
            '--reason', 'Remove verified duplicate content-addressed workspace copy.',
        ]).stdout)
        self.assertEqual(dedup_rcpt['removed_files'], 1)
        self.assertFalse(orig_file.exists())
        self.assertTrue(stray.is_file())

        # Now plan + execute compact-objects (under LOCK_SH for plan)
        with shared(self.store / 'exchange/owner.lock'):
            compact_plan = json.loads(self.run_cli([*self.store_cli, 'compact-objects', 'plan']).stdout)
        self.assertGreaterEqual(compact_plan['candidate_count'], 1)
        self.assertIn(p_sha, [c['object'] for c in compact_plan['candidates']])
        compact_plan_file = _write_json(self.root / 'compact-plan.json', compact_plan)
        compact_rcpt = json.loads(self.run_cli([
            *self.store_cli, 'compact-objects', 'execute',
            '--plan', str(compact_plan_file), '--expected-plan', compact_plan['plan_sha256'],
            '--reason', 'Compact exchange objects with verified DEFLATE.',
        ]).stdout)
        self.assertGreaterEqual(compact_rcpt['compacted_objects'], 1)
        self.assertTrue((self.store / 'exchange/objects' / (p_sha + '.deflate')).is_file())
        self.assertFalse((self.store / 'exchange/objects' / p_sha).exists())

        # After BOTH dedup and DEFLATE compaction: status, compose, and new object:<sha> put all pass!
        st = json.loads(self.run_cli([*self.store_cli, 'status', '--verify-all-basis']).stdout)
        self.assertEqual(st['gaps']['changed_record_basis'], [])
        comp_view = Composition(System(self.store)).inspect('comp-dedup')
        self.assertEqual(comp_view['basis'][0]['status'], 'current')
        self.assertEqual(comp_view['basis'][0]['custody'], 'exchange-object')

        # Explicit object:<sha> basis reference works cleanly
        obj_rec = _write_json(self.root / 'obj-basis-rec.json', {
            'id': 'obj-ref-rec', 'kind': 'note', 'content': {'ok': True}, 'basis': [f'object:{p_sha}']})
        self.run_cli([*self.store_cli, 'put', '--record', str(obj_rec), '--expected', '0', '--no-status'])
        st2 = json.loads(self.run_cli([*self.store_cli, 'status', '--verify-all-basis']).stdout)
        self.assertEqual(st2['gaps']['changed_record_basis'], [])

    def test_g2s06_cold_archive_safety_guards_and_transparent_readback(self):
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            system.put({'id': 'duty-open', 'kind': 'obligation',
                        'content': {'text': 'keep owed', 'disposition': 'open'}, 'active': False}, 0)
            system.put({'id': 'active-parent', 'kind': 'note', 'content': {'t': 'parent'},
                        'relations': [{'relation': 'supports', 'target': 'inactive-referenced'}], 'active': True}, 0)
            system.put({'id': 'inactive-referenced', 'kind': 'note', 'content': {'t': 'referenced'}, 'active': False}, 0)
            system.put({'id': 'inactive-sealed-ref', 'kind': 'note',
                        'content': {'ref': 'w2/sealed-d12'}, 'active': False}, 0)
            system.put({'id': 'cold-candidate', 'kind': 'research-source-reference',
                        'content': {'title': 'Cold archive me', 'payload': 'X' * 500}, 'active': False}, 0)
            system.reindex()

        # Verify sealed file is NEVER opened during plan_archive_records
        real_read = Path.read_bytes

        def guard_sealed(path):
            if path.resolve() == self.sealed_path.resolve():
                raise AssertionError('sealed source body opened during cold archive planning!')
            return real_read(path)

        with patch.object(Path, 'read_bytes', guard_sealed):
            from native_runtime.cold_archive import plan_archive_records
            plan = plan_archive_records(
                System(self.store),
                ids=['goal', 'duty-open', 'active-parent', 'inactive-referenced',
                     'inactive-sealed-ref', 'cold-candidate'],
            )
        self.assertEqual([r['id'] for r in plan['eligible']], ['cold-candidate'])
        ineligible_map = {r['id']: r['reasons'] for r in plan['ineligible']}
        self.assertIn('protected-governing-id', ineligible_map['goal'])
        self.assertIn('protected-record-kind:obligation', ineligible_map['duty-open'])
        self.assertIn('active-record', ineligible_map['active-parent'])
        self.assertIn('referenced-by-active-record', ineligible_map['inactive-referenced'])
        self.assertIn('references-sealed-source', ineligible_map['inactive-sealed-ref'])

        plan_file = _write_json(self.root / 'archive-plan.json', plan)
        rcpt = json.loads(self.run_cli([
            *self.store_cli, 'archive-records', 'execute',
            '--plan', str(plan_file), '--expected-plan', plan['plan_sha256'],
            '--reason', 'Archive cold unreferenced source reference.',
        ]).stdout)
        self.assertEqual(rcpt['archived_count'], 1)

        # Hot file removed, HeadIndex updated, hot status skips it, exact show/history still work!
        self.assertFalse((self.store / 'work/records/cold-candidate.json').exists())
        s = System(self.store)
        self.assertNotIn('cold-candidate', s.record_store.ids())
        self.assertNotIn('cold-candidate', s.record_store.indexed_summaries())
        self.assertTrue(s.record_store.has_record('cold-candidate'))
        reloaded = json.loads(self.run_cli([*self.store_cli, 'show', 'cold-candidate']).stdout)
        self.assertEqual(reloaded['content']['title'], 'Cold archive me')
        hist = json.loads(self.run_cli([*self.store_cli, 'history', 'cold-candidate']).stdout)
        self.assertEqual(hist['records'][0]['id'], 'cold-candidate')

        # Attempting rev-0 put onto archived ID must be refused!
        shadow = _write_json(self.root / 'shadow.json', {'id': 'cold-candidate', 'kind': 'note', 'content': 'shadow'})
        self.run_cli([*self.store_cli, 'put', '--record', str(shadow), '--expected', '0', '--no-status'], expected_rc=2)

        # Inactive record pinned by work/interpretations/*.json evidence must be refused by archive-records!
        with exclusive(self.store / 'exchange/owner.lock'):
            s2 = System(self.store)
            s2.put({'id': 'interp-pinned-cold', 'kind': 'note', 'content': {'v': 1}, 'active': False}, 0)
            acct = self.root / 'interp-acct.txt'
            acct.write_text('Pinned cold record evidence.', encoding='utf-8')
            s2.interpret(1, 'continue', acct, ['records/interp-pinned-cold.json'])
        refused_plan = json.loads(self.run_cli([
            *self.store_cli, 'archive-records', 'plan', '--id', 'interp-pinned-cold',
        ]).stdout)
        self.assertEqual(refused_plan['eligible'], [])
        self.assertIn('referenced-by-interpretation-or-staged-packet', refused_plan['ineligible'][0]['reasons'])

    def test_g2s06_9layer_transitive_audit_objects_and_guarded_gc(self):
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            # Layer 1: live record basis + previous_object chain
            live_file = system.work / 'sources/live.txt'
            live_file.parent.mkdir(parents=True, exist_ok=True)
            live_file.write_text('live basis content', encoding='utf-8')
            r_live = system.put({'id': 'live-rec', 'kind': 'note', 'content': {'v': 1}, 'basis': ['sources/live.txt']}, 0)
            live_obj = system.show('live-rec')['basis'][0]['sha256']

            # Layer 2: cold-archived record basis
            cold_file = system.work / 'sources/cold-basis.txt'
            cold_file.write_text('cold archived basis content', encoding='utf-8')
            system.put({'id': 'cold-rec', 'kind': 'note', 'content': {'v': 1},
                        'basis': ['sources/cold-basis.txt'], 'active': False}, 0)
            cold_obj = system.show('cold-rec')['basis'][0]['sha256']
            from native_runtime.cold_archive import execute_archive_records, plan_archive_records
            cplan = plan_archive_records(system, ids=['cold-rec'])
            execute_archive_records(system, plan=cplan, expected_plan=cplan['plan_sha256'], reason='archive for audit')

            # Layer 4 (CRITICAL): transitive exposure.json -> full_view_object -> nested object
            nested_in_view = system.ex.objects.put(b'{"nested_view_dep": true}\n')
            full_view_obj = system.ex.objects.put(encode({'selected_records': [], 'nested_ref': nested_in_view}))
            exposure_obj = system.ex.objects.put(encode({'full_view_object': full_view_obj}))
            pkt_dir = system.ex.root / 'packets/fake-exposure-turn'
            pkt_dir.mkdir(parents=True, exist_ok=True)
            _write_json(pkt_dir / 'request.json', {
                'version': 2, 'packet': 'fake-exposure-turn', 'consumer': 'test',
                'brief': {'path': 'b', 'sha256': None, 'bytes': 0},
                'methods': [], 'targets': [], 'returns': [], 'artifacts': [],
                'materials': [{'path': '.system/turns/fake-exposure-turn/exposure.json',
                               'sha256': exposure_obj, 'bytes': 10}],
            })

            # Layer 6 (CRITICAL): work/interpretations/*.json evidence object
            interp_obj = system.ex.objects.put(b'interpretation evidence bytes\n')
            _write_json(system.work / 'interpretations/interp-1.json', {
                'evidence': [{'path': 'x', 'sha256': interp_obj, 'bytes': 30}],
            })

            # Layer 8: work/.system/batches/*.json pinned_object
            batch_res = put_batch(system, {
                'format': 'managed-ai-batch-update/1',
                'id': 'audit-batch',
                'instance_id': 'wave2-store',
                'pin_predecessors': True,
                'operations': [{
                    'id': 'live-rec', 'expected_revision': 1, 'expected_sha256': r_live['sha256'],
                    'mode': 'patch', 'replace_content_fields': {'v': 2},
                }],
            })
            pinned_pred_obj = batch_res['records'][0]['pinned_object']

            # Layer 9: work/sources/received/*.json object
            recv_obj = system.ex.objects.put(b'received source transfer bytes\n')
            _write_json(system.work / 'sources/received/transfer-1.json', {'object': recv_obj})

            # 2 genuinely unreferenced orphan objects (1 raw, 1 compressed .deflate)
            orphan_raw = system.ex.objects.put(b'unreferenced orphan raw blob 12345')
            orphan_comp = system.ex.objects.put(b'unreferenced orphan compressed blob ' * 40, compress=True)

        # Audit under LOCK_SH
        with shared(self.store / 'exchange/owner.lock'):
            audit = json.loads(self.run_cli([*self.store_cli, 'audit-objects']).stdout)
        unreachable_ids = {row['object'] for row in audit['unreachable']}
        self.assertEqual(unreachable_ids, {orphan_raw, orphan_comp})
        # Verify ALL critical layers kept their objects reachable
        for protected in (live_obj, cold_obj, exposure_obj, full_view_obj, nested_in_view,
                          interp_obj, pinned_pred_obj, recv_obj):
            self.assertNotIn(protected, unreachable_ids)
        self.assertGreaterEqual(audit['by_layer']['packets_transitive_exposure']['objects'], 2)
        self.assertGreaterEqual(audit['by_layer']['interpretations']['objects'], 1)

        # TOCTOU defense: make orphan_raw reachable after planning -> gc-objects MUST refuse!
        plan_file = _write_json(self.root / 'gc-plan.json', audit)
        with exclusive(self.store / 'exchange/owner.lock'):
            System(self.store).put({'id': 'adopt-orphan', 'kind': 'note',
                                    'content': {'ref': orphan_raw}}, 0)
        self.run_cli([*self.store_cli, 'gc-objects', '--plan', str(plan_file),
                      '--expected-plan', audit['plan_sha256'], '--reason', 'stale gc'], expected_rc=2)
        self.assertTrue(System(self.store).ex.objects.has(orphan_raw))
        self.assertTrue(System(self.store).ex.objects.has(orphan_comp))

        # Re-audit and prune ONLY orphan_comp cleanly
        audit2 = json.loads(self.run_cli([*self.store_cli, 'audit-objects']).stdout)
        self.assertEqual([r['object'] for r in audit2['unreachable']], [orphan_comp])
        plan_file2 = _write_json(self.root / 'gc-plan-2.json', audit2)
        gc_rcpt = json.loads(self.run_cli([
            *self.store_cli, 'gc-objects', '--plan', str(plan_file2),
            '--expected-plan', audit2['plan_sha256'], '--reason', 'Prune verified unreferenced orphan.',
        ]).stdout)
        self.assertEqual(gc_rcpt['removed_count'], 1)
        self.assertFalse(System(self.store).ex.objects.has(orphan_comp))
        self.assertTrue(System(self.store).ex.objects.has(full_view_obj))
        self.assertTrue(System(self.store).ex.objects.has(interp_obj))

        # Corrupt JSON in a critical root directory must fail closed on audit-objects (never silently drop roots!)
        corrupt_interp = self.store / 'work/interpretations/corrupt.json'
        corrupt_interp.write_bytes(b'{not-valid-json')
        try:
            proc = self.run_cli([*self.store_cli, 'audit-objects'], expected_rc=2)
            self.assertIn('unreadable root JSON', proc.stderr)
        finally:
            corrupt_interp.unlink(missing_ok=True)

    def test_g2s07_and_l3_graph_cli_indexed_parity_and_discovery_enhancements(self):
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            system.put({'id': 'A', 'kind': 'research-claim', 'content': {
                'title': 'Claim A', 'directions': ['B', 'not a valid record id sentence'],
            }, 'relations': [
                {'relation': 'supports', 'target': 'B'},
                {'relation': 'supports', 'target': 'B'},  # duplicate edge
                {'relation': 'revises native navigation from', 'target': 'A'},  # whitelisted self-loop
                {'relation': 'custom-self-pin', 'target': 'A'},  # domain-whitelisted self-loop
                {'relation': 'bogus-self', 'target': 'A'},  # non-provenance self-loop!
                {'relation': 'refines', 'target': 'dangling-target-id'},  # dangling record target!
                {'relation': 'grounded in original edition', 'target': 'evidence/file.md'},  # external non-record
                {'relation': 'free-text-predicate', 'target': 'C'},  # uncontrolled predicate warning!
            ]}, 0)
            system.put({'id': 'B', 'kind': 'research-direction', 'content': {'title': 'Dir B', 'claim_ids': ['C']},
                        'relations': [{'relation': 'answers', 'target': 'Q1'}]}, 0)
            system.put({'id': 'C', 'kind': 'note', 'content': {'title': 'Node C'}, 'relations': []}, 0)
            system.put({'id': 'Q1', 'kind': 'question',
                        'content': {'question': 'Does context geometry matter?', 'selection': 'qualified_unissued'},
                        'active': True}, 0)
            system.put({'id': 'Q2', 'kind': 'question',
                        'content': {'question': '  does   context geometry matter? ', 'selection': 'open'},
                        'active': True}, 0)

        # Unindexed graph CLI outputs
        nb_un = json.loads(self.run_cli([*self.store_cli, 'graph', 'neighbors', 'A', '--hops', '2', '--records-only']).stdout)
        lk_un = json.loads(self.run_cli([*self.store_cli, 'graph', 'links', '--target', 'Q1', '--relation', 'answers']).stdout)
        pt_un = json.loads(self.run_cli([*self.store_cli, 'graph', 'path', 'A', 'Q1']).stdout)
        lt_un = json.loads(self.run_cli([*self.store_cli, 'graph', 'lint']).stdout)

        self.assertFalse(lt_un['indexed'])
        self.assertEqual(lt_un['summary']['dangling_record_targets'], 1)
        self.assertEqual(lt_un['summary']['non_id_content_refs'], 1)
        self.assertEqual(lt_un['summary']['non_provenance_self_loops'], 1)
        self.assertEqual(lt_un['summary']['duplicate_edges'], 1)
        self.assertEqual(lt_un['summary']['content_relation_drift'], 1)  # B.content.claim_ids -> C missing in B.relations
        self.assertEqual(lt_un['summary']['duplicate_open_questions'], 1)  # Q1 + Q2 cluster
        self.assertEqual(lt_un['summary']['answered_open_questions'], 1)  # Q1 has inbound answers from B
        self.assertEqual(lt_un['summary']['uncontrolled_predicates'], 1)  # A -> C 'free-text-predicate'
        self.assertEqual(lk_un['matched'], 1)
        self.assertEqual(lk_un['links'][0]['from']['id'], 'B')
        self.assertTrue(pt_un['found'])
        self.assertEqual(pt_un['hops'], 2)
        self.assertEqual([n['id'] for n in pt_un['nodes']], ['A', 'B', 'Q1'])

        # Reindex -> run ALL graph subcommands under LOCK_SH and verify exact parity!
        self.run_cli([*self.store_cli, 'reindex'])
        with shared(self.store / 'exchange/owner.lock'):
            nb_ix = json.loads(self.run_cli([*self.store_cli, 'graph', 'neighbors', 'A', '--hops', '2', '--records-only']).stdout)
            lk_ix = json.loads(self.run_cli([*self.store_cli, 'graph', 'links', '--target', 'Q1', '--relation', 'answers']).stdout)
            pt_ix = json.loads(self.run_cli([*self.store_cli, 'graph', 'path', 'A', 'Q1']).stdout)
            lt_ix = json.loads(self.run_cli([*self.store_cli, 'graph', 'lint']).stdout)

        self.assertTrue(lt_ix['indexed'])
        for un, ix in ((nb_un, nb_ix), (lk_un, lk_ix), (pt_un, pt_ix), (lt_un, lt_ix)):
            self.assertEqual({k: v for k, v in ix.items() if k != 'indexed'},
                             {k: v for k, v in un.items() if k != 'indexed'})

        # L3: context_discovery.py with content=True skips sealed catalogue entries without opening sealed body
        real_read = Path.read_bytes

        def guard_sealed(path):
            if path.resolve() == self.sealed_path.resolve():
                raise AssertionError('sealed source body opened during content=True discovery!')
            return real_read(path)

        with patch.object(Path, 'read_bytes', guard_sealed):
            disc = Discovery(System(self.store)).search({
                'query': 'alpha', 'content': True, 'scopes': ['techniques'], 'limit': 10,
            })
        self.assertEqual([c['ref'] for c in disc['candidates']], ['technique:w2/available'])
        self.assertTrue(any(s['ref'] == 'technique:w2/sealed-d12' and s['reason'] == 'sealed-source'
                            for s in disc['coverage']['skipped']))

        # G2S-07(3): records_only, exclude_relation_labels, include_content_links
        disc_rec = Discovery(System(self.store)).search({
            'anchors': ['record:B'], 'scopes': ['records'], 'hops': 1,
            'records_only': True, 'exclude_relation_labels': ['answers'],
            'include_content_links': True, 'limit': 10,
        })
        cand_refs = {c['ref'] for c in disc_rec['candidates']}
        self.assertIn('record:C', cand_refs)  # reached via content:claim_ids!
        self.assertNotIn('record:Q1', cand_refs)  # excluded via exclude_relation_labels=['answers']!
        self.assertEqual(disc_rec['coverage']['unresolved_references'], [])

    def test_perf68_raw_hmac_head_index_legacy_fallback_and_nofollow_body_read(self):
        import hmac as hmac_mod
        from native_runtime.head_index import INDEX_DOMAIN, INDEX_FORMAT
        from native_runtime.selection.domain_selection import read_selected_body

        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            system.reindex()
            idx_path = system.record_store.index.path
            raw_new = idx_path.read_bytes()
            # Fast-path format starts with {"hmac":"<64hex>", and is valid JSON
            self.assertTrue(raw_new.startswith(b'{"hmac":"'))
            parsed = json.loads(raw_new)
            self.assertEqual(parsed['format'], INDEX_FORMAT)
            self.assertIsNotNone(system.record_store.index.load(system.record_store.format))
            # Single-byte tamper caught before json.loads
            idx_path.write_bytes(raw_new.replace(b'"goal"', b'"goa1"', 1))
            self.assertIsNone(system.record_store.index.load(system.record_store.format))
            # Backward-compatible Wave 1 sorted-key envelope still loads cleanly
            body = {k: v for k, v in parsed.items() if k != 'hmac'}
            payload = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
            key = system.record_store.receipts._key(create=False)
            tag = hmac_mod.digest(key, INDEX_DOMAIN + payload, 'sha256').hex()
            legacy_bytes = json.dumps(dict(body, hmac=tag), sort_keys=True, ensure_ascii=False,
                                      separators=(',', ':'), allow_nan=False).encode('utf-8')
            self.assertFalse(legacy_bytes.startswith(b'{"hmac":"'))
            idx_path.write_bytes(legacy_bytes)
            self.assertIsNotNone(system.record_store.index.load(system.record_store.format))

        # O_NOFOLLOW dir-FD traversal in read_selected_body rejects symlink leaf & symlink intermediate dir
        src_root = self.root / 'nofollow-root'
        (src_root / 'real-sub').mkdir(parents=True)
        real_doc = src_root / 'real-sub/doc.md'
        real_doc.write_bytes(b'authentic body\n')
        (src_root / 'sym-sub').symlink_to(src_root / 'real-sub')
        (src_root / 'real-sub/sym-doc.md').symlink_to(real_doc)
        entry_ok = {
            'id': 'nf/ok', 'availability': 'available', 'source_root': str(src_root),
            'body_path': 'real-sub/doc.md', 'bytes': 15, 'sha256': sha(b'authentic body\n'),
        }
        self.assertEqual(read_selected_body(entry_ok, validated_root=True), b'authentic body\n')
        for bad_path in ('sym-sub/doc.md', 'real-sub/sym-doc.md'):
            with self.assertRaisesRegex(ValueError, 'ordinary selected source required'):
                read_selected_body(dict(entry_ok, body_path=bad_path), validated_root=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
