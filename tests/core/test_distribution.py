"""Checks for the source-distribution interface, not research claims."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3] / 'engine'

class DistributionContract(unittest.TestCase):
    def call(self, *args):
        return subprocess.run([sys.executable, "-B", str(ROOT / "core/cli/collect_returns.py"), *args],
                              capture_output=True, text=True, timeout=10)
    def test_help(self):
        p = self.call("--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("admit", p.stdout)
    def test_no_runtime_in_distribution(self):
        target = ROOT / "forbidden-runtime"
        p = self.call("--root", str(target), "list")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("outside", p.stderr)
        self.assertFalse(target.exists())
    def test_external_empty_runtime(self):
        with tempfile.TemporaryDirectory(prefix="g60-external-") as temp:
            p = self.call("--root", str(Path(temp) / "runtime"), "list")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.strip(), "[]")

if __name__ == "__main__":
    unittest.main(verbosity=2)
