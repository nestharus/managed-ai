"""Release gates for compressed Research originals."""

import gzip
import hashlib
import runpy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
ENGINE = REPO / "src/managed_ai_information"
RELEASE = REPO / "scripts/release"
package = runpy.run_path(str(RELEASE / "package.py"))


class CompressedResearchEvidenceTest(unittest.TestCase):
    def setUp(self):
        data = gzip.compress(b'{"count": 4}\n')
        pin = hashlib.sha256(data).hexdigest()
        self.basis = {
            "path": f"evidence/research-originals/{pin[:2]}/{pin}.gz",
            "sha256": pin,
            "bytes": len(data),
        }
        self.source = {
            "relation": "grounded in original edition",
            "origin": "study/cells.json.gz",
            "target": self.basis["path"],
            "sha256": pin,
            "bytes": len(data),
        }
        self.ref = {"id": "trial", "revision": 1, "sha256": "a" * 64}
        head = dict(self.ref, relations=[self.source], basis=[self.basis])
        self.accepted = []
        self.system = SimpleNamespace(
            records=lambda: [head], completed_import_records=lambda: iter(self.accepted)
        )

    def test_receipt_must_acknowledge_the_exact_head(self):
        evidence = package["received_compressed_text"]
        self.assertEqual(evidence(self.system), set())
        self.accepted.append(self.ref)
        self.assertEqual(
            evidence(self.system), {(self.basis["path"], self.basis["sha256"], self.basis["bytes"])}
        )
        self.accepted[0] = dict(self.ref, revision=2)
        self.assertEqual(evidence(self.system), set())

    def test_acknowledged_transport_origin_is_ineligible(self):
        self.accepted.append(self.ref)
        self.source["origin"] = "study/collection.tar.gz"
        self.assertEqual(package["received_compressed_text"](self.system), set())


class CompressedPayloadTest(unittest.TestCase):
    def test_decoder_accepts_text_and_refuses_binary_content(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "source.gz"
            path.write_bytes(gzip.compress(b'{"count": 4}\n'))
            package["check_compressed_text"](path)

            path.write_bytes(gzip.compress(b"PK\x03\x04nested"))
            with self.assertRaisesRegex(ValueError, "Binary content in compressed research data"):
                package["check_compressed_text"](path)

    def test_members_refuses_valid_gzip_outside_research_originals(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.gz").write_bytes(gzip.compress(b"valid text"))
            with self.assertRaisesRegex(
                ValueError, "Select extracted useful source instead of nested transport"
            ):
                package["members"](root, ENGINE)


if __name__ == "__main__":
    unittest.main()
