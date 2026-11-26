"""Unified OS Platform Adapter for Managed AI.

This leaf package is the ONLY place in src/managed_ai/ that branches on
os.name to select Windows vs POSIX primitives. It is stdlib-only at runtime so
library/ preserves its zero-core/store/deployment sandbox and material_retention
recovery archives can vendor it verbatim.
"""

import os

from ._common import (
    KeyedLockPool,
    ThreadLock,
    ThreadRLockState,
    ThreadRWLock,
    admission_deadline,
    has_ordinary_dir,
    has_ordinary_file,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    is_reparse_or_symlink,
    is_reparse_or_symlink_stat,
    is_reserved_windows_segment,
    safe_relpath,
    sha256_stream,
    strict_json_loads,
    validate_safe_segment,
)
from .adapter import LockMode, PlatformAdapter


def _select_platform() -> PlatformAdapter:
    if os.name == "nt":
        from ._windows import WindowsPlatformAdapter

        return WindowsPlatformAdapter()

    from ._posix import PosixPlatformAdapter

    return PosixPlatformAdapter()


PLATFORM: PlatformAdapter = _select_platform()

OPEN_RO_NOFOLLOW: int = PLATFORM.open_ro_nofollow_flags
OPEN_DIR_NOFOLLOW: int = PLATFORM.open_dir_nofollow_flags


def get_platform() -> PlatformAdapter:
    """Return the active OS PlatformAdapter singleton."""
    return PLATFORM


lock_file = PLATFORM.lock_file
lock_file_nonblocking = PLATFORM.lock_file_nonblocking
unlock_file = PLATFORM.unlock_file
logical_cwd = PLATFORM.logical_cwd
check_ordinary_ancestors = PLATFORM.check_ordinary_ancestors
check_unfollowed_ancestors = PLATFORM.check_unfollowed_ancestors
check_unfollowed_descendant = PLATFORM.check_unfollowed_descendant
sync_dir = PLATFORM.sync_dir
atomic_bytes = PLATFORM.atomic_bytes
publish_exclusive = PLATFORM.publish_exclusive
sha256_file = PLATFORM.sha256_file
set_private_file_mode = PLATFORM.set_private_file_mode
enter_proposal_origin = PLATFORM.enter_proposal_origin
verify_exact_filename = PLATFORM.verify_exact_filename
detached_subprocess_kwargs = PLATFORM.detached_subprocess_kwargs
isolated_process_group_kwargs = PLATFORM.isolated_process_group_kwargs
worker_environment = PLATFORM.worker_environment
env_policy_description = PLATFORM.env_policy_description
process_group_exists = PLATFORM.process_group_exists
signal_process_group = PLATFORM.signal_process_group
child_cpu_times = PLATFORM.child_cpu_times

__all__ = [
    "OPEN_DIR_NOFOLLOW",
    "OPEN_RO_NOFOLLOW",
    "PLATFORM",
    "KeyedLockPool",
    "LockMode",
    "PlatformAdapter",
    "ThreadLock",
    "ThreadRLockState",
    "ThreadRWLock",
    "admission_deadline",
    "atomic_bytes",
    "check_ordinary_ancestors",
    "check_unfollowed_ancestors",
    "check_unfollowed_descendant",
    "child_cpu_times",
    "detached_subprocess_kwargs",
    "enter_proposal_origin",
    "env_policy_description",
    "get_platform",
    "has_ordinary_dir",
    "has_ordinary_file",
    "is_ordinary_dir_stat",
    "is_ordinary_file_stat",
    "is_reparse_or_symlink",
    "is_reparse_or_symlink_stat",
    "is_reserved_windows_segment",
    "isolated_process_group_kwargs",
    "lock_file",
    "lock_file_nonblocking",
    "logical_cwd",
    "process_group_exists",
    "publish_exclusive",
    "safe_relpath",
    "set_private_file_mode",
    "sha256_file",
    "sha256_stream",
    "signal_process_group",
    "strict_json_loads",
    "sync_dir",
    "unlock_file",
    "validate_safe_segment",
    "verify_exact_filename",
    "worker_environment",
]
