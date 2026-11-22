"""A coherently registered body can still have a stale identity in the bundle's source list."""
import gzip
import runpy
from types import SimpleNamespace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


RELEASE = Path(__file__).resolve().parent


class SourceListTest(unittest.TestCase):
    def test_compressed_scientific_basis_requires_a_completed_native_receipt(self):
        api = runpy.run_path(str(RELEASE / "package.py"))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            receipts = root / ".system/imports"
            receipts.mkdir(parents=True)
            path = root / "sample.gz"
            path.write_bytes(gzip.compress(b'{"count": 4}\n'))
            pin = hashlib.sha256(path.read_bytes()).hexdigest()
            basis = {"path": f"evidence/research-originals/{pin[:2]}/{pin}.gz",
                     "sha256": pin, "bytes": path.stat().st_size}
            source = {"relation": "grounded in original edition", "origin": "study/cells.json.gz",
                      "target": basis["path"], "sha256": pin, "bytes": basis["bytes"]}
            ref = {"id": "trial", "revision": 1, "sha256": "a" * 64}
            head = dict(ref, relations=[source], basis=[basis])
            system = SimpleNamespace(work=root, instance_id="test", records=lambda: [head])
            receipt = {"format": "managed-ai-import-receipt/1", "complete": True,
                       "instance_id": "test", "records": [ref]}
            self.assertFalse(api["received_compressed_text"](system))
            receipt_path = receipts / "trial.json"
            receipt_path.write_text(json.dumps(receipt))
            self.assertIn((basis["path"], pin, basis["bytes"]), api["received_compressed_text"](system))
            api["check_compressed_text"](path)
            for wrong in (dict(receipt, complete=False), dict(receipt, instance_id="other"),
                          dict(receipt, records=[dict(ref, revision=2)])):
                receipt_path.write_text(json.dumps(wrong))
                self.assertFalse(api["received_compressed_text"](system))
            receipt_path.write_text(json.dumps(receipt))
            source["origin"] = "study/collection.tar.gz"
            self.assertFalse(api["received_compressed_text"](system))
            path.write_bytes(gzip.compress(b"PK\x03\x04nested"))
            with self.assertRaises(ValueError):
                api["check_compressed_text"](path)
            with self.assertRaises(ValueError):
                api["members"](root, None)

    def test_stale_second_identity_refuses_before_publication(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            core = root / "engine"
            (core / "release").mkdir(parents=True)
            (core / "prototype/agent-system").mkdir(parents=True)
            rows = [{"id": name, "source": name + ".md"} for name in ("neighbor", "guide")]
            (core / "release/current_sources.json").write_text(json.dumps({"edition": "test", "bodies": rows}))
            (core / "prototype/agent-system/catalogue.json").write_text(json.dumps({"entries": rows}))
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
