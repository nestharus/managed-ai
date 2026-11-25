"""Wave 4 regression tests for companions/peer (W4-B3, W4-H4, W4-H5, W4-B7, W4-B8)."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
PEER_DIR = REPO_ROOT / 'companions/peer'
if str(PEER_DIR) not in sys.path:
    sys.path.insert(0, str(PEER_DIR))

import peer_harness
import peer_protocol
import peer_workbench


WORKER_ECHO = """
import base64, json, sys
req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
owner = req["owner"]
rev = req["revision"]
if owner == "alpha" and rev == 1:
    body = b"from-alpha\\n"
    resp = {
        "schema": "peer-return/1", "owner": owner, "revision": rev,
        "state": {"done": True}, "status": "complete", "obligations": [],
        "ack": [], "send": [{"id": "m1", "to": "beta", "payload": {"note": "ready"}}],
        "files": {"alpha.txt": base64.b64encode(body).decode("ascii")},
    }
elif owner == "beta" and not req["inbox"]:
    resp = {
        "schema": "peer-return/1", "owner": owner, "revision": rev,
        "state": {"waited": True}, "status": "waiting", "obligations": ["need-alpha"],
        "ack": [], "send": [], "files": {},
    }
else:
    alpha_peer = req["peers"]["alpha"]
    assert alpha_peer["manifest_path"] is not None, "explicit manifest_path missing"
    manifest = json.loads(open(alpha_peer["manifest_path"], "rb").read().decode("utf-8"))
    assert manifest[0]["path"] == "alpha.txt"
    body = b"from-beta:" + str(len(req["inbox"])).encode("ascii") + b"\\n"
    resp = {
        "schema": "peer-return/1", "owner": owner, "revision": rev,
        "state": {"done": True}, "status": "complete", "obligations": [],
        "ack": [m["id"] for m in req["inbox"]], "send": [],
        "files": {"beta.txt": base64.b64encode(body).decode("ascii")},
    }
