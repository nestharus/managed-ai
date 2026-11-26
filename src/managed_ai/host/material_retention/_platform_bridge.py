"""Resolve the stdlib-only platform adapter in-repo or inside standalone recovery archives."""
from __future__ import annotations

try:
    from . import _platform as platform
except ImportError:
    import managed_ai.platform as platform

__all__ = ["platform"]
