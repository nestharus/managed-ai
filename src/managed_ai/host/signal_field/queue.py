#!/usr/bin/env python3
"""Optional host queue facade for supplied observing uses; no semantic observer or effect authority.

One local SQLite owner. The capable host supplies use definitions, source observations,
worker invocation and semantic acceptance. Public symbols are re-exported here so direct
CLI execution, importlib loading, and external domain planner exec() stay compatible.
"""
from __future__ import annotations

from pathlib import Path
import sys

if not __package__:
    _src_root = str(Path(__file__).resolve().parents[3])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai.host.signal_field.cli import OPERATIONS, dispatch_operation, main
from managed_ai.host.signal_field.config import (
    DEFAULT_CONFIG,
    FORMAT,
    MAX_WORKER_DRAIN_BYTES,
    MAX_WORKER_STDOUT_BYTES,
    MAX_WORKER_STREAM_BYTES,
    MODES,
    SignalFieldConfig,
    SignalFieldService,
    digest,
    encoded,
    loads_strict,
    require,
)
from managed_ai.host.signal_field.models import (
    StreamCapture as _StreamCapture,
    WorkerRunResult as _WorkerRunResult,
    stream_details as _stream_details,
    worker_outcome as _worker_outcome,
)
from managed_ai.host.signal_field.operations import Field
from managed_ai.host.signal_field.storage import (
    inside_product,
    no_journal,
    planning_snapshot,
    product_root as _product_root,
    require_ordinary_non_symlink as _require_ordinary_non_symlink,
    validate_db_path,
)
from managed_ai.host.signal_field.worker import run_worker_bounded as _run_worker_bounded
from managed_ai.platform import is_reparse_or_symlink as _is_reparse_or_symlink

__all__ = [
    "DEFAULT_CONFIG",
    "FORMAT",
    "Field",
    "MAX_WORKER_DRAIN_BYTES",
    "MAX_WORKER_STDOUT_BYTES",
    "MAX_WORKER_STREAM_BYTES",
    "MODES",
    "OPERATIONS",
    "SignalFieldConfig",
    "SignalFieldService",
    "digest",
    "dispatch_operation",
    "encoded",
    "inside_product",
    "loads_strict",
    "main",
    "no_journal",
    "planning_snapshot",
    "require",
    "validate_db_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
