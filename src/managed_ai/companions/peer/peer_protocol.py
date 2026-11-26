"""Shared peer harness/workbench protocol contract, strict JSON, and durable I/O."""

import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from managed_ai.platform import (
    atomic_bytes,
    is_reparse_or_symlink,
    strict_json_loads,
    sync_dir,
)

REQUEST_SCHEMA = "peer-request/1"
RETURN_SCHEMA = "peer-return/1"
HARNESS_CONFIG_SCHEMA = "peer-harness-config/1"
HARNESS_RESULT_SCHEMA = "peer-harness-result/1"
PROCESS_SCHEMA = "peer-process/1"
WORKBENCH_CONFIG_SCHEMA = "peer-workbench-config/1"
WORKBENCH_STATE_SCHEMA = "peer-workbench-state/1"

OWNER_STATUSES = ("continue", "waiting", "complete")
PEER_ENTRY_FIELDS = ("revision", "status", "return_path", "manifest_path", "artifacts_dir")
ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


def canonical(value: Any) -> bytes:
    """Encode finite JSON canonically as UTF-8 bytes."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(body: bytes | bytearray | memoryview) -> str:
    """Return the lowercase SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(body).hexdigest()


def identity(body: bytes | bytearray | memoryview) -> dict[str, Any]:
    """Return byte count and SHA-256 hex digest for body."""
    return {"bytes": len(body), "sha256": digest(body)}


def loads_strict(raw: bytes | bytearray | str) -> Any:
    """Decode strict UTF-8 JSON, rejecting duplicate object keys and nonfinite numbers."""
    if not isinstance(raw, (bytes, bytearray, str)):
        raise TypeError("JSON input must be bytes or str")
    payload: bytes | str = bytes(raw) if isinstance(raw, bytearray) else raw
    value = strict_json_loads(payload, label="Peer JSON", require_object=False)
    canonical(value)
    return value


def detached(value: Any) -> Any:
    """Return a detached strict-JSON roundtrip copy of value."""
    return loads_strict(canonical(value))


def atomic_write_bytes(path: Path | str, data: bytes, *, exclusive: bool = False) -> None:
    """Durably publish bytes via unique temp file, fsync, atomic install, and dir sync."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_bytes(target, data, exclusive=exclusive)


def atomic_write_json(
    path: Path | str,
    value: Any,
    *,
    indent: int | None = None,
    exclusive: bool = False,
) -> None:
    """Durably publish strict JSON followed by a trailing newline."""
    if indent is None:
        payload = canonical(value) + b"\n"
    else:
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=indent,
            )
            + "\n"
        ).encode("utf-8")
    atomic_write_bytes(path, payload, exclusive=exclusive)


def need(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def fields(value: Any, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> None:
    need(isinstance(value, dict), "Expected an object")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    need(
        not missing and not unknown,
        f"Invalid fields; missing={sorted(missing)}, unknown={sorted(unknown)}",
    )


def integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def positive_number(value: Any, integer_only: bool = False) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int if integer_only else (int, float))
        and math.isfinite(value)
        and value > 0
    )


def identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and ID_PATTERN.fullmatch(value) is not None
        and value not in (".", "..")
    )


def target_path(value: Any) -> list[str]:
    need(
        isinstance(value, str) and bool(value) and "\\" not in value and "\x00" not in value,
        "Invalid target path",
    )
    need(
        not PureWindowsPath(value).drive and not (len(value) >= 2 and value[1] == ":"),
        "Target paths must not include a drive specifier",
    )
    parts = value.split("/")
    need(
        not any(
            part in ("", ".", "..")
            or PureWindowsPath(part).drive
            or (len(part) >= 2 and part[1] == ":")
            for part in parts
        )
        and not PurePosixPath(value).is_absolute(),
        "Target paths must be normalized relative POSIX file paths",
    )
    return parts


def path_conflicts(paths: Any) -> bool:
    unique = set(paths)
    for path in unique:
        pieces = path.split("/")
        if any("/".join(pieces[:n]) in unique for n in range(1, len(pieces))):
            return True
    return False


def canonical_absolute(
    value: Any,
    label: str,
    *,
    directory: bool = False,
    file: bool = False,
) -> Path:
    need(
        isinstance(value, str) and bool(value) and "\x00" not in value,
        label + " must be a nonempty path string",
    )
    path = Path(value)
    need(path.is_absolute(), label + " must be absolute")
    need(
        not any(is_reparse_or_symlink(part) for part in (path, *path.parents)),
        label + " cannot pass through a symlink or reparse point",
    )
    need(path == path.resolve(), label + " must be a canonical path without symlink traversal")
    if directory:
        need(path.is_dir(), label + " must be an existing ordinary directory")
    if file:
        need(path.is_file(), label + " must be an existing ordinary file")
    return path


def scoped_file(root: Path | str, name: str) -> Path:
    base = Path(root)
    path = base
    for part in target_path(name):
        path = path / part
        need(
            not is_reparse_or_symlink(path),
            "Symlink or reparse point is outside the file contract: " + name,
        )
    need(
        path.is_file() and path.resolve().is_relative_to(base.resolve()),
        "Ordinary scoped file required: " + name,
    )
    return path


def validate_peer_entry(entry: dict[str, Any]) -> None:
    """Validate one peer snapshot entry with explicit checkpoint paths."""
    fields(entry, PEER_ENTRY_FIELDS)
    need(
        integer(entry["revision"]) and entry["status"] in OWNER_STATUSES,
        "Invalid peer revision/status",
    )
    if entry["revision"] == 0:
        need(
            entry["return_path"] is None
            and entry["manifest_path"] is None
            and entry["artifacts_dir"] is None,
            "Unreturned peer cannot name captured files",
        )
    else:
        need(
            isinstance(entry["return_path"], str)
            and isinstance(entry["manifest_path"], str)
            and isinstance(entry["artifacts_dir"], str),
            "Returned peer requires explicit return_path, manifest_path, and artifacts_dir",
        )


__all__ = [
    "HARNESS_CONFIG_SCHEMA",
    "HARNESS_RESULT_SCHEMA",
    "ID_PATTERN",
    "OWNER_STATUSES",
    "PEER_ENTRY_FIELDS",
    "PROCESS_SCHEMA",
    "REQUEST_SCHEMA",
    "RETURN_SCHEMA",
    "WORKBENCH_CONFIG_SCHEMA",
    "WORKBENCH_STATE_SCHEMA",
    "atomic_write_bytes",
    "atomic_write_json",
    "canonical",
    "canonical_absolute",
    "detached",
    "digest",
    "fields",
    "identifier",
    "identity",
    "integer",
    "is_reparse_or_symlink",
    "loads_strict",
    "need",
    "path_conflicts",
    "positive_number",
    "scoped_file",
    "sync_dir",
    "target_path",
    "validate_peer_entry",
]
