"""Unit tests for capture_native qualification, composition, and public receipt inspection."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src"
HOST_DIR = SRC_ROOT / "managed_ai/host/continuing_work"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from managed_ai.host.continuing_work import (  # noqa: E402
    capture_native,
    native_bridge,
    repair_custody,
    scope_lifecycle,
)
from managed_ai.host.continuing_work.continuing_work import Host  # noqa: E402


class Wave4HostArchitectureTests(unittest.TestCase):
    def test_native_sources_narrows_to_deployment_core_store_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "src/managed_ai"
            included = (
                "platform/files.py",
                "deployment/select_deployment.py",
                "core/attention.py",
                "store/manage_store.py",
                "store/system.py",
            )
            excluded = (
                "store/test_ignored.py",
                "library/lexical.py",
                "tools/collect_returns.py",
                "host/continuing_work/continuing_work.py",
                "release/__init__.py",
                "companions/peer/peer_harness.py",
                "collective/store/__init__.py",
            )
            for rel in included + excluded:
                path = engine / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# {rel}\n", encoding="utf-8", newline="\n")

            native_path, programs = capture_native.native_sources(engine / "store/manage_store.py")
            self.assertEqual(native_path, (engine / "store/system.py").resolve())
            self.assertEqual(set(programs), set(included))
            self.assertFalse(
                any(
                    k.startswith(
                        ("library/", "tools/", "host/", "release/", "companions/", "collective/")
                    )
                    for k in programs
                )
            )
            self.assertNotIn("store/test_ignored.py", programs)

    def test_host_uses_composition_instead_of_mixin_inheritance(self):
        self.assertFalse(issubclass(Host, native_bridge.NativeBridge))
        self.assertFalse(issubclass(Host, repair_custody.RepairCustody))
        self.assertFalse(issubclass(Host, scope_lifecycle.ScopeLifecycle))
        with tempfile.TemporaryDirectory() as tmp:
            host = Host(Path(tmp) / "host")
            self.assertIsInstance(host.bridge, native_bridge.NativeBridge)
            self.assertIsInstance(host.repairs, repair_custody.RepairCustody)
            self.assertIsInstance(host.lifecycle, scope_lifecycle.ScopeLifecycle)

    def test_reconcile_assessment_uses_public_inspect_receipt_api(self):
        inspected = []
        supplied = {
            "id": "r-1",
            "assessments": [{"item": "x", "disposition": "considered", "account": "ok"}],
        }
        encoded_bytes = json.dumps(supplied, sort_keys=True, separators=(",", ":")).encode("utf-8")
        source_sha = hashlib.sha256(encoded_bytes).hexdigest()
        feedback_obj = "objects/fb1"
        receipt_obj = "objects/rc1"

        class FakeAttention:
            def inspect_receipt(self, rid):
                inspected.append(rid)
                return {
                    "id": rid,
                    "object": receipt_obj,
                    "record": {
                        "id": rid,
                        "producer": "auditor",
                        "feedback": "fb:turn-1",
                        "feedback_object": feedback_obj,
                        "source": {"sha256": source_sha},
                        "assessments": [
                            {"item": "x", "disposition": "considered", "account": "ok"}
                        ],
                        "boundary": "explicit",
                    },
                    "feedback_object": feedback_obj,
                }

            def _load(self):
                raise AssertionError("Private _load must not be called when inspect_receipt exists")

        class FakeExchange:
            def directory(self, turn):
                return Path("/tmp") / turn

            def packet_summary(self, directory, consumer, publications, *, returned, outcome):
                return {
                    "packet": directory.name,
                    "transport": "prepared",
                    "publications": publications,
                }

        fake_system = SimpleNamespace(
            attention=FakeAttention(),
            ex=FakeExchange(),
        )
        fake_native = SimpleNamespace(
            read=lambda path: json.loads(Path(path).read_bytes()),
            encode=lambda obj: json.dumps(obj, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            ),
            sha=lambda raw: hashlib.sha256(raw).hexdigest(),
            feedback_id=lambda turn: "fb:" + turn,
            packet_id=lambda raw_id: raw_id,
        )
        observed = {
            "consumer": "reader",
            "publications": [],
            "return": None,
            "transport_outcome": None,
        }

        with tempfile.TemporaryDirectory() as tmp:
            receipt_file = Path(tmp) / "receipt.json"
            receipt_file.write_bytes(encoded_bytes)
            matched = capture_native._reconcile_assessment(
                fake_native,
                fake_system,
                "turn-1",
                receipt_file,
                "auditor",
                observed,
            )
            self.assertEqual(inspected, ["r-1"])
            self.assertTrue(matched["reconciled"])
            self.assertEqual(
                matched["attention"],
                {
                    "receipt": "r-1",
                    "object": receipt_obj,
                    "recorded": ["x"],
                    "boundary": "explicit",
                },
            )
            # Wrong producer must refuse reconciliation.
            with self.assertRaisesRegex(ValueError, "receipt identity already used"):
                capture_native._reconcile_assessment(
                    fake_native,
                    fake_system,
                    "turn-1",
                    receipt_file,
                    "impostor",
                    observed,
                )
            # Different receipt bytes must refuse reconciliation.
            other_file = Path(tmp) / "other.json"
            other_file.write_bytes(
                json.dumps(
                    {
                        "id": "r-1",
                        "assessments": [
                            {"item": "x", "disposition": "deferred", "account": "changed"}
                        ],
                    }
                ).encode("utf-8")
            )
            with self.assertRaisesRegex(ValueError, "receipt identity already used"):
                capture_native._reconcile_assessment(
                    fake_native,
                    fake_system,
                    "turn-1",
                    other_file,
                    "auditor",
                    observed,
                )

    def test_repair_custody_overlap_limit_message_has_no_typo(self):
        combined = (
            (HOST_DIR / "continuing_work.py").read_text(encoding="utf-8")
            + (HOST_DIR / "scope_prepare.py").read_text(encoding="utf-8")
            + (HOST_DIR / "scope_lifecycle.py").read_text(encoding="utf-8")
        )
        self.assertNotIn("1 through 2000", combined)
        self.assertIn("from 1 through 200", combined)

    # --- C41-2b: qualified_engine context keeps external managed_ai mounted across lazy imports ---
    def test_qualified_engine_context_keeps_external_managed_ai_mounted_for_lazy_imports(self):
        import importlib

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ext_engine = root / "ext_pkg"
            for sub in ("deployment", "core", "store"):
                (ext_engine / sub).mkdir(parents=True)
                (ext_engine / sub / "__init__.py").write_text("", encoding="utf-8", newline="\n")
            (ext_engine / "__init__.py").write_text(
                "ENGINE_MARKER = 'EXTERNAL_ENGINE'\n",
                encoding="utf-8",
                newline="\n",
            )
            (ext_engine / "store/manage_store.py").write_text(
                "# external entry\n", encoding="utf-8", newline="\n"
            )
            (ext_engine / "store/lazy_helper.py").write_text(
                "SOURCE = 'EXTERNAL_LAZY_HELPER'\n",
                encoding="utf-8",
                newline="\n",
            )
            (ext_engine / "store/system.py").write_text(
                "\n".join(
                    [
                        "from contextlib import contextmanager",
                        "from pathlib import Path",
                        "from types import SimpleNamespace",
                        "ENTRYPOINT = SimpleNamespace(",
                        "    engine_api='managed-ai-runtime/1',",
                        "    deployment_api='agent-system-deployment/2',",
                        ")",
                        "@contextmanager",
                        "def exclusive(lock_path):",
                        "    yield",
                        "class Attention:",
                        "    def inspect_receipt(self, rid): return None",
                        "class Exchange:",
                        "    def __init__(self, root):",
                        "        self.root = Path(root) / 'exchange'",
                        "    def inspect(self, turn):",
                        (
                            "        return {'consumer': 'reader', 'publications': [],"
                            " 'return': None, 'transport_outcome': None}"
                        ),
                        "    def directory(self, turn): return self.root / 'packets' / turn",
                        (
                            "    def packet_summary(self, directory, consumer, publications,"
                            " *, returned, outcome):"
                        ),
                        "        from managed_ai import ENGINE_MARKER",
                        "        from managed_ai.store.lazy_helper import SOURCE",
                        (
                            "        return {'packet': directory.name, 'transport': 'prepared',"
                            " 'publications': publications,"
                        ),
                        "                'marker': ENGINE_MARKER, 'lazy_source': SOURCE}",
                        "class System:",
                        "    def __init__(self, root):",
                        "        self.root = Path(root)",
                        "        self.ex = Exchange(root)",
                        "        self.attention = Attention()",
                        "    def assess(self, turn, receipt, producer): return {}",
                        "def resolve_lazy():",
                        "    from managed_ai import ENGINE_MARKER",
                        "    from managed_ai.store.lazy_helper import SOURCE",
                        "    return (ENGINE_MARKER, SOURCE)",
                    ]
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            ext_entry = ext_engine / "store/manage_store.py"
            _, programs = capture_native.native_sources(ext_entry)
            live_profile = capture_native._profile_digest(programs)

            colocated_init = (SRC_ROOT / "managed_ai/__init__.py").resolve()
            self.assertEqual(Path(sys.modules["managed_ai"].__file__).resolve(), colocated_init)

            # In-process context keeps external managed_ai.* mounted for lazy imports and blocks
            # co-located leaks.
            with capture_native.qualified_engine(ext_entry, expected_profile=live_profile) as (
                mod,
                info,
            ):
                self.assertEqual(info["admission"], "expected-profile")
                self.assertEqual(info["profile"], live_profile)
                self.assertEqual(mod.resolve_lazy(), ("EXTERNAL_ENGINE", "EXTERNAL_LAZY_HELPER"))
                self.assertEqual(
                    Path(sys.modules["managed_ai.store.lazy_helper"].__file__).resolve(),
                    (ext_engine / "store/lazy_helper.py").resolve(),
                )
                with self.assertRaises(ModuleNotFoundError):
                    importlib.import_module("managed_ai.host.continuing_work.occurrence_files")

            # Context exit cleanly restores co-located managed_ai.* and removes external submodules.
            self.assertEqual(Path(sys.modules["managed_ai"].__file__).resolve(), colocated_init)
            self.assertNotIn("managed_ai.store.lazy_helper", sys.modules)

            # End-to-end CLI invocation executes System + packet_summary lazy imports inside
            # qualified_engine().
            fake_store = root / "fake-store"
            (fake_store / "exchange").mkdir(parents=True)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(HOST_DIR / "capture_native.py"),
                    "--engine",
                    str(ext_entry),
                    "--root",
                    str(fake_store),
                    "--expected-profile",
                    live_profile,
                    "inspect-turn",
                    "turn-lazy",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["turn"]["marker"], "EXTERNAL_ENGINE")
            self.assertEqual(out["turn"]["lazy_source"], "EXTERNAL_LAZY_HELPER")
            self.assertEqual(out["capture_adapter"]["admission"], "expected-profile")

    # --- Wave 5 (#213 Part B): Co-located auto-admission + external --expected-profile /
    # --deployment ---
    def test_colocated_engine_auto_admits_and_records_live_profile_and_git_commit(self):
        entry = SRC_ROOT / "managed_ai/store/manage_store.py"
        module, info = capture_native.qualify_engine(entry)
        self.assertEqual(info["admission"], "colocated")
        self.assertEqual(len(info["profile"]), 64)
        self.assertTrue(hasattr(module, "ENTRYPOINT"))
        self.assertTrue(callable(getattr(module.Attention, "inspect_receipt", None)))
        self.assertTrue(callable(getattr(module.Exchange, "packet_summary", None)))
        self.assertTrue(callable(getattr(module.System, "assess", None)))
        if info["git_head_commit"] is not None:
            self.assertEqual(len(info["git_head_commit"]), 40)

        # Hermetic test of _git_head_commit() covering direct .git/ refs AND linked worktree +
        # packed-refs.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            direct_repo = root / "direct"
            (direct_repo / ".git/refs/heads").mkdir(parents=True)
            (direct_repo / ".git/HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (direct_repo / ".git/refs/heads/main").write_text("a" * 40 + "\n", encoding="utf-8")
            eng = direct_repo / "engine"
            eng.mkdir()
            self.assertEqual(capture_native._git_head_commit(eng), "a" * 40)

            common_git = root / "common-git"
            wt_git = common_git / "worktrees/wt1"
            wt_git.mkdir(parents=True)
            (common_git / "packed-refs").write_text(
                "# pack-refs\n" + ("b" * 40) + " refs/heads/feature\n",
                encoding="utf-8",
            )
            (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
            (wt_git / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
            wt_dir = root / "wt"
            (wt_dir / "engine").mkdir(parents=True)
            (wt_dir / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")
            self.assertEqual(capture_native._git_head_commit(wt_dir / "engine"), "b" * 40)

    def test_external_engine_refused_without_pin_and_admitted_with_expected_profile_or_deployment(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ext_engine = root / "ext_engine"
            ext_engine.mkdir()
            shutil.copy2(SRC_ROOT / "managed_ai/__init__.py", ext_engine / "__init__.py")
            for subdir in ("deployment", "core", "store", "platform"):
                shutil.copytree(
                    SRC_ROOT / "managed_ai" / subdir,
                    ext_engine / subdir,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            ext_entry = ext_engine / "store/manage_store.py"

            # Inject a side-effect sentinel into external store/system.py to prove untrusted code is
            # NEVER executed.
            sentinel = root / "should-not-execute.txt"
            ext_system = ext_engine / "store/system.py"
            orig_system = ext_system.read_text(encoding="utf-8")
            ext_system.write_text(
                "from pathlib import Path as _P\n"
                f"_P({str(sentinel)!r}).write_text('RAN', encoding='utf-8')\n" + orig_system,
                encoding="utf-8",
                newline="\n",
            )

            # 1. External engine WITHOUT --expected-profile or --deployment is refused before exec!
            with self.assertRaisesRegex(ValueError, "External native engine requires"):
                capture_native.qualify_engine(ext_entry)
            self.assertFalse(sentinel.exists())

            # 2. External engine with WRONG --expected-profile is refused before exec!
            with self.assertRaisesRegex(ValueError, "does not match"):
                capture_native.qualify_engine(ext_entry, expected_profile="0" * 64)
            self.assertFalse(sentinel.exists())

            # Restore clean external system.py and compute its live profile.
            ext_system.write_text(orig_system, encoding="utf-8", newline="\n")
            _, programs = capture_native.native_sources(ext_entry)
            live_profile = capture_native._profile_digest(programs)

            # 3. External engine with matching --expected-profile succeeds & stays mounted across
            # calls!
            store_dir = root / "store"
            with capture_native.qualified_engine(ext_entry, expected_profile=live_profile) as (
                mod_prof,
                info_prof,
            ):
                self.assertEqual(info_prof["admission"], "expected-profile")
                self.assertEqual(info_prof["profile"], live_profile)
                # 4. Initialize a real store inside the qualified external context.
                mod_prof.System.initialize(
                    store_dir, {"contribution": "Test external deployment qualification."}
                )

            # Initialize real Deployment pointing to ext_engine, then qualify via --deployment!
            dep_dir = root / "deployment"
            from managed_ai.deployment.controller import Deployment

            Deployment.initialize(dep_dir, store_dir, engine=ext_engine)

            # CLI checks: unqualified refused (rc=2), --expected-profile accepted (rc=0),
            # --deployment accepted (rc=0)!
            cli = str(HOST_DIR / "capture_native.py")
            p_refused = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    cli,
                    "--engine",
                    str(ext_entry),
                    "--root",
                    str(store_dir),
                    "inspect-turn",
                    "start",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(p_refused.returncode, 2)
            self.assertIn("External native engine requires", json.loads(p_refused.stderr)["error"])

            p_profile = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    cli,
                    "--engine",
                    str(ext_entry),
                    "--root",
                    str(store_dir),
                    "--expected-profile",
                    live_profile,
                    "inspect-turn",
                    "start",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(p_profile.returncode, 0, p_profile.stderr)
            out_prof = json.loads(p_profile.stdout)
            self.assertEqual(out_prof["capture_adapter"]["admission"], "expected-profile")
            self.assertEqual(out_prof["capture_adapter"]["native_profile_sha256"], live_profile)

            p_dep = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    cli,
                    "--engine",
                    str(ext_entry),
                    "--root",
                    str(store_dir),
                    "--deployment",
                    str(dep_dir),
                    "inspect-turn",
                    "start",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(p_dep.returncode, 0, p_dep.stderr)
            out_dep = json.loads(p_dep.stdout)
            self.assertEqual(out_dep["capture_adapter"]["admission"], "deployment-pin")
            self.assertEqual(out_dep["capture_adapter"]["native_profile_sha256"], live_profile)

            # 5. Tampering one byte of ext_engine breaks --deployment pin!
            formats_py = ext_engine / "core/formats.py"
            formats_py.write_text(
                formats_py.read_text(encoding="utf-8") + "\n# drift\n",
                encoding="utf-8",
                newline="\n",
            )
            p_tampered = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    cli,
                    "--engine",
                    str(ext_entry),
                    "--root",
                    str(store_dir),
                    "--deployment",
                    str(dep_dir),
                    "inspect-turn",
                    "start",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(p_tampered.returncode, 2)
            self.assertIn(
                "code differs from verified deployment pin", json.loads(p_tampered.stderr)["error"]
            )

            # 6. Symlink / NTFS junction inside external engine tree is refused!
            extra_dir = root / "outside"
            extra_dir.mkdir()
            link_inside = ext_engine / "store/linked_subdir"
            if os.name == "nt":
                mk = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link_inside), str(extra_dir)],
                    capture_output=True,
                )
                made = mk.returncode == 0
            else:
                link_inside.symlink_to(extra_dir)
                made = True
            if made:
                try:
                    with self.assertRaisesRegex(ValueError, "symlinks or reparse points"):
                        capture_native.native_sources(ext_entry)
                finally:
                    if os.name == "nt":
                        subprocess.run(
                            ["cmd", "/c", "rmdir", str(link_inside)], capture_output=True
                        )
                    else:
                        link_inside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
