"""Wave 4 regression tests for companions/publication_review (W4-B7, W4-B10, W4-H5)."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
PUB_DIR = REPO_ROOT / 'companions/publication_review'
if str(PUB_DIR) not in sys.path:
    sys.path.insert(0, str(PUB_DIR))

import continuation_assessor
import publication_observer
import publication_review


class PublicationReviewWave4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_assessor_cli_rejects_duplicate_keys_and_nonfinite_json(self):
        prev = self.root / 'prev.json'
        curr = self.root / 'curr.json'
        prev.write_text('{"artifacts":[],"obligations":[],"obligations":[]}\n', encoding='utf-8')
        curr.write_text('{"requirements":{},"sources":{},"artifacts":{}}\n', encoding='utf-8')
        rc = continuation_assessor._main(['--previous', str(prev), '--current', str(curr)])
        self.assertEqual(rc, 2)

        prev.write_text('{"artifacts":[],"obligations":[{"status":"open","score":NaN}]}\n', encoding='utf-8')
        rc2 = continuation_assessor._main(['--previous', str(prev), '--current', str(curr)])
        self.assertEqual(rc2, 2)

    def test_observer_docstring_and_path_traversal_symlink_guards(self):
        self.assertIn('docs/OBSERVATION-CONTRACT.md', publication_observer.__doc__)
        self.assertNotIn('OBSERVER-INTERFACE.md', publication_observer.__doc__)

        src_root = self.root / 'src'
        src_root.mkdir()
        (src_root / 'doc.md').write_text('hello\n', encoding='utf-8')

        for bad_path in (r'sub\doc.md', 'C:doc.md', 'D:foo/bar.md', '../escape.md', '/abs.md', 'a//b.md'):
            with self.subTest(bad_path=bad_path):
                decl = {
                    'schema': publication_observer.DECLARATION_SCHEMA,
                    'roots': {'src': str(src_root)},
                    'sources': {'s1': {'root': 'src', 'path': bad_path}},
                }
                with self.assertRaises(ValueError):
                    publication_observer.observe(decl)

        # Symlink (POSIX) or NTFS junction (Windows) under configured root must be refused.
        outside_dir = self.root / 'outside-dir'
        outside_dir.mkdir()
        (outside_dir / 'secret.md').write_text('secret\n', encoding='utf-8')
        if os.name == 'nt':
            junc = src_root / 'junc'
            proc = subprocess.run(
                ['cmd', '/c', 'mklink', '/J', str(junc), str(outside_dir)],
                capture_output=True, text=True,
            )
            if proc.returncode == 0:
                decl_junc = {
                    'schema': publication_observer.DECLARATION_SCHEMA,
                    'roots': {'src': str(src_root)},
                    'sources': {'s1': {'root': 'src', 'path': 'junc/secret.md'}},
                }
                with self.assertRaisesRegex(ValueError, 'symlink|reparse'):
                    publication_observer.observe(decl_junc)
                subprocess.run(['cmd', '/c', 'rmdir', str(junc)], capture_output=True)
        outside = self.root / 'outside.md'
        outside.write_text('secret\n', encoding='utf-8')
        link = src_root / 'linked.md'
        try:
            link.symlink_to(outside)
        except OSError:
            return
        decl_link = {
            'schema': publication_observer.DECLARATION_SCHEMA,
            'roots': {'src': str(src_root)},
            'sources': {'s1': {'root': 'src', 'path': 'linked.md'}},
        }
        with self.assertRaisesRegex(ValueError, 'symlink|reparse'):
            publication_observer.observe(decl_link)

    def test_publication_review_direct_import_and_atomic_retain(self):
        src_root = self.root / 'src'
        src_root.mkdir()
        body = b'published text\n'
        (src_root / 'art.md').write_bytes(body)
        sha = hashlib.sha256(body).hexdigest()

        previous = {
            'artifacts': [{
                'id': 'art', 'artifact_sha256': sha,
                'requirements': {}, 'sources': {},
            }],
            'obligations': [],
        }
        declaration = {
            'schema': publication_observer.DECLARATION_SCHEMA,
            'roots': {'src': str(src_root)},
            'artifacts': {'art': {'root': 'src', 'path': 'art.md'}},
        }
        report = publication_review.review(previous, declaration)
        self.assertEqual(report['assessment']['artifacts'][0]['status'], 'current')
        actual_assessor = (PUB_DIR / 'continuation_assessor.py').read_bytes()
        self.assertEqual(report['assessor'], {
            'bytes': len(actual_assessor),
            'sha256': hashlib.sha256(actual_assessor).hexdigest(),
        })

        # Atomic _retain: failure partway leaves NO partial output_dir!
        out_dir = self.root / 'retained-fail'
        real_json_text = publication_review._json_text
        calls = {'n': 0}

        def fail_midway(value):
            calls['n'] += 1
            if calls['n'] == 3:
                raise OSError('simulated disk full midway')
            return real_json_text(value)

        with patch.object(publication_review, '_json_text', side_effect=fail_midway):
            with self.assertRaisesRegex(OSError, 'simulated disk full'):
                publication_review._retain(out_dir, previous, report)
        self.assertFalse(out_dir.exists())
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.retain-')], [])

        # Successful atomic _retain publishes all 6 JSON files cleanly.
        ok_dir = self.root / 'retained-ok'
        published = publication_review._retain(ok_dir, previous, report)
        self.assertEqual(Path(published), ok_dir.resolve())
        self.assertEqual(
            sorted(p.name for p in ok_dir.iterdir()),
            ['assessment.json', 'current.json', 'observation.json', 'observed-origins.json', 'previous.json', 'report.json'],
        )
        # Re-retaining to an existing target refuses.
        with self.assertRaises(FileExistsError):
            publication_review._retain(ok_dir, previous, report)


if __name__ == '__main__':
    unittest.main(verbosity=2)