sys.stdout.buffer.write((json.dumps(resp) + "\\n").encode("utf-8"))
"""


class PeerHarnessAndWorkbenchWave4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    # --- W4-B3 + W4-H4: Native Windows + POSIX peer_harness run with explicit manifest_path ---
    def test_peer_harness_end_to_end_cross_platform_and_manifest_path(self):
        worker_py = self.root / 'worker.py'
        worker_py.write_text(WORKER_ECHO, encoding='utf-8', newline='\n')
        cfg = {
            'schema': 'peer-harness-config/1',
            'run_id': 'w4-xplat-run',
            'brief': 'Two owners exchange a message and submit artifacts.',
            'workers': [
                {'owner': 'alpha', 'command': [sys.executable, '-B', str(worker_py)], 'input': {}},
                {'owner': 'beta', 'command': [sys.executable, '-B', str(worker_py)], 'input': {}},
            ],
            'limits': {
                'parallelism': 2, 'max_turns': 8,
                'max_output_bytes': 65536, 'max_artifact_bytes': 65536,
                'wall_seconds': 30, 'turn_seconds': 15, 'terminate_grace_seconds': 2,
            },
        }
        cfg_path = self.root / 'harness-config.json'
        cfg_path.write_text(json.dumps(cfg), encoding='utf-8')
        run_dir = self.root / 'run'
        proc = subprocess.run(
            [sys.executable, '-B', str(PEER_DIR / 'peer_harness.py'),
             '--config', str(cfg_path), '--run-dir', str(run_dir)],
            capture_output=True, text=True, encoding='utf-8',
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads((run_dir / 'result.json').read_bytes())
        self.assertTrue(result['submitted'], result)
        self.assertEqual(result['done_reason'], 'submitted')
        self.assertIsNotNone(result['owners']['alpha']['manifest_path'])
        self.assertTrue(Path(result['owners']['alpha']['manifest_path']).is_file())
        self.assertEqual((run_dir / 'submitted/alpha.txt').read_bytes(), b'from-alpha\n')
        self.assertEqual((run_dir / 'submitted/beta.txt').read_bytes(), b'from-beta:1\n')

    def test_peer_harness_enforces_worker_timeout_and_output_limit_cross_platform(self):
        # 1. Worker timeout
        sleep_cfg = {
            'schema': 'peer-harness-config/1', 'run_id': 'timeout-run', 'brief': 'Timeout test',
            'workers': [{'owner': 'slow', 'command': [sys.executable, '-B', '-c', 'import time; time.sleep(10)'], 'input': {}}],
            'limits': {
                'parallelism': 1, 'max_turns': 2, 'max_output_bytes': 4096, 'max_artifact_bytes': 4096,
                'wall_seconds': 15, 'turn_seconds': 0.4, 'terminate_grace_seconds': 0.5,
            },
        }
        run_t = self.root / 'run-timeout'
        run_t.mkdir()
        res_t = peer_harness.Harness(sleep_cfg, peer_protocol.canonical(sleep_cfg), run_t).run()
        self.assertFalse(res_t['submitted'])
        self.assertEqual(res_t['done_reason'], 'worker_timeout')

        # 2. Output limit
        loud_cfg = {
            'schema': 'peer-harness-config/1', 'run_id': 'loud-run', 'brief': 'Output limit test',
            'workers': [{'owner': 'loud', 'command': [sys.executable, '-B', '-c', 'import sys; sys.stdout.write("A" * 5000)'], 'input': {}}],
            'limits': {
                'parallelism': 1, 'max_turns': 2, 'max_output_bytes': 128, 'max_artifact_bytes': 4096,
                'wall_seconds': 15, 'turn_seconds': 5, 'terminate_grace_seconds': 0.5,
            },
        }
        run_l = self.root / 'run-loud'
        run_l.mkdir()
        res_l = peer_harness.Harness(loud_cfg, peer_protocol.canonical(loud_cfg), run_l).run()
        self.assertFalse(res_l['submitted'])
        self.assertEqual(res_l['done_reason'], 'output_limit')

    # --- W4-H4 + W4-H5 + W4-B8: explicit manifest_path without harness layout coupling, clean sys.modules, closed SourceService ---
    def test_peer_workbench_reads_explicit_checkpoint_paths_and_closes_source_service(self):
        corpus = self.root / 'corpus'
        corpus.mkdir()
        # Non-harness workspace path (NOT under workspaces/<owner>!)
        workspace = self.root / 'custom-workspace'
        workspace.mkdir()
        # Peer checkpoint placed in an arbitrary unrelated directory!
        ckpt = self.root / 'external-peer-store/alpha-v1'
        files_dir = ckpt / 'files'
        files_dir.mkdir(parents=True)
        payload = b'peer artifact content\n'
        (files_dir / 'shared.txt').write_bytes(payload)
        resp_doc = {
            'schema': 'peer-return/1', 'owner': 'alpha', 'revision': 1,
            'state': {}, 'status': 'complete', 'obligations': [], 'ack': [], 'send': [],
            'files': {'shared.txt': base64.b64encode(payload).decode('ascii')},
        }
        manifest_doc = [{
            'path': 'shared.txt', 'bytes': len(payload),
            'sha256': peer_protocol.digest(payload), 'owner': 'alpha', 'revision': 1,
        }]
        return_path = ckpt / 'response.json'
        manifest_path = ckpt / 'manifest.json'
        peer_protocol.atomic_write_json(return_path, resp_doc, exclusive=True)
        peer_protocol.atomic_write_json(manifest_path, manifest_doc, exclusive=True)

        request = {
            'schema': 'peer-request/1', 'run_id': 'explicit-paths-test', 'owner': 'beta',
            'turn': 1, 'revision': 1, 'brief': 'Read peer via explicit protocol paths.',
            'input': {}, 'state': None, 'inbox': [],
            'peers': {
                'alpha': {
                    'revision': 1, 'status': 'complete',
                    'return_path': str(return_path.resolve()),
                    'manifest_path': str(manifest_path.resolve()),
                    'artifacts_dir': str(files_dir.resolve()),
                },
            },
            'workspace': str(workspace.resolve()),
            'limits_remaining': {
                'wall_seconds': 30, 'turns': 3, 'turn_seconds': 10,
                'max_output_bytes': 100000, 'max_artifact_bytes': 10000,
            },
        }
        config = {
            'schema': 'peer-workbench-config/1', 'task': 'Read alpha artifact and complete.',
            'sources': {}, 'corpus_root': str(corpus.resolve()),
            'evidence_root': str((self.root / 'evidence').resolve()),
            'model': {'kind': 'mailbox'}, 'max_steps': 4, 'obligations': [],
        }

        modules, _ = peer_workbench.components()
        # W4-H5: components() must NOT pollute global unprefixed sys.modules names.
        self.assertNotIn('evidence_support', sys.modules)
        self.assertNotIn('agent_runtime', sys.modules)

        closed = []
        real_close = modules['source_tools'].SourceService.close

        def track_close(svc):
            closed.append(svc.state_dir)
            return real_close(svc)

        observed_peer = {}

        def model(req):
            if req['step'] == 1:
                return {'kind': 'tool', 'name': 'peer_read', 'arguments': {'owner': 'alpha', 'paths': ['shared.txt']}}
            if req['step'] == 2:
                observed_peer['body'] = req['history'][-1]['value']['files']['shared.txt']['body']
                return {'kind': 'tool', 'name': 'peer_stage_return', 'arguments': {
                    'status': 'complete', 'state': {}, 'obligations': [], 'ack': [], 'send': [], 'files': [],
                }}
            return {'kind': 'final', 'answer': 'Read peer cleanly.'}

        with patch.object(modules['source_tools'].SourceService, 'close', track_close):
            res = peer_workbench.run_peer_turn(config, request, model=model)
        self.assertTrue(res['ok'], res)
        self.assertEqual(observed_peer['body'], 'peer artifact content\n')
        self.assertEqual(len(closed), 1)

    # --- C28-1: Windows drive-relative path escape refused in protocol & harness ---
    def test_drive_relative_target_paths_are_rejected_by_protocol_and_harness(self):
        for bad in ('C:escape.txt', 'D:foo/bar.txt', 'sub/C:escape.txt', 'a\\b.txt'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    peer_protocol.target_path(bad)

        evil_worker = self.root / 'evil_worker.py'
        evil_worker.write_text(
            'import base64, json, sys\n'
            'req = json.loads(sys.stdin.buffer.read().decode("utf-8"))\n'
            'resp = {"schema": "peer-return/1", "owner": req["owner"], "revision": req["revision"],\n'
            '        "state": {}, "status": "complete", "obligations": [], "ack": [], "send": [],\n'
            '        "files": {"C:escape.txt": base64.b64encode(b"pwn").decode("ascii")}}\n'
            'sys.stdout.buffer.write((json.dumps(resp) + "\\n").encode("utf-8"))\n',
            encoding='utf-8', newline='\n',
        )
        cfg = {
            'schema': 'peer-harness-config/1', 'run_id': 'drive-escape', 'brief': 'Refuse drive-relative path',
            'workers': [{'owner': 'evil', 'command': [sys.executable, '-B', str(evil_worker)], 'input': {}}],
            'limits': {
                'parallelism': 1, 'max_turns': 2, 'max_output_bytes': 4096, 'max_artifact_bytes': 4096,
                'wall_seconds': 15, 'turn_seconds': 5, 'terminate_grace_seconds': 1,
            },
        }
        run_dir = self.root / 'run-drive-escape'
        run_dir.mkdir()
        res = peer_harness.Harness(cfg, peer_protocol.canonical(cfg), run_dir).run()
        self.assertFalse(res['submitted'])
        self.assertEqual(res['done_reason'], 'protocol_error')


if __name__ == '__main__':
    unittest.main(verbosity=2)
