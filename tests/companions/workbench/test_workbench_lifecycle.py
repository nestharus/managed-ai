"""Wave 4 regression tests for companions/workbench (W4-B1, B7, B8, B9, H2, H3, H6)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / 'src'
WORKBENCH_DIR = SRC_ROOT / 'managed_ai/companions/workbench'
for _p in (str(SRC_ROOT), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from managed_ai.companions.workbench import (
    config as wb_config,
    durable_agent,
    inspect_config,
    io_utils,
    model_bridges,
    resumable_workbench,
    source_tools,
    workbench_agent,
)
from tests.companions.workbench._fixtures import search_layout


class WorkbenchWave4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.corpus = self.root / 'corpus'
        self.corpus.mkdir()
        (self.corpus / 'note.md').write_text('# Note\n\nBody evidence.\n', encoding='utf-8', newline='\n')
        self.workspace = self.root / 'workspace'
        self.state = self.root / 'state'

    # --- W4-B1: All BasisMismatchError cases map to status='binding_mismatch' ---
    def test_basis_mismatch_in_restore_maps_to_binding_mismatch_not_state_error(self):
        durable_dir = self.root / 'durable-b1'
        calls = []

        def model(req):
            calls.append(req['step'])
            return {'kind': 'final', 'answer': 'done'}

        first = durable_agent.run_durable_agent(
            state_dir=durable_dir,
            binding={'v': 1},
            task='Check restore mismatch mapping.',
            sources={},
            model=model,
            tools={},
            max_steps=1,
            checkpoint=lambda: {'basis': 'v1'},
            restore=lambda cp: None,
        )
        self.assertEqual(first['status'], 'complete', first)

        # Case 1: plain BasisMismatchError (e.g. basis_sha256 / selector_id drift)
        def raise_basis(cp):
            raise workbench_agent.WorkbenchBasisMismatch('Checkpoint belongs to another workbench basis')

        second = durable_agent.run_durable_agent(
            state_dir=durable_dir,
            binding={'v': 1},
            task='Check restore mismatch mapping.',
            sources={},
            model=model,
            tools={},
            max_steps=1,
            resume=True,
            checkpoint=lambda: {'basis': 'v1'},
            restore=raise_basis,
        )
        self.assertEqual(second['status'], 'binding_mismatch', second)
        self.assertEqual(second['error']['code'], 'binding_mismatch')
        self.assertEqual(calls, [1])

        # Case 2: workspace_changed BasisMismatchError preserves code + changed_paths
        def raise_ws(cp):
            raise workbench_agent.WorkbenchBasisMismatch(
                'Workspace drifted',
                code='workspace_changed',
                details={'changed_paths': [{'path': 'out.md', 'expected': None, 'observed': {'bytes': 1}}]},
            )

        third = durable_agent.run_durable_agent(
            state_dir=durable_dir,
            binding={'v': 1},
            task='Check restore mismatch mapping.',
            sources={},
            model=model,
            tools={},
            max_steps=1,
            resume=True,
            checkpoint=lambda: {'basis': 'v1'},
            restore=raise_ws,
        )
        self.assertEqual(third['status'], 'binding_mismatch', third)
        self.assertEqual(third['error']['code'], 'workspace_changed')
        self.assertEqual(third['error']['changed_paths'][0]['path'], 'out.md')

    def test_workbench_workspace_drift_returns_binding_mismatch(self):
        cfg = {
            'run_id': 'ws-drift',
            'task': 'Write a file then stop.',
            'corpus_root': str(self.corpus),
            'workspace': str(self.workspace),
            'state_dir': str(self.state),
            'max_steps': 2,
            'model': {'kind': 'injected', 'identity': 'ws-drift-model'},
        }

        def model(req):
            if req['step'] == 1:
                return {'kind': 'tool', 'name': 'workspace_write', 'arguments': {'files': {'a.txt': 'v1'}}}
            return {'kind': 'final', 'answer': 'ok'}

        res1 = resumable_workbench.run_workbench(cfg, model=model)
        self.assertEqual(res1['status'], 'complete', res1)
        (self.workspace / 'a.txt').write_text('external-edit', encoding='utf-8')
        res2 = resumable_workbench.run_workbench(cfg, resume=True, model=model)
        self.assertEqual(res2['status'], 'binding_mismatch', res2)
        self.assertEqual(res2['error']['code'], 'workspace_changed')
        self.assertEqual(res2['error']['changed_paths'][0]['path'], 'a.txt')

    # --- W4-B8: workbench_agent.run() always closes source_service ---
    def test_workbench_agent_run_closes_source_service_on_completion_and_error(self):
        closed = []
        real_close = source_tools.SourceService.close

        def track_close(self_svc):
            closed.append(self_svc.state_dir)
            return real_close(self_svc)

        cmd_dir = self.root / 'cmd-model'
        cfg = {
            'task': 'Return final immediately.',
            'corpus_root': str(self.corpus),
            'workspace': str(self.workspace),
            'state_dir': str(self.state),
            'max_steps': 1,
            'model': {
                'kind': 'command',
                'directory': str(cmd_dir),
                'argv': [sys.executable, '-B', '-c', 'import json,sys; sys.stdin.read(); print(json.dumps({"kind":"final","answer":"hi"}))'],
            },
        }
        with patch.object(source_tools.SourceService, 'close', track_close):
            res = workbench_agent.run(cfg)
        self.assertEqual(res['status'], 'complete', res)
        self.assertEqual(len(closed), 1)

        # Even if model config fails inside run(), source_service is still closed!
        bad_cfg = dict(cfg, state_dir=str(self.root / 'state-err'), model={'kind': 'invalid'})
        with patch.object(source_tools.SourceService, 'close', track_close):
            with self.assertRaisesRegex(ValueError, 'model.kind'):
                workbench_agent.run(bad_cfg)
        self.assertEqual(len(closed), 2)

    # --- W4-B9: resolve command validates allowlisted fields without blind **splat ---
    def test_resolve_payload_rejects_extra_and_missing_fields(self):
        base = {
            'operation_id': '000001',
            'value': {'ok': True},
            'checkpoint': {},
            'evidence': 'Observed manually.',
            'expected_contract_sha256': 'a' * 64,
        }
        self.assertEqual(set(wb_config.validate_resolution_payload(base)), wb_config.RESOLVE_FIELDS)
        with self.assertRaisesRegex(ValueError, 'invalid fields'):
            wb_config.validate_resolution_payload({**base, 'state_dir': '/etc/passwd'})
        with self.assertRaisesRegex(ValueError, 'invalid fields'):
            wb_config.validate_resolution_payload({k: v for k, v in base.items() if k != 'evidence'})

        res_file = self.root / 'bad-resolution.json'
        res_file.write_text(json.dumps({**base, 'extra_arg': 1}), encoding='utf-8')
        proc = subprocess.run(
            [sys.executable, '-B', str(WORKBENCH_DIR / 'resumable_workbench.py'),
             'resolve', '--state-dir', str(self.state), '--resolution', str(res_file)],
            capture_output=True, text=True, encoding='utf-8',
        )
        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload['status'], 'state_error')
        self.assertIn('invalid fields', payload['error']['message'])

    # --- W4-B7 & W4-H2: CommandModel durability + strict JSON + explicit step/ordinal methods ---
    def test_command_model_step_controls_and_strict_json(self):
        cmd_dir = self.root / 'cm'
        cm = model_bridges.CommandModel(
            [sys.executable, '-B', '-c', 'import sys; sys.stdin.read(); sys.stdout.write(\'{"a":1,"a":2}\')'],
            cmd_dir,
        )
        cm.set_step(3)
        self.assertEqual(cm.calls, 2)
        cm.resume_at(0)
        self.assertEqual(cm.calls, 0)
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            cm({'step': 1})
        self.assertTrue((cmd_dir / '0001.request.json').is_file())
        self.assertTrue((cmd_dir / '0001.transport.json').is_file())

    # --- W4-H3: SearchImplementation binds canonical library/lexical.py + config.py and blocks escape ---
    def test_search_implementation_wave4_library_and_escape_refusal(self):
        library, entry = search_layout(self.root)
        impl = source_tools.SearchImplementation(entry)
        self.assertEqual(len(impl.pins), 2)
        self.assertEqual(impl.library_root, library.resolve())
        # Escape attempt via level=2 relative import above library_root must be refused.
        bad = library / 'bad_escape.py'
        bad.write_text('from ..outside import evil\n', encoding='utf-8', newline='\n')
        with self.assertRaisesRegex(ValueError, 'escapes bounded library root|bounded relative'):
            source_tools.SearchImplementation(bad)
        # Escape attempt via level=3 must also be refused.
        bad_deep = library / 'bad_escape_deep.py'
        bad_deep.write_text('from ...outside import evil\n', encoding='utf-8', newline='\n')
        with self.assertRaisesRegex(ValueError, 'bounded relative'):
            source_tools.SearchImplementation(bad_deep)

        # C48-3: default_search_paths() aligns with SearchImplementation (library/lexical.py + library/config.py).
        default_entry, default_deps = wb_config.default_search_paths()
        self.assertEqual(default_entry.name, 'lexical.py')
        self.assertEqual([p.name for p in default_deps], ['config.py'])
        loaded_default = source_tools.SearchImplementation.cached(default_entry)
        self.assertIn(str(default_entry.resolve()), loaded_default.pins)
        self.assertIn(str(default_deps[0].resolve()), loaded_default.pins)

    # --- W4-H6: inspect_config aligns controller_dir default and core/ package check ---
    def test_inspect_config_defaults_controller_dir_and_validates_wave4_layout(self):
        cfg_path = self.root / 'valid-cfg.json'
        cfg_path.write_text(json.dumps({
            'task': 'Check config.',
            'corpus_root': str(self.corpus),
            'workspace': str(self.workspace),
            'state_dir': str(self.state),
            'model': {'kind': 'mailbox', 'directory': str(self.root / 'mailbox')},
        }), encoding='utf-8')
        proc = subprocess.run(
            [sys.executable, '-B', str(WORKBENCH_DIR / 'inspect_config.py'), '--config', str(cfg_path)],
            capture_output=True, text=True, encoding='utf-8',
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['issues'], [])

    # --- C28-1: Windows drive-relative paths rejected across config, workbench_agent, source_tools ---
    def test_drive_relative_paths_rejected_in_workbench_and_source_tools(self):
        for bad in ('C:escape.txt', 'D:dir/file.txt', 'sub/C:escape.txt'):
            with self.subTest(bad=bad):
                self.assertFalse(wb_config.is_clean_relative_posix(bad))
                with self.assertRaises(ValueError):
                    workbench_agent.inside(self.workspace, bad)
        for bad_md in ('C:escape.md', 'sub/D:note.md'):
            with self.subTest(bad_md=bad_md):
                with self.assertRaises(ValueError):
                    source_tools.relative(bad_md)


if __name__ == '__main__':
    unittest.main(verbosity=2)
