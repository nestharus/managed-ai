"""Wave 4 regression tests for companions/representation (W4-B2, W4-B6)."""

import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src"
REP_DIR = SRC_ROOT / "managed_ai/companions/representation"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from managed_ai.companions.representation import representation  # noqa: E402


class RepresentationWave4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_run_cli_action_acquires_owner_lock_before_constructing_system(self):
        events = []
        store_root = self.root / "store"
        work = store_root / "work"
        exchange = store_root / "exchange"
        work.mkdir(parents=True)
        exchange.mkdir(parents=True)
        (work / "basis.md").write_text("UTF-8 café basis.\n", encoding="utf-8")

        @contextmanager
        def fake_exclusive(lock_path):
            events.append(("lock_enter", Path(lock_path).name))
            try:
                yield
            finally:
                events.append(("lock_exit", Path(lock_path).name))

        class FakeLayout:
            def __init__(self, root):
                self.exchange = Path(root) / "exchange"

        class FakeExchange:
            def __init__(self):
                self.root = exchange
                self.workspace = work

            def path(self, rel):
                return work / rel

        class FakeSystem:
            def __init__(self, root):
                events.append(("system_init", str(root)))
                self.root = Path(root)
                self.ex = FakeExchange()

        args = Namespace(
            engine=self.root / "engine",
            root=store_root,
            action="pin",
            path="basis.md",
            accepted_in=None,
        )

        def fake_loader(engine):
            return (
                FakeSystem,
                fake_exclusive,
                FakeLayout,
                lambda x: x,
                lambda x: x,
            )

        with patch.object(representation, "_artifact_target_fn", return_value=lambda x: x):
            result = representation.run_cli_action(args, engine_loader=fake_loader)
        self.assertEqual(result["path"], "basis.md")
        self.assertEqual(
            [e[0] for e in events],
            ["lock_enter", "system_init", "lock_exit"],
        )
        self.assertEqual(events[0][1], "owner.lock")

    def test_prepare_cleans_up_directory_on_context_failure_so_retry_succeeds(self):
        store = self.root / "store"
        work = store / "work"
        work.mkdir(parents=True)
        src = work / "basis.md"
        src.write_text("UTF-8 café basis.\n", encoding="utf-8")
        attempts = {"n": 0}

        class FakeExchange:
            def __init__(self):
                self.workspace = work

            def path(self, rel):
                return work / rel

        class FakeSystem:
            def __init__(self):
                self.root = store
                self.ex = FakeExchange()

            def context(self, packet_id, spec):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise RuntimeError("transient context failure")
                return {"packet": packet_id, "spec": spec}

        sys_obj = FakeSystem()
        ops = representation.RepresentationOperations(sys_obj)
        src_pin = {"path": "basis.md", "sha256": representation.digest(src.read_bytes())}
        spec = {
            "format": representation.OPERATION,
            "turn": "rep-1",
            "mode": "develop",
            "task": "Describe basis.",
            "consumer": "reviewer",
            "representations": [],
            "sources": [src_pin],
            "targets": ["out.json"],
            "applications": [
                {
                    "id": "app-1",
                    "sources": ["basis.md"],
                    "representations": [],
                    "operation": "Summarize basis.",
                    "prediction": "Produces out.json.",
                }
            ],
            "interactions": {"account": "Independent unit test.", "unknowns": []},
        }
        with (
            patch.object(representation, "_packet_id_fn", return_value=lambda x: x),
            patch.object(representation, "_artifact_target_fn", return_value=lambda x: x),
        ):
            with self.assertRaisesRegex(RuntimeError, "transient context failure"):
                ops.prepare(spec)
            rep_dir = work / "representation-operations/rep-1"
            # Directory was cleaned up so immediate retry with same turn is NOT wedged!
            self.assertFalse(rep_dir.exists())

            res = ops.prepare(spec)
            self.assertEqual(res["packet"], "rep-1")
            self.assertTrue((rep_dir / "operation.json").is_file())
            self.assertTrue((rep_dir / "contract.md").is_file())

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers(self):
        dup = self.root / "dup.json"
        dup.write_text('{"id":"a","id":"b"}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            representation.read_json(dup)

        nan_file = self.root / "nan.json"
        nan_file.write_text('{"val":NaN}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "nonfinite JSON"):
            representation.read_json(nan_file)

    # --- W5-B11: NTFS junction / symlink refusal in pin(), check_pin(), and _atomic_bytes() ---
    def test_pin_check_pin_and_atomic_bytes_refuse_reparse_or_symlink(self):
        import os
        import subprocess

        store = self.root / "store"
        work = store / "work"
        work.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.json").write_text('{"k": 1}\n', encoding="utf-8")
        link_dir = work / "linked"
        if os.name == "nt":
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_dir), str(outside)], capture_output=True
            )
            if proc.returncode != 0:
                return
            self.addCleanup(
                lambda: subprocess.run(["cmd", "/c", "rmdir", str(link_dir)], capture_output=True)
            )
        else:
            link_dir.symlink_to(outside)

        class FakeExchange:
            def __init__(self):
                self.workspace = work

            def path(self, rel):
                return work / rel

        class FakeSystem:
            def __init__(self):
                self.ex = FakeExchange()

        ops = representation.RepresentationOperations(FakeSystem())
        with patch.object(representation, "_artifact_target_fn", return_value=lambda x: x):
            with self.assertRaisesRegex(ValueError, "ordinary file"):
                ops.pin("linked/secret.json")
            with self.assertRaisesRegex(ValueError, "ordinary file"):
                ops.check_pin(
                    {
                        "path": "linked/secret.json",
                        "sha256": representation.digest((outside / "secret.json").read_bytes()),
                    }
                )
        with self.assertRaisesRegex(ValueError, "symlink or reparse point"):
            representation._atomic_bytes(link_dir / "out.json", b"{}")

    # --- C47-1: external --engine mounting must load external package, not local checkout ---
    def test_c47_1_mount_engine_and_cli_load_external_engine_not_local(self):
        import json
        import os
        import shutil
        import subprocess

        ext_engine = self.root / "ext_synth" / "managed_ai"
        marker_log = self.root / "synth_marker.log"
        for sub in ("core", "store", "store/protocol"):
            (ext_engine / sub).mkdir(parents=True, exist_ok=True)
            (ext_engine / sub / "__init__.py").write_text("", encoding="utf-8", newline="\n")
        (ext_engine / "__init__.py").write_text("MARKER = 'EXT'\n", encoding="utf-8", newline="\n")
        (ext_engine / "core/storage.py").write_text(
            "from contextlib import contextmanager\n"
            "@contextmanager\n"
            "def exclusive(path):\n"
            "    yield\n",
            encoding="utf-8",
            newline="\n",
        )
        (ext_engine / "core/ownership.py").write_text(
            "\n".join(
                [
                    "from pathlib import Path",
                    "class NativeLayout:",
                    "    def __init__(self, root):",
                    "        self.exchange = Path(root) / 'exchange'",
                    "def packet_id(turn):",
                    f"    with open({str(marker_log)!r}, 'a', encoding='utf-8') as f:",
                    "        f.write('external_packet_id\\n')",
                    "    return turn",
                ]
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        (ext_engine / "store/protocol/inputs.py").write_text(
            "\n".join(
                [
                    "def artifact_target(path):",
                    f"    with open({str(marker_log)!r}, 'a', encoding='utf-8') as f:",
                    "        f.write('external_artifact_target\\n')",
                    "    return path",
                ]
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        (ext_engine / "store/system.py").write_text(
            "\n".join(
                [
                    "from pathlib import Path",
                    "class _FakeEx:",
                    "    def __init__(self, root):",
                    "        self.root = Path(root) / 'exchange'",
                    "        self.workspace = Path(root) / 'work'",
                    "    def path(self, rel):",
                    "        return self.workspace / rel",
                    "class System:",
                    "    ENGINE_SOURCE = 'EXTERNAL_SYNTH'",
                    "    def __init__(self, root):",
                    f"        with open({str(marker_log)!r}, 'a', encoding='utf-8') as f:",
                    "            f.write('external_system_init\\n')",
                    "        self.root = Path(root)",
                    "        self.ex = _FakeEx(root)",
                ]
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

        local_pkg_file = Path(sys.modules["managed_ai"].__file__).resolve()
        with representation.mount_engine(ext_engine) as (sys_cls, _, _, _, _):
            self.assertEqual(getattr(sys_cls, "ENGINE_SOURCE", None), "EXTERNAL_SYNTH")
            self.assertEqual(
                Path(sys.modules["managed_ai.store.system"].__file__).resolve(),
                (ext_engine / "store/system.py").resolve(),
            )
            self.assertEqual(representation._packet_id_fn()("t-1"), "t-1")
            self.assertEqual(representation._artifact_target_fn()("a.json"), "a.json")

        self.assertEqual(Path(sys.modules["managed_ai"].__file__).resolve(), local_pkg_file)

        store_root = self.root / "cli_store"
        (store_root / "exchange").mkdir(parents=True)
        (store_root / "work").mkdir(parents=True)
        (store_root / "work/basis.md").write_text("external pin test\n", encoding="utf-8")
        marker_log.write_text("", encoding="utf-8")

        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        rep_proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REP_DIR / "representation.py"),
                "--engine",
                str(ext_engine),
                "--root",
                str(store_root),
                "pin",
                "basis.md",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(rep_proc.returncode, 0, rep_proc.stderr)
        self.assertEqual(json.loads(rep_proc.stdout)["path"], "basis.md")
        events = marker_log.read_text(encoding="utf-8").splitlines()
        self.assertIn("external_system_init", events)
        self.assertIn("external_artifact_target", events)

        ext_real = self.root / "ext_real" / "managed_ai"
        ext_real.mkdir(parents=True)
        local_engine = SRC_ROOT / "managed_ai"
        shutil.copy2(local_engine / "__init__.py", ext_real / "__init__.py")
        for sub in ("platform", "core", "store"):
            shutil.copytree(
                local_engine / sub,
                ext_real / sub,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        check_marker = self.root / "check_rep_marker.txt"
        sys_file = ext_real / "store/system.py"
        sys_file.write_text(
            sys_file.read_text(encoding="utf-8")
            + "\n"
            + "\n".join(
                [
                    f"with open({str(check_marker)!r}, 'a', encoding='utf-8') as _mf:",
                    "    _mf.write('EXTERNAL_CHECK_LOADED\\n')",
                ]
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        chk_proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(REP_DIR / "check_representation.py"),
                "--engine",
                str(ext_real),
                "NativeRepresentationChecks.test_selected_accepted_state_and_application_are_actual_context_inputs",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(chk_proc.returncode, 0, chk_proc.stderr)
        self.assertTrue(
            check_marker.is_file(), "check_representation.py failed to load external --engine"
        )
        self.assertIn("EXTERNAL_CHECK_LOADED", check_marker.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
