"""Domain-native deployment selection and admission contracts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class EnginePin:
    """Verified reference to one shared executable engine tree."""

    path: str
    code_sha256: str
    engine_api: str
    git_commit: str | None = None
    git_clean: bool | None = None


@dataclass(frozen=True)
class StorePin:
    """Verified reference to one schema-governed local store."""

    path: str
    instance_id: str
    store_schema: str
    engine_api: str


@dataclass(frozen=True)
class DeploymentStatus:
    """Observed verified status of a deployment binding."""

    format: str
    deployment: str
    engine: str
    engine_code_sha256: str
    store: str
    instance_id: str
    store_schema: str
    engine_api: str
    descriptor_sha256: str
    scope: str
    engine_git_commit: str | None = None
    engine_git_clean: bool | None = None


@dataclass(frozen=True)
class StageReceipt:
    """Receipt emitted when staging a candidate engine selection."""

    edition: str
    candidate_sha256: str
    engine: str
    engine_git_commit: str | None = None
    engine_git_clean: bool | None = None


@runtime_checkable
class DeploymentService(Protocol):
    """Contract for selecting, staging, activating, and dispatching a deployment."""

    root: Path
    descriptor_sha256: str

    def status(self) -> dict[str, Any]: ...

    def verify(self) -> dict[str, Any]: ...

    def stage(
        self,
        edition: str | None,
        engine: Path | str,
        *,
        from_head: bool = False,
    ) -> dict[str, Any]: ...

    def activate(
        self,
        edition: str,
        expected: str,
        manifest_sha: str,
        reason: str,
    ) -> dict[str, Any]: ...

    def system(self, arguments: list[str]) -> Any: ...
