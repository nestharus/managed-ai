"""Facade composing scope preparation, overlap indexing, receive/resume, supersession, and
status.
"""

from pathlib import Path
from typing import Any

from managed_ai_information.host.continuing_work.scope_prepare import (
    operation_guide_path,
    overlap_rows,
    prepare_attention_selection,
    prepare_questions,
    prepare_scope,
    write_overlap_index,
)
from managed_ai_information.host.continuing_work.scope_receive import (
    collect_status,
    finish_supersession,
    receive_scope,
    record_partial_publication,
    resume_scope,
    supersede_scope,
)


class ScopeLifecycle:
    """Composed collaborator owning scope preparation, receiving, resume, supersession, and
    status.
    """

    def __init__(self, host: Any) -> None:
        self._host = host

    def write_overlap_index(
        self,
        sid: str,
        sources: list[dict[str, Any]],
        resource_changes: list[dict[str, Any]],
        reads: list[str],
        writes: list[str],
        overlap_limit: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        return write_overlap_index(
            self._host,
            sid,
            sources,
            resource_changes,
            reads,
            writes,
            overlap_limit,
        )

    def prepare(self, spec: dict[str, Any]) -> dict[str, Any]:
        return prepare_scope(self._host, spec)

    def record_partial_publication(
        self,
        path: Path,
        scope: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        record_partial_publication(self._host, path, scope, **kwargs)

    def receive(self, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
        return receive_scope(self._host, sid, spec)

    def resume(self, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
        return resume_scope(self._host, sid, spec)

    def finish_supersession(
        self,
        path: Path,
        scope: dict[str, Any],
        returned: dict[str, Any],
    ) -> dict[str, Any]:
        return finish_supersession(self._host, path, scope, returned)

    def supersede(self, sid: str, spec: dict[str, Any]) -> dict[str, Any]:
        return supersede_scope(self._host, sid, spec)

    def status(self) -> dict[str, Any]:
        return collect_status(self._host)


__all__ = [
    "ScopeLifecycle",
    "operation_guide_path",
    "overlap_rows",
    "prepare_attention_selection",
    "prepare_questions",
]
