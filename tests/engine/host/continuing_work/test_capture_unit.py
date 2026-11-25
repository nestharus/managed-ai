"""Unit tests for Wave 4 capture_native narrowing, composition, and public receipt inspection."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

REPO_ROOT = Path(__file__).resolve().parents[4]
HOST_DIR = REPO_ROOT / 'engine/host/continuing_work'
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

import capture_native
from continuing_work import Host
import native_bridge
import repair_custody


class Wave4HostArchitectureTests(unittest.TestCase):
    def test_native_sources_narrows_to_deployment_core_store_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / 'engine'
            included = (
                'deployment/select_deployment.py',
                'core/attention.py',
                'store/manage_store.py',
                'store/system.py',
            )
            excluded = (
                'store/test_ignored.py',
                'library/reader/knowledge.py',
                'tools/collect_returns.py',
                'host/continuing_work/continuing_work.py',
                'release/package.py',
            )
            for rel in included + excluded:
                path = engine / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'# {rel}\n', encoding='utf-8', newline='\n')

            native_path, programs = capture_native.native_sources(engine / 'store/manage_store.py')
            self.assertEqual(native_path, (engine / 'store/system.py').resolve())
            self.assertEqual(set(programs), set(included))
            self.assertFalse(any(k.startswith(('library/', 'tools/', 'host/', 'release/')) for k in programs))
            self.assertNotIn('store/test_ignored.py', programs)

    def test_host_uses_composition_instead_of_mixin_inheritance(self):
        self.assertFalse(issubclass(Host, native_bridge.NativeBridge))
        self.assertFalse(issubclass(Host, repair_custody.RepairCustody))
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(Path(tmp) / 'host')
            self.assertIsInstance(host.bridge, native_bridge.NativeBridge)
            self.assertIsInstance(host.repairs, repair_custody.RepairCustody)

    def test_reconcile_assessment_uses_public_inspect_receipt_api(self):
        inspected = []
        supplied = {'id': 'r-1', 'assessments': [{'item': 'x', 'disposition': 'considered', 'account': 'ok'}]}
        encoded_bytes = json.dumps(supplied, sort_keys=True, separators=(',', ':')).encode('utf-8')
        source_sha = hashlib.sha256(encoded_bytes).hexdigest()
        feedback_obj = 'objects/fb1'
        receipt_obj = 'objects/rc1'

        class FakeAttention:
            def inspect_receipt(self, rid):
                inspected.append(rid)
                return {
                    'id': rid,
                    'object': receipt_obj,
                    'record': {
                        'id': rid,
                        'producer': 'auditor',
                        'feedback': 'fb:turn-1',
                        'feedback_object': feedback_obj,
                        'source': {'sha256': source_sha},
                        'assessments': [{'item': 'x', 'disposition': 'considered', 'account': 'ok'}],
                        'boundary': 'explicit',
                    },
                    'feedback_object': feedback_obj,
                }

            def _load(self):
                raise AssertionError('Private _load must not be called when inspect_receipt exists')

        class FakeExchange:
            def directory(self, turn):
                return Path('/tmp') / turn

            def packet_summary(self, directory, consumer, publications, *, returned, outcome):
                return {'packet': directory.name, 'transport': 'prepared', 'publications': publications}

        fake_system = SimpleNamespace(
            attention=FakeAttention(),
            ex=FakeExchange(),
        )
        fake_native = SimpleNamespace(
            read=lambda path: json.loads(Path(path).read_bytes()),
            encode=lambda obj: json.dumps(obj, sort_keys=True, separators=(',', ':')).encode('utf-8'),
            sha=lambda raw: hashlib.sha256(raw).hexdigest(),
            feedback_id=lambda turn: 'fb:' + turn,
            packet_id=lambda raw_id: raw_id,
        )
        observed = {'consumer': 'reader', 'publications': [], 'return': None, 'transport_outcome': None}

        with tempfile.TemporaryDirectory() as tmp:
            receipt_file = Path(tmp) / 'receipt.json'
            receipt_file.write_bytes(encoded_bytes)
            matched = capture_native._reconcile_assessment(
                fake_native, fake_system, 'turn-1', receipt_file, 'auditor', observed,
            )
            self.assertEqual(inspected, ['r-1'])
            self.assertTrue(matched['reconciled'])
            self.assertEqual(matched['attention'], {
                'receipt': 'r-1',
                'object': receipt_obj,
                'recorded': ['x'],
                'boundary': 'explicit',
            })
            # Wrong producer must refuse reconciliation.
            with self.assertRaisesRegex(ValueError, 'receipt identity already used'):
                capture_native._reconcile_assessment(
                    fake_native, fake_system, 'turn-1', receipt_file, 'impostor', observed,
                )
            # Different receipt bytes must refuse reconciliation.
            other_file = Path(tmp) / 'other.json'
            other_file.write_bytes(json.dumps({
                'id': 'r-1',
                'assessments': [{'item': 'x', 'disposition': 'deferred', 'account': 'changed'}],
            }).encode('utf-8'))
            with self.assertRaisesRegex(ValueError, 'receipt identity already used'):
                capture_native._reconcile_assessment(
                    fake_native, fake_system, 'turn-1', other_file, 'auditor', observed,
                )

    def test_repair_custody_overlap_limit_message_has_no_typo(self):
        source = (HOST_DIR / 'continuing_work.py').read_text(encoding='utf-8')
        self.assertNotIn('1 through 2000', source)
        self.assertIn('from 1 through 200', source)


if __name__ == '__main__':
    unittest.main(verbosity=2)
