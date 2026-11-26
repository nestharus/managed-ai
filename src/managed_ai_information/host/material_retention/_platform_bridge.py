"""Resolve the stdlib-only platform adapter in-repo or inside standalone recovery archives."""

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import managed_ai_information.platform as platform
else:
    try:
        platform = (
            importlib.import_module(f"{__package__}._platform")
            if __package__
            else importlib.import_module("managed_ai_information.platform")
        )
    except ImportError:
        import managed_ai_information.platform as platform

__all__ = ["platform"]
