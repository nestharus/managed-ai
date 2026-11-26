"""Backward-compatible re-export of managed_ai.platform primitives for core callers."""
from __future__ import annotations

from managed_ai.platform import (
    OPEN_DIR_NOFOLLOW,
    OPEN_RO_NOFOLLOW,
    PLATFORM,
    admission_deadline,
    check_ordinary_ancestors,
    has_ordinary_dir,
    has_ordinary_file,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    is_reparse_or_symlink,
    is_reserved_windows_segment,
    lock_file_nonblocking,
    safe_relpath,
    strict_json_loads,
    sync_dir,
    unlock_file,
    validate_safe_segment,
)

__all__ = [
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
    "is_reserved_windows_segment",
    "lock_file_nonblocking",
    "safe_relpath",
    "strict_json_loads",
    "sync_dir",
    "unlock_file",
    "validate_safe_segment",
]
