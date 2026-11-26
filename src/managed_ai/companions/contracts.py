"""Domain-native service contracts for Managed AI companions."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class ModelBridge(Protocol):
    """Callable model transport protocol (mailbox, command, or injected callable)."""

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        """Submit one bounded step request and return a finite JSON action."""


@runtime_checkable
class SourceServiceProtocol(Protocol):
    """Scoped Markdown source observation, lexical discovery, and durable receipt service."""

    def inventory(self, refresh: bool = False) -> dict[str, Any]:
        """Return or refresh the observed eligible source inventory."""

    def discover(self, query: str, limit: int = 8) -> dict[str, Any]:
        """Nominate candidate sources from the observed inventory."""

    def read(
        self,
        path: str,
        *,
        fresh: bool = False,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Read one complete UTF-8 source file with content identity receipt."""

    def receipts(self) -> dict[str, Any]:
        """Return a detached snapshot of all published source/selection/use receipts."""

    def stats(self) -> dict[str, Any]:
        """Return current read, cache, and receipt accounting counters."""

    def close(self) -> None:
        """Release the in-memory lexical index handle."""


@runtime_checkable
class WorkbenchService(Protocol):
    """Local source-backed construction workbench contract."""

    def checkpoint(self) -> dict[str, Any]:
        """Capture deterministic workbench state excluding disposable caches."""

    def restore(self, checkpoint: dict[str, Any]) -> None:
        """Verify basis/workspace integrity and restore recorded associations."""

    def tools(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        """Expose bounded source, workspace, and receiving-check tool callables."""


@runtime_checkable
class PeerHarnessService(Protocol):
    """Cooperative finite-turn peer subprocess harness contract."""

    def run(self) -> dict[str, Any]:
        """Execute bounded peer worker turns and return the structured run result."""


@runtime_checkable
class PeerTurnRunner(Protocol):
    """Callable contract for executing one bounded peer workbench turn."""

    def __call__(
        self,
        config: dict[str, Any],
        request: dict[str, Any],
        *,
        model: ModelBridge | None = None,
    ) -> dict[str, Any]:
        """Execute one peer turn and return its response, report, and evidence directory."""


@runtime_checkable
class PublicationReviewService(Protocol):
    """Declared observation and continuation-assessment contract."""

    def review(
        self,
        previous: dict[str, Any],
        declaration: dict[str, Any],
        roots: dict[str, str | Path] | None = None,
        *,
        base_dir: Path | str | None = None,
        assessor_path: Path | str | None = None,
    ) -> dict[str, Any]:
        """Observe declared inputs and assess currentness against previous receipts."""


@runtime_checkable
class RepresentationService(Protocol):
    """Bound representation operation lifecycle contract."""

    def prepare(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Prepare a native turn bound to explicit accepted representations and sources."""

    def receive(
        self,
        turn: str,
        reply: Path | str,
        witness: dict[str, Any],
        producer: str,
        proposals: list[str],
    ) -> dict[str, Any]:
        """Verify authored witness bindings and submit candidate proposals to native receive."""

    def adopt(self, turn: str, target: str, reason: str) -> dict[str, Any]:
        """Verify bound witness and before/after hashes before delegating native adoption."""
