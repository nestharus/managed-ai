"""Unit tests for core.storage ObjectStore hybrid loose + reserved shard slab reader."""

from __future__ import annotations

import struct
import sys
import unittest
import zlib
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from tests._paths import SRC as _SRC  # noqa: E402

_ = _SRC

from managed_ai_information.core.storage import (  # noqa: E402
    IDX_FANOUT_OFFSET,
    MAX_BYTES,
    OCC_LIVE_DEFLATE,
    OCC_LIVE_RAW,
    OCC_SEALED,
    ObjectStore,
    PackSession,
    parse_pack_current,
    sha,
)
from managed_ai_information.store.records.pack_slab import write_pack_current, write_shard  # noqa: E402


class CoreStoragePackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory(prefix="managed-ai-core-storage-")
        self.addCleanup(self.tmp.cleanup)
        self.obj_dir = Path(self.tmp.name) / "objects"
        self.obj_dir.mkdir()
        self.store = ObjectStore(self.obj_dir)

    def test_pack_session_lookup_crc_tamper_and_sealed_protection(self) -> None:
        raw_a = b"raw-object-payload-alpha"
        raw_b = b"deflate-object-payload-beta " * 100
        comp_b = zlib.compress(raw_b, level=6)
        raw_sealed = b"sealed-body-do-not-open"

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
        with self.assertRaisesRegex(ValueError, "sealed packed object"):
            self.store.read(key_s)

        # Sequential verified scan yields non-sealed live objects in slab offset order.
        with self.store.pack_session() as sess:
            scanned = dict(sess.iter_verified_sequential())
            self.assertEqual(scanned, {key_a: raw_a, key_b: raw_b})

        # Corrupt 1 payload byte in .slab -> CRC32 / digest tamper caught cleanly!
        slab_path = self.store.packs_dir / desc["slab"]
        orig_slab = slab_path.read_bytes()
        tampered = bytearray(orig_slab)
        tampered[64 + 48 + 2] ^= 0xFF
        slab_path.write_bytes(bytes(tampered))
        with self.assertRaisesRegex(ValueError, "corrupt pack frame"):
            self.store.read(key_a)
        slab_path.write_bytes(orig_slab)

        # Pointer path traversal rejected by parse_pack_current.
        ptr_path = self.store.packs_dir / "CURRENT.json"
        orig_ptr = ptr_path.read_bytes()
        ptr_path.write_bytes(orig_ptr.replace(b"pack-000001-0000.slab", b"../escape.slab"))
        with self.assertRaisesRegex(ValueError, "invalid pack slab filename"):
            parse_pack_current(self.store.packs_dir)
        ptr_path.write_bytes(orig_ptr)

    def test_packed_zip_bomb_respects_max_bytes_unless_retained(self) -> None:
        bomb = b"Z" * (MAX_BYTES + 4096)
        bomb_key = sha(bomb)
        comp = zlib.compress(bomb, level=9)
        desc = write_shard(
            self.store.packs_dir,
            generation=1,
            shard_id=0,
            items=[(bytes.fromhex(bomb_key), len(bomb), OCC_LIVE_DEFLATE, comp)],
        )
        write_pack_current(self.store.packs_dir, 1, 1, [desc])

        with self.assertRaisesRegex(ValueError, "file exceeds"):
            self.store.read(bomb_key, retained=False)
        self.assertEqual(
            len(self.store.read(bomb_key, retained=True, expected_bytes=len(bomb))), len(bomb)
        )

    def test_pack_idx_corrupt_fanout_rejected_on_open(self) -> None:
        """C52-2: corrupt .idx fanout (> object_count, non-monotonic, or tail != object_count) fails
        closed."""
        raw_1 = b"fanout-object-one"
        raw_2 = b"fanout-object-two"
        key_1 = sha(raw_1)
        key_2 = sha(raw_2)
        desc = write_shard(
            self.store.packs_dir,
            generation=1,
            shard_id=0,
            items=[
                (bytes.fromhex(key_1), len(raw_1), OCC_LIVE_RAW, raw_1),
                (bytes.fromhex(key_2), len(raw_2), OCC_LIVE_RAW, raw_2),
            ],
        )
        write_pack_current(self.store.packs_dir, 1, 1, [desc])
        self.assertTrue(self.store.has(key_1))
        self.assertTrue(self.store.has(key_2))

        idx_path = self.store.packs_dir / desc["idx"]
        orig_idx = idx_path.read_bytes()

        # Case A: fanout bucket exceeds object_count (would cause struct.error in binary search if
        # unchecked).
        tampered_a = bytearray(orig_idx)
        first_byte = bytes.fromhex(key_1)[0]
        struct.pack_into("<I", tampered_a, IDX_FANOUT_OFFSET + first_byte * 4, 999)
        idx_path.write_bytes(bytes(tampered_a))
        with self.assertRaisesRegex(ValueError, "corrupt pack idx fanout"):
            self.store.has(key_1)
        with self.assertRaisesRegex(ValueError, "corrupt pack idx fanout"):
            PackSession(self.store.packs_dir)

        # Case B: non-monotonic fanout (fanout[0] = 2 > fanout[1] = 1, even though tail == 2).
        tampered_b = bytearray(orig_idx)
        struct.pack_into("<II", tampered_b, IDX_FANOUT_OFFSET, 2, 1)
        struct.pack_into("<I", tampered_b, IDX_FANOUT_OFFSET + 255 * 4, 2)
        idx_path.write_bytes(bytes(tampered_b))
        with self.assertRaisesRegex(ValueError, "corrupt pack idx fanout"):
            PackSession(self.store.packs_dir)

        # Case C: monotonic but final fanout[255] != object_count (1 != 2).
        tampered_c = bytearray(orig_idx)
        for bucket in range(256):
            struct.pack_into("<I", tampered_c, IDX_FANOUT_OFFSET + bucket * 4, 1)
        idx_path.write_bytes(bytes(tampered_c))
        with self.assertRaisesRegex(ValueError, "corrupt pack idx fanout"):
            PackSession(self.store.packs_dir)

        idx_path.write_bytes(orig_idx)
        self.assertTrue(self.store.has(key_1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
