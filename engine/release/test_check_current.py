"""The release gate runs the requested runtime even under a cross-product test environment."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ENGINE = Path(__file__).resolve().parents[1]


class ReleaseAcceptance(unittest.TestCase):
    def test_explicit_product_supplies_the_imported_implementation(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / 'candidate'
            for edition in (candidate, root / 'inherited'):
                native = edition / 'runtime/native'
                native.mkdir(parents=True)
                (native / 'edition.py').write_text(f'NAME = {edition.name!r}\n')
            suites = candidate / 'runtime/tests/native_store'
            suites.mkdir(parents=True)
            shutil.copyfile(ENGINE / 'runtime/tests/native_store/native_test_paths.py',
                            suites / 'native_test_paths.py')
            program = suites / 'test_shared_instances.py'
            program.write_text('import native_test_paths\nfrom edition import NAME\nprint(NAME)\n')
            env = dict(os.environ, G60_TEST_PRODUCT=str(root / 'inherited/runtime'))
            standalone = subprocess.check_output([sys.executable, '-B', program], env=env, text=True)
            self.assertEqual(standalone.strip(), 'inherited')
            subprocess.run([sys.executable, '-B', ENGINE / 'release/check_current.py',
                            '--product', candidate, '--records', root], env=env,
                           check=True, capture_output=True)
            report = json.loads((root / 'ACCEPTANCE.json').read_bytes())
            self.assertEqual(report['stdout'].strip(), 'candidate')
            self.assertEqual(report['program'], str(program))
            self.assertEqual(report['runtime'], str(candidate / 'runtime'))
