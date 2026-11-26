"""Verify the PEP-517 wheel distribution built from the root pyproject.toml and src/managed_ai."""
from importlib.util import find_spec
from pathlib import Path
import json
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


REPO = Path(__file__).resolve().parents[2]


class PackageWheelTest(unittest.TestCase):
    @unittest.skipUnless(find_spec('setuptools') and find_spec('wheel'), 'requires the wheel build tools')
    def test_wheel_version_package_data_and_console_targets(self):
        with TemporaryDirectory() as tmp:
            build = Path(tmp) / 'managed-ai-build'
            build.mkdir()
            for filename in ('pyproject.toml', 'LICENSE', 'README.md'):
                shutil.copy2(REPO / filename, build / filename)
            shutil.copytree(
                REPO / 'src',
                build / 'src',
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'),
            )
            subprocess.run(
                [sys.executable, '-B', '-c',
                 "from setuptools.build_meta import build_wheel; build_wheel('.')"],
                cwd=build,
                check=True,
                capture_output=True,
            )
            wheel, = build.glob('*.whl')
            probe = """
import json, sys
sys.path.insert(0, sys.argv[1])
import managed_ai
from importlib.metadata import distribution
dist = distribution('managed-ai')
commands = [ep.name for ep in dist.entry_points if ep.group == 'console_scripts' and callable(ep.load())]
print(json.dumps([dist.version, managed_ai.__version__, sorted(commands)]))
"""
            raw = subprocess.check_output([sys.executable, '-I', '-c', probe, str(wheel)], cwd=build)
            metadata, imported, commands = json.loads(raw)
            self.assertEqual(metadata, imported)
            self.assertEqual(wheel.name, f'managed_ai-{imported}-py3-none-any.whl')
            self.assertEqual(
                commands,
                ['g60-attend', 'g60-collect', 'g60-work', 'managed-ai', 'managed-ai-deploy', 'managed-ai-library'],
            )


if __name__ == '__main__':
    unittest.main()
