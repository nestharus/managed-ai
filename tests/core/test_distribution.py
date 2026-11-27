"""Checks for the source-distribution interface, not research claims."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _p in (_REPO / "src", _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from managed_ai_information.core.exchange.transport.collector import (  # noqa: E402
    name as validate_collector_name,
)
from tests._paths import ENGINE as ROOT  # noqa: E402


class DistributionContract(unittest.TestCase):
    def call(self, *args):
        return subprocess.run(
            [sys.executable, "-B", str(ROOT / "core/exchange/transport/collector.py"), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )

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
        self.assertEqual(validate_collector_name("job-alpha", "job"), "job-alpha")
        with self.assertRaises(ValueError):
            validate_collector_name("   ", "job")


if __name__ == "__main__":
    unittest.main(verbosity=2)
