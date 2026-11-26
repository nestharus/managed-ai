"""Dual-OS tests for Wave 8 Phase 8B: Reserved append-only shard slabs, pack-objects CLI, logical/physical GC & ColdArchive."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
import zlib

if __package__:
    from .native_test_paths import ENGINE
else:
    from native_test_paths import ENGINE

from managed_ai.core.storage import (
    OCC_LIVE_DEFLATE,
    OCC_LIVE_RAW,
    OCC_SEALED,
    PackSession,
    encode,
    exclusive,
    parse_pack_current,
    sha,
    shared,
)
from managed_ai.store.records.cold_archive import (
    execute_archive_records,
    plan_archive_records,
    restore_archived_records,
)
from managed_ai.store.records.object_audit import audit_objects, gc_objects
from managed_ai.store.records.pack_slab import (
    PACK_OBJECTS_PLAN_FORMAT,
    PACK_OBJECTS_RECEIPT_FORMAT,
    execute_pack_objects,
    plan_pack_objects,
    write_pack_current,
    write_shard,
)
from managed_ai.store.system import System


def _write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    return path


class PackSlabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix='managed-ai-w8-slab-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / 'store'
        System.initialize(
            self.store,
            {'contribution': 'Verify Wave 8 reserved append-only shard slabs, audit, GC, and cold archive.'},
            instance_id='w8-slab-store',
        )
        self.store_cli = [
            sys.executable, '-B', str(ENGINE / 'store/manage_store.py'),
            '--root', str(self.store),
        ]

    def run_cli(self, args: list[str], *, expected_rc: int = 0) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [*self.store_cli, *args],
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=60,
        )
        self.assertEqual(proc.returncode, expected_rc, f'args={args}\nstdout={proc.stdout}\nstderr={proc.stderr}')
        return proc

    def test_pack_objects_dry_run_apply_multishard_and_hybrid_loose_writes(self) -> None:
        system = System(self.store)
        initial_loose = len(system.ex.objects.entries())
        payloads: dict[str, bytes] = {}
        with exclusive(system.ex.root / 'owner.lock'):
            for idx in range(25):
                # Mix raw and compressed objects across multiple small shards.
                data = (f'slab-object-{idx:02d}-' + ('A' * 300)).encode('utf-8')
                key = system.ex.objects.put(data, compress=(idx % 2 == 0))
                payloads[key] = data
            # Pin one object via a live record so it stays reachable.
            first_key = next(iter(payloads))
            system.put({'id': 'rec-pin', 'kind': 'note', 'content': {'ref': first_key}}, 0)

        expected_total = initial_loose + 25
        # 1. Dry-run pack-objects runs safely under LOCK_SH and makes ZERO mutations.
        with shared(self.store / 'exchange/owner.lock'):
            dry_plan = json.loads(self.run_cli(['pack-objects', '--shard-bytes', '4096']).stdout)
        self.assertEqual(dry_plan['format'], PACK_OBJECTS_PLAN_FORMAT)
        self.assertEqual(dry_plan['mode'], 'dry-run')
        self.assertEqual(dry_plan['loose_pack_candidates'], expected_total)
        self.assertGreaterEqual(dry_plan['planned_new_shards'], 2)
        self.assertFalse((self.store / 'exchange/objects/packs').exists())

        # Wrong --expected-plan refused cleanly.
        self.run_cli(
            ['pack-objects', '--apply', '--shard-bytes', '4096', '--expected-plan', '0' * 64],
            expected_rc=2,
        )
        self.assertFalse((self.store / 'exchange/objects/packs').exists())

        # 2. Apply packing with exact plan guard.
        rcpt = json.loads(self.run_cli([
            'pack-objects', '--apply',
            '--shard-bytes', '4096',
            '--expected-plan', dry_plan['plan_sha256'],
            '--reason', 'Wave 8 multi-shard pack test',
        ]).stdout)
        self.assertEqual(rcpt['format'], PACK_OBJECTS_RECEIPT_FORMAT)
        self.assertEqual(rcpt['mode'], 'applied')
        self.assertEqual(rcpt['packed_objects'], expected_total)
        self.assertEqual(rcpt['removed_loose_files'], expected_total)
        self.assertGreaterEqual(rcpt['new_shards'], 2)
        self.assertTrue(Path(rcpt['receipt_path']).is_file())

        # All loose files removed; every object readable via 256-fanout mmap PackSession!
        s = System(self.store)
        self.assertEqual(s.ex.objects.entries(), [])
        self.assertEqual(len(s.ex.objects.packed_entries()), expected_total)
        for key, expected in payloads.items():
            self.assertTrue(s.ex.objects.has(key))
            self.assertEqual(s.ex.objects.read(key, retained=True, expected_bytes=len(expected)), expected)

        # Verify both raw and deflate codecs were preserved in .occ bytemaps.
        codecs = {row['occ'] for row in s.ex.objects.packed_entries()}
        self.assertIn(OCC_LIVE_RAW, codecs)
        self.assertIn(OCC_LIVE_DEFLATE, codecs)

        # 3. Hybrid hot loose writes coexist cleanly with packed slabs!
        hot_data = b'brand-new hot loose object written after packing'
        with exclusive(self.store / 'exchange/owner.lock'):
            hot_key = s.ex.objects.put(hot_data)
            # Re-putting an already packed object does NOT create a duplicate loose file!
            reput_key = s.ex.objects.put(payloads[first_key])
            self.assertEqual(reput_key, first_key)
            self.assertFalse((s.ex.objects.directory / first_key).exists())
            self.assertFalse((s.ex.objects.directory / (first_key + '.deflate')).exists())

        self.assertTrue((s.ex.objects.directory / hot_key).is_file())
        self.assertEqual(s.ex.objects.read(hot_key), hot_data)
        self.assertEqual(len(list(s.ex.objects.iter_objects())), expected_total + 1)

        # 4. Second incremental pack-objects --apply appends a new shard without rewriting existing shards!
        before_shards = parse_pack_current(s.ex.objects.packs_dir)['shards']
        rcpt2 = json.loads(self.run_cli(['pack-objects', '--apply', '--shard-bytes', '4096']).stdout)
        self.assertEqual(rcpt2['packed_objects'], 1)
        self.assertEqual(rcpt2['new_shards'], 1)
        after_shards = parse_pack_current(s.ex.objects.packs_dir)['shards']
        self.assertEqual(after_shards[:len(before_shards)], before_shards)
        self.assertEqual(s.ex.objects.read(hot_key), hot_data)

    def test_mixed_loose_and_packed_audit_logical_occ_gc_and_over_30pct_physical_repack(self) -> None:
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            # Reachable transitive chain: live record -> obj_root -> obj_child.
            obj_child = system.ex.objects.put(b'{"leaf": "deep_packed"}\n')
            obj_root = system.ex.objects.put(encode({'nested': obj_child}))
            system.put({'id': 'keep-chain', 'kind': 'note', 'content': {'root_obj': obj_root}}, 0)

            # 6 reachable ~2 KiB objects + 4 unreachable ~12 KiB objects in ONE single shard:
            # 1 orphan (~12 KiB / ~85 KiB = ~14% <= 30% -> logical .occ GC only);
            # all 4 orphans (~48 KiB / ~85 KiB = ~56% > 30% -> physical shard repack!).
            keep_keys: list[str] = []
            orphan_keys: list[str] = []
            for idx in range(6):
                body = encode({'kind': 'keep', 'i': idx, 'pad': 'K' * 1800})
                k = system.ex.objects.put(body)
                keep_keys.append(k)
                system.put({'id': f'keep-{idx}', 'kind': 'note', 'content': {'obj': k}}, 0)
            for idx in range(4):
                body = encode({'kind': 'orphan', 'i': idx, 'pad': 'O' * 12000})
                orphan_keys.append(system.ex.objects.put(body))

            # Baseline loose audit before packing.
            audit_loose = audit_objects(system)
            self.assertEqual({r['object'] for r in audit_loose['unreachable']}, set(orphan_keys))

            # Pack everything into ONE shard (64 MiB default target).
            pack_rcpt = execute_pack_objects(system, reason='pack for gc test')
            self.assertEqual(pack_rcpt['new_shards'], 1)

            # Add one more loose orphan so audit & gc run on a mixed loose + packed store!
            loose_orphan = system.ex.objects.put(b'loose-orphan-mixed-store')

            audit_mixed = audit_objects(system)
            unreach_mixed = {r['object'] for r in audit_mixed['unreachable']}
            self.assertEqual(unreach_mixed, set(orphan_keys) | {loose_orphan})
            self.assertNotIn(obj_root, unreach_mixed)
            self.assertNotIn(obj_child, unreach_mixed)

            # Phase 1 GC: prune ONLY 1 packed orphan (~8% dead <= 30% threshold) + loose_orphan!
            # Must perform LOGICAL .occ GC ONLY (same .slab & .idx filenames, new .occ2!).
            cur_before = parse_pack_current(system.ex.objects.packs_dir)
            assert cur_before is not None
            slab_before = cur_before['shards'][0]['slab']
            idx_before = cur_before['shards'][0]['idx']
            occ_before = cur_before['shards'][0]['occ']

            # Create a custom plan targeting ONLY orphan_keys[0] + loose_orphan via a sub-audit:
            # Make orphan_keys[1..3] temporarily reachable so audit nominates only orphan_keys[0] + loose_orphan.
            system.put({'id': 'temp-pin', 'kind': 'note', 'content': {'pins': orphan_keys[1:]}}, 0)
            audit_small_gc = audit_objects(system)
            self.assertEqual({r['object'] for r in audit_small_gc['unreachable']}, {orphan_keys[0], loose_orphan})
            gc1 = gc_objects(
                system,
                plan=audit_small_gc,
                expected_plan=audit_small_gc['plan_sha256'],
                reason='Logical occ GC under 30% dead threshold',
            )
            self.assertEqual(gc1['removed_count'], 2)
            self.assertIn('packed_gc', gc1)
            self.assertEqual(gc1['packed_gc']['tombstoned_count'], 1)
            self.assertEqual(gc1['packed_gc']['repacked_shards'], [])
            self.assertEqual(gc1['packed_gc']['dropped_shards'], [])

            cur_after_logical = parse_pack_current(system.ex.objects.packs_dir)
            assert cur_after_logical is not None
            self.assertEqual(cur_after_logical['shards'][0]['slab'], slab_before)
            self.assertEqual(cur_after_logical['shards'][0]['idx'], idx_before)
            self.assertNotEqual(cur_after_logical['shards'][0]['occ'], occ_before)
            self.assertFalse(system.ex.objects.has(orphan_keys[0]))
            self.assertFalse(system.ex.objects.has(loose_orphan))
            self.assertTrue(system.ex.objects.has(keep_keys[0]))

            # Phase 2 GC: release temp-pin and prune remaining 3 packed orphans (cumulative dead ~33% > 30%)!
            # Must trigger PHYSICAL shard repack into a fresh generation shrinking slab_bytes!
            slab_bytes_before = cur_after_logical['shards'][0]['slab_bytes']
            system.put({'id': 'temp-pin', 'kind': 'note', 'content': {'pins': []}}, 1)
            # Note: rev 1 of temp-pin was not retained under default current-use retention!
            audit_repack = audit_objects(system)
            self.assertEqual({r['object'] for r in audit_repack['unreachable']}, set(orphan_keys[1:]))

            gc2 = gc_objects(
                system,
                plan=audit_repack,
                expected_plan=audit_repack['plan_sha256'],
                reason='Physical repack over 30% dead threshold',
            )
            self.assertEqual(gc2['packed_gc']['tombstoned_count'], 3)
            self.assertEqual(gc2['packed_gc']['repacked_shards'], [0])
            self.assertGreater(gc2['packed_gc']['physical_reclaimed_bytes'], 0)

            cur_after_repack = parse_pack_current(system.ex.objects.packs_dir)
            assert cur_after_repack is not None
            self.assertNotEqual(cur_after_repack['shards'][0]['slab'], slab_before)
            self.assertLess(cur_after_repack['shards'][0]['slab_bytes'], slab_bytes_before)
            self.assertEqual(cur_after_repack['shards'][0]['dead_bytes'], 0)

            # All surviving objects still read back bit-identically after physical repack!
            for k in (obj_root, obj_child, *keep_keys):
                self.assertTrue(system.ex.objects.has(k))
                raw = system.ex.objects.read(k, retained=True)
                self.assertEqual(sha(raw), k)

            # Phase 3 GC: pack an all-orphan second shard, then GC it -> 100%-dead shard dropped cleanly!
            drop_orphan = system.ex.objects.put(b'drop-entire-shard-orphan-' + (b'D' * 4096))
            rcpt_s1 = execute_pack_objects(system, reason='pack all-orphan shard 1')
            self.assertEqual(rcpt_s1['new_shards'], 1)
            shard1_id = rcpt_s1['shards'][0]['shard']
            audit_drop = audit_objects(system)
            self.assertEqual({r['object'] for r in audit_drop['unreachable']}, {drop_orphan})
            gc3 = gc_objects(
                system,
                plan=audit_drop,
                expected_plan=audit_drop['plan_sha256'],
                reason='Drop 100%-dead shard cleanly',
            )
            self.assertEqual(gc3['packed_gc']['dropped_shards'], [shard1_id])
            self.assertFalse(system.ex.objects.has(drop_orphan))
            cur_after_drop = parse_pack_current(system.ex.objects.packs_dir)
            assert cur_after_drop is not None
            self.assertNotIn(shard1_id, [s['shard'] for s in cur_after_drop['shards']])

    def test_concurrent_pack_mmap_reader_during_gc_and_repack_zero_permission_error(self) -> None:
        """Hold active PackSession mmap on gen1 while GC writes .occ2 and physically repacks to gen2."""
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            keep_k = system.ex.objects.put(b'keep-data-' + (b'K' * 1024))
            dead_a = system.ex.objects.put(b'dead-data-a-' + (b'A' * 24000))
            dead_b = system.ex.objects.put(b'dead-data-b-' + (b'B' * 24000))
            system.put({'id': 'pin-keep', 'kind': 'note', 'content': {'k': keep_k}}, 0)
            execute_pack_objects(system, reason='gen1 pack')

            # Hold open PackSession on gen1 while running >30% physical repack GC!
            with PackSession(system.ex.objects.packs_dir) as reader_gen1:
                audit = audit_objects(system)
                self.assertEqual({r['object'] for r in audit['unreachable']}, {dead_a, dead_b})
                rcpt = gc_objects(
                    system,
                    plan=audit,
                    expected_plan=audit['plan_sha256'],
                    reason='Repack while reader holds gen1 mmap',
                )
                self.assertEqual(rcpt['packed_gc']['repacked_shards'], [0])

                # Gen1 reader still reads its immutable gen1 snapshot cleanly without error!
                self.assertEqual(reader_gen1.read(dead_a, retained=True), b'dead-data-a-' + (b'A' * 24000))
                self.assertEqual(reader_gen1.read(keep_k, retained=True), b'keep-data-' + (b'K' * 1024))

            # New reader sees gen2 where dead_a/dead_b are gone and keep_k survives!
            self.assertFalse(system.ex.objects.has(dead_a))
            self.assertFalse(system.ex.objects.has(dead_b))
            self.assertEqual(system.ex.objects.read(keep_k), b'keep-data-' + (b'K' * 1024))

    def test_cold_archive_packed_slab_read_audit_and_restore(self) -> None:
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            basis_file = system.work / 'sources/cold-ev.txt'
            basis_file.parent.mkdir(parents=True, exist_ok=True)
            basis_file.write_text('cold archive packed basis evidence\n', encoding='utf-8')
            for idx in range(3):
                system.put({
                    'id': f'cold-{idx}',
                    'kind': 'note',
                    'content': {'title': f'Cold {idx}', 'body': 'archived payload ' * 40},
                    'basis': ['sources/cold-ev.txt'],
                    'active': False,
                }, 0)
            basis_obj = system.show('cold-0')['basis'][0]['sha256']

            plan = plan_archive_records(system, ids=['cold-0', 'cold-1', 'cold-2'])
            execute_archive_records(system, plan=plan, expected_plan=plan['plan_sha256'], reason='archive 3 records')

            # Pack cold archive blobs into exchange/cold-records/packs/ via pack-objects --include-cold-archive!
            rcpt = execute_pack_objects(system, include_cold_archive=True, reason='pack objects and cold archive')
            self.assertEqual(rcpt['cold_archive']['packed_records'], 3)
            self.assertEqual(rcpt['cold_archive']['removed_loose_blobs'], 3)

            # Loose .json.deflate files are gone, yet show(), size(), history(), and audit_objects() work seamlessly!
            archive = system.record_store.cold_archive
            for idx in range(3):
                self.assertFalse(archive.blob_path(f'cold-{idx}').exists())
                rec = system.record_store.show(f'cold-{idx}')
                self.assertEqual(rec['content']['title'], f'Cold {idx}')
                self.assertGreater(system.record_store.size(f'cold-{idx}'), 100)

            # Layer 2 in audit_objects still reads packed cold records and keeps their basis object reachable!
            audit = audit_objects(system)
            unreach = {r['object'] for r in audit['unreachable']}
            self.assertNotIn(basis_obj, unreach)

            # Restore cold-1 back to hot tier -> succeeds cleanly and tombstones/repacks cold-records/packs/!
            restore_rcpt = restore_archived_records(system, ids=['cold-1'], reason='restore from packed cold slab')
            self.assertEqual(restore_rcpt['restored_count'], 1)
            self.assertTrue(system.record_path('cold-1').is_file())
            self.assertFalse(archive.has('cold-1'))
            self.assertEqual(system.show('cold-1')['content']['title'], 'Cold 1')
            # Remaining packed cold records still read back cleanly!
            self.assertEqual(system.show('cold-0')['content']['title'], 'Cold 0')
            self.assertEqual(system.show('cold-2')['content']['title'], 'Cold 2')

    def test_pack_objects_deflate_only_incompressible_and_suboptimal_recompression(self) -> None:
        """C52-1 Repro A & B: deflate-only loose objects where len(.deflate) >= raw_len pack cleanly."""
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            # Repro A1 (compress=False): tiny incompressible deflate-only loose object (12B >= 4B).
            tiny_a = b'tiny'
            key_a = sha(tiny_a)
            def_a = zlib.compress(tiny_a, level=6)
            self.assertGreaterEqual(len(def_a), len(tiny_a))
            (system.ex.objects.directory / (key_a + '.deflate')).write_bytes(def_a)
            self.assertFalse((system.ex.objects.directory / key_a).exists())

            rcpt_a = execute_pack_objects(system, compress=False, reason='C52-1 repro A1 compress=False')
            self.assertGreaterEqual(rcpt_a['packed_objects'], 1)
            self.assertFalse((system.ex.objects.directory / (key_a + '.deflate')).exists())
            self.assertEqual(system.ex.objects.read(key_a), tiny_a)
            row_a = next(r for r in system.ex.objects.packed_entries() if r['object'] == key_a)
            self.assertEqual(row_a['occ'], OCC_LIVE_RAW)
            self.assertEqual(row_a['stored_bytes'], len(tiny_a))

            # Repro A2 (compress=True): tiny incompressible deflate-only loose object (11B >= 3B).
            tiny_b = b'abc'
            key_b = sha(tiny_b)
            def_b = zlib.compress(tiny_b, level=6)
            self.assertGreaterEqual(len(def_b), len(tiny_b))
            (system.ex.objects.directory / (key_b + '.deflate')).write_bytes(def_b)
            self.assertFalse((system.ex.objects.directory / key_b).exists())

            # Repro B (compress=True): compressible payload stored ONLY as level-0 .deflate (511B >= 500B).
            subopt_raw = b'A' * 500
            key_sub = sha(subopt_raw)
            def_sub = zlib.compress(subopt_raw, level=0)
            self.assertGreaterEqual(len(def_sub), len(subopt_raw))
            (system.ex.objects.directory / (key_sub + '.deflate')).write_bytes(def_sub)
            self.assertFalse((system.ex.objects.directory / key_sub).exists())

            rcpt_b = execute_pack_objects(system, compress=True, reason='C52-1 repro A2 + B compress=True')
            self.assertEqual(rcpt_b['packed_objects'], 2)
            self.assertFalse((system.ex.objects.directory / (key_b + '.deflate')).exists())
            self.assertFalse((system.ex.objects.directory / (key_sub + '.deflate')).exists())
            self.assertEqual(system.ex.objects.read(key_b), tiny_b)
            self.assertEqual(system.ex.objects.read(key_sub), subopt_raw)

            packed_map = {r['object']: r for r in system.ex.objects.packed_entries()}
            self.assertEqual(packed_map[key_b]['occ'], OCC_LIVE_RAW)
            self.assertEqual(packed_map[key_b]['stored_bytes'], len(tiny_b))
            self.assertEqual(packed_map[key_sub]['occ'], OCC_LIVE_DEFLATE)
            self.assertLess(packed_map[key_sub]['stored_bytes'], len(subopt_raw))

    def test_audit_objects_skips_occ_sealed_slots_and_reuses_cold_pack_session(self) -> None:
        """C52-O1: OCC_SEALED packed slots never audited/nominated; C52-O5: ColdArchive reuses one PackSession."""
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            # 1. C52-O1: create a shard containing one OCC_SEALED slot and one live orphan slot.
            sealed_raw = b'sealed-packed-payload-never-read-by-audit'
            orphan_raw = b'normal-live-orphan-payload'
            key_sealed = sha(sealed_raw)
            key_orphan = sha(orphan_raw)
            desc = write_shard(
                system.ex.objects.packs_dir,
                generation=1,
                shard_id=0,
                items=[
                    (bytes.fromhex(key_sealed), len(sealed_raw), OCC_SEALED, sealed_raw),
                    (bytes.fromhex(key_orphan), len(orphan_raw), OCC_LIVE_RAW, orphan_raw),
                ],
            )
            write_pack_current(system.ex.objects.packs_dir, 1, 1, [desc])

            audit_s = audit_objects(system)
            unreach_s = {r['object'] for r in audit_s['unreachable']}
            self.assertNotIn(key_sealed, unreach_s)
            self.assertIn(key_orphan, unreach_s)

            # 2. C52-O5: pack 3 cold records and prove audit_objects opens only ONE cold-archive PackSession.
            for idx in range(3):
                system.put({
                    'id': f'cold-sess-{idx}',
                    'kind': 'note',
                    'content': {'title': f'Cold Sess {idx}', 'body': 'shared session payload ' * 10},
                    'active': False,
                }, 0)
            plan = plan_archive_records(system, ids=['cold-sess-0', 'cold-sess-1', 'cold-sess-2'])
            execute_archive_records(system, plan=plan, expected_plan=plan['plan_sha256'], reason='archive 3')
            execute_pack_objects(system, include_cold_archive=True, reason='pack cold archive')

            cold_packs_dir = system.record_store.cold_archive.packs_dir
            cold_opens = 0
            real_pack_session = PackSession

            def tracking_session(packs_dir: Path):
                nonlocal cold_opens
                if Path(packs_dir) == cold_packs_dir:
                    cold_opens += 1
                return real_pack_session(packs_dir)

            with mock.patch('managed_ai.store.records.cold_archive.PackSession', side_effect=tracking_session):
                audit_objects(system)
            self.assertEqual(cold_opens, 1)

    def test_p8_pack_single_collect_pass_and_single_sha_on_packed_read(self) -> None:
        """P8-1: packed ObjectStore.read hashes once; P8-2: execute_pack_objects collects loose candidates once."""
        import managed_ai.core.storage as storage_mod
        import managed_ai.store.records.pack_slab as pack_slab_mod

        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            payload = b'p8-single-pass-payload-' + (b'P' * 256)
            key = system.ex.objects.put(payload)

            with mock.patch.object(
                pack_slab_mod,
                '_collect_loose_pack_candidates',
                wraps=pack_slab_mod._collect_loose_pack_candidates,
            ) as collect_spy:
                rcpt = execute_pack_objects(system, reason='P8-2 single collect pass check')
            self.assertGreaterEqual(rcpt['packed_objects'], 1)
            self.assertEqual(collect_spy.call_count, 1)

            # Unbatched and pack_session() packed reads verify SHA-256 exactly once inside read_frame().
            with mock.patch.object(storage_mod, 'sha', wraps=storage_mod.sha) as sha_spy:
                self.assertEqual(system.ex.objects.read(key), payload)
            self.assertEqual(sha_spy.call_count, 1)

            with system.ex.objects.pack_session():
                with mock.patch.object(storage_mod, 'sha', wraps=storage_mod.sha) as sha_spy2:
                    self.assertEqual(system.ex.objects.read(key), payload)
                self.assertEqual(sha_spy2.call_count, 1)

    def test_w9_pack_session_multi_object_callers_and_zero_handle_leaks(self) -> None:
        """Wave 9: explain_delivery, turn_retirement, compaction, record_transfer & composition share PackSession cleanly."""
        import managed_ai.core.storage as storage_mod
        from managed_ai.store.composition.composition import Composition
        from managed_ai.store.records.compaction import execute_compact_objects, plan_compact_objects
        from managed_ai.store.records.record_transfer import export as export_record, inspect as inspect_transfer, receive as receive_transfer
        from managed_ai.store.turns.turn_retirement import plan_turn_retirement

        system = System(self.store)
        peer_store = self.root / 'peer-store'
        System.initialize(peer_store, {'contribution': 'Peer instance for transfer test.'}, instance_id='w9-peer')
        peer = System(peer_store)

        with exclusive(peer.ex.root / 'owner.lock'):
            peer.put({'id': 'peer-note', 'kind': 'note', 'content': {'summary': 'Transferred claim'}}, 0)
            bundle_path = self.root / 'peer-note.transfer.json'
            export_record(peer, 'peer-note', bundle_path)

        with exclusive(system.ex.root / 'owner.lock'):
            # 1. Create a cross-instance observation so its object moves into a packed shard.
            receive_transfer(system, bundle_path, 'obs-peer', 'Receive peer note for Wave 9 test.', 0)

            # 2. Create a composition record whose basis is object-backed (object:<sha256>).
            ev_bytes = b'packed composition evidence payload\n'
            ev_sha = system.ex.objects.put(ev_bytes)
            comp_rec = {
                'id': 'comp-w9',
                'kind': 'design',
                'content': {
                    'format': 'g60-composition/1',
                    'purpose': 'Verify packed object basis in Composition',
                    'premises': [{
                        'id': 'p1',
                        'account': 'Packed basis resolves',
                        'basis': [f'object:{ev_sha}'],
                        'disposition': 'available',
                    }],
                    'opportunities': [],
                },
                'basis': [f'object:{ev_sha}'],
            }
            system.put(comp_rec, 0)

            # 2b. Create a wrapped information record so Information.inspect() reads pinned packed objects.
            system.put({'id': 'contract-w9', 'kind': 'content-contract', 'content': 'Contract definition'}, 0)
            system.put({'id': 'info-w9', 'kind': 'note', 'content': {'v': 1}}, 0)
            c_head = system.show('contract-w9')
            i_head = system.show('info-w9')
            from managed_ai.store.query.information import Information

            Information(system).wrap({
                'source': {'id': i_head['id'], 'revision': i_head['revision'], 'sha256': i_head['sha256']},
                'contract': {'id': c_head['id'], 'revision': c_head['revision'], 'sha256': c_head['sha256']},
                'account': 'Wrap for Wave 9 packed info test',
            })

            # 3. Create a prepared turn context so explain_delivery, packet inspection & turn_retirement read packed objects.
            system.context('turn-w9', {'task': 'Exercise packed turn recovery and explanation'})

            # Pack ALL existing loose objects into shard slabs (zero loose files remain).
            rcpt = execute_pack_objects(system, reason='Wave 9 pack all objects for multi-caller test')
            self.assertGreater(rcpt['packed_objects'], 0)
            self.assertEqual(system.ex.objects.entries(), [])

            # 4. Add one fresh compressible loose object to exercise compaction alongside active packs.
            fresh_loose = system.ex.objects.put(b'compressible-loose-for-compaction ' * 30)
            self.assertTrue((system.ex.objects.directory / fresh_loose).is_file())

            # Exercise every multi-object caller while counting PackSession opens and verifying zero handle leaks!
            with mock.patch.object(
                system.ex.objects,
                '_open_pack_session',
                wraps=system.ex.objects._open_pack_session,
            ) as open_spy:
                explained = system.explain_context('turn-w9')
                self.assertEqual(explained['packet'], 'turn-w9')
                self.assertEqual(open_spy.call_count, 1)

                open_spy.reset_mock()
                inspected_turn = system.ex.inspect('turn-w9')
                self.assertEqual(inspected_turn['packet'], 'turn-w9')
                self.assertEqual(open_spy.call_count, 1)

                open_spy.reset_mock()
                listed = system.ex.list()
                self.assertGreaterEqual(len(listed), 2)
                self.assertEqual(open_spy.call_count, 1)

                open_spy.reset_mock()
                ret_plan = plan_turn_retirement(system, turns=['turn-w9'])
                self.assertIn('ineligible', ret_plan)
                self.assertLessEqual(open_spy.call_count, 2)

                open_spy.reset_mock()
                obs_info = inspect_transfer(system, 'obs-peer')
                self.assertEqual(obs_info['source_record']['id'], 'peer-note')
                self.assertEqual(open_spy.call_count, 1)

                open_spy.reset_mock()
                comp_view = Composition(system).inspect('comp-w9')
                self.assertEqual(comp_view['premises'][0]['support']['sources'][0]['status'], 'current')
                self.assertEqual(open_spy.call_count, 1)

                open_spy.reset_mock()
                info_view = Information(system).inspect('info-w9')
                self.assertTrue(info_view['contracted'])
                self.assertEqual(open_spy.call_count, 1)

                open_spy.reset_mock()
                comp_head = system.show('comp-w9')
                batch_rcpt = system.put_batch({
                    'format': 'managed-ai-batch-update/1',
                    'id': 'w9-packed-batch',
                    'instance_id': system.instance_id,
                    'pin_predecessors': True,
                    'operations': [{
                        'id': 'comp-w9',
                        'expected_revision': comp_head['revision'],
                        'expected_sha256': comp_head['sha256'],
                        'mode': 'patch',
                        'set_active': True,
                    }],
                })
                self.assertEqual(len(batch_rcpt['records']), 1)
                self.assertLessEqual(open_spy.call_count, 2)

                open_spy.reset_mock()
                cplan = plan_compact_objects(system)
                self.assertEqual(cplan['candidate_count'], 2)
                self.assertEqual(open_spy.call_count, 1)

                open_spy.reset_mock()
                crcpt = execute_compact_objects(
                    system,
                    plan=cplan,
                    expected_plan=cplan['plan_sha256'],
                    reason='Compact loose objects alongside active packs',
                )
                self.assertEqual(crcpt['compacted_objects'], 2)
                self.assertEqual(open_spy.call_count, 1)

            # Verify zero active pack session or leaked Windows mmap handles after all calls!
            self.assertIsNone(system.ex.objects._active_pack_session)
            self.assertEqual(system.ex.objects._pack_session_depth, 0)

            # Verify exception inside pack_session() still closes deterministically (0 leaked handles).
            with self.assertRaisesRegex(RuntimeError, 'injected boom'):
                with system.ex.objects.pack_session():
                    self.assertIsNotNone(system.ex.objects._active_pack_session)
                    raise RuntimeError('injected boom')
            self.assertIsNone(system.ex.objects._active_pack_session)
            self.assertEqual(system.ex.objects._pack_session_depth, 0)

            # Prove zero open Windows mmap handles remain by unlinking the .slab file directly!
            cur = parse_pack_current(system.ex.objects.packs_dir)
            assert cur is not None
            slab_path = system.ex.objects.packs_dir / cur['shards'][0]['slab']
            slab_path.unlink()
            self.assertFalse(slab_path.exists())
            _ = storage_mod

    def _make_dir_link(self, link: Path, target: Path) -> bool:
        if os.name == 'nt':
            proc = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True)
            return proc.returncode == 0
        link.symlink_to(target)
        return True

    def _remove_dir_link(self, link: Path) -> None:
        from managed_ai.platform import is_reparse_or_symlink

        if not is_reparse_or_symlink(link):
            return
        if os.name == 'nt':
            subprocess.run(['cmd', '/c', 'rmdir', str(link)], capture_output=True)
        else:
            link.unlink(missing_ok=True)

    def test_qa9_1_packs_dir_junction_and_symlink_rejected(self) -> None:
        """QA9-1: NTFS junction (Win) / POSIX symlink (WSL) replacing packs/ dir is refused on ObjectStore & ColdArchive."""
        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            key = system.ex.objects.put(b'qa9-1-object-payload-' + (b'J' * 256))
            system.put({'id': 'qa9-keep', 'kind': 'note', 'content': {'k': key}}, 0)
            system.put({'id': 'qa9-cold', 'kind': 'note', 'content': {'v': 'cold'}, 'active': False}, 0)
            execute_pack_objects(system, reason='QA9-1 pack objects')

            plan = plan_archive_records(system, ids=['qa9-cold'])
            execute_archive_records(system, plan=plan, expected_plan=plan['plan_sha256'], reason='QA9-1 archive')
            system.record_store.cold_archive.pack_blobs()

            # Move real exchange/objects/packs outside store and replace with junction/symlink.
            real_obj_packs = system.ex.objects.packs_dir
            moved_obj_packs = self.root / 'external-obj-packs'
            real_obj_packs.rename(moved_obj_packs)
            self.assertTrue(self._make_dir_link(real_obj_packs, moved_obj_packs))
            self.addCleanup(self._remove_dir_link, real_obj_packs)

            with self.assertRaisesRegex(ValueError, 'ordinary packs directory required'):
                parse_pack_current(real_obj_packs)
            with self.assertRaisesRegex(ValueError, 'ordinary packs directory required'):
                PackSession(real_obj_packs)
            with self.assertRaisesRegex(ValueError, 'ordinary packs directory required'):
                system.ex.objects.has(key)
            with self.assertRaisesRegex(ValueError, 'ordinary packs directory required'):
                system.ex.objects.read(key)
            with self.assertRaisesRegex(ValueError, 'ordinary packs directory required'):
                with system.ex.objects.pack_session():
                    pass

            self._remove_dir_link(real_obj_packs)
            moved_obj_packs.rename(real_obj_packs)

            # Repeat for exchange/cold-records/packs.
            archive = system.record_store.cold_archive
            real_cold_packs = archive.packs_dir
            moved_cold_packs = self.root / 'external-cold-packs'
            real_cold_packs.rename(moved_cold_packs)
            self.assertTrue(self._make_dir_link(real_cold_packs, moved_cold_packs))
            self.addCleanup(self._remove_dir_link, real_cold_packs)

            with self.assertRaisesRegex(ValueError, 'ordinary packs directory required'):
                archive.read('qa9-cold')
            with self.assertRaisesRegex(ValueError, 'ordinary packs directory required'):
                with archive.pack_session():
                    pass

            self._remove_dir_link(real_cold_packs)
            moved_cold_packs.rename(real_cold_packs)

    def test_qa9_2_3_4_corrupt_occ_truncated_slab_and_missing_slab_bytes(self) -> None:
        """QA9-2/3/4: 0xFF .occ byte, truncated .slab, and missing slab_bytes raise ValueError (never struct.error/KeyError)."""
        from managed_ai.core.pack_reader import OCC_HEADER_STRUCT, PackShardHandle, SLAB_HEADER_STRUCT
        from managed_ai.store.records.pack_gc import _extract_verified_live_frames, gc_packed_objects

        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            keys: list[str] = []
            for idx in range(4):
                keys.append(system.ex.objects.put(f'qa9-payload-{idx}-'.encode('utf-8') + (b'X' * 1024)))
            execute_pack_objects(system, reason='QA9-2/3/4 pack')

            packs_dir = system.ex.objects.packs_dir
            cur = parse_pack_current(packs_dir)
            assert cur is not None
            orig_cur_bytes = (packs_dir / 'CURRENT.json').read_bytes()
            shard = cur['shards'][0]
            occ_path = packs_dir / shard['occ']
            slab_path = packs_dir / shard['slab']
            orig_occ_bytes = occ_path.read_bytes()
            orig_slab_bytes = slab_path.read_bytes()

            # QA9-2a: Flip slot 0 .occ byte to 0xFF (both mismatched AND re-signed occ_sha256 must fail!).
            bad_occ = bytearray(orig_occ_bytes)
            bad_occ[OCC_HEADER_STRUCT.size] = 0xFF
            occ_path.write_bytes(bytes(bad_occ))
            with self.assertRaisesRegex(ValueError, 'occ sha256 mismatch'):
                PackShardHandle(packs_dir, shard)
            with self.assertRaisesRegex(ValueError, 'occ sha256 mismatch'):
                system.ex.objects.has(keys[0])
            with self.assertRaisesRegex(ValueError, 'occ sha256 mismatch'):
                gc_packed_objects(packs_dir, {keys[-1]}, repack_threshold=0.95)

            # Even if CURRENT.json occ_sha256 is forged to match the 0xFF .occ file, state validation rejects it!
            forged = json.loads(orig_cur_bytes.decode('utf-8'))
            forged['shards'][0]['occ_sha256'] = sha(bytes(bad_occ))
            (packs_dir / 'CURRENT.json').write_bytes(encode(forged))
            with self.assertRaisesRegex(ValueError, 'corrupt pack occ state byte'):
                PackShardHandle(packs_dir, forged['shards'][0])
            with self.assertRaisesRegex(ValueError, 'corrupt pack occ state byte'):
                system.ex.objects.has(keys[0])
            with self.assertRaisesRegex(ValueError, 'corrupt pack occ state byte'):
                gc_packed_objects(packs_dir, {keys[-1]}, repack_threshold=0.95)

            # Restore clean .occ & CURRENT.json.
            occ_path.write_bytes(orig_occ_bytes)
            (packs_dir / 'CURRENT.json').write_bytes(orig_cur_bytes)

            # QA9-3: Mid-frame truncated .slab raises ValueError on open, has(), and repack (never struct.error!).
            slab_path.write_bytes(orig_slab_bytes[:SLAB_HEADER_STRUCT.size + 10])
            with self.assertRaisesRegex(ValueError, 'corrupt pack slab size'):
                PackShardHandle(packs_dir, shard)
            with self.assertRaisesRegex(ValueError, 'corrupt pack slab size'):
                system.ex.objects.has(keys[0])
            with self.assertRaisesRegex(ValueError, 'corrupt pack slab size'):
                gc_packed_objects(packs_dir, set(keys[:3]), repack_threshold=0.10)

            # Also verify _extract_verified_live_frames bounds check raises ValueError on out-of-range slab_off.
            slab_path.write_bytes(orig_slab_bytes)
            handle = PackShardHandle(packs_dir, shard)
            try:
                with self.assertRaisesRegex(ValueError, 'corrupt pack frame bounds'):
                    _extract_verified_live_frames(
                        handle,
                        [(bytes.fromhex(keys[0]), handle._slab_size - 4, 32, 32, OCC_LIVE_RAW)],
                    )
            finally:
                handle.close()

            # QA9-4: Missing or invalid slab_bytes in CURRENT.json raises ValueError (never KeyError!).
            no_slab_bytes = json.loads(orig_cur_bytes.decode('utf-8'))
            del no_slab_bytes['shards'][0]['slab_bytes']
            (packs_dir / 'CURRENT.json').write_bytes(encode(no_slab_bytes))
            with self.assertRaisesRegex(ValueError, 'slab_bytes'):
                parse_pack_current(packs_dir)
            with self.assertRaisesRegex(ValueError, 'slab_bytes'):
                gc_packed_objects(packs_dir, set(keys))

            bad_slab_bytes = json.loads(orig_cur_bytes.decode('utf-8'))
            bad_slab_bytes['shards'][0]['slab_bytes'] = SLAB_HEADER_STRUCT.size - 1
            (packs_dir / 'CURRENT.json').write_bytes(encode(bad_slab_bytes))
            with self.assertRaisesRegex(ValueError, 'slab_bytes'):
                parse_pack_current(packs_dir)

            (packs_dir / 'CURRENT.json').write_bytes(orig_cur_bytes)

    def test_p9_1_2_concurrent_exchange_list_explain_single_read_and_cold_restore_session(self) -> None:
        """P9-1/P9-2/P9-8: concurrent packed Exchange.list() is thread-safe, explain reads each object once, cold restore batches."""
        from concurrent.futures import ThreadPoolExecutor

        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            for idx in range(6):
                system.put({'id': f'p9-note-{idx}', 'kind': 'note', 'content': {'i': idx, 'pad': 'N' * 300}}, 0)
            for idx in range(4):
                system.context(f'p9-turn-{idx}', {
                    'task': f'Concurrent packed list turn {idx}',
                    'records': [f'p9-note-{idx}'],
                })

            # Archive 3 inactive records and pack both object store & cold archive into small multi-shards.
            for idx in range(3):
                system.put({'id': f'p9-cold-{idx}', 'kind': 'note', 'content': {'c': idx}, 'active': False}, 0)
            cplan = plan_archive_records(system, ids=[f'p9-cold-{idx}' for idx in range(3)])
            execute_archive_records(system, plan=cplan, expected_plan=cplan['plan_sha256'], reason='P9-8 cold pack')
            system.record_store.cold_archive.pack_blobs(shard_target_bytes=4096)

            rcpt = execute_pack_objects(system, shard_target_bytes=4096, reason='P9-1 multi-shard pack')
            self.assertGreaterEqual(rcpt['new_shards'], 2)

            # P9-1: Concurrent Exchange.list() across 4 threads (each fanning out internally across ThreadPoolExecutor).
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: system.ex.list(), range(8)))
            for listed in results:
                names = {row['packet'] for row in listed}
                for idx in range(4):
                    self.assertIn(f'p9-turn-{idx}', names)
            self.assertIsNone(system.ex.objects._active_pack_session)
            self.assertEqual(system.ex.objects._pack_session_depth, 0)

            # P9-2: _verify_packet_entries reads each selected record object once (no duplicate L65/L72 read:
            # 1 read in exchange.load() + 1 read in _verify_packet_entries() == same count as brief, never 3!).
            read_counts: dict[str, int] = {}
            orig_read = system.ex.objects.read

            def counting_read(key: str, **kwargs):
                read_counts[key] = read_counts.get(key, 0) + 1
                return orig_read(key, **kwargs)

            with mock.patch.object(system.ex.objects, 'read', side_effect=counting_read):
                explained = system.explain_context('p9-turn-0')
            brief_unit = next(u for u in explained['units'] if u['role'] == 'brief')
            rec_unit = next(u for u in explained['units'] if u['path'] == 'records/p9-note-0.json')
            self.assertEqual(read_counts.get(rec_unit['sha256']), read_counts.get(brief_unit['sha256']))
            self.assertEqual(read_counts.get(rec_unit['sha256']), 2)

            # P9-8: restore_archived_records shares ONE ColdArchive PackSession across all 3 packed cold records!
            archive = system.record_store.cold_archive
            cold_opens = 0
            real_session = PackSession

            def counting_cold_session(packs_dir: Path, **kwargs):
                nonlocal cold_opens
                if Path(packs_dir) == archive.packs_dir:
                    cold_opens += 1
                return real_session(packs_dir, **kwargs)

            with mock.patch('managed_ai.store.records.cold_archive.PackSession', side_effect=counting_cold_session):
                restored = restore_archived_records(
                    system,
                    ids=[f'p9-cold-{idx}' for idx in range(3)],
                    reason='P9-8 batched cold restore',
                )
            self.assertEqual(restored['restored_count'], 3)
            self.assertEqual(cold_opens, 1)
            self.assertIsNone(archive._active_pack_session)
            self.assertEqual(archive._pack_session_depth, 0)

    def test_c58_1_overlapping_threads_cold_archive_pack_session_closes_cleanly(self) -> None:
        """C58-1: overlapping 2-thread ColdArchive.pack_session() closes & clears session when last thread exits."""
        import threading

        system = System(self.store)
        with exclusive(system.ex.root / 'owner.lock'):
            system.put({'id': 'c58-cold', 'kind': 'note', 'content': {'v': 'overlap'}, 'active': False}, 0)
            plan = plan_archive_records(system, ids=['c58-cold'])
            execute_archive_records(system, plan=plan, expected_plan=plan['plan_sha256'], reason='C58-1 pack')
            archive = system.record_store.cold_archive
            archive.pack_blobs()

            a_entered = threading.Event()
            b_entered = threading.Event()
            a_exited = threading.Event()
            sessions: list[PackSession | None] = []
            errors: list[BaseException] = []

            def thread_a() -> None:
                try:
                    with archive.pack_session() as sess:
                        sessions.append(sess)
                        a_entered.set()
                        assert b_entered.wait(timeout=5.0)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    a_exited.set()

            def thread_b() -> None:
                try:
                    assert a_entered.wait(timeout=5.0)
                    with archive.pack_session() as sess:
                        sessions.append(sess)
                        b_entered.set()
                        assert a_exited.wait(timeout=5.0)
                        rec, _ = archive.read('c58-cold')
                        self.assertEqual(rec['id'], 'c58-cold')
                except BaseException as exc:
                    errors.append(exc)

            t_a = threading.Thread(target=thread_a)
            t_b = threading.Thread(target=thread_b)
            t_a.start()
            t_b.start()
            t_a.join(timeout=10.0)
            t_b.join(timeout=10.0)

            self.assertEqual(errors, [])
            self.assertEqual(len(sessions), 2)
            self.assertIsNotNone(sessions[0])
            self.assertIs(sessions[0], sessions[1])
            self.assertEqual(archive._pack_session_depth, 0)
            self.assertIsNone(archive._active_pack_session)
            self.assertTrue(sessions[0]._closed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
