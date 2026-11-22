"""A coherently registered body can still have a stale identity in the bundle's source list."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


RELEASE = Path(__file__).resolve().parent


class SourceListTest(unittest.TestCase):
    def test_stale_second_identity_refuses_before_publication(self):
        for catalogue_root in ("runtime/native", "prototype/agent-system"):
            with self.subTest(catalogue_root=catalogue_root):
                self.check_stale_identity(catalogue_root)

    def check_stale_identity(self, catalogue_root):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            core = root / "engine"
            (core / "release").mkdir(parents=True)
            (core / catalogue_root).mkdir(parents=True)
            rows = [{"id": name, "source": name + ".md"} for name in ("neighbor", "guide")]
            (core / "release/current_sources.json").write_text(json.dumps({"edition": "test", "bodies": rows}))
            (core / catalogue_root / "catalogue.json").write_text(json.dumps({"entries": rows}))
            (core / "release/navigation.json").write_text("{}")
            (core / "neighbor.md").write_bytes(b"neighbor")
            (core / "guide.md").write_bytes(b"current")
            subprocess.run([sys.executable, "-B", RELEASE / "refresh_guidance.py", "--root", core],
                           check=True, capture_output=True)

            page = core / "SOURCES.md"
            current = "7 bytes; SHA-256 `" + hashlib.sha256(b"current").hexdigest() + "`"
            stale = "3 bytes; SHA-256 `" + hashlib.sha256(b"old").hexdigest() + "`"
            text = page.read_text(encoding="utf-8")
            self.assertEqual(text.count(current), 1)
            page.write_text(text.replace(current, stale), encoding="utf-8")
            transport = root.parent / "prior.zip"
            protected = [root / "MANIFEST.json", core / "MANIFEST.json", transport]
            for path in protected:
                path.write_bytes(b"prior contents")
            for args in (["--refresh-only"], ["--output", transport, "--replace"]):
                with self.subTest(args=args):
                    result = subprocess.run([sys.executable, "-B", RELEASE / "package.py", "--root", root, *args],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 1, result)
                    self.assertIn("Expected guide: [guide.md](guide.md) — " + current, result.stderr)
                    self.assertIn("Observed guide: [guide.md](guide.md) — " + stale, result.stderr)
                    self.assertIn("Run release guidance refresh", result.stderr)
                    for path in protected:
                        self.assertEqual(path.read_bytes(), b"prior contents")


if __name__ == "__main__":
    unittest.main()
