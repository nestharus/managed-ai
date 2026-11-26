"""Regression tests for optional/collective hardening (W4-B4, W4-B6)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / 'src'
COLLECTIVE_DIR = SRC_ROOT / 'managed_ai/optional/collective'
MANAGE_CLI = COLLECTIVE_DIR / 'manage_collective.py'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from managed_ai.optional.collective.collective_store import contract, database


class CollectiveHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / 'work.sqlite'

    def run_cli(self, *args, db=None):
        target_db = self.db_path if db is None else db
        proc = subprocess.run(
            [sys.executable, '-B', str(MANAGE_CLI), '--db', str(target_db), *args],
            capture_output=True, text=True, encoding='utf-8',
        )
        return proc.returncode, json.loads(proc.stdout), proc.stderr

    def test_refuses_database_inside_optional_collective_and_engine(self):
        for forbidden in (
            COLLECTIVE_DIR / 'forbidden.sqlite',
            SRC_ROOT / 'managed_ai/forbidden.sqlite',
            SRC_ROOT / 'managed_ai/host/forbidden.sqlite',
        ):
            with self.subTest(forbidden=str(forbidden)):
                self.assertTrue(database.inside_product(forbidden, COLLECTIVE_DIR))
                code, out, _ = self.run_cli('init', db=forbidden)
                self.assertEqual(code, 3)
                self.assertEqual(out['status'], 'unresolved')
                self.assertFalse(forbidden.exists())

    def test_refuses_directory_and_symlink_database_and_sidecars(self):
        dir_db = self.root / 'not-a-file'
        dir_db.mkdir()
        code, out, _ = self.run_cli('init', db=dir_db)
        self.assertEqual(code, 3)
        self.assertEqual(out['status'], 'unresolved')

        if os.name == 'nt':
            probe = self.root / 'probe_link'
            try:
                probe.symlink_to(self.root / 'probe_target')
            except OSError:
                return
            probe.unlink()

        target = self.root / 'real.sqlite'
        target.write_bytes(b'')
        link_db = self.root / 'link.sqlite'
        link_db.symlink_to(target)
        code, out, _ = self.run_cli('init', db=link_db)
        self.assertEqual(code, 3)
        self.assertIn('symlink', out['error'].lower())

        sidecar_db = self.root / 'sidecar.sqlite'
        wal_link = Path(str(sidecar_db) + '-wal')
        wal_link.symlink_to(target)
        code, out, _ = self.run_cli('init', db=sidecar_db)
        self.assertEqual(code, 3)
        self.assertIn('symlink', out['error'].lower())

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON key'):
            contract.loads_strict('{"a": 1, "a": 2}')
        with self.assertRaisesRegex(ValueError, 'Nonfinite JSON'):
            contract.loads_strict('{"a": NaN}')
        with self.assertRaisesRegex(ValueError, 'Nonfinite JSON'):
            contract.loads_strict('{"a": Infinity}')

        code, _, _ = self.run_cli('init')
        self.assertEqual(code, 0)
        dup_file = self.root / 'dup.json'
        dup_file.write_text('{"id":"a","id":"b"}\n', encoding='utf-8')
        code, out, _ = self.run_cli('call', '--request', str(dup_file))
        self.assertEqual(code, 3)
        self.assertIn('Duplicate JSON key', out['error'])

        nan_file = self.root / 'nan.json'
        nan_file.write_text('{"score":NaN}\n', encoding='utf-8')
        code, out, _ = self.run_cli('call', '--request', str(nan_file))
        self.assertEqual(code, 3)
        self.assertIn('Nonfinite JSON', out['error'])

    def test_utf8_roundtrip_external_store(self):
        code, out, _ = self.run_cli('init')
        self.assertEqual(code, 0)
        self.assertEqual(out['database'], str(self.db_path))
        code, changes, _ = self.run_cli('changes')
        self.assertEqual(code, 0)
        self.assertTrue(changes['complete'])

    # --- C28-3: Ancestor symlink/NTFS-junction refusal on --db and --request ---
    def test_refuses_symlink_or_junction_ancestor_directories_for_db_and_request(self):
        real_dir = self.root / 'real-dir'
        real_dir.mkdir()
        req_file = real_dir / 'req.json'
        req_file.write_text('{"op":"noop"}\n', encoding='utf-8')
        alias_dir = self.root / 'alias-dir'
        is_nt_junc = False
        if os.name == 'nt':
            proc = subprocess.run(
                ['cmd', '/c', 'mklink', '/J', str(alias_dir), str(real_dir)],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                return
            is_nt_junc = True
        else:
            alias_dir.symlink_to(real_dir)

        try:
            # --db inside a symlink/junction directory must be refused without creating a file!
            code, out, _ = self.run_cli('init', db=alias_dir / 'escaped.sqlite')
            self.assertEqual(code, 3)
            self.assertIn('symlink', out['error'].lower())
            self.assertFalse((real_dir / 'escaped.sqlite').exists())

            # --request inside a symlink/junction directory must also be refused!
            code_init, _, _ = self.run_cli('init')
            self.assertEqual(code_init, 0)
            code_req, out_req, _ = self.run_cli('call', '--request', str(alias_dir / 'req.json'))
            self.assertEqual(code_req, 3)
            self.assertIn('symlink', out_req['error'].lower())
        finally:
            if is_nt_junc:
                subprocess.run(['cmd', '/c', 'rmdir', str(alias_dir)], capture_output=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
