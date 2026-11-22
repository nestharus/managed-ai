"""Resume replays completed calls only under the original source binding."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import durable_agent
import resumable_workbench
from source_tools import SearchImplementation
from workbench_fixtures import change_query_parser, search_layout


class SourceReplay(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        corpus = self.root / "corpus"
        corpus.mkdir()
        (corpus / "basis.md").write_text("# Basis\n\nThe receiving owner judges the current source.\n")
        self.config = {"run_id": "source-replay", "task": "Acquire the source and return.",
                       "corpus_root": str(corpus), "workspace": str(self.root / "workspace"),
                       "state_dir": str(self.root / "state"), "max_steps": 2,
                       "model": {"kind": "injected", "identity": "source-replay-fixture"}}

    def test_resume_refuses_dependency_drift_before_dispatch(self):
        runtime, legacy = search_layout(self.root)
        steps = []

        def model(request):
            steps.append(request['step'])
            if request['step'] == 1:
                return {"kind": "tool", "name": "source_inventory", "arguments": {}}
            return {"kind": "final", "answer": "Ready."}

        with patch.object(resumable_workbench, 'SearchImplementation', side_effect=lambda: SearchImplementation(legacy)):
            start = resumable_workbench.run_workbench(self.config, model=model)
            self.assertEqual(start['status'], 'complete', start)
            self.assertEqual(steps, [1, 2])
            state = self.root / 'state'
            receipts = state / 'source-service/receipts.json'
            manifest_file = state / 'durable/manifest.json'
            receipt_bytes, manifest_bytes = receipts.read_bytes(), manifest_file.read_bytes()
            binding = json.loads(manifest_bytes)['contract']['binding']
            self.assertEqual(set(binding['source_search_code']),
                             {str(runtime / name) for name in ('knowledge.py', 'library_format.py',
                             'lexical_search.py', 'product_bundle.py')})
            self.assertIn('tokenizer', binding['source_search_analyzer'])
            predecessor = change_query_parser(runtime / 'lexical_search.py')
            drift = resumable_workbench.run_workbench(self.config, resume=True, model=model)
            self.assertEqual(drift['status'], 'binding_mismatch', drift)
            self.assertEqual(steps, [1, 2])
            self.assertEqual(receipts.read_bytes(), receipt_bytes)
            self.assertEqual(manifest_file.read_bytes(), manifest_bytes)

            (runtime / 'lexical_search.py').write_bytes(predecessor)
            resumed = resumable_workbench.run_workbench(self.config, resume=True, model=model)
            self.assertEqual(resumed['status'], 'complete', resumed)
            self.assertEqual(steps, [1, 2])

    def test_resumption_reuses_actual_source_observation(self):
        dispatches = []

        def model(request):
            dispatches.append(request["step"])
            if request["step"] == 1:
                return {"kind": "tool", "name": "source_read", "arguments": {"paths": ["basis.md"]}}
            return {"kind": "final", "answer": "Source acquired."}

        first = resumable_workbench.run_workbench(self.config, model=model)
        self.assertEqual(first["status"], "complete", first)
        self.assertEqual(dispatches, [1, 2])
        second = resumable_workbench.run_workbench(self.config, resume=True, model=model)
        self.assertEqual(second["status"], "complete", second)
        self.assertEqual(dispatches, [1, 2])
        self.assertEqual(second["durable"]["actual_dispatches_this_invocation"], {"model": 0, "tool": 0})


    def test_unwritten_completion_leaves_an_intent_that_resume_will_not_dispatch(self):
        calls = []
        def model(request):
            calls.append(request['step'])
            return {"kind": "final", "answer": "Ready."}

        write_record = durable_agent._write_new
        def fail_completion(path, record):
            if path.name.endswith('.return.json'):
                raise OSError("cannot record completion")
            write_record(path, record)

        with patch.object(durable_agent, '_write_new', fail_completion):
            first = resumable_workbench.run_workbench(self.config, model=model)
        self.assertEqual(first['status'], 'unresolved', first)
        resumed = resumable_workbench.run_workbench(self.config, resume=True, model=model)
        self.assertEqual(resumed['status'], 'unresolved', resumed)
        self.assertEqual(calls, [1])
        self.assertEqual(resumed['durable']['actual_dispatches_this_invocation'], {"model": 0, "tool": 0})

    def test_observer_failure_leaves_a_completed_call_for_replay(self):
        calls = []
        def model(request):
            calls.append(request['step'])
            return {"kind": "final", "answer": "Ready."}

        def stop_after_completion(event):
            raise OSError("observer stopped")

        first = resumable_workbench.run_workbench(self.config, model=model, after_record=stop_after_completion)
        self.assertEqual(first['status'], 'state_error', first)
        resumed = resumable_workbench.run_workbench(self.config, resume=True, model=model,
                                                    after_record=stop_after_completion)
        self.assertEqual(resumed['status'], 'complete', resumed)
        self.assertEqual(calls, [1])
        self.assertEqual(resumed['durable']['replayed_callbacks_this_invocation'], {"model": 1, "tool": 0})
