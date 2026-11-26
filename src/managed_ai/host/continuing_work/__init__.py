"""Continuing-work host bridge package."""

from .continuing_work import Host
from .contract import DEFAULT_HOST_CONFIG, ContinuingWorkService, HostConfig

__all__ = [
    "DEFAULT_HOST_CONFIG",
    "ContinuingWorkService",
    "Host",
    "HostConfig",
]
