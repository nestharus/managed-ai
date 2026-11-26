"""Compatibility re-export shim for managed_ai.library.bundle."""
from __future__ import annotations

from ..bundle import bundle_bytes, context, export, import_bundle, inspect_bundle, product, resolve

__all__ = ["bundle_bytes", "context", "export", "import_bundle", "inspect_bundle", "product", "resolve"]
