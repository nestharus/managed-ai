"""POSIX PlatformAdapter implementation (Linux, macOS, WSL).

Centralizes fcntl.flock, O_DIRECTORY fsync, logical $PWD ancestor verification,
no-clobber hardlink publishing, and POSIX process-group signaling in one module.
"""

import errno
import os
import sys
import time
from collections.abc import Iterable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, override

from ._common import (
    atomic_bytes_common,
    check_ordinary_ancestors_common,
    check_unfollowed_ancestors_common,
    check_unfollowed_descendant_common,
    is_reparse_or_symlink,
    sha256_file_common,
    validate_lock_timeout,
)
from .adapter import PlatformAdapter


class PosixPlatformAdapter(PlatformAdapter):
    """PlatformAdapter implementation for POSIX hosts."""

    platform_key: str = "posix"
    is_case_insensitive_fs: bool = sys.platform == "darwin"
    should_signal_group_on_normal_exit: bool = True
    force_kill_if_streams_open: bool = False
    open_ro_nofollow_flags: int = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOCTTY", 0)
    )
    open_dir_nofollow_flags: int = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )

    @override
    def lock_file_nonblocking(self, stream: Any, *, exclusive: bool) -> None:
        if sys.platform == "win32":
            raise NotImplementedError("PosixPlatformAdapter requires a POSIX host")

        import fcntl

        mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        fcntl.flock(stream.fileno(), mode)

    @override
    def lock_file(
        self,
        stream: Any,
        *,
        shared: bool = False,
        nonblocking: bool = False,
        timeout: float | None = 60.0,
    ) -> None:
        if sys.platform == "win32":
            raise NotImplementedError("PosixPlatformAdapter requires a POSIX host")

        import fcntl

        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        if nonblocking:
            fcntl.flock(stream.fileno(), mode | fcntl.LOCK_NB)
            return

        validated_timeout = validate_lock_timeout(timeout)
        if validated_timeout is None:
            fcntl.flock(stream.fileno(), mode)
            return

        deadline = time.monotonic() + validated_timeout
        while True:
            try:
                fcntl.flock(stream.fileno(), mode | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    @override
    def unlock_file(self, stream: Any) -> None:
        if sys.platform == "win32":
            raise NotImplementedError("PosixPlatformAdapter requires a POSIX host")

        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @override
    def is_reparse_or_symlink(self, target: os.stat_result | Path | str) -> bool:
        return is_reparse_or_symlink(target)

    @override
    def logical_cwd(self) -> Path:
        pwd = os.environ.get("PWD")
        if pwd and os.path.isabs(pwd):
            with suppress(OSError):
                if os.path.samefile(pwd, "."):
                    return Path(pwd)

        return Path.cwd()

    @override
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

    @override
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

    @override
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

    @override
    def sync_dir(self, directory: Path | str) -> None:
        if sys.platform == "win32":
            raise NotImplementedError("PosixPlatformAdapter requires a POSIX host")
        if not hasattr(os, "O_DIRECTORY"):
            return

        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.EBADF, errno.EROFS):
                raise
        finally:
            os.close(fd)

    @override
    def publish_exclusive(
        self,
        staged: Path | str,
        target: Path | str,
        *,
        error_message: str = "output exists; inspect it rather than overwrite",
    ) -> None:
        staged_path = Path(staged)
        target_path = Path(target)

        try:
            os.link(staged_path, target_path)
        except FileExistsError:
            raise ValueError(error_message) from None
        except OSError:
            if os.path.lexists(target_path):
                raise ValueError(error_message) from None
            os.replace(staged_path, target_path)
            return

        staged_path.unlink(missing_ok=True)

    @override
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

    @override
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
            open_ro_nofollow_flags=self.open_ro_nofollow_flags,
        )

    @override
    def set_private_file_mode(self, path: Path | str) -> None:
        with suppress(OSError):
            os.chmod(path, 0o600)

    @override
    def enter_proposal_origin(self, raw_path: bytes) -> None:
        if not os.path.isabs(raw_path) or b"\0" in raw_path:
            raise ValueError("absolute proposal origin required")

        anchor = b"//" if raw_path.startswith(b"//") and not raw_path.startswith(b"///") else b"/"
        os.chdir(anchor)

        for component in raw_path.split(b"/"):
            if component:
                os.chdir(component)

    @override
    def verify_exact_filename(self, path: Path, expected_name: str) -> bool:
        if not self.is_case_insensitive_fs:
            return True

        try:
            return path.resolve().name == expected_name
        except OSError:
            return False

    @override
    def detached_subprocess_kwargs(self) -> dict[str, Any]:
        return {"start_new_session": True}

    @override
    def isolated_process_group_kwargs(self) -> dict[str, Any]:
        return {"start_new_session": True}

    @override
    def worker_environment(self, extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {"PATH": os.defpath, "LANG": "C.UTF-8"}
        if extra_env:
            env.update(extra_env)

        return env

    @override
    def env_policy_description(self) -> str:
        return "PATH=os.defpath; LANG=C.UTF-8; explicit worker env only"

    @override
    def process_group_exists(self, proc: Any) -> bool:
        if sys.platform == "win32":
            raise NotImplementedError("PosixPlatformAdapter requires a POSIX host")
        if proc is None:
            return False

        try:
            os.killpg(proc.pid, 0)
            return True
        except ProcessLookupError:
            return False

    @override
    def signal_process_group(self, proc: Any, kind: str) -> bool:
        if kind not in ("term", "kill"):
            raise ValueError(f"unsupported signal kind: {kind!r}")

        if proc is None:
            return False

        if sys.platform == "win32":
            raise NotImplementedError("PosixPlatformAdapter requires a POSIX host")

        import signal

        sig = signal.SIGTERM if kind == "term" else signal.SIGKILL

        try:
            os.killpg(proc.pid, sig)
            return True
        except ProcessLookupError:
            return False

    @override
    def child_cpu_times(self) -> tuple[float, float]:
        if sys.platform == "win32":
            raise NotImplementedError("PosixPlatformAdapter requires a POSIX host")
        try:
            import resource
        except ImportError:
            return 0.0, 0.0

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return float(usage.ru_utime), float(usage.ru_stime)
