"""Unit tests for core.storage ObjectStore hybrid loose + reserved shard slab reader."""
from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
import zlib

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from tests._paths import SRC as _SRC

_ = _SRC

from managed_ai.core.storage import (
    MAX_BYTES,
    OCC_LIVE_DEFLATE,
    OCC_LIVE_RAW,
    OCC_SEALED,
    ObjectStore,
    PackSession,
    parse_pack_current,
    sha,
)
from managed_ai.store.records.pack_slab import write_pack_current, write_shard


class CoreStoragePackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix='managed-ai-core-storage-')
        self.addCleanup(self.tmp.cleanup)
        self.obj_dir = Path(self.tmp.name) / 'objects'
        self.obj_dir.mkdir()
        self.store = ObjectStore(self.obj_dir)

    def test_pack_session_lookup_crc_tamper_and_sealed_protection(self) -> None:
        raw_a = b'raw-object-payload-alpha'
        raw_b = b'deflate-object-payload-beta ' * 100
        comp_b = zlib.compress(raw_b, level=6)
        raw_sealed = b'sealed-body-do-not-open'

        key_a = sha(raw_a)
        key_b = sha(raw_b)
        key_s = sha(raw_sealed)

        desc = write_shard(
            self.store.packs_dir,
            generation=1,
            shard_id=0,
            items=[
                (bytes.fromhex(key_a), len(raw_a), OCC_LIVE_RAW, raw_a),
                (bytes.fromhex(key_b), len(raw_b), OCC_LIVE_DEFLATE, comp_b),
                (bytes.fromhex(key_s), len(raw_sealed), OCC_SEALED, raw_sealed),
            ],
        )
        write_pack_current(self.store.packs_dir, 1, 1, [desc])

        # Loose entries() skips ordinary packs/ dir without raising.
        self.assertEqual(self.store.entries(), [])
        self.assertTrue(self.store.has(key_a))
        self.assertTrue(self.store.has(key_b))
        self.assertEqual(self.store.read(key_a), raw_a)
        self.assertEqual(self.store.read(key_b), raw_b)

        # Sealed slot refuses direct read().
        with self.assertRaisesRegex(ValueError, 'sealed packed object'):
            self.store.read(key_s)

        # Sequential verified scan yields non-sealed live objects in slab offset order.
        with self.store.pack_session() as sess:
            scanned = dict(sess.iter_verified_sequential())
            self.assertEqual(scanned, {key_a: raw_a, key_b: raw_b})

        # Corrupt 1 payload byte in .slab -> CRC32 / digest tamper caught cleanly!
        slab_path = self.store.packs_dir / desc['slab']
        orig_slab = slab_path.read_bytes()
        tampered = bytearray(orig_slab)
        tampered[64 + 48 + 2] ^= 0xFF
        slab_path.write_bytes(bytes(tampered))
        with self.assertRaisesRegex(ValueError, 'corrupt pack frame'):
            self.store.read(key_a)
        slab_path.write_bytes(orig_slab)

        # Pointer path traversal rejected by parse_pack_current.
        ptr_path = self.store.packs_dir / 'CURRENT.json'
        orig_ptr = ptr_path.read_bytes()
        ptr_path.write_bytes(orig_ptr.replace(b'pack-000001-0000.slab', b'../escape.slab'))
        with self.assertRaisesRegex(ValueError, 'invalid pack slab filename'):
            parse_pack_current(self.store.packs_dir)
        ptr_path.write_bytes(orig_ptr)

    def test_packed_zip_bomb_respects_max_bytes_unless_retained(self) -> None:
        bomb = b'Z' * (MAX_BYTES + 4096)
        bomb_key = sha(bomb)
        comp = zlib.compress(bomb, level=9)
        desc = write_shard(
            self.store.packs_dir,
            generation=1,
            shard_id=0,
            items=[(bytes.fromhex(bomb_key), len(bomb), OCC_LIVE_DEFLATE, comp)],
        )
        write_pack_current(self.store.packs_dir, 1, 1, [desc])

        with self.assertRaisesRegex(ValueError, 'file exceeds'):
            self.store.read(bomb_key, retained=False)
        self.assertEqual(len(self.store.read(bomb_key, retained=True, expected_bytes=len(bomb))), len(bomb))


if __name__ == '__main__':
    unittest.main(verbosity=2)
