"""Dual-OS tests for Wave 8 Phase 8B: Reserved append-only shard slabs, pack-objects CLI, logical/physical GC & ColdArchive."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

if __package__:
    from .native_test_paths import ENGINE
else:
    from native_test_paths import ENGINE

from managed_ai.core.storage import (
    OCC_LIVE_DEFLATE,
    OCC_LIVE_RAW,
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
