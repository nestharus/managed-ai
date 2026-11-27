"""Wave 7/8 architecture guards and regression tests for host, companions, and collective."""

from __future__ import annotations

import ast
import gc
import io
import json
import subprocess
import sys
import tempfile
import tokenize
import unittest
import zipfile
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

OWNED_PACKAGE_ROOTS = (
    SRC_ROOT / "managed_ai_information/host",
    SRC_ROOT / "managed_ai_information/companions",
    SRC_ROOT / "managed_ai_information/collective",
)

FORBIDDEN_OS_MODULES = frozenset({"fcntl", "msvcrt", "ctypes", "resource"})
FORBIDDEN_WIN32_NAMES = frozenset({"WinDLL", "LockFileEx", "UnlockFileEx"})


def _iter_owned_py_files() -> list[Path]:
    files: list[Path] = []
    for root in OWNED_PACKAGE_ROOTS:
        files.extend(sorted(root.rglob("*.py")))
    return files


class Wave7ArchitectureGuardTests(unittest.TestCase):
    """Enforce Wave 7 architectural invariants across host, companions, and optional."""

    def test_zero_os_branching_load_sibling_semicolons_crlf_and_bare_json_loads(self) -> None:
        files = _iter_owned_py_files()
        self.assertGreaterEqual(len(files), 70)

        semicolons: list[str] = []
        os_violations: list[str] = []
        load_sibling_hits: list[str] = []
        crlf_files: list[str] = []
        bare_json_loads: list[str] = []
        missing_allow_abbrev: list[str] = []
        total_lines = 0
        blank_lines = 0

        for path in files:
            rel = path.relative_to(REPO_ROOT).as_posix()
            raw = path.read_bytes()
            if b"\r\n" in raw:
                crlf_files.append(rel)

            text = raw.decode("utf-8")
            lines = text.splitlines()
            total_lines += len(lines)
            blank_lines += sum(1 for line in lines if not line.strip())

            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type == tokenize.OP and token.string == ";":
                    semicolons.append(f"{rel}:{token.start[0]}")

            tree = ast.parse(text, filename=rel)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".", 1)[0]
                        if top in FORBIDDEN_OS_MODULES:
                            os_violations.append(f"{rel}:{node.lineno}:import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    top = (node.module or "").split(".", 1)[0]
                    if top in FORBIDDEN_OS_MODULES:
                        os_violations.append(f"{rel}:{node.lineno}:from {node.module}")
                elif isinstance(node, ast.Attribute):
                    if (
                        isinstance(node.value, ast.Name)
                        and node.value.id == "os"
                        and node.attr == "name"
                    ):
                        os_violations.append(f"{rel}:{node.lineno}:os.name")
                    if node.attr in FORBIDDEN_WIN32_NAMES:
                        os_violations.append(f"{rel}:{node.lineno}:.{node.attr}")
                    if node.attr == "_load_sibling":
                        load_sibling_hits.append(f"{rel}:{node.lineno}")
                elif isinstance(node, ast.Name):
                    if node.id in FORBIDDEN_WIN32_NAMES:
                        os_violations.append(f"{rel}:{node.lineno}:{node.id}")
                    if node.id == "_load_sibling":
                        load_sibling_hits.append(f"{rel}:{node.lineno}")
                elif isinstance(node, ast.FunctionDef) and node.name == "_load_sibling":
                    load_sibling_hits.append(f"{rel}:{node.lineno}")
                elif isinstance(node, ast.Call):
                    func = node.func
                    if (
                        isinstance(func, ast.Attribute)
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "json"
                        and func.attr == "loads"
                    ):
                        kw_names = {kw.arg for kw in node.keywords}
                        if "object_pairs_hook" not in kw_names or "parse_constant" not in kw_names:
                            bare_json_loads.append(f"{rel}:{node.lineno}")

                    is_parser_call = (
                        isinstance(func, ast.Attribute)
                        and func.attr in {"ArgumentParser", "add_parser"}
                    ) or (isinstance(func, ast.Name) and func.id == "ArgumentParser")
                    if is_parser_call:
                        allow_kw = [kw for kw in node.keywords if kw.arg == "allow_abbrev"]
                        if (
                            not allow_kw
                            or not isinstance(allow_kw[0].value, ast.Constant)
                            or allow_kw[0].value.value is not False
                        ):
                            missing_allow_abbrev.append(f"{rel}:{node.lineno}")

        blank_ratio = blank_lines / max(total_lines, 1)
        self.assertEqual(semicolons, [], f"Semicolon-packed statements found: {semicolons}")
        self.assertEqual(
            os_violations, [], f"Direct OS/Win32 branching outside platform/: {os_violations}"
        )
        self.assertEqual(load_sibling_hits, [], f"_load_sibling hacks remain: {load_sibling_hits}")
        self.assertEqual(crlf_files, [], f"CRLF files found: {crlf_files}")
        self.assertEqual(bare_json_loads, [], f"Bare json.loads calls found: {bare_json_loads}")
        self.assertEqual(
            missing_allow_abbrev,
            [],
            f"ArgumentParser/add_parser missing allow_abbrev=False: {missing_allow_abbrev}",
        )
        self.assertGreaterEqual(
            blank_ratio,
            0.12,
            f"Overall blank-line ratio {blank_ratio:.3f} dropped below 12%",
        )

    def test_domain_native_contracts_and_no_top_level_contracts_bucket(self) -> None:
        self.assertFalse((SRC_ROOT / "managed_ai_information/contracts").exists())

        from managed_ai_information.companions.contracts import (
            ModelBridge,
            PeerHarnessService,
            PeerTurnRunner,
            PublicationReviewService,
            RepresentationService,
            SourceServiceProtocol,
            WorkbenchService,
        )
        from managed_ai_information.host.continuing_work.continuing_work import Host
        from managed_ai_information.host.continuing_work.contract import (
            DEFAULT_HOST_CONFIG,
            ContinuingWorkService,
            HostConfig,
        )
        from managed_ai_information.host.material_retention.config import (
            DEFAULT_LIMITS,
            MaterialRetentionService,
            RetentionLimits,
        )
        from managed_ai_information.host.signal_field.config import (
            DEFAULT_CONFIG,
            SignalFieldConfig,
            SignalFieldService,
        )
        from managed_ai_information.host.signal_field.operations import Field

        for cfg in (DEFAULT_HOST_CONFIG, DEFAULT_CONFIG, DEFAULT_LIMITS):
            self.assertIsInstance(cfg, (HostConfig, SignalFieldConfig, RetentionLimits))
            with self.assertRaises(FrozenInstanceError):
                setattr(cfg, next(iter(cfg.__dataclass_fields__)), "mutated")

        for proto in (
            ContinuingWorkService,
            SignalFieldService,
            MaterialRetentionService,
            ModelBridge,
            SourceServiceProtocol,
            WorkbenchService,
            PeerHarnessService,
            PeerTurnRunner,
            PublicationReviewService,
            RepresentationService,
        ):
            self.assertTrue(
                getattr(proto, "_is_runtime_protocol", False), f"{proto} not runtime_checkable"
            )

        with tempfile.TemporaryDirectory() as tmp:
            host = Host(Path(tmp) / "host")
            self.assertIsInstance(host, ContinuingWorkService)

            field = Field(Path(tmp) / "field.sqlite")
            try:
                self.assertIsInstance(field, SignalFieldService)
            finally:
                field.close()


class Wave7PerformanceAndContractRegressionTests(unittest.TestCase):
    """Targeted regression tests for owned P7 and QA7 Wave 7 fixes."""

    def test_p7_2_verify_clears_cache_and_export_streams_standalone_zip(self) -> None:
        from managed_ai_information.host.material_retention import managed_material as mm

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "src"
            src_dir.mkdir()
            (src_dir / "note.txt").write_text("hello managed material\n", encoding="utf-8")
            spec_path = root / "spec.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "format": "managed-material-selection/1",
                        "id": "occ-p7-2",
                        "roots": [{"label": "src", "path": str(src_dir)}],
                        "account": "p7-2 test snapshot",
                    },
                ),
                encoding="utf-8",
            )

            store = mm.Store(root / "store", create=True)
            snap = mm.snapshot(store, spec_path)

            verified = mm.verify(store)
            self.assertEqual(verified["manifest"], snap["manifest"])
            self.assertGreater(verified["objects_verified"], 0)
            self.assertEqual(store.cache, {})
            self.assertEqual(store.cache_bytes, 0)

            reload_calls = 0
            orig_reload = store.reload

            def counting_reload(*args, **kwargs):
                nonlocal reload_calls
                reload_calls += 1
                return orig_reload(*args, **kwargs)

            export_zip = root / "export.zip"
            with mock.patch.object(store, "reload", side_effect=counting_reload):
                mm.export(store, export_zip)

            self.assertLessEqual(reload_calls, 1)

            extracted = root / "extracted"
            with zipfile.ZipFile(export_zip) as zf:
                zf.extractall(extracted)

            probe = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    (
                        "import json, sys; sys.path.insert(0, sys.argv[1]); "
                        "import material_retention.managed_material as mm; "
                        "store = mm.Store(sys.argv[1]); "
                        "res = mm.verify(store); "
                        "leaked = [m for m in sys.modules "
                        "if m == 'managed_ai_information' "
                        "or m.startswith('managed_ai_information.')]; "
                        "print(json.dumps({'manifest': res['manifest'], 'leaked': leaked}))"
                    ),
                    str(extracted),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            payload = json.loads(probe.stdout)
            self.assertEqual(payload["manifest"], snap["manifest"])
            self.assertEqual(payload["leaked"], [])

    def test_p7_4_material_retention_import_defers_portable_bundle_read(self) -> None:
        probe = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                (
                    "import json, sys; sys.path.insert(0, sys.argv[1]); "
                    "from managed_ai_information.host.material_retention import snapshot_ops; "
                    "before = snapshot_ops.load_portable_bundle.cache_info().currsize; "
                    "bundle = snapshot_ops.load_portable_bundle(); "
                    "after = snapshot_ops.load_portable_bundle.cache_info().currsize; "
                    "print(json.dumps({'before': before, 'after': after, 'files': len(bundle[1])}))"
                ),
                str(SRC_ROOT),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        payload = json.loads(probe.stdout)
        self.assertEqual(payload["before"], 0)
        self.assertEqual(payload["after"], 1)
        self.assertGreater(payload["files"], 0)

    def test_p7_5_single_store_io_module_and_weakref_lock_pools_gc(self) -> None:
        from managed_ai_information.host.continuing_work import occurrence_files
        from managed_ai_information.host.material_retention import managed_material as mm
        from managed_ai_information.host.material_retention import store_io

        self.assertIs(mm._store_io, store_io)

        gc.collect()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for idx in range(20):
                p = base / f"ephemeral-{idx}"
                store_io._LOCK_POOL.get(p)
                store_io._EVENT_LOCK_POOL.get(p)
                occurrence_files._LOCK_POOL.get(p)
                del p

        gc.collect()
        self.assertEqual(len(store_io._LOCK_POOL._locks), 0)
        self.assertEqual(len(store_io._EVENT_LOCK_POOL._locks), 0)
        self.assertEqual(len(occurrence_files._LOCK_POOL._locks), 0)

    def test_p7_6_signal_field_exec_compat_and_single_read_guard_planning_snapshot(self) -> None:
        from managed_ai_information.host.signal_field.operations import Field

        queue_file = SRC_ROOT / "managed_ai_information/host/signal_field/queue.py"
        namespace: dict[str, object] = {
            "__name__": "signal_field_queue",
            "__file__": str(queue_file),
        }
        code = compile(queue_file.read_text(encoding="utf-8"), str(queue_file), "exec")
        exec(code, namespace)  # noqa: S102
        self.assertIn("planning_snapshot", namespace)
        self.assertIn("no_journal", namespace)

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "signal_field.sqlite"
            field = Field(db)
            try:
                field.enroll(
                    {
                        "id": "use-a",
                        "mode": "live",
                        "question": "q-a",
                        "purpose": "p-a",
                        "sources": ["src-1"],
                        "observer": "obs-a",
                        "scope": "scope-a",
                    },
                )
                field.enroll(
                    {
                        "id": "use-b",
                        "mode": "each-change",
                        "question": "q-b",
                        "purpose": "p-b",
                        "sources": ["src-1"],
                        "observer": "obs-b",
                        "scope": "scope-b",
                    },
                )
                field.change("src-1", "e1", 1, {"v": 1})
                field.change("src-1", "e1", 2, {"v": 2})

                status = field.status()
                self.assertEqual(status["uses"], 2)
                self.assertGreaterEqual(status["remaining_jobs"], 1)
                self.assertGreaterEqual(status["eligible"], 1)

                page = field.pending(limit=10)
                self.assertGreaterEqual(len(page["items"]), 1)

                rec = field.reconcile()
                self.assertIsInstance(rec, dict)
            finally:
                field.close()

            guard_calls = 0

            def counting_guard() -> bytes:
                nonlocal guard_calls
                guard_calls += 1
                return db.read_bytes()

            snap = namespace["planning_snapshot"](db, counting_guard)

            self.assertEqual(guard_calls, 1)
            self.assertEqual(len(snap["uses"]), 2)
            self.assertEqual(len(snap["sources"]), 1)

    def test_p7_7_search_cache_canonical_ledger_and_source_service_del(self) -> None:
        from managed_ai_information.companions.workbench import source_tools

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code_file = root / "knowledge.py"
            code_v1 = (
                "\n".join(
                    [
                        "def search_query(q):",
                        "    return q",
                        "class FtsAnalyzer:",
                        "    @classmethod",
                        "    def identity(cls):",
                        "        return {'name': 'stub', 'v': 1}",
                        "    @classmethod",
                        "    def create(cls, db, records):",
                        "        return None",
                        "    @classmethod",
                        "    def search(cls, db, query, limit=8):",
                        "        return []",
                    ]
                )
                + "\n"
            )
            code_file.write_text(code_v1, encoding="utf-8")
            impl1 = source_tools.SearchImplementation.cached(code_file)
            impl2 = source_tools.SearchImplementation.cached(code_file)
            self.assertIs(impl1, impl2)

            code_file.write_text(code_v1.replace("'v': 1", "'v': 2"), encoding="utf-8")
            impl3 = source_tools.SearchImplementation.cached(code_file)
            self.assertIsNot(impl3, impl1)
            self.assertEqual(impl3.analyzer["v"], 2)

        encoded_entries = {
            "inventories": {"inv-1": b'{"id":"inv-1","n":1}'},
            "selections": {},
            "sources": {"src-1": b'{"id":"src-1","sha":"abc"}'},
            "uses": {},
        }
        assembled = source_tools._assemble_ledger_bytes(
            encoded_entries,
            extra_category="uses",
            extra_ident="use-1",
            extra_raw=b'{"id":"use-1"}',
        )
        expected = (
            source_tools.encoded(
                {
                    "inventories": {"inv-1": {"id": "inv-1", "n": 1}},
                    "schema": source_tools.VERSION,
                    "selections": {},
                    "sources": {"src-1": {"id": "src-1", "sha": "abc"}},
                    "uses": {"use-1": {"id": "use-1"}},
                },
            )
            + b"\n"
        )
        self.assertEqual(assembled, expected)

        from managed_ai_information.library.lexical import FtsAnalyzer

        orphan = object.__new__(source_tools.SourceService)
        orphan.db = FtsAnalyzer.new_store()
        orphan.__del__()
        self.assertIsNone(orphan.db)

    def test_p7_8_immutable_below_enforces_integrity_and_relative_reparse_checks(self) -> None:
        from managed_ai_information.host.material_retention import store_io

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store"
            root.mkdir()
            store_io.immutable_below(root, "manifests/m1.json", b'{"ok":true}\n')
            store_io.immutable_below(root, "manifests/m1.json", b'{"ok":true}\n')
            with self.assertRaises(ValueError):
                store_io.immutable_below(root, "manifests/m1.json", b'{"ok":false}\n')

    def test_p7_14_peer_harness_closes_events_handle_on_init_failure(self) -> None:
        from managed_ai_information.companions.peer import peer_harness

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            spec = {
                "workers": [
                    {
                        "owner": "w1",
                        "command": [sys.executable, "-c", "pass"],
                    },
                ],
                "limits": {
                    "parallelism": 1,
                    "max_turns": 1,
                    "max_output_bytes": 4096,
                    "max_artifact_bytes": 4096,
                    "wall_seconds": 10.0,
                    "turn_seconds": 5.0,
                    "terminate_grace_seconds": 1.0,
                },
            }
            config_bytes = json.dumps(spec).encode("utf-8")
            with (
                mock.patch.object(peer_harness.Harness, "event", side_effect=RuntimeError("boom")),
                self.assertRaisesRegex(RuntimeError, "boom"),
            ):
                peer_harness.Harness(spec, config_bytes, run_dir)

            events_file = run_dir / "events.jsonl"
            self.assertTrue(events_file.exists())
            events_file.unlink()
            self.assertFalse(events_file.exists())

    def test_qa7_2_reserved_windows_names_and_trailing_dots_rejected(self) -> None:
        from managed_ai_information.host.continuing_work import occurrence_files
        from managed_ai_information.host.material_retention import store_io

        bad_segments = ("CON", "PRN", "AUX", "NUL", "COM1", "LPT9", "con.txt", "name.", "name ")
        for bad in bad_segments:
            with self.assertRaises(ValueError, msg=f"occurrence_files.ident accepted {bad!r}"):
                occurrence_files.ident(bad)
            with self.assertRaises(
                ValueError, msg=f"material_retention.safe_relative accepted {bad!r}"
            ):
                store_io.safe_relative(f"dir/{bad}")

    def test_qa7_3_and_qa7_8_cli_clean_errors_and_roots_validation(self) -> None:
        from managed_ai_information.collective import record_evidence, run_study
        from managed_ai_information.companions.workbench import workbench_agent
        from managed_ai_information.host.continuing_work import continuing_work
        from managed_ai_information.host.material_retention import managed_material as mm

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_spec = root / "bad_spec.json"
            bad_spec.write_text(
                json.dumps(
                    {
                        "format": "managed-material-selection/1",
                        "id": "occ-bad",
                        "roots": "not-a-list",
                        "account": "test",
                    },
                ),
                encoding="utf-8",
            )
            store = mm.Store(root / "store", create=True)
            with self.assertRaisesRegex(ValueError, "list"):
                mm.snapshot(store, bad_spec)

            malformed = root / "malformed.json"
            malformed.write_text("{bad json", encoding="utf-8")
            src_file = root / "source.txt"
            src_file.write_text("evidence source\n", encoding="utf-8")

            def assert_clean_rc2(fn, argv):
                err = io.StringIO()
                with mock.patch("sys.stderr", err):
                    try:
                        rc = fn(argv)
                    except SystemExit as exc:
                        rc = exc.code
                stderr_text = err.getvalue()
                self.assertEqual(rc, 2, stderr_text)
                self.assertTrue(stderr_text.strip())
                self.assertNotIn("Traceback", stderr_text)
                self.assertNotIn("usage:", stderr_text.lower())

            assert_clean_rc2(
                mm.main,
                ["snapshot", "--store", str(root / "store"), "--spec", str(malformed)],
            )
            assert_clean_rc2(
                continuing_work.main,
                ["--root", str(root / "host"), "init", "--spec", str(malformed)],
            )
            assert_clean_rc2(
                workbench_agent.main,
                ["run", "--config", str(malformed)],
            )
            assert_clean_rc2(
                record_evidence.main,
                [
                    "--db",
                    str(root / "evidence.sqlite"),
                    "record",
                    "ev-1",
                    "--actor",
                    "observer",
                    "--command",
                    "cmd-1",
                    "--source",
                    str(src_file),
                    "--spec",
                    str(malformed),
                ],
            )

            existing_dir = root / "existing_study"
            existing_dir.mkdir()
            assert_clean_rc2(run_study.main, ["--directory", str(existing_dir)])

    def test_c47_3_direct_script_execution_and_portable_zip_isolation(self) -> None:
        from managed_ai_information.host.material_retention import managed_material as mm

        scripts = (
            SRC_ROOT / "managed_ai_information/host/material_retention/managed_material.py",
            SRC_ROOT / "managed_ai_information/companions/workbench/workbench_agent.py",
            SRC_ROOT / "managed_ai_information/companions/workbench/durable_agent.py",
            SRC_ROOT / "managed_ai_information/collective/__main__.py",
        )
        clean_env = {k: v for k, v in dict(__import__("os").environ).items() if k != "PYTHONPATH"}
        clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
        for script in scripts:
            proc = subprocess.run(
                [sys.executable, "-B", str(script), "--help"],
                capture_output=True,
                text=True,
                env=clean_env,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, f"{script}: {proc.stderr}")
            self.assertIn("usage:", proc.stdout.lower())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "src"
            src_dir.mkdir()
            (src_dir / "item.txt").write_text("portable\n", encoding="utf-8")
            spec_path = root / "spec.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "format": "managed-material-selection/1",
                        "id": "occ-c47-3",
                        "roots": [{"label": "src", "path": str(src_dir)}],
                        "account": "c47-3 portable direct script check",
                    },
                ),
                encoding="utf-8",
            )
            store = mm.Store(root / "store", create=True)
            mm.snapshot(store, spec_path)
            export_zip = root / "portable.zip"
            mm.export(store, export_zip)

            extracted = root / "extracted"
            with zipfile.ZipFile(export_zip) as zf:
                zf.extractall(extracted)

            direct_zip_script = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(extracted / "material_retention/managed_material.py"),
                    "--help",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(direct_zip_script.returncode, 0, direct_zip_script.stderr)
            self.assertIn("usage:", direct_zip_script.stdout.lower())

    def test_c47_o1_retire_guards_selection_descendant_path(self) -> None:
        from managed_ai_information.host.material_retention import managed_material as mm
        from managed_ai_information.host.material_retention import retirement

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src_dir = root / "src"
            src_dir.mkdir()
            (src_dir / "item.txt").write_text("retire check\n", encoding="utf-8")
            spec_path = root / "spec.json"
            spec_path.write_text(
                json.dumps(
                    {
                        "format": "managed-material-selection/1",
                        "id": "occ-c47-o1",
                        "roots": [{"label": "src", "path": str(src_dir)}],
                        "account": "c47-o1 retirement guard test",
                    },
                ),
                encoding="utf-8",
            )
            store = mm.Store(root / "store", create=True)
            snap = mm.snapshot(store, spec_path)
            plan_path = root / "plan.json"
            plan_bytes = json.dumps({"source_manifest": snap["manifest"]}).encode("utf-8")
            plan_path.write_bytes(plan_bytes)

            with (
                mock.patch.object(
                    retirement,
                    "check_unfollowed_descendant",
                    side_effect=ValueError(
                        "Current selection must not traverse a symlink or reparse point"
                    ),
                ) as mocked_guard,
                self.assertRaisesRegex(ValueError, "symlink or reparse point"),
            ):
                mm.retire(store, plan_path, mm.sha(plan_bytes), snap["selection"])
            mocked_guard.assert_called_once()

    def test_qa7_4_and_qa7_7_check_representation_import_safe_and_strict_engine(self) -> None:
        from managed_ai_information.companions.representation import check_representation

        self.assertTrue(callable(check_representation.main))

        with tempfile.TemporaryDirectory() as tmp:
            not_an_engine = Path(tmp) / "empty"
            not_an_engine.mkdir()
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                rc = check_representation.main(["--engine", str(not_an_engine)])
            self.assertEqual(rc, 2)
            self.assertNotIn("Traceback", err.getvalue())

    def test_wave8_layout_cleanup_and_declarative_routers(self) -> None:
        from managed_ai_information.collective.store import cli as collective_cli
        from managed_ai_information.host.continuing_work import capture_native, native_bridge

        self.assertFalse((SRC_ROOT / "managed_ai_information/optional").exists())
        self.assertFalse((REPO_ROOT / "tests/optional").exists())
        self.assertFalse((SRC_ROOT / "managed_ai_information/companions/workbench/core").exists())
        self.assertFalse(
            (SRC_ROOT / "managed_ai_information/companions/workbench/controller").exists()
        )
        self.assertTrue((SRC_ROOT / "managed_ai_information/companions/workbench/configs").is_dir())
        self.assertFalse((SRC_ROOT / "managed_ai_information/collective/collective.py").exists())
        self.assertFalse(
            (SRC_ROOT / "managed_ai_information/collective/manage_collective.py").exists()
        )
        self.assertTrue((SRC_ROOT / "managed_ai_information/collective/__main__.py").is_file())
        self.assertTrue((SRC_ROOT / "managed_ai_information/collective/store/cli.py").is_file())

        expected_ops = {
            "init",
            "upgrade",
            "call",
            "show",
            "context",
            "proposal",
            "receipt",
            "notice",
            "next-review",
            "use-source",
            "encounters",
            "inbox",
            "changes",
        }
        registered_ops = set(collective_cli._LIFECYCLE_HANDLERS) | set(
            collective_cli._STORE_HANDLERS
        )
        self.assertEqual(registered_ops, expected_ops)

        self.assertIn("collective", capture_native._CODE_ID_EXCLUDED_DIRS)
        self.assertNotIn("optional", capture_native._CODE_ID_EXCLUDED_DIRS)

        argv, env = native_bridge.build_sibling_cli_command(
            SRC_ROOT / "managed_ai_information/store/manage_store.py",
            "managed_ai_information.core.attention",
            "core/attention.py",
            "track_attention.py",
            "--help",
        )
        self.assertEqual(argv[2:4], ["-m", "managed_ai_information.core.attention"])
        self.assertIsNotNone(env)
        self.assertEqual(Path(env["PYTHONPATH"]).resolve(), SRC_ROOT.resolve())

        module_help = subprocess.run(
            [sys.executable, "-B", "-m", "managed_ai_information.collective", "--help"],
            capture_output=True,
            text=True,
            env={
                **dict(__import__("os").environ),
                "PYTHONPATH": str(SRC_ROOT),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            check=False,
        )
        self.assertEqual(module_help.returncode, 0, module_help.stderr)
        self.assertIn("usage:", module_help.stdout.lower())


if __name__ == "__main__":
    unittest.main()
