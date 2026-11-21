"""The enclosing owner supplies its actual data boundary without changing the component."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest


class DistributionEntry(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.product = self.base / 'renamed-product'
        self.component = self.product / 'collective-work'
        self.component.mkdir(parents=True)
        source = Path(__file__).resolve().parent
        shutil.copy2(source / 'collective.py', self.component / 'collective.py')
        shutil.copytree(source / 'collective_store', self.component / 'collective_store')
        shutil.copy2(source.parent / 'collective.py', self.product / 'collective.py')

    def call(self, entry, database, *operation, flag='--db'):
        result = subprocess.run([sys.executable, '-B', str(entry), flag, str(database), *operation], capture_output=True, text=True)
        return result.returncode, json.loads(result.stdout)

    def test_refuse_all_internal_placements_before_creation(self):
        for path in [self.component / 'state.sqlite', self.product / 'runtime' / 'state.sqlite']:
            with self.subTest(path=path):
                code, result = self.call(self.product / 'collective.py', path, 'init')
                self.assertEqual(code, 3)
                self.assertEqual(result['status'], 'unresolved')
                self.assertFalse(path.exists())
        self.assertFalse((self.product / 'runtime').exists())
        code, _ = self.call(self.product / 'collective.py', self.product / 'short-option.sqlite', 'init', flag='--d')
        self.assertEqual(code, 3)
        self.assertFalse((self.product / 'short-option.sqlite').exists())

    def test_external_initialization_and_later_read_remain_available(self):
        path = self.base / 'owned-runtime' / 'collective.sqlite'
        code, result = self.call(self.product / 'collective.py', path, 'init')
        self.assertEqual(code, 0)
        self.assertEqual(result['database'], str(path))
        code, result = self.call(self.product / 'collective.py', path, 'changes')
        self.assertEqual(code, 0)
        self.assertEqual(result['items'], [])
        self.assertTrue(result['complete'])

    def test_opening_also_obeys_enclosing_boundary(self):
        path = self.product / 'existing.sqlite'
        code, _ = self.call(self.component / 'collective.py', path, 'init')
        self.assertEqual(code, 0)
        before = path.read_bytes()
        code, result = self.call(self.product / 'collective.py', path, 'changes')
        self.assertEqual(code, 3)
        self.assertEqual(result['status'], 'unresolved')
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
