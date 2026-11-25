"""The release gate runs the requested engine even under a cross-product test environment."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


REPO = Path(__file__).resolve().parents[3]
ENGINE = REPO / 'engine'


class ReleaseAcceptance(unittest.TestCase):
    def test_explicit_product_supplies_the_imported_implementation(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_repo = root / 'candidate'
            inherited_repo = root / 'inherited'
            for repo, label in ((candidate_repo, 'candidate'), (inherited_repo, 'inherited')):
                store = repo / 'engine/store'
                store.mkdir(parents=True)
                (store / '__init__.py').write_text('', encoding='utf-8')
                (store / 'edition.py').write_text(f'NAME = {label!r}\n', encoding='utf-8')
            suites = candidate_repo / 'tests/engine/store'
            suites.mkdir(parents=True)
            program = suites / 'test_shared_instances.py'
            program.write_text(
                'import os, sys\n'
                'sys.path.insert(0, os.environ["G60_TEST_PRODUCT"])\n'
                'from store.edition import NAME\n'
                'print(NAME)\n',
                encoding='utf-8',
            )
            env = dict(os.environ, G60_TEST_PRODUCT=str(inherited_repo / 'engine'))
            standalone = subprocess.check_output([sys.executable, '-B', str(program)], env=env, text=True)
            self.assertEqual(standalone.strip(), 'inherited')
            subprocess.run(
                [sys.executable, '-B', str(ENGINE / 'release/check_current.py'),
                 '--product', str(candidate_repo / 'engine'), '--records', str(root)],
                env=env,
                check=True,
                capture_output=True,
            )
            report = json.loads((root / 'ACCEPTANCE.json').read_bytes())
            self.assertEqual(report['stdout'].strip(), 'candidate')
            self.assertEqual(report['program'], str(program))
            self.assertEqual(report['product'], str(candidate_repo / 'engine'))


if __name__ == '__main__':
    unittest.main()
