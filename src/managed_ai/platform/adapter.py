"""OS platform adapter protocol and cooperative locking mode.

Owned directly by managed_ai.platform (stdlib-only leaf package) so every
runtime layer shares one explicit, verifiable OS boundary.
"""

import os
from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class LockMode(Enum):
    """Cooperative file locking mode."""

    SHARED = "shared"
    EXCLUSIVE = "exclusive"


@runtime_checkable
class PlatformAdapter(Protocol):
    """Unified OS abstraction eliminating inline platform branches outside platform/."""

    platform_key: str
    is_case_insensitive_fs: bool
    should_signal_group_on_normal_exit: bool
    force_kill_if_streams_open: bool
    open_ro_nofollow_flags: int
    open_dir_nofollow_flags: int

    def lock_file(
        self,
        stream: Any,
        *,
        shared: bool = False,
        nonblocking: bool = False,
        timeout: float | None = 60.0,
    ) -> None: ...

    def lock_file_nonblocking(self, stream: Any, *, exclusive: bool) -> None: ...

    def unlock_file(self, stream: Any) -> None: ...

    def is_reparse_or_symlink(self, target: os.stat_result | Path | str) -> bool: ...

    def logical_cwd(self) -> Path: ...

    def check_ordinary_ancestors(
        self,
        path: Path | str,
        *,
        expect_dir: bool = False,
        error_prefix: str = "ordinary file required",
    ) -> Path: ...

    def check_unfollowed_ancestors(
        self,
        path: Path | str,
        role: str = "Path",
        *,
        include_target: bool = True,
    ) -> Path: ...

    def check_unfollowed_descendant(
        self,
        validated_root: Path | str,
        rel_path: Path | str,
        *,
        expect_dir: bool = False,
        allow_missing_leaf: bool = False,
        role: str = "Path",
    ) -> Path: ...

    def sync_dir(self, directory: Path | str) -> None: ...

    def atomic_bytes(
        self,
        path: Path | str,
        payload: bytes | bytearray | memoryview | Iterable[bytes | bytearray | memoryview],
        *,
        exclusive: bool = False,
    ) -> None: ...

    def publish_exclusive(
        self,
        staged: Path | str,
        target: Path | str,
        *,
        error_message: str = "output exists; inspect it rather than overwrite",
    ) -> None: ...

    def sha256_file(
        self,
        path: Path | str,
        *,
        chunk_size: int = 65536,
        follow_symlinks: bool = False,
    ) -> str: ...

    def set_private_file_mode(self, path: Path | str) -> None: ...

    def enter_proposal_origin(self, raw_path: bytes) -> None: ...

    def verify_exact_filename(self, path: Path, expected_name: str) -> bool: ...

    def detached_subprocess_kwargs(self) -> dict[str, Any]: ...

    def isolated_process_group_kwargs(self) -> dict[str, Any]: ...

    def worker_environment(self, extra_env: Mapping[str, str] | None = None) -> dict[str, str]: ...

    def env_policy_description(self) -> str: ...

    def process_group_exists(self, proc: Any) -> bool: ...

    def signal_process_group(self, proc: Any, kind: str) -> bool: ...

    def child_cpu_times(self) -> tuple[float, float]: ...
