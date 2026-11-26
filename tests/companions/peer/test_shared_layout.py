"""Receiving checks for the consolidated shared implementation boundary."""
import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
PEER_DIR = REPO_ROOT / 'companions/peer'
WORKBENCH_CORE = REPO_ROOT / 'companions/workbench/core'
TESTS_COMPANIONS = REPO_ROOT / 'tests/companions'
for _p in (str(PEER_DIR), str(WORKBENCH_CORE), str(TESTS_COMPANIONS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import peer_workbench
import source_tools
from source_tools import SearchImplementation, SourceService, identity
from workbench_fixtures import change_query_parser, search_layout


class SharedLayout(unittest.TestCase):
    def test_runtime_layout_binds_exact_effective_snapshot_and_detects_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library, entry = search_layout(root)
            corpus = root / 'corpus'
            corpus.mkdir()
            (corpus / 'example.md').write_text('# Example lexical fixture\n', encoding='utf-8', newline='\n')
            tools = root / 'companions/workbench/core/source_tools.py'
            tools.parent.mkdir(parents=True)
            tools.write_bytes(Path(source_tools.__file__).read_bytes())

            with patch.object(source_tools, '__file__', str(tools)):
                with SourceService(corpus, root / 'explicit', search_module_path=entry) as explicit:
                    inventory = explicit.inventory()
                with SourceService(corpus, root / 'direct') as direct:
                    self.assertEqual(direct.inventory(), inventory)
                    recorded = inventory['selector']['search_code']
                    expected_files = {
                        'knowledge.py': library / 'reader/knowledge.py',
                        'library_format.py': library / 'reader/library_format.py',
                        'lexical_search.py': library / 'search/lexical_search.py',
                        'product_bundle.py': library / 'bundles/product_bundle.py',
                    }
                    for name, path in expected_files.items():
                        data = path.read_bytes()
                        self.assertEqual(recorded[name], {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
                    self.assertEqual(inventory['selector']['acquired_search_sha256'], recorded['knowledge.py']['sha256'])
                    self.assertEqual(inventory['selector_id'], identity(inventory['selector']))
                    change_query_parser(library / 'search/lexical_search.py')
                    self.assertEqual(direct.discover('the example')['query_interpretation']['terms'], ['example'])

                with SourceService(corpus, root / 'direct') as changed:
                    self.assertEqual(changed.discover('the example')['query_interpretation']['terms'], ['changed'])
                    self.assertNotEqual(changed.selector_id, inventory['selector_id'])
                    self.assertEqual(changed.selector['acquired_search_sha256'], recorded['knowledge.py']['sha256'])
                    self.assertIn(inventory['receipt_id'], changed.receipts()['inventories'])

    def test_unknown_delegation_and_oversized_dependency_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library, entry = search_layout(root)
            delegator = root / 'unrecognized_delegator.py'
            delegator.write_text('import runpy\nrunpy.run_path("knowledge.py")\n', encoding='utf-8', newline='\n')
            with self.assertRaisesRegex(ValueError, 'supported entrance'):
                SearchImplementation(delegator)
            (library / 'search/lexical_search.py').write_bytes(b'#' * (256 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, 'exceeds byte bound'):
                SearchImplementation(entry)

    def test_relocated_exposure_boundary_blocks_read_and_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / 'corpus'
            corpus.mkdir()
            protected = ['evidence/fleet/D12/fixture.md',
                         'instruments/incident-corpus/D12/fixture.md']
            for name in protected:
                path = corpus / name
                path.parent.mkdir(parents=True)
                path.write_text('# Synthetic protected fixture\n', encoding='utf-8', newline='\n')
            (corpus / 'public.md').write_text('# Public fixture\n', encoding='utf-8', newline='\n')
            with SourceService(corpus, root / 'state') as service:
                with patch.object(service, '_body', wraps=service._body) as reads:
                    for name in protected:
                        with self.assertRaisesRegex(ValueError, 'protected'):
                            service.read(name)
                    self.assertEqual(reads.call_count, 0)
                    inventory = service.refresh()
                    self.assertEqual(set(inventory['members']), {'public.md'})
                    self.assertEqual(set(inventory['excluded']), set(protected))
                    self.assertEqual([call.args[0] for call in reads.call_args_list], ['public.md'])
                    result = service.discover('protected')
                    self.assertEqual(result['hits'], [])

    def test_peer_requires_a_registered_file_for_each_loaded_role(self):
        manifest = json.loads((peer_workbench.HERE / 'COMPONENTS.json').read_bytes())
        del manifest['files']['workbench/controller/agent_runtime.py']
        with patch.object(peer_workbench, 'decode_json', return_value=manifest):
            with self.assertRaisesRegex(ValueError, 'Missing peer component: agent_runtime'):
                peer_workbench.components()

    def test_peer_turn_uses_shared_code_and_captures_exact_return(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"
            corpus.mkdir()
            workspace = root / "run/workspaces/owner"
            workspace.mkdir(parents=True)
            request = {
                "schema": "peer-request/1", "run_id": "layout-test", "owner": "owner",
                "turn": 1, "revision": 1, "brief": "Capture a declared file.", "input": {},
                "state": None, "inbox": [], "peers": {}, "workspace": str(workspace),
                "limits_remaining": {
                    "wall_seconds": 30, "turns": 3, "turn_seconds": 10,
                    "max_output_bytes": 100000, "max_artifact_bytes": 10000,
                },
            }
            config = {
                "schema": "peer-workbench-config/1", "task": "Capture a declared file.",
                "sources": {}, "corpus_root": str(corpus), "evidence_root": str(root / "evidence"),
                "model": {"kind": "mailbox"}, "max_steps": 3, "obligations": [],
            }
            expected = "An exact current contribution.\n"

            def model(value):
                self.assertTrue({"peer-request", "peer-workbench-contract", "workbench-tool-contract"} <= value["sources"].keys())
                if value["step"] == 1:
                    return {"kind": "tool", "name": "workspace_write", "arguments": {"files": {"result.txt": expected}}}
                if value["step"] == 2:
                    return {"kind": "tool", "name": "peer_stage_return", "arguments": {
                        "status": "complete", "state": {}, "obligations": [], "ack": [], "send": [], "files": ["result.txt"]}}
                return {"kind": "final", "answer": "Ready."}

            result = peer_workbench.run_peer_turn(config, request, model=model)
            self.assertTrue(result["ok"], result)
            self.assertEqual(base64.b64decode(result["response"]["files"]["result.txt"]).decode("utf-8"), expected)
            self.assertEqual(result["response"]["status"], "complete")

    def test_corpus_is_data_and_shared_peer_components_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"
            corpus.mkdir()
            body = "# Capacity\n\nAn available resource and a planned resource are different.\n"
            (corpus / "capacity.md").write_text(body, encoding="utf-8", newline="\n")
            with SourceService(corpus, root / "source-state") as service:
                observed = service.read("capacity.md")
                self.assertEqual(observed["text"], body)
                self.assertEqual(observed["sha256"], hashlib.sha256(body.encode("utf-8")).hexdigest())
                result = service.discover("capacity")
                self.assertIn("capacity.md", json.dumps(result))
                self.assertFalse((corpus / "knowledge").exists())
        modules, manifest = peer_workbench.components()
        self.assertEqual(
            set(modules),
            {"io_utils", "config", "model_bridges", "source_tools", "evidence_support", "agent_runtime", "workbench_agent"},
        )
        self.assertEqual(
            Path(modules["workbench_agent"].__file__).resolve(),
            (WORKBENCH_CORE / "workbench_agent.py").resolve(),
        )
        self.assertEqual(len(manifest["files"]), 7)
        self.assertTrue(all(not key.startswith("..") for key in manifest["files"]))
        # Wave 5 (#213 Part B): Git-tracked COMPONENTS.json is declarative (no hardcoded identity),
        # while peer_workbench.components() measures live identities for every loaded component.
        raw_manifest = json.loads((peer_workbench.HERE / "COMPONENTS.json").read_bytes())
        self.assertTrue(all("identity" not in entry for entry in raw_manifest["files"].values()))
        for key, entry in manifest["files"].items():
            self.assertEqual(entry["source"], key)
            self.assertIn("identity", entry)
            self.assertEqual(len(entry["identity"]["sha256"]), 64)

    def test_peer_components_verifies_stamped_identities_and_records_live_input_identities(self):
        # Stamped manifest (e.g. from a release bundle) passes when matching and refuses tampering!
        _, live_manifest = peer_workbench.components()
        with patch.object(peer_workbench, "decode_json", return_value=live_manifest):
            loaded, verified = peer_workbench.components()
            self.assertEqual(verified["files"], live_manifest["files"])
            self.assertIn("workbench_agent", loaded)

        tampered = json.loads(json.dumps(live_manifest))
        tampered["files"]["workbench/core/io_utils.py"]["identity"]["sha256"] = "0" * 64
        with patch.object(peer_workbench, "decode_json", return_value=tampered):
            with self.assertRaisesRegex(ValueError, "Acquired component differs from accepted body"):
                peer_workbench.components()

        # Verify peer_workbench and peer_harness CLIs refuse abbreviated flags (W5-B11).
        import subprocess as _subprocess
        for script in ("peer_workbench.py", "peer_harness.py"):
            proc = _subprocess.run(
                [sys.executable, "-B", str(PEER_DIR / script), "--hel"],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertTrue("required" in proc.stderr or "unrecognized" in proc.stderr, proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
