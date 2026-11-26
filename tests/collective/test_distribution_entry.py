"""The enclosing owner supplies its actual data boundary without changing the component."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ENCLOSING_ENTRY = """from pathlib import Path
import sys

PRODUCT = Path(__file__).resolve().parent
sys.path.insert(0, str(PRODUCT))

from collective.store.cli import main

leaked = [
    m for m in sys.modules
    if m == 'managed_ai_information' or m.startswith('managed_ai_information.')
]
assert not leaked, f'Unexpected managed_ai_information imports: {leaked}'
raise SystemExit(main(product=PRODUCT))
"""


class DistributionEntry(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.product = self.base / "renamed-product"
        self.component = self.product / "collective"
        self.component.mkdir(parents=True)
        source = Path(__file__).resolve().parents[2] / "src/managed_ai_information/collective"
        shutil.copy2(source / "__init__.py", self.component / "__init__.py")
        shutil.copy2(source / "__main__.py", self.component / "__main__.py")
        shutil.copytree(source / "store", self.component / "store")
        self.enclosing_entry = self.product / "collective_entry.py"
        self.enclosing_entry.write_text(_ENCLOSING_ENTRY, encoding="utf-8", newline="\n")

    def call(self, entry, database, *operation, flag="--db"):
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(entry), flag, str(database), *operation],
            capture_output=True,
            text=True,
        )
        return result.returncode, json.loads(result.stdout)

    def test_refuse_all_internal_placements_before_creation(self):
        for path in [self.component / "state.sqlite", self.product / "runtime" / "state.sqlite"]:
            with self.subTest(path=path):
                code, result = self.call(self.enclosing_entry, path, "init")
                self.assertEqual(code, 3)
                self.assertEqual(result["status"], "unresolved")
                self.assertFalse(path.exists())
        self.assertFalse((self.product / "runtime").exists())
        short_result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(self.enclosing_entry),
                "--d",
                str(self.product / "short-option.sqlite"),
                "init",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(short_result.returncode, 2)
        self.assertIn("--db", short_result.stderr)
        self.assertFalse((self.product / "short-option.sqlite").exists())

    def test_external_initialization_and_later_read_remain_available(self):
        path = self.base / "owned-runtime" / "collective.sqlite"
        code, result = self.call(self.enclosing_entry, path, "init")
        self.assertEqual(code, 0)
        self.assertEqual(result["database"], str(path))
        code, result = self.call(self.enclosing_entry, path, "changes")
        self.assertEqual(code, 0)
        self.assertEqual(result["items"], [])
        self.assertTrue(result["complete"])

    def test_opening_also_obeys_enclosing_boundary(self):
        path = self.product / "existing.sqlite"
        code, _ = self.call(self.component / "__main__.py", path, "init")
        self.assertEqual(code, 0)
        before = path.read_bytes()
        code, result = self.call(self.enclosing_entry, path, "changes")
        self.assertEqual(code, 3)
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
