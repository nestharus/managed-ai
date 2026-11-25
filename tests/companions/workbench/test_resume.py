"""Resume replays completed calls only under the original source binding."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKBENCH_CORE = REPO_ROOT / 'companions/workbench/core'
TESTS_COMPANIONS = REPO_ROOT / 'tests/companions'
for _p in (str(WORKBENCH_CORE), str(TESTS_COMPANIONS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

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
        (corpus / "basis.md").write_text("# Basis\n\nThe receiving owner judges the current source.\n",
                                         encoding="utf-8", newline="\n")
        self.config = {"run_id": "source-replay", "task": "Acquire the source and return.",
                       "corpus_root": str(corpus), "workspace": str(self.root / "workspace"),
                       "state_dir": str(self.root / "state"), "max_steps": 2,
                       "model": {"kind": "injected", "identity": "source-replay-fixture"}}

    def test_resume_refuses_dependency_drift_before_dispatch(self):
        runtime, entry = search_layout(self.root)
        steps = []

        def model(request):
            steps.append(request['step'])
            if request['step'] == 1:
                return {"kind": "tool", "name": "source_inventory", "arguments": {}}
            return {"kind": "final", "answer": "Ready."}

        with patch.object(resumable_workbench, 'SearchImplementation', side_effect=lambda: SearchImplementation(entry)):
            start = resumable_workbench.run_workbench(self.config, model=model)
            self.assertEqual(start['status'], 'complete', start)
            self.assertEqual(steps, [1, 2])
            state = self.root / 'state'
            receipts = state / 'source-service/receipts.json'
            manifest_file = state / 'durable/manifest.json'
            receipt_bytes, manifest_bytes = receipts.read_bytes(), manifest_file.read_bytes()
            binding = json.loads(manifest_bytes)['contract']['binding']
            self.assertEqual(set(binding['source_search_code']),
                             {str(runtime / rel) for rel in (
                                 'reader/knowledge.py', 'reader/library_format.py',
                                 'search/lexical_search.py', 'bundles/product_bundle.py',
                             )})
            self.assertIn('tokenizer', binding['source_search_analyzer'])
            predecessor = change_query_parser(runtime / 'search/lexical_search.py')
            drift = resumable_workbench.run_workbench(self.config, resume=True, model=model)
            self.assertEqual(drift['status'], 'binding_mismatch', drift)
            self.assertEqual(steps, [1, 2])
            self.assertEqual(receipts.read_bytes(), receipt_bytes)
            self.assertEqual(manifest_file.read_bytes(), manifest_bytes)

            (runtime / 'search/lexical_search.py').write_bytes(predecessor)
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

    def test_resume_sees_receipt_published_before_it_obtains_the_journal_lock(self):
        calls = []
        def model(request):
            calls.append(request['step'])
            if request['step'] == 1:
                return {"kind": "tool", "name": "source_read", "arguments": {"path": "basis.md"}}
            return {"kind": "final", "answer": "Read."}

        def interrupt(event):
            raise OSError("paused after recorded model request")

        paused = resumable_workbench.run_workbench(self.config, model=model, after_record=interrupt)
        self.assertEqual(paused['status'], 'state_error')
        lock = durable_agent._lock
        def finish_before_lock(root):
            with patch.object(durable_agent, '_lock', lock):
                first = resumable_workbench.run_workbench(self.config, resume=True, model=model)
                self.assertEqual(first['status'], 'complete', first)
            return lock(root)

        with patch.object(durable_agent, '_lock', finish_before_lock):
            second = resumable_workbench.run_workbench(self.config, resume=True, model=model)
        self.assertEqual(second['status'], 'complete', second)
        self.assertEqual(second['durable']['actual_dispatches_this_invocation'], {'model': 0, 'tool': 0})
        self.assertEqual(calls, [1, 2])


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

    def test_cross_process_durable_owner_lock_refuses_concurrent_holder(self):
        import subprocess
        import sys
        import time

        durable_dir = self.root / 'locked-durable'
        durable_dir.mkdir()
        ready = self.root / 'ready.flag'
        release = self.root / 'release.flag'
        holder = "\n".join([
            "import sys, time",
            "from pathlib import Path",
            "sys.path.insert(0, sys.argv[1])",
            "import durable_agent",
            "with durable_agent._lock(Path(sys.argv[2])):",
            "    Path(sys.argv[3]).write_text('ready', encoding='utf-8')",
            "    deadline = time.monotonic() + 15",
            "    while not Path(sys.argv[4]).exists() and time.monotonic() < deadline:",
            "        time.sleep(0.05)",
        ])
        proc = subprocess.Popen(
            [sys.executable, "-B", "-c", holder, str(WORKBENCH_CORE),
             str(durable_dir), str(ready), str(release)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.exists():
                out, err = proc.communicate(timeout=5)
                self.fail(f"Lock holder failed (rc={proc.returncode}): {err or out}")
            blocked = durable_agent.run_durable_agent(
                state_dir=durable_dir,
                binding={"id": "lock-check"},
                task="Check lock contention.",
                sources={},
                model=lambda req: {"kind": "final", "answer": "unused"},
                tools={},
                max_steps=1,
            )
            self.assertEqual(blocked["status"], "state_error", blocked)
            self.assertIn("another local owner holds this occurrence", blocked["error"]["message"])
        finally:
            release.write_text("go", encoding="utf-8")
            proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0)

    def test_command_code_and_inspect_config_agree_on_backslash_paths(self):
        import os
        import inspect_config

        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        runner = bin_dir / "runner.py"
        runner.write_text("print('ok')\n", encoding="utf-8", newline="\n")
        runner.chmod(0o755)
        if os.name == "nt":
            abs_cmd = str(runner).replace("/", "\\")
            rel_cmd = "bin\\runner.py"
        else:
            abs_cmd = str(runner)
            rel_cmd = "bin/runner.py"

        insp = inspect_config.Inspection()
        insp.command("model.argv", [abs_cmd])
        self.assertEqual(insp.issues, [])

        pinned_abs = resumable_workbench._command_code([abs_cmd])
        self.assertIn(str(runner.resolve()), pinned_abs)
        pinned_rel = resumable_workbench._command_code([rel_cmd], cwd=self.root)
        self.assertIn(str(runner.resolve()), pinned_rel)

        # Even on POSIX, a backslash in argv[0] takes the path branch rather than shutil.which.
        bs_runner = self.root / "sub\\runner.py"
        if os.name != "nt":
            bs_runner.write_text("print('ok')\n", encoding="utf-8", newline="\n")
            bs_runner.chmod(0o755)
            pinned_bs = resumable_workbench._command_code(["sub\\runner.py"], cwd=self.root)
            self.assertIn(str(bs_runner.resolve()), pinned_bs)
