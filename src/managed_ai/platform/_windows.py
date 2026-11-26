"""Windows PlatformAdapter implementation.

Centralizes Win32 LockFileEx/UnlockFileEx, process-group signaling, minimal
worker environment construction, and NTFS reparse/case semantics in one module.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import errno
import os
from pathlib import Path
import time
from typing import Any

from ._common import (
    atomic_bytes_common,
    check_ordinary_ancestors_common,
    check_unfollowed_ancestors_common,
    check_unfollowed_descendant_common,
    is_reparse_or_symlink,
    sha256_file_common,
    validate_lock_timeout,
)

_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33
_ERROR_NOT_LOCKED = 158
_ERROR_IO_PENDING = 997
_LOCK_ALL_LOW = 0xFFFFFFFF
_LOCK_ALL_HIGH = 0xFFFFFFFF

_WIN_MINIMAL_ENV_KEYS = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "PATH",
)

_WIN32_API: tuple[Any, Any, Any, Any] | None = None


def _load_win32_api() -> tuple[Any, Any, Any, Any]:
    """Lazily initialize kernel32 LockFileEx/UnlockFileEx bindings to keep cold imports fast."""
    global _WIN32_API
    if _WIN32_API is not None:
        return _WIN32_API

    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    kernel32.LockFileEx.restype = wintypes.BOOL
    kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    kernel32.UnlockFileEx.restype = wintypes.BOOL

    _WIN32_API = (ctypes, msvcrt, kernel32, _Overlapped)
    return _WIN32_API


class WindowsPlatformAdapter:
    """PlatformAdapter implementation for native Windows hosts."""

    platform_key: str = "nt"
    is_case_insensitive_fs: bool = True
    should_signal_group_on_normal_exit: bool = False
    force_kill_if_streams_open: bool = True
    open_ro_nofollow_flags: int = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    open_dir_nofollow_flags: int = os.O_RDONLY

    def lock_file_nonblocking(self, stream: Any, *, exclusive: bool) -> None:
        ctypes, msvcrt, kernel32, overlapped_cls = _load_win32_api()
        handle = msvcrt.get_osfhandle(stream.fileno())
        flags = _LOCKFILE_FAIL_IMMEDIATELY | (_LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0)
        ov = overlapped_cls()

        ok = kernel32.LockFileEx(handle, flags, 0, _LOCK_ALL_LOW, _LOCK_ALL_HIGH, ctypes.byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err in (_ERROR_LOCK_VIOLATION, _ERROR_SHARING_VIOLATION, _ERROR_IO_PENDING):
                raise BlockingIOError(errno.EAGAIN, "file is locked by another owner")
            raise ctypes.WinError(err)

    def lock_file(
        self,
        stream: Any,
        *,
        shared: bool = False,
        nonblocking: bool = False,
        timeout: float | None = 60.0,
    ) -> None:
        exclusive = not shared
        if nonblocking:
            self.lock_file_nonblocking(stream, exclusive=exclusive)
            return

        validated_timeout = validate_lock_timeout(timeout)
        deadline = None if validated_timeout is None else time.monotonic() + validated_timeout

        while True:
            try:
                self.lock_file_nonblocking(stream, exclusive=exclusive)
                return
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    def unlock_file(self, stream: Any) -> None:
        ctypes, msvcrt, kernel32, overlapped_cls = _load_win32_api()
        handle = msvcrt.get_osfhandle(stream.fileno())
        ov = overlapped_cls()

        ok = kernel32.UnlockFileEx(handle, 0, _LOCK_ALL_LOW, _LOCK_ALL_HIGH, ctypes.byref(ov))
        if not ok:
            err = ctypes.get_last_error()
            if err != _ERROR_NOT_LOCKED:
                raise ctypes.WinError(err)

    def is_reparse_or_symlink(self, target: os.stat_result | Path | str) -> bool:
        return is_reparse_or_symlink(target)

    def logical_cwd(self) -> Path:
        return Path.cwd()

    def check_ordinary_ancestors(
        self,
        path: Path | str,
        *,
        expect_dir: bool = False,
        error_prefix: str = "ordinary file required",
    ) -> Path:
        return check_ordinary_ancestors_common(
            path,
            expect_dir=expect_dir,
            error_prefix=error_prefix,
            logical_cwd_fn=self.logical_cwd,
        )

    def check_unfollowed_ancestors(
        self,
        path: Path | str,
        role: str = "Path",
        *,
        include_target: bool = True,
    ) -> Path:
        return check_unfollowed_ancestors_common(
            path,
            role=role,
            include_target=include_target,
            logical_cwd_fn=self.logical_cwd,
        )

    def check_unfollowed_descendant(
        self,
        validated_root: Path | str,
        rel_path: Path | str,
        *,
        expect_dir: bool = False,
        allow_missing_leaf: bool = False,
        role: str = "Path",
    ) -> Path:
        return check_unfollowed_descendant_common(
            validated_root,
            rel_path,
            expect_dir=expect_dir,
            allow_missing_leaf=allow_missing_leaf,
            role=role,
        )

    def sync_dir(self, directory: Path | str) -> None:
        return None

    def publish_exclusive(
        self,
        staged: Path | str,
        target: Path | str,
        *,
        error_message: str = "output exists; inspect it rather than overwrite",
    ) -> None:
        try:
            os.rename(staged, target)
        except FileExistsError:
            raise ValueError(error_message) from None

    def atomic_bytes(
        self,
        path: Path | str,
        payload: bytes | bytearray | memoryview | Iterable[bytes | bytearray | memoryview],
        *,
        exclusive: bool = False,
    ) -> None:
        atomic_bytes_common(
            path,
            payload,
            exclusive=exclusive,
            sync_dir_fn=self.sync_dir,
            publish_exclusive_fn=self.publish_exclusive,
        )

    def sha256_file(
        self,
        path: Path | str,
        *,
        chunk_size: int = 65536,
        follow_symlinks: bool = False,
    ) -> str:
        return sha256_file_common(
            path,
            chunk_size=chunk_size,
            follow_symlinks=follow_symlinks,
            open_ro_nofollow_flags=0,
        )

    def set_private_file_mode(self, path: Path | str) -> None:
        return None

    def enter_proposal_origin(self, raw_path: bytes) -> None:
        path_text = os.fsdecode(raw_path)
        if not os.path.isabs(path_text) or "\0" in path_text:
            raise ValueError("absolute proposal origin required")

        os.chdir(path_text)

    def verify_exact_filename(self, path: Path, expected_name: str) -> bool:
        try:
            return path.resolve().name == expected_name
        except OSError:
            return False

    def detached_subprocess_kwargs(self) -> dict[str, Any]:
        import subprocess

        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    def isolated_process_group_kwargs(self) -> dict[str, Any]:
        return self.detached_subprocess_kwargs()

    def worker_environment(self, extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {key: os.environ[key] for key in _WIN_MINIMAL_ENV_KEYS if key in os.environ}
        env.setdefault("PATH", os.defpath)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        if extra_env:
            env.update(extra_env)

        return env

    def env_policy_description(self) -> str:
        return "Minimal Windows OS keys + PYTHONUTF8=1; explicit worker env only"

    def process_group_exists(self, proc: Any) -> bool:
        return proc is not None and proc.poll() is None

    def signal_process_group(self, proc: Any, kind: str) -> bool:
        if kind not in ("term", "kill"):
            raise ValueError(f"unsupported signal kind: {kind!r}")

        if proc is None or proc.poll() is not None:
            return False

        if kind == "term":
            try:
                proc.terminate()
                return True
            except OSError:
                return False

        import subprocess

        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

        return True

    def child_cpu_times(self) -> tuple[float, float]:
        return 0.0, 0.0
