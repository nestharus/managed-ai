"""A coherently registered body can still have a stale identity in the bundle's source list."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


REPO = Path(__file__).resolve().parents[3]
RELEASE = REPO / "engine/release"


class SourceListTest(unittest.TestCase):
    def test_checkout_metadata_refuses_before_manifest_or_transport_write(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            release = root / "engine/release"
            release.mkdir(parents=True)
            (release / "current_sources.json").write_text("{}", encoding="utf-8")
            domain = root / "domains/research"
            domain.mkdir(parents=True)
            transport = root.parent / "current.zip"
            prior = [root / "MANIFEST.json", root / "engine/MANIFEST.json", transport]
            for path in prior:
                path.write_bytes(b"existing")
            for path, directory in [(root / ".git", True), (domain / ".git", False)]:
                with self.subTest(path=path):
                    if directory:
                        path.mkdir()
                    else:
                        path.write_text("gitdir: example", encoding="utf-8")
                    result = subprocess.run([sys.executable, "-B", str(RELEASE / "package.py"), "--root", str(root),
                                             "--output", str(transport), "--replace"], capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Git checkout metadata", result.stderr)
                    self.assertTrue(all(p.read_bytes() == b"existing" for p in prior))
                    if directory:
                        path.rmdir()
                    else:
                        path.unlink()

    def test_stale_second_identity_refuses_before_publication(self):
        self.check_stale_identity("store")

    def check_stale_identity(self, catalogue_root):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            core = root / "engine"
            (core / "release").mkdir(parents=True)
            (core / catalogue_root).mkdir(parents=True)
            rows = [{"id": name, "source": name + ".md"} for name in ("neighbor", "guide")]
            (core / "release/current_sources.json").write_text(
                json.dumps({"edition": "test", "bodies": rows}), encoding="utf-8"
            )
            (core / catalogue_root / "catalogue.json").write_text(
                json.dumps({"entries": rows}), encoding="utf-8"
            )
            (core / "release/navigation.json").write_text("{}", encoding="utf-8")
            (core / "neighbor.md").write_bytes(b"neighbor")
            (core / "guide.md").write_bytes(b"current")
            subprocess.run([sys.executable, "-B", str(RELEASE / "refresh_guidance.py"), "--root", str(core)],
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
            for args in (["--refresh-only"], ["--output", str(transport), "--replace"]):
                with self.subTest(args=args):
                    result = subprocess.run([sys.executable, "-B", str(RELEASE / "package.py"), "--root", str(root), *args],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 1, result)
                    self.assertIn("Expected guide: [guide.md](guide.md) — " + current, result.stderr)
                    self.assertIn("Observed guide: [guide.md](guide.md) — " + stale, result.stderr)
                    self.assertIn("Run release guidance refresh", result.stderr)
                    for path in protected:
                        self.assertEqual(path.read_bytes(), b"prior contents")


if __name__ == "__main__":
    unittest.main()
