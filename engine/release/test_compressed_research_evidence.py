"""Release admission of compressed Research originals through their native import receipts."""
import gzip
import hashlib
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest


RELEASE = Path(__file__).resolve().parent


class CompressedResearchEvidenceTest(unittest.TestCase):
    def test_compressed_scientific_basis_requires_a_completed_native_receipt(self):
        api = runpy.run_path(str(RELEASE / "package.py"))
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "sample.gz"
            path.write_bytes(gzip.compress(b'{"count": 4}\n'))
            pin = hashlib.sha256(path.read_bytes()).hexdigest()
            basis = {"path": f"evidence/research-originals/{pin[:2]}/{pin}.gz",
                     "sha256": pin, "bytes": path.stat().st_size}
            source = {"relation": "grounded in original edition", "origin": "study/cells.json.gz",
                      "target": basis["path"], "sha256": pin, "bytes": basis["bytes"]}
            ref = {"id": "trial", "revision": 1, "sha256": "a" * 64}
            head = dict(ref, relations=[source], basis=[basis])
            accepted = []
            system = SimpleNamespace(records=lambda: [head], completed_import_records=lambda: iter(accepted))
            self.assertFalse(api["received_compressed_text"](system))
            accepted.append(ref)
            self.assertIn((basis["path"], pin, basis["bytes"]), api["received_compressed_text"](system))
            api["check_compressed_text"](path)
            accepted[0] = dict(ref, revision=2)
            self.assertFalse(api["received_compressed_text"](system))
            accepted[0] = ref
            source["origin"] = "study/collection.tar.gz"
            self.assertFalse(api["received_compressed_text"](system))
            path.write_bytes(gzip.compress(b"PK\x03\x04nested"))
            with self.assertRaises(ValueError):
                api["check_compressed_text"](path)
            with self.assertRaises(ValueError):
                api["members"](root, None)


if __name__ == "__main__":
    unittest.main()
