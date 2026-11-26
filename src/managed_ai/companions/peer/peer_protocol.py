"""Shared peer harness/workbench protocol contract, strict JSON, and durable I/O."""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import uuid


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


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(body):
    return hashlib.sha256(body).hexdigest()


def identity(body):
    return {"bytes": len(body), "sha256": digest(body)}


def loads_strict(raw):
    """Decode strict UTF-8 JSON, rejecting duplicate object keys and nonfinite numbers."""
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8")
    elif isinstance(raw, str):
        text = raw
    else:
        raise TypeError("JSON input must be bytes or str")

    def no_constant(value):
        raise ValueError("Nonstandard JSON constant: " + value)

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key: " + key)
            result[key] = value
        return result

    value = json.loads(text, parse_constant=no_constant, object_pairs_hook=unique_pairs)
    canonical(value)
    return value


def detached(value):
    return loads_strict(canonical(value))


def sync_dir(path):
    if not hasattr(os, "O_DIRECTORY"):
        return
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.EBADF, errno.EROFS):
            raise
    finally:
        os.close(directory)


def atomic_write_bytes(path, data, *, exclusive=False):
    """Durably publish bytes via unique temp file, fsync, atomic install, and dir sync."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive and path.exists():
        raise FileExistsError(str(path))
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
            temporary.unlink()
        else:
            os.replace(temporary, path)
        sync_dir(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path, value, *, indent=None, exclusive=False):
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


def need(condition, message):
    if not condition:
        raise ValueError(message)


def fields(value, required, optional=()):
    need(isinstance(value, dict), "Expected an object")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    need(
        not missing and not unknown,
        f"Invalid fields; missing={sorted(missing)}, unknown={sorted(unknown)}",
    )


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def positive_number(value, integer_only=False):
    return (
        not isinstance(value, bool)
        and isinstance(value, int if integer_only else (int, float))
        and math.isfinite(value)
        and value > 0
    )


def identifier(value):
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None and value not in (".", "..")


def target_path(value):
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
            part in ("", ".", "..") or PureWindowsPath(part).drive or (len(part) >= 2 and part[1] == ":")
            for part in parts
        )
        and not PurePosixPath(value).is_absolute(),
        "Target paths must be normalized relative POSIX file paths",
    )
    return parts


def path_conflicts(paths):
    paths = set(paths)
    for path in paths:
        pieces = path.split("/")
        if any("/".join(pieces[:n]) in paths for n in range(1, len(pieces))):
            return True
    return False


def is_reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return bool(
        stat.S_ISLNK(info.st_mode)
        or (getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


def canonical_absolute(value, label, *, directory=False, file=False):
    need(isinstance(value, str) and bool(value) and "\x00" not in value, label + " must be a nonempty path string")
    path = Path(value)
    need(path.is_absolute(), label + " must be absolute")
    need(not any(is_reparse_or_symlink(part) for part in (path, *path.parents)), label + " cannot pass through a symlink or reparse point")
    need(path == path.resolve(), label + " must be a canonical path without symlink traversal")
    if directory:
        need(path.is_dir(), label + " must be an existing ordinary directory")
    if file:
        need(path.is_file(), label + " must be an existing ordinary file")
    return path


def scoped_file(root, name):
    root = Path(root)
    path = root
    for part in target_path(name):
        path = path / part
        need(not is_reparse_or_symlink(path), "Symlink or reparse point is outside the file contract: " + name)
    need(path.is_file() and path.resolve().is_relative_to(root.resolve()), "Ordinary scoped file required: " + name)
    return path


def validate_peer_entry(entry):
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
