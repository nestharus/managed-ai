"""Tests for the unified stdlib-only managed_ai.platform leaf adapter."""

from __future__ import annotations

import ast
import gc
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import time
import tokenize
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import managed_ai.platform as platform_pkg  # noqa: E402
from managed_ai.platform import (  # noqa: E402
    OPEN_DIR_NOFOLLOW,
    OPEN_RO_NOFOLLOW,
    PLATFORM,
    KeyedLockPool,
    LockMode,
    PlatformAdapter,
    ThreadLock,
    ThreadRLockState,
    ThreadRWLock,
    admission_deadline,
    atomic_bytes,
    check_ordinary_ancestors,
    check_unfollowed_ancestors,
    check_unfollowed_descendant,
    child_cpu_times,
    detached_subprocess_kwargs,
    env_policy_description,
    get_platform,
    has_ordinary_dir,
    has_ordinary_file,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    is_reparse_or_symlink,
    is_reserved_windows_segment,
    isolated_process_group_kwargs,
    lock_file,
    lock_file_nonblocking,
    logical_cwd,
    process_group_exists,
    publish_exclusive,
    safe_relpath,
    sha256_file,
    sha256_stream,
    signal_process_group,
    strict_json_loads,
    sync_dir,
    unlock_file,
    validate_safe_segment,
    verify_exact_filename,
    worker_environment,
)
from managed_ai.platform._posix import PosixPlatformAdapter  # noqa: E402
from managed_ai.platform._windows import WindowsPlatformAdapter  # noqa: E402
from tests._paths import ENGINE, SRC, cleanup_tempdir  # noqa: E402


class PlatformAdapterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="w7_platform_")
        self.root = Path(self._tmp.name)

    def tearDown(self):
        cleanup_tempdir(self._tmp)

    def _make_dir_link(self, link: Path, target: Path) -> bool:
        if os.name == "nt":
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
            )
            return proc.returncode == 0
        link.symlink_to(target)
        return True

    def _remove_dir_link(self, link: Path) -> None:
        if not link.exists() and not link.is_symlink():
            return
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)
        else:
            link.unlink(missing_ok=True)

    def test_platform_leaf_isolation_and_zero_runtime_managed_ai_imports(self):
        platform_dir = ENGINE / "platform"
        py_files = sorted(platform_dir.rglob("*.py"))
        self.assertEqual(
            {p.name for p in py_files},
            {"__init__.py", "_common.py", "_posix.py", "_windows.py", "adapter.py"},
        )

        allowed_stdlib = set(sys.stdlib_module_names) | {"__future__"}
        violations = []

        for py in py_files:
            raw = py.read_bytes()
            self.assertNotIn(b"\r\n", raw, f"CRLF detected in {py}")
            text = raw.decode("utf-8")
            tree = ast.parse(text, filename=str(py))

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".", 1)[0]
                        if top not in allowed_stdlib:
                            violations.append(f"{py.name}:{node.lineno}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    top = node.module.split(".", 1)[0]
                    if top not in allowed_stdlib:
                        violations.append(f"{py.name}:{node.lineno}: from {node.module}")

        self.assertEqual(violations, [], f"Platform leaf violations: {violations}")
        self.assertFalse(
            (ENGINE / "contracts").exists(), "No generic top-level contracts/ bucket folder"
        )

    def test_platform_hygiene_zero_semicolons_and_clean_spacing(self):
        targets = sorted((ENGINE / "platform").rglob("*.py"))
        for py in targets:
            text = py.read_text(encoding="utf-8")
            semi_lines = [
                tok.start[0]
                for tok in tokenize.generate_tokens(io.StringIO(text).readline)
                if tok.type == tokenize.OP and tok.string == ";"
            ]
            self.assertEqual(semi_lines, [], f"Semicolons in {py.name}: {semi_lines}")
            lines = text.splitlines()
            if py.name != "__init__.py" and len(lines) >= 40:
                blank_ratio = sum(1 for line in lines if not line.strip()) / len(lines)
                self.assertGreaterEqual(
                    blank_ratio, 0.15, f"Low blank ratio in {py.name}: {blank_ratio:.2%}"
                )

    def test_cold_import_avoids_heavy_stdlib_modules(self):
        code = (
            "import sys, managed_ai.platform; "
            "heavy = [m for m in ('concurrent.futures', 'logging', 'subprocess', 'ctypes') "
            "if m in sys.modules]; "
            "print(','.join(heavy))"
        )
        env = dict(os.environ, PYTHONPATH=str(SRC))
        proc = subprocess.run(
            [sys.executable, "-B", "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), "", f"Unexpected eager imports: {proc.stdout!r}")

    def test_singleton_and_both_adapters_satisfy_protocol(self):
        self.assertIs(get_platform(), PLATFORM)
        self.assertIsInstance(PLATFORM, PlatformAdapter)
        self.assertIsInstance(WindowsPlatformAdapter(), PlatformAdapter)
        self.assertIsInstance(PosixPlatformAdapter(), PlatformAdapter)
        self.assertEqual(PLATFORM.platform_key, "nt" if os.name == "nt" else "posix")
        self.assertEqual(LockMode.SHARED.value, "shared")
        self.assertEqual(LockMode.EXCLUSIVE.value, "exclusive")

    def test_core_platform_wrapper_retired_and_platform_exports_canonical(self):
        self.assertFalse((ENGINE / "core/_platform.py").exists())
        expected_symbols = (
            "OPEN_DIR_NOFOLLOW",
            "OPEN_RO_NOFOLLOW",
            "PLATFORM",
            "admission_deadline",
            "check_ordinary_ancestors",
            "has_ordinary_dir",
            "has_ordinary_file",
            "is_ordinary_dir_stat",
            "is_ordinary_file_stat",
            "is_reparse_or_symlink",
            "lock_file_nonblocking",
            "safe_relpath",
            "strict_json_loads",
            "sync_dir",
            "unlock_file",
        )
        for name in expected_symbols:
            self.assertTrue(
                hasattr(platform_pkg, name), f"Missing canonical platform export: {name}"
            )
        self.assertIsInstance(OPEN_RO_NOFOLLOW, int)
        self.assertIsInstance(OPEN_DIR_NOFOLLOW, int)

    def test_locking_shared_coexistence_and_exclusive_contention(self):
        lock_path = self.root / "coop.lock"
        lock_path.write_bytes(b"")

        # Two shared locks coexist cleanly.
        with lock_path.open("a+b") as s1, lock_path.open("a+b") as s2:
            lock_file_nonblocking(s1, exclusive=False)
            try:
                lock_file_nonblocking(s2, exclusive=False)
                try:
                    with (
                        lock_path.open("a+b") as s3,
                        self.assertRaises(BlockingIOError),
                    ):
                        lock_file_nonblocking(s3, exclusive=True)
                finally:
                    unlock_file(s2)
            finally:
                unlock_file(s1)

        # Exclusive lock blocks nonblocking and times out bounded lock_file().
        with lock_path.open("a+b") as holder:
            lock_file(holder, shared=False, nonblocking=True)
            try:
                with lock_path.open("a+b") as contender:
                    with self.assertRaises(BlockingIOError):
                        lock_file(contender, shared=True, nonblocking=True)
                    t0 = time.monotonic()
                    with self.assertRaises(BlockingIOError):
                        lock_file(contender, shared=False, nonblocking=False, timeout=0.05)
                    self.assertGreaterEqual(time.monotonic() - t0, 0.04)
            finally:
                unlock_file(holder)

        # Unlocked file is immediately re-acquirable.
        with lock_path.open("a+b") as reopened:
            lock_file(reopened, shared=False, nonblocking=True)
            unlock_file(reopened)

    def test_ordinary_ancestors_and_fast_descendant_checks(self):
        real_dir = self.root / "real_dir"
        nested = real_dir / "a" / "b"
        nested.mkdir(parents=True)
        leaf = nested / "file.txt"
        leaf.write_text("hello", encoding="utf-8")

        self.assertTrue(has_ordinary_dir(real_dir))
        self.assertTrue(has_ordinary_file(leaf))
        self.assertTrue(is_ordinary_dir_stat(real_dir.lstat()))
        self.assertTrue(is_ordinary_file_stat(leaf.lstat()))
        self.assertFalse(is_reparse_or_symlink(leaf))
        self.assertFalse(is_reparse_or_symlink(leaf.lstat()))
        self.assertFalse(is_reparse_or_symlink(self.root / "does-not-exist"))

        checked = check_ordinary_ancestors(leaf, expect_dir=False)
        self.assertEqual(checked, leaf.resolve())

        missing = self.root / "future.lock"
        self.assertEqual(
            check_unfollowed_ancestors(missing, include_target=True), missing.resolve()
        )

        # Fast relative descendant check skips re-walking drive root.
        validated_root = check_ordinary_ancestors(real_dir, expect_dir=True)
        self.assertEqual(
            check_unfollowed_descendant(validated_root, "a/b/file.txt"), leaf.resolve()
        )
        self.assertEqual(
            check_unfollowed_descendant(validated_root, "a/b/new.txt", allow_missing_leaf=True),
            nested / "new.txt",
        )
        for bad in ("../escape", "/abs/path", "a/../../escape", "a/\x00b", "C:drive-rel"):
            with self.assertRaises(ValueError):
                check_unfollowed_descendant(validated_root, bad, allow_missing_leaf=True)

        # Reparse / symlink rejection on ancestors & descendants.
        link_dir = real_dir / "a" / "linked"
        if self._make_dir_link(link_dir, nested):
            try:
                self.assertTrue(is_reparse_or_symlink(link_dir))
                self.assertTrue(is_reparse_or_symlink(link_dir.lstat()))
                with self.assertRaises(ValueError):
                    check_ordinary_ancestors(link_dir / "file.txt")
                with self.assertRaises(ValueError):
                    check_unfollowed_ancestors(link_dir / "file.txt")
                with self.assertRaises(ValueError):
                    check_unfollowed_descendant(validated_root, "a/linked/file.txt")
            finally:
                self._remove_dir_link(link_dir)

    def test_streaming_sha256_file_and_stream(self):
        payload = b"0123456789abcdef" * 16384  # 256 KiB across multiple chunks
        expected = hashlib.sha256(payload).hexdigest()
        target = self.root / "big.bin"
        target.write_bytes(payload)

        self.assertEqual(sha256_file(target), expected)
        self.assertEqual(sha256_file(target, chunk_size=4096), expected)
        self.assertEqual(sha256_stream(io.BytesIO(payload), chunk_size=4096), expected)
        with self.assertRaises(ValueError):
            sha256_stream(io.BytesIO(b"x"), chunk_size=0)

    def test_scatter_write_atomic_bytes_and_publish_exclusive(self):
        target = self.root / "data.json"
        atomic_bytes(target, b'{"a": 1}\n')
        self.assertEqual(target.read_bytes(), b'{"a": 1}\n')

        # Scatter-write via iterable of bytes + memoryview without joining.
        buf = memoryview(b'{"header":true,"body":"value"}\n')
        atomic_bytes(target, (b'{"sig":"ok",', buf[1:]))
        self.assertEqual(target.read_bytes(), b'{"sig":"ok","header":true,"body":"value"}\n')

        # Exclusive publish succeeds once and refuses overwrite on existing file.
        once = self.root / "once.bin"
        atomic_bytes(once, b"first", exclusive=True)
        self.assertEqual(once.read_bytes(), b"first")
        with self.assertRaises(ValueError):
            atomic_bytes(once, b"second", exclusive=True)
        self.assertEqual(once.read_bytes(), b"first")

        staged = self.root / ".staged.bin"
        staged.write_bytes(b"payload")
        dest = self.root / "published.bin"
        publish_exclusive(staged, dest)
        self.assertEqual(dest.read_bytes(), b"payload")
        staged.unlink(missing_ok=True)
        self.assertEqual([p.name for p in self.root.glob(".data.json.*")], [])

    def test_keyed_lock_pool_weakref_eviction_and_rwlock(self):
        pool = KeyedLockPool(ThreadLock)
        for i in range(200):
            lk = pool.get(self.root / f"ephemeral-{i}.lock")
            with lk:
                pass
        del lk
        gc.collect()
        self.assertEqual(len(pool), 0)

        rlock_pool = KeyedLockPool(ThreadRLockState)
        state = rlock_pool.get(self.root / "store-a")
        self.assertIs(rlock_pool.get(self.root / "store-a"), state)
        del state
        gc.collect()
        self.assertEqual(len(rlock_pool), 0)

        rw_pool = KeyedLockPool(ThreadRWLock)
        rw = rw_pool.get(self.root / "rw.lock")
        with (
            rw.hold(shared=True, nonblocking=True),
            rw.hold(shared=True, nonblocking=True),
            self.assertRaises(BlockingIOError),
            rw.hold(shared=False, nonblocking=True),
        ):
            pass
        with (
            rw.hold(shared=False, nonblocking=True),
            self.assertRaises(BlockingIOError),
            rw.hold(shared=True, nonblocking=True),
        ):
            pass
        del rw
        gc.collect()
        self.assertEqual(len(rw_pool), 0)

    def test_strict_json_relpath_deadline_and_sync_dir(self):
        self.assertEqual(strict_json_loads(b'{"a": 1}'), {"a": 1})
        self.assertEqual(strict_json_loads("[1, 2]", require_object=False), [1, 2])
        with self.assertRaises(ValueError):
            strict_json_loads('{"a": 1, "a": 2}')
        with self.assertRaises(ValueError):
            strict_json_loads('{"a": NaN}')
        with self.assertRaises(ValueError):
            strict_json_loads("[1, 2]", require_object=True)

        self.assertEqual(safe_relpath(self.root / "a" / "b", self.root), "a/b")
        self.assertEqual(safe_relpath(self.root, self.root / "a" / "b"), "../..")
        self.assertTrue(logical_cwd().is_absolute())
        sync_dir(self.root)

        # QA7-2: Windows reserved device names and trailing dot/space collision rejection.
        for bad_seg in (
            "CON",
            "con",
            "CON.json",
            "PRN",
            "prn.txt",
            "AUX",
            "aux.log",
            "NUL",
            "nul.tar.gz",
            "COM1",
            "com9.json",
            "LPT1",
            "lpt9.bin",
            "foo.",
            "foo ",
            "CON ",
            "CON.",
        ):
            with self.assertRaises(ValueError, msg=f"expected rejection for {bad_seg!r}"):
                validate_safe_segment(bad_seg)
            with self.assertRaises(
                ValueError, msg=f"safe_relpath expected rejection for {bad_seg!r}"
            ):
                safe_relpath(self.root / "sub" / bad_seg, self.root)

        self.assertTrue(is_reserved_windows_segment("CON"))
        self.assertTrue(is_reserved_windows_segment("nul.json"))
        self.assertFalse(is_reserved_windows_segment("console.json"))
        self.assertFalse(is_reserved_windows_segment("COM0"))
        self.assertFalse(is_reserved_windows_segment("LPT10"))
        for ok_seg in (
            "console.json",
            "null_check.py",
            ".hidden",
            "README.md",
            "COM0",
            "LPT10",
            "a.b.c",
        ):
            self.assertEqual(validate_safe_segment(ok_seg), ok_seg)

        sample = self.root / "ExactCase.json"
        sample.write_text("{}", encoding="utf-8")
        self.assertTrue(verify_exact_filename(sample, "ExactCase.json"))
        if PLATFORM.is_case_insensitive_fs:
            self.assertFalse(verify_exact_filename(sample, "exactcase.json"))

        prev = os.environ.get("MANAGED_AI_DOMAIN_ADMISSION_SECONDS")
        try:
            os.environ["MANAGED_AI_DOMAIN_ADMISSION_SECONDS"] = "5"
            dl = admission_deadline()
            self.assertIsNotNone(dl)
            os.environ["MANAGED_AI_DOMAIN_ADMISSION_SECONDS"] = "999"
            with self.assertRaises(ValueError):
                admission_deadline()
        finally:
            if prev is None:
                os.environ.pop("MANAGED_AI_DOMAIN_ADMISSION_SECONDS", None)
            else:
                os.environ["MANAGED_AI_DOMAIN_ADMISSION_SECONDS"] = prev

    def test_worker_env_and_process_group_lifecycle(self):
        env = worker_environment({"CUSTOM_KEY": "ok"})
        self.assertEqual(env["CUSTOM_KEY"], "ok")
        self.assertIn("PATH", env)
        self.assertTrue(env_policy_description())
        self.assertIsInstance(detached_subprocess_kwargs(), dict)
        self.assertIsInstance(isolated_process_group_kwargs(), dict)
        user_t, sys_t = child_cpu_times()
        self.assertIsInstance(user_t, float)
        self.assertIsInstance(sys_t, float)

        proc = subprocess.Popen(
            [sys.executable, "-B", "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **isolated_process_group_kwargs(),
        )
        try:
            self.assertTrue(process_group_exists(proc))
            self.assertTrue(signal_process_group(proc, "term"))
            proc.wait(timeout=5)
            self.assertFalse(process_group_exists(proc))
        finally:
            if proc.poll() is None:
                signal_process_group(proc, "kill")
                proc.wait(timeout=5)

    def test_c45_cross_os_parity_and_hardening_guards(self):
        # C45-2: sha256_file(..., follow_symlinks=False) raises ValueError uniformly for missing,
        # dir, and link.
        with self.assertRaises(ValueError):
            sha256_file(self.root / "missing.bin", follow_symlinks=False)
        subdir = self.root / "a_dir"
        subdir.mkdir()
        with self.assertRaises(ValueError):
            sha256_file(subdir, follow_symlinks=False)

        real_file = self.root / "real.txt"
        real_file.write_bytes(b"hello")
        link_file = self.root / "link.txt"
        try:
            link_file.symlink_to(real_file)
        except OSError:
            pass
        else:
            with self.assertRaises(ValueError):
                sha256_file(link_file, follow_symlinks=False)

        # C45-3A: regular-file ancestor (ENOTDIR on POSIX) behaves identically to Windows.
        child_under_file = real_file / "child"
        self.assertFalse(is_reparse_or_symlink(child_under_file))
        self.assertEqual(
            check_unfollowed_ancestors(child_under_file, include_target=True),
            Path(os.path.abspath(child_under_file)),
        )

        # C45-3B: signal_process_group validates kind and returns False on already-exited proc.
        exited = subprocess.Popen(
            [sys.executable, "-B", "-c", "pass"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **isolated_process_group_kwargs(),
        )
        exited.wait(timeout=5)
        self.assertFalse(signal_process_group(exited, "term"))
        self.assertFalse(signal_process_group(exited, "kill"))
        for adapter in (WindowsPlatformAdapter(), PosixPlatformAdapter()):
            with self.assertRaises(ValueError):
                adapter.signal_process_group(exited, "sigkill")
            with self.assertRaises(ValueError):
                adapter.signal_process_group(None, "unknown")

        # C45-O1: ThreadRWLock.release guards against underflow and unacquired release.
        rw = ThreadRWLock()
        with self.assertRaises(RuntimeError):
            rw.release(shared=True)
        with self.assertRaises(RuntimeError):
            rw.release(shared=False)
        rw.acquire(shared=True, nonblocking=True)
        rw.release(shared=True)
        with self.assertRaises(RuntimeError):
            rw.release(shared=True)
        self.assertEqual(rw._readers, 0)

        # C45-O2: lock_file rejects non-finite or negative timeouts.
        lock_path = self.root / "timeout_guard.lock"
        lock_path.write_bytes(b"")
        with lock_path.open("a+b") as stream:
            for bad_timeout in (float("nan"), float("inf"), float("-inf"), -1.0, True):
                with self.assertRaises(ValueError):
                    lock_file(stream, shared=False, nonblocking=False, timeout=bad_timeout)

        # C45-O3: publish_exclusive cleans up staged path uniformly on both OSes.
        staged = self.root / ".staged_cleanup.bin"
        staged.write_bytes(b"cleanup-check")
        dest = self.root / "dest_cleanup.bin"
        publish_exclusive(staged, dest)
        self.assertEqual(dest.read_bytes(), b"cleanup-check")
        self.assertFalse(staged.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
