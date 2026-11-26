"""Verify the distribution built from the engine/core package source."""
from importlib.util import find_spec
from pathlib import Path
import json
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


REPO = Path(__file__).resolve().parents[3]
CORE = REPO / 'engine/core'


class ContinuationWheel(unittest.TestCase):
    @unittest.skipUnless(find_spec('setuptools') and find_spec('wheel'), 'requires the wheel build tools')
    def test_wheel_version_and_console_targets(self):
        with TemporaryDirectory() as tmp:
            build = Path(tmp) / 'core-build'
            shutil.copytree(CORE, build, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
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
import core
from importlib.metadata import distribution
dist = distribution('g60-continuation-support')
commands = [ep.name for ep in dist.entry_points if ep.group == 'console_scripts' and callable(ep.load())]
print(json.dumps([dist.version, core.__version__, sorted(commands)]))
"""
            raw = subprocess.check_output([sys.executable, '-I', '-c', probe, str(wheel)], cwd=build)
            metadata, imported, commands = json.loads(raw)
            self.assertEqual(metadata, imported)
            self.assertEqual(wheel.name, f'g60_continuation_support-{imported}-py3-none-any.whl')
            self.assertEqual(commands, ['g60-attend', 'g60-collect', 'g60-work'])


if __name__ == '__main__':
    unittest.main()
