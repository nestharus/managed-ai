"""A coherently registered body can still have a stale identity in the bundle's source list."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


RELEASE = Path(__file__).resolve().parent
GUIDE_ID = "prototype/agent-system/GUIDE.md"
GUIDE_PATH = "runtime/store/GUIDE.md"


class SourceListTest(unittest.TestCase):
    def test_checkout_metadata_refuses_before_manifest_or_transport_write(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            release = root / "engine/release"
            release.mkdir(parents=True)
            (release / "current_sources.json").write_text("{}")
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
                        path.write_text("gitdir: example")
                    result = subprocess.run([sys.executable, "-B", RELEASE / "package.py", "--root", root,
                                             "--output", transport, "--replace"], capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Git checkout metadata", result.stderr)
                    self.assertTrue(all(p.read_bytes() == b"existing" for p in prior))
                    if directory:
                        path.rmdir()
                    else:
                        path.unlink()

    def test_stale_second_identity_refuses_before_publication(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            core = root / "engine"
            (core / "release").mkdir(parents=True)
            (core / "runtime/store").mkdir(parents=True)
            rows = [{"id": "neighbor", "source": "neighbor.md"}, {"id": GUIDE_ID, "source": GUIDE_PATH}]
            (core / "release/current_sources.json").write_text(json.dumps({"edition": "test", "bodies": rows}))
            catalogue = core / "runtime/store/catalogue.json"
            catalogue.write_text(json.dumps({"entries": rows}))
            (core / "release/navigation.json").write_text("{}")
            (core / "neighbor.md").write_bytes(b"neighbor")
            (core / GUIDE_PATH).write_bytes(b"current")
            subprocess.run([sys.executable, "-B", RELEASE / "refresh_guidance.py", "--root", core],
                           check=True, capture_output=True)
            self.assertEqual(json.loads(catalogue.read_bytes())["entries"][1]["body_path"], GUIDE_PATH)

            page = core / "SOURCES.md"
            current = "7 bytes; SHA-256 `" + hashlib.sha256(b"current").hexdigest() + "`"
            stale = "3 bytes; SHA-256 `" + hashlib.sha256(b"old").hexdigest() + "`"
            text = page.read_text(encoding="utf-8")
            self.assertEqual(text.count(current), 1)
            self.assertIn("<a id=\"source-" + hashlib.sha256(GUIDE_ID.encode()).hexdigest()[:20] + "\"></a>", text)
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
                    self.assertIn(f"Expected {GUIDE_ID}: [{GUIDE_PATH}]({GUIDE_PATH}) — " + current, result.stderr)
                    self.assertIn(f"Observed {GUIDE_ID}: [{GUIDE_PATH}]({GUIDE_PATH}) — " + stale, result.stderr)
                    self.assertIn("Run release guidance refresh", result.stderr)
                    for path in protected:
                        self.assertEqual(path.read_bytes(), b"prior contents")

    def test_current_runtime_requires_current_catalogue_member(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "engine"
            (root / "release").mkdir(parents=True)
            (root / "release/current_sources.json").write_text("{}")
            (root / "CURRENT-RELEASE.json").write_text("{}")
            (root / "runtime/store").mkdir(parents=True)
            (root / "prototype/agent-system").mkdir(parents=True)
            (root / "prototype/agent-system/catalogue.json").write_text("{}")
            result = subprocess.run([sys.executable, "-B", RELEASE / "package.py", "--root", root,
                                     "--refresh-only"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result)
            self.assertIn("requires the exact catalogue member runtime/store/catalogue.json", result.stderr)
            self.assertFalse((root / "MANIFEST.json").exists())


if __name__ == "__main__":
    unittest.main()
