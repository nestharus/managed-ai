"""Domain-native configuration and service contract for material_retention."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

FORMAT = "managed-material/1"
RAW_BODY_KINDS = frozenset({"blob", "slice-view", "record-view", "recipe-view"})
IMMUTABLE_RECORD_DIRS = (
    "manifests",
    "selections",
    "lifecycle",
    "occurrences",
    "transitions",
)
PACKAGE_MODULES = (
    "__init__.py",
    "__main__.py",
    "_platform_bridge.py",
    "config.py",
    "store_io.py",
    "retention_closures.py",
    "store_core.py",
    "snapshot_ops.py",
    "transfer_ops.py",
    "material_ops.py",
    "retirement.py",
    "managed_material.py",
)
PLATFORM_MODULES = (
    "__init__.py",
    "adapter.py",
    "_common.py",
    "_posix.py",
    "_windows.py",
)


@dataclass(frozen=True)
class RetentionLimits:
    """Immutable safety and caching bounds for material retention stores."""

    max_zip_depth: int = 12
    max_zip_members: int = 200_000
    max_member_bytes: int = 1024**3
    max_expanded_bytes: int = 4 * 1024**3
    max_cache_object_bytes: int = 64 * 1024**2
    max_cache_total_bytes: int = 96 * 1024**2
    stream_chunk_bytes: int = 65_536


DEFAULT_LIMITS = RetentionLimits()


@runtime_checkable
class MaterialRetentionService(Protocol):
    """Domain-native contract for content-addressed material retention operations."""

    def snapshot(self, spec_path: Path | str) -> dict[str, Any]:
        """Capture and publish a verified material selection occurrence."""

    def verify(self, occurrence: str | None = None) -> dict[str, Any]:
        """Verify every reachable object and receipt in a selection closure."""

    def export(self, output: Path | str, occurrence: str | None = None) -> dict[str, Any]:
        """Export a self-contained portable recovery archive for a verified closure."""

    def restore(
        self,
        output: Path | str,
        occurrence: str | None = None,
        prefix: str | None = None,
    ) -> dict[str, Any]:
        """Materialize and readback-verify files from a retained occurrence."""

    def retain_artifact(self, path: Path | str, label: str, account: str) -> dict[str, Any]:
        """Retain an additional historical artifact without replacing current roles."""

    def retirement_plan(
        self,
        spec_path: Path | str | dict[str, Any],
        occurrence: str | None = None,
    ) -> dict[str, Any]:
        """Verify live equality and recoverability for candidate retirement prefixes."""

    def retire(
        self,
        plan_path: Path | str,
        expected_plan: str,
        expected_selection: str,
    ) -> dict[str, Any]:
        """Execute an explicitly received retirement plan against live materializations."""
