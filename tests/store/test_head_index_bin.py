"""Dual-OS tests for Wave 8 Phase 8A: Binary mmap HeadIndex v3, CSR graph fast paths, deltas and rotation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

if __package__:
    from .native_test_paths import ENGINE as _ENGINE
else:
    from native_test_paths import ENGINE as _ENGINE

_ = _ENGINE

from managed_ai.core.formats import HEAD_INDEX_FORMAT, HEAD_INDEX_FORMAT_V1
from managed_ai.core.storage import encode, exclusive, sha
from managed_ai.store.query import graph_query
from managed_ai.store.records.head_index import INDEX_DOMAIN
from managed_ai.store.records.head_index_bin import HEAD_INDEX_FORMAT_V3, read_current_pointer
from managed_ai.store.system import System


class HeadIndexBinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix='managed-ai-w8-hidx-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / 'store'

        dom_dir = self.root / 'domain'
        dom_dir.mkdir()
        cat_bytes = encode({'format': 'managed-ai-source-catalogue/1', 'sources': []})
        (dom_dir / 'catalogue.json').write_bytes(cat_bytes)
        dom_manifest = dom_dir / 'domain.json'
        dom_manifest.write_bytes(encode({
            'format': 'managed-ai-domain/1',
            'id': 'w8-test-domain',
            'version': '1',
            'engine_api': 'managed-ai-runtime/1',
            'source_root': '.',
            'catalogue': {'path': 'catalogue.json', 'sha256': sha(cat_bytes)},
            'schemas': {},
            'entry_sources': [],
            'tools': [],
            'relation_vocabulary': {
                'predicates': ['supports', 'refines', 'answers', 'contradicts'],
                'provenance_self_loop_relations': ['revises native navigation from', 'self-ok'],
            },
        }))

        System.initialize(
            self.store,
            {'contribution': 'Verify Wave 8 binary mmap HeadIndex v3 parity and safety.'},
            instance_id='w8-hidx-store',
            domains=[dom_manifest],
        )

    def _populate_rich_graph(self, system: System) -> None:
        src_file = system.work / 'sources/evidence-1.txt'
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text('pinned basis evidence text\n', encoding='utf-8')

        with exclusive(system.ex.root / 'owner.lock'):
            system.put({
                'id': 'claim-a',
                'kind': 'research-claim',
                'content': {
                    'title': 'Claim Alpha',
                    'directions': ['dir-b', 'not a valid id sentence'],
                    'body': 'Detailed alpha token geometry explanation.',
                },
                'relations': [
                    {'relation': 'supports', 'target': 'dir-b'},
                    {'relation': 'supports', 'target': 'dir-b'},
                    {'relation': 'revises native navigation from', 'target': 'claim-a'},
                    {'relation': 'self-ok', 'target': 'claim-a'},
                    {'relation': 'bad-self', 'target': 'claim-a'},
                    {'relation': 'refines', 'target': 'missing-target-id'},
                    {'relation': 'grounded in original edition', 'target': 'evidence/doc-01.md'},
                    {'relation': 'uncontrolled-rel', 'target': 'note-c'},
                ],
                'basis': ['sources/evidence-1.txt'],
                'active': True,
            }, 0)
            system.put({
                'id': 'dir-b',
                'kind': 'research-direction',
                'content': {'title': 'Direction Beta', 'claim_ids': ['note-c']},
                'relations': [
                    {'relation': 'answers', 'target': 'q-one'},
                    {'relation': 'supports', 'target': 'note-c'},
                ],
                'active': True,
            }, 0)
            system.put({
                'id': 'note-c',
                'kind': 'note',
                'content': {'title': 'Note Gamma', 'text': 'gamma searchable text'},
                'relations': [{'relation': 'refines', 'target': 'q-one'}],
                'active': False,
            }, 0)
            system.put({
                'id': 'q-one',
                'kind': 'question',
                'content': {'question': 'Does binary mmap preserve parity?', 'selection': 'open'},
                'active': True,
            }, 0)
            system.put({
                'id': 'q-two',
                'kind': 'question',
                'content': {'question': '  does binary   mmap preserve parity? ', 'selection': 'qualified_unissued'},
                'active': True,
            }, 0)
            system.put({
                'id': 'duty-one',
                'kind': 'obligation',
                'content': {'text': 'Maintain exact parity across binary and JSON indexes.', 'disposition': 'open'},
                'active': True,
            }, 0)
            # Record with >512 tokens and >512-char exact ref to verify truncation bitflags.
            long_origin = 'origin/' + ('x' * 600)
            many_words = ' '.join(f'tok{i}' for i in range(600))
            system.put({
                'id': 'trunc-rec',
                'kind': 'research-source-reference',
                'content': {'origin': long_origin, 'body': many_words},
                'active': False,
            }, 0)

    def test_exact_parity_across_status_graph_lookup_and_discovery(self) -> None:
        system = System(self.store)
        self._populate_rich_graph(system)

        # Capture unindexed baselines.
        st_un = system.status()
        st_full_un = system.status(verify_all_basis=True)
        nb_both_un = graph_query.neighbors(system, 'dir-b', direction='both', hops=2)
        nb_in_un = graph_query.neighbors(system, 'q-one', direction='in', hops=2, active_only=True)
        nb_rec_un = graph_query.neighbors(system, 'claim-a', direction='out', hops=2, records_only=True)
        lk_rec_un = graph_query.links(system, target='q-one', relations=['answers'])
        lk_ext_un = graph_query.links(system, target='evidence/doc-01.md')
        pt_un = graph_query.shortest_path(system, 'claim-a', 'q-one')
        pt_act_un = graph_query.shortest_path(system, 'claim-a', 'note-c', active_only=True)
        lt_un = graph_query.lint(system)
        self.assertFalse(lt_un['indexed'])

        # Build binary v3 index.
        with exclusive(system.ex.root / 'owner.lock'):
            receipt = system.reindex()

        self.assertEqual(receipt['format'], HEAD_INDEX_FORMAT_V3)
        self.assertEqual(receipt['generation'], 1)
        self.assertEqual(receipt['indexed_records'], 8)
        bin_path = system.record_store.index.binary_path()
        self.assertIsNotNone(bin_path)
        assert bin_path is not None
        self.assertEqual(bin_path.name, 'head-index.gen1.bin')
        self.assertFalse(system.record_store.index.path.exists())

        pointer = read_current_pointer(system.record_store.index.directory)
        self.assertIsNotNone(pointer)
        assert pointer is not None
        self.assertEqual(pointer['generation'], 1)
        self.assertLess(system.record_store.index.current_path.stat().st_size, 256)

        # Check columnar bytemap counts & O(log N) lookup directly via open_view().
        with system.record_store.index.open_view(system.record_store.format) as view:
            self.assertIsNotNone(view)
            assert view is not None
            self.assertEqual(view.record_count, 8)
            self.assertEqual(view.active_count_base(), 6)
            counts = view.kind_counts()
            self.assertEqual(counts['question'], (2, 2))
            self.assertEqual(counts['obligation'], (1, 1))
            self.assertEqual(len(view.slots_for_kind('obligation')), 1)

            trunc_entry = view.get_entry('trunc-rec')
            self.assertIsNotNone(trunc_entry)
            assert trunc_entry is not None
            self.assertTrue(trunc_entry.get('tokens_truncated'))
            self.assertTrue(trunc_entry.get('exact_refs_truncated'))
            self.assertIsNone(view.get_entry('nonexistent-id'))

        # O(log N) verified single-record lookup matches full indexed_summaries().
        single = system.record_store.get_indexed_entry('claim-a')
        summaries = system.record_store.indexed_summaries()
        self.assertIsNotNone(single)
        self.assertIsNotNone(summaries)
        assert summaries is not None
        self.assertEqual(single, summaries['claim-a'])

        # Exact parity across status + all 4 graph operations!
        self.assertEqual(system.status(), st_un)
        self.assertEqual(system.status(verify_all_basis=True), st_full_un)

        nb_both_ix = graph_query.neighbors(system, 'dir-b', direction='both', hops=2)
        nb_in_ix = graph_query.neighbors(system, 'q-one', direction='in', hops=2, active_only=True)
        nb_rec_ix = graph_query.neighbors(system, 'claim-a', direction='out', hops=2, records_only=True)
        lk_rec_ix = graph_query.links(system, target='q-one', relations=['answers'])
        lk_ext_ix = graph_query.links(system, target='evidence/doc-01.md')
        pt_ix = graph_query.shortest_path(system, 'claim-a', 'q-one')
        pt_act_ix = graph_query.shortest_path(system, 'claim-a', 'note-c', active_only=True)
        lt_ix = graph_query.lint(system)

        self.assertTrue(lt_ix['indexed'])
        self.assertTrue(nb_both_ix['indexed'])
        self.assertTrue(lk_ext_ix['indexed'])
        self.assertEqual(lk_ext_ix['matched'], 1)
        self.assertIsNone(lk_ext_ix['target_head'])

        for un, ix in (
            (nb_both_un, nb_both_ix),
            (nb_in_un, nb_in_ix),
            (nb_rec_un, nb_rec_ix),
            (lk_rec_un, lk_rec_ix),
            (lk_ext_un, lk_ext_ix),
            (pt_un, pt_ix),
            (pt_act_un, pt_act_ix),
            (lt_un, lt_ix),
        ):
            self.assertEqual(
                {k: v for k, v in ix.items() if k != 'indexed'},
                {k: v for k, v in un.items() if k != 'indexed'},
            )

        # Discovery still passes with canonical library/cli.py recovery wiring.
        disc = system.discover({'query': 'Alpha', 'content': True, 'scopes': ['records']})
        self.assertIn('record:claim-a', [c['ref'] for c in disc['candidates']])

    def test_hmac_tamper_truncation_pointer_traversal_and_legacy_json_fallback(self) -> None:
        system = System(self.store)
        self._populate_rich_graph(system)
        with exclusive(system.ex.root / 'owner.lock'):
            system.reindex()

        hi = system.record_store.index
        bin_path = hi.binary_path()
        assert bin_path is not None
        orig_bin = bin_path.read_bytes()
        orig_ptr = hi.current_path.read_bytes()

        # 1. Flip 1 byte in binary payload -> HMAC mismatch -> clean None & full-scan fallback.
        tampered = bytearray(orig_bin)
        tampered[200] ^= 0x01
        bin_path.write_bytes(bytes(tampered))
        self.assertIsNone(hi.open_view(system.record_store.format))
        self.assertIsNone(hi.load(system.record_store.format))
        self.assertEqual(len(system.status()['records']), 8)
        self.assertFalse(graph_query.lint(system)['indexed'])

        # 2. Truncated binary file -> clean None (never struct.error).
        bin_path.write_bytes(orig_bin[:100])
        self.assertIsNone(hi.open_view(system.record_store.format))

        # 3. Restore binary, tamper footer HMAC -> clean None.
        bad_footer = orig_bin[:-1] + bytes([orig_bin[-1] ^ 0xFF])
        bin_path.write_bytes(bad_footer)
        self.assertIsNone(hi.open_view(system.record_store.format))
        bin_path.write_bytes(orig_bin)

        # 4. Path traversal inside head-index.current.json -> rejected cleanly.
        bad_ptr = json.loads(orig_ptr.decode('utf-8'))
        bad_ptr['file'] = '../head-index.gen1.bin'
        hi.current_path.write_text(json.dumps(bad_ptr), encoding='utf-8')
        self.assertIsNone(hi.open_view(system.record_store.format))

        # 5. Legacy head-index.json fallback works seamlessly when binary v3 is absent.
        hi.current_path.unlink()
        bin_path.unlink()
        entries = {}
        for rec in system.record_store.scan_records():
            from managed_ai.store.records.head_index import build_entry

            raw = system.record_path(rec['id']).read_bytes()
            entries[rec['id']] = build_entry(rec, raw, system.record_path(rec['id']).lstat())
        hi.save_json(system.record_store.format, entries, create_key=False)
        self.assertTrue(hi.path.is_file())
        loaded = hi.load_with_format(system.record_store.format)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded[1], HEAD_INDEX_FORMAT)
        self.assertEqual(len(loaded[0]), 8)

        # Also check v1 legacy JSON loads with HEAD_INDEX_FORMAT_V1.
        import hmac as hmac_mod

        parsed = json.loads(hi.path.read_bytes())
        body_v1 = {k: v for k, v in parsed.items() if k != 'hmac'}
        body_v1['format'] = HEAD_INDEX_FORMAT_V1
        payload = json.dumps(body_v1, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        key = system.record_store.receipts._key(create=False)
        assert key is not None
        tag = hmac_mod.digest(key, INDEX_DOMAIN + payload, 'sha256').hex()
        hi.path.write_bytes(b'{"hmac":"' + tag.encode('ascii') + b'",' + payload[1:])
        loaded_v1 = hi.load_with_format(system.record_store.format)
        self.assertIsNotNone(loaded_v1)
        assert loaded_v1 is not None
        self.assertEqual(loaded_v1[1], HEAD_INDEX_FORMAT_V1)

        # Reindex upgrades cleanly to binary v3 and removes stale head-index.json.
        with exclusive(system.ex.root / 'owner.lock'):
            rcpt2 = system.reindex()
        self.assertEqual(rcpt2['format'], HEAD_INDEX_FORMAT_V3)
        self.assertFalse(hi.path.exists())
        self.assertTrue(hi.current_path.is_file())

    def test_delta_sidecar_overlay_and_64_op_compaction_to_next_generation(self) -> None:
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            system.put({'id': 'base-rec', 'kind': 'note', 'content': {'v': 0}}, 0)
            rcpt1 = system.reindex()
            self.assertEqual(rcpt1['generation'], 1)

            # Incremental puts land in head-index.delta.jsonl bound to gen1 HMAC.
            system.put({'id': 'delta-0', 'kind': 'note', 'content': {'title': 'Delta Zero'}}, 0)
            hi = system.record_store.index
            self.assertTrue(hi.delta_path.is_file())
            self.assertEqual(len(hi.delta_path.read_text(encoding='utf-8').splitlines()), 1)

            with hi.open_view(system.record_store.format) as view:
                self.assertIsNotNone(view)
                assert view is not None
                self.assertTrue(view.has_deltas)
                got = view.get_entry('delta-0')
                self.assertIsNotNone(got)
                assert got is not None
                self.assertEqual(got['graph']['title'], 'Delta Zero')

            # Status and graph see delta-0 accurately.
            st = system.status()
            self.assertIn('delta-0', {r['id'] for r in st['records']})

            # Push past 64 delta ops -> auto-compacts into head-index.gen2.bin!
            for idx in range(1, 66):
                system.put({'id': f'delta-{idx:02d}', 'kind': 'note', 'content': {'n': idx}}, 0)

            ptr = read_current_pointer(hi.directory)
            self.assertIsNotNone(ptr)
            assert ptr is not None
            self.assertGreaterEqual(ptr['generation'], 2)
            self.assertLess(
                len(hi.delta_path.read_text(encoding='utf-8').splitlines()) if hi.delta_path.exists() else 0,
                65,
            )
            self.assertIsNotNone(system.record_store.get_indexed_entry('delta-65'))

    def test_concurrent_mmap_reader_during_writer_generation_rotation_zero_permission_error(self) -> None:
        """Hold active HeadIndexView mmap on gen1 while writer rotates to gen2/gen3 (Windows-safe!)."""
        system = System(self.store)
        hi = system.record_store.index
        with exclusive(system.ex.root / 'owner.lock'):
            system.put({'id': 'rot-a', 'kind': 'note', 'content': {'title': 'Gen 1'}}, 0)
            rcpt1 = system.reindex()
            gen1_path = Path(rcpt1['path'])
            self.assertTrue(gen1_path.is_file())

            # Hold open mmap on gen1 while performing multiple writer rotations!
            reader_v1 = hi.open_view(system.record_store.format)
            self.assertIsNotNone(reader_v1)
            assert reader_v1 is not None
            try:
                system.put({'id': 'rot-b', 'kind': 'note', 'content': {'title': 'Gen 2'}}, 0)
                rcpt2 = system.reindex()
                self.assertEqual(rcpt2['generation'], 2)

                system.put({'id': 'rot-c', 'kind': 'note', 'content': {'title': 'Gen 3'}}, 0)
                rcpt3 = system.reindex()
                self.assertEqual(rcpt3['generation'], 3)

                # Active gen1 reader still sees exact gen1 snapshot without error!
                self.assertEqual(reader_v1.generation, 1)
                self.assertEqual(reader_v1.record_count, 2)
                self.assertIsNotNone(reader_v1.get_entry('rot-a'))
                self.assertIsNone(reader_v1.get_entry('rot-c'))

                # New concurrent reader sees latest gen3 snapshot!
                with hi.open_view(system.record_store.format) as reader_v3:
                    self.assertIsNotNone(reader_v3)
                    assert reader_v3 is not None
                    self.assertEqual(reader_v3.generation, 3)
                    self.assertEqual(reader_v3.record_count, 4)
                    self.assertIsNotNone(reader_v3.get_entry('rot-c'))
            finally:
                reader_v1.close()

            # Once gen1 reader closes, next rotation prunes stale gen1 cleanly!
            rcpt4 = system.reindex()
            self.assertEqual(rcpt4['generation'], 4)
            self.assertFalse(gen1_path.exists())
            self.assertTrue(Path(rcpt4['path']).is_file())


if __name__ == '__main__':
    unittest.main(verbosity=2)
