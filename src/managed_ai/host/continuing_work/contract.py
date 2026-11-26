"""Domain-native configuration and service contract for continuing_work."""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class HostConfig:
    """Immutable format and bounding parameters for continuing-work host custody."""

    format_version: str = "g60-continuing-work-host/1"
    max_event_stream_bytes: int = 16 * 1024
    max_event_line_bytes: int = 64 * 1024
    default_overlap_limit: int = 8
    max_overlap_limit: int = 200
    max_attention_items: int = 2000
    max_attention_groups: int = 100


DEFAULT_HOST_CONFIG = HostConfig()


@runtime_checkable
class ContinuingWorkService(Protocol):
    """Domain-native contract for continuing-work host custody and scope lifecycle."""

    def init(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Initialize a new continuing-work host or attach an existing native store."""

    def commission(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Revise current instructions while preserving enrolled duties."""

    def upgrade(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Select a regenerated host release with byte-identical native programs."""

    def prepare(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Prepare an immutable producing scope with overlap nominations and context."""

    def capture(self, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Retain candidate assessments through the qualified native capture adapter."""

    def receive(self, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Receive candidate proposals and adopt verified target publications."""

    def resume(self, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Reconcile pending captures and resume or settle retained native returns."""

    def supersede(self, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Close an obsolete preparation without output and carry duties to a successor."""

    def duty(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Account for fulfilling or reopening an enrolled parent duty."""

    def finish(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Record whole-goal completion once all parent duties are settled."""

    def status(self) -> dict[str, Any]:
        """Report native turns, host scope states, pending captures, and repairs."""

    def repair_open(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Retain a causal failure occurrence with pinned inputs and producing means."""

    def repair_accept(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Verify mechanical lineage and semantic receiving to accept a repair."""
