"""Receiving checks for the consolidated shared implementation boundary."""
import hashlib
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import peer_workbench
import source_tools
from source_tools import SearchImplementation, SourceService, identity


from workbench_fixtures import change_query_parser, search_layout


class SharedLayout(unittest.TestCase):
    def test_runtime_layout_binds_exact_effective_snapshot_and_detects_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, entry = search_layout(root)
            corpus = root / 'corpus'
            corpus.mkdir()
            (corpus / 'example.md').write_text('# Example lexical fixture\n', encoding='utf-8', newline='\n')
            tools = root / 'companions/workbench/source_tools.py'
            tools.parent.mkdir(parents=True)
            tools.write_bytes(Path(source_tools.__file__).read_bytes())

            with patch.object(source_tools, '__file__', str(tools)):
                with SourceService(corpus, root / 'explicit', search_module_path=entry) as explicit:
                    inventory = explicit.inventory()
                with SourceService(corpus, root / 'direct') as direct:
                    self.assertEqual(direct.inventory(), inventory)
                    recorded = inventory['selector']['search_code']
                    for name in ('knowledge.py', 'library_format.py', 'lexical_search.py', 'product_bundle.py'):
                        data = (runtime / name).read_bytes()
                        self.assertEqual(recorded[name], {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
                    self.assertEqual(inventory['selector']['acquired_search_sha256'], recorded['knowledge.py']['sha256'])
                    self.assertEqual(inventory['selector_id'], identity(inventory['selector']))
                    change_query_parser(runtime / 'lexical_search.py')
                    self.assertEqual(direct.discover('the example')['query_interpretation']['terms'], ['example'])

                with SourceService(corpus, root / 'direct') as changed:
                    self.assertEqual(changed.discover('the example')['query_interpretation']['terms'], ['changed'])
                    self.assertNotEqual(changed.selector_id, inventory['selector_id'])
                    self.assertEqual(changed.selector['acquired_search_sha256'], recorded['knowledge.py']['sha256'])
                    self.assertIn(inventory['receipt_id'], changed.receipts()['inventories'])

    def test_unknown_delegation_and_oversized_dependency_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, entry = search_layout(root)
            delegator = root / 'unrecognized_delegator.py'
            delegator.write_text('import runpy\nrunpy.run_path("knowledge.py")\n', encoding='utf-8', newline='\n')
            with self.assertRaisesRegex(ValueError, 'supported entrance'):
                SearchImplementation(delegator)
            (runtime / 'lexical_search.py').write_bytes(b'#' * (256 * 1024 + 1))
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
            service = SourceService(corpus, root / 'state')
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
        del manifest['files']['controller/agent_runtime.py']
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
            request = {"schema": "peer-request/1", "run_id": "layout-test", "owner": "owner",
                "turn": 1, "revision": 1, "brief": "Capture a declared file.", "input": {},
                "state": None, "inbox": [], "peers": {}, "workspace": str(workspace),
                "limits_remaining": {"wall_seconds": 30, "turns": 3, "turn_seconds": 10,
                                     "max_output_bytes": 100000, "max_artifact_bytes": 10000}}
            config = {"schema": "peer-workbench-config/1", "task": "Capture a declared file.",
                "sources": {}, "corpus_root": str(corpus), "evidence_root": str(root / "evidence"),
                "model": {"kind": "mailbox"}, "max_steps": 3, "obligations": []}
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
            self.assertEqual(base64.b64decode(result["response"]["files"]["result.txt"]).decode(), expected)
            self.assertEqual(result["response"]["status"], "complete")

    def test_corpus_is_data_and_shared_peer_components_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"
            corpus.mkdir()
            body = "# Capacity\n\nAn available resource and a planned resource are different.\n"
            (corpus / "capacity.md").write_text(body, encoding="utf-8", newline="\n")
            service = SourceService(corpus, root / "source-state")
            observed = service.read("capacity.md")
            self.assertEqual(observed["text"], body)
            self.assertEqual(observed["sha256"], hashlib.sha256(body.encode()).hexdigest())
            result = service.discover("capacity")
            self.assertIn("capacity.md", json.dumps(result))
            self.assertFalse((corpus / "knowledge").exists())
        modules, manifest = peer_workbench.components()
        self.assertEqual(set(modules), {"model_bridges", "source_tools", "evidence_support", "agent_runtime", "workbench_agent"})
        self.assertEqual(Path(modules["workbench_agent"].__file__).resolve(), Path(__file__).with_name("workbench_agent.py").resolve())
        self.assertEqual(len(manifest["files"]), 5)



if __name__ == "__main__":
    unittest.main(verbosity=2)
