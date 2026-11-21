"""Receiving checks for the consolidated shared implementation boundary."""
import hashlib
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import peer_workbench
import resumable_workbench
from source_tools import SourceService, bundled_search_module_path


class SharedLayout(unittest.TestCase):
    def test_reader_layout_selection_prefers_the_current_edition(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Path(directory) / 'engine'
            old = engine / 'prototype/knowledge-system/knowledge.py'
            old.parent.mkdir(parents=True)
            old.write_bytes(bundled_search_module_path().read_bytes())
            self.assertEqual(bundled_search_module_path(engine), old)
            (engine / 'runtime').mkdir()
            self.assertEqual(bundled_search_module_path(engine),
                             engine / 'runtime/knowledge-system/knowledge.py')

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
                path.write_text('# Synthetic protected fixture\n')
            (corpus / 'public.md').write_text('# Public fixture\n')
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
            (corpus / "capacity.md").write_text(body)
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

    def test_resumption_reuses_actual_source_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"
            corpus.mkdir()
            (corpus / "basis.md").write_text("# Basis\n\nThe receiving owner judges the current source.\n")
            config = {"run_id": "shared-layout", "task": "Acquire basis and return.",
                "requirements": "Preserve exact source observation across resume.",
                "corpus_root": str(corpus), "workspace": str(root / "workspace"),
                "state_dir": str(root / "state"), "sources": {}, "obligations": [],
                "max_steps": 3, "max_cache_bytes": 65536,
                "model": {"kind": "injected", "identity": "shared-layout-fixture", "inputs": [str(Path(__file__).resolve())]},
                "receivers": {}}
            dispatches = []

            def model(request):
                dispatches.append(request["step"])
                if request["step"] == 1:
                    return {"kind": "tool", "name": "source_read", "arguments": {"paths": ["basis.md"]}}
                return {"kind": "final", "answer": "Source acquired."}

            first = resumable_workbench.run_workbench(config, model=model)
            self.assertEqual(first["status"], "complete", first)
            self.assertEqual(dispatches, [1, 2])
            second = resumable_workbench.run_workbench(config, resume=True, model=model)
            self.assertEqual(second["status"], "complete", second)
            self.assertEqual(dispatches, [1, 2])
            self.assertEqual(second["durable"]["actual_dispatches_this_invocation"], {"model": 0, "tool": 0})

            manifest = root / 'state/durable/manifest.json'
            retained = manifest.read_bytes()
            reader = bundled_search_module_path()
            pins = json.loads(retained)['contract']['binding']['source_search_code']
            self.assertEqual(pins, {str(reader): {'bytes': reader.stat().st_size,
                                                  'sha256': hashlib.sha256(reader.read_bytes()).hexdigest()}})
            alternate = root / 'engine/prototype/knowledge-system/knowledge.py'
            alternate.parent.mkdir(parents=True)
            alternate.write_bytes(reader.read_bytes())
            with patch('source_tools.bundled_search_module_path', return_value=alternate):
                refused = resumable_workbench.run_workbench(config, resume=True, model=model)
            self.assertEqual(refused['status'], 'binding_mismatch', refused)
            self.assertEqual(dispatches, [1, 2])
            self.assertEqual(manifest.read_bytes(), retained)


if __name__ == "__main__":
    unittest.main(verbosity=2)
