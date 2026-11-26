"""Continuing-work host bridge package."""
from __future__ import annotations

from .continuing_work import Host
from .contract import ContinuingWorkService, DEFAULT_HOST_CONFIG, HostConfig

__all__ = [
    "ContinuingWorkService",
    "DEFAULT_HOST_CONFIG",
    "Host",
    "HostConfig",
]
