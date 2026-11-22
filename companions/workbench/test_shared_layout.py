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
import resumable_workbench
from source_tools import SearchImplementation, SourceService, identity


def search_layout(root):
    engine = Path(__file__).resolve().parents[2] / 'engine'
    runtime = root / 'engine/runtime/source_library'
    runtime.mkdir(parents=True)
    for name in ('knowledge.py', 'library_format.py', 'lexical_search.py', 'product_bundle.py'):
        (runtime / name).write_bytes((engine / 'runtime/source_library' / name).read_bytes())
    relative = 'prototype/knowledge-system/knowledge.py'
    legacy = root / 'engine' / relative
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes((engine / relative).read_bytes())
    return runtime, legacy


def change_query_parser(path):
    previous = path.read_bytes()
    changed = previous.replace(b'    need(backend in', b"    query = 'changed'\n    need(backend in", 1)
    if changed == previous:
        raise AssertionError('Fixture must change the selected parser')
    path.write_bytes(changed)
    return previous


class SharedLayout(unittest.TestCase):
    def test_forwarding_and_direct_layouts_bind_the_same_effective_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, legacy = search_layout(root)
            corpus = root / 'corpus'
            corpus.mkdir()
            (corpus / 'example.md').write_text('# Example lexical fixture\n')
            tools = root / 'companions/workbench/source_tools.py'
            tools.parent.mkdir(parents=True)
            tools.write_bytes(Path(source_tools.__file__).read_bytes())

            with patch.object(source_tools, '__file__', str(tools)):
                with SourceService(corpus, root / 'forwarded') as forwarded:
                    inventory = forwarded.inventory()
                legacy.unlink()
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
            runtime, legacy = search_layout(Path(directory))
            legacy.write_bytes(legacy.read_bytes() + b'\n# Unrecognized delegation.\n')
            with self.assertRaisesRegex(ValueError, 'supported entrance'):
                SearchImplementation(legacy)
            (runtime / 'lexical_search.py').write_bytes(b'#' * (256 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, 'exceeds byte bound'):
                SearchImplementation(runtime / 'knowledge.py')

    def test_resume_refuses_dependency_drift_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, legacy = search_layout(root)
            corpus = root / 'corpus'
            corpus.mkdir()
            (corpus / 'example.md').write_text('# Example\n')
            config = {"run_id": "source-drift", "task": "Prepare an inventory.",
                      "corpus_root": str(corpus), "workspace": str(root / 'workspace'),
                      "state_dir": str(root / 'state'), "max_steps": 2,
                      "model": {"kind": "injected", "identity": "source-drift-fixture"}}
            steps = []

            def model(request):
                steps.append(request['step'])
                if request['step'] == 1:
                    return {"kind": "tool", "name": "source_inventory", "arguments": {}}
                return {"kind": "final", "answer": "Ready."}

            with patch.object(resumable_workbench, 'SearchImplementation', side_effect=lambda: SearchImplementation(legacy)):
                start = resumable_workbench.run_workbench(config, model=model)
                self.assertEqual(start['status'], 'complete', start)
                self.assertEqual(steps, [1, 2])
                state = root / 'state'
                receipts = state / 'source-service/receipts.json'
                manifest_file = state / 'durable/manifest.json'
                receipt_bytes, manifest_bytes = receipts.read_bytes(), manifest_file.read_bytes()
                binding = json.loads(manifest_bytes)['contract']['binding']
                self.assertEqual(set(binding['source_search_code']),
                                 {str(runtime / name) for name in ('knowledge.py', 'library_format.py',
                                 'lexical_search.py', 'product_bundle.py')})
                self.assertIn('tokenizer', binding['source_search_analyzer'])
                predecessor = change_query_parser(runtime / 'lexical_search.py')
                drift = resumable_workbench.run_workbench(config, resume=True, model=model)
                self.assertEqual(drift['status'], 'binding_mismatch', drift)
                self.assertEqual(steps, [1, 2])
                self.assertEqual(receipts.read_bytes(), receipt_bytes)
                self.assertEqual(manifest_file.read_bytes(), manifest_bytes)

                (runtime / 'lexical_search.py').write_bytes(predecessor)
                resumed = resumable_workbench.run_workbench(config, resume=True, model=model)
                self.assertEqual(resumed['status'], 'complete', resumed)
                self.assertEqual(steps, [1, 2])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
