"""Bounded POSIX/Windows hot-record I/O and HeadIndex v3 metadata helpers for Discovery."""

import errno
import hashlib
import hmac
import os
import stat
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any

from managed_ai.core.ownership import packet_id
from managed_ai.core.storage import _OPEN_RO_NOFOLLOW, is_ordinary_file_stat
from managed_ai.store.protocol.inputs import parse_input_bytes
from managed_ai.store.query.discovery_report import digest
from managed_ai.store.records.head_index_bin import FLAG_ACTIVE
from managed_ai.store.records.record_scanner import open_verified_index_view
from managed_ai.store.records.records import RecordStore


def prepare_record_contexts(
    discovery: Any,
) -> tuple[AbstractContextManager[int | None], AbstractContextManager[Any]]:
    """Open fast-path record directory handle and verified HeadIndex v3 view when eligible."""
    fd_ctx: AbstractContextManager[int | None] = nullcontext(None)
    verified_ctx: AbstractContextManager[Any] = nullcontext(None)

    use_fast_records = (
        "records" in discovery.req["scopes"]
        and type(discovery.records).size is RecordStore.size
        and type(discovery.records).read is RecordStore.read
        and (discovery.records.work / "records").is_dir()
    )
    if not use_fast_records:
        return fd_ctx, verified_ctx

    if hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
        fd_ctx = discovery.records.ex.open_workspace_dir("records")
    else:
        _, discovery._records_dir_str = discovery.records._validated_records_dir_win()

    opened_view = open_verified_index_view(discovery.records)
    if opened_view is not None:
        verified_ctx = opened_view

    return fd_ctx, verified_ctx


def _fallback_read_bounded(
    discovery: Any, ref: str, ident: str
) -> tuple[dict[str, Any], bytes] | None:
    byte_limit = discovery.records.size(ident)
    if not discovery._budget_bytes(ref, byte_limit):
        return None
    return discovery.records.read(ident, byte_limit=byte_limit)


def _read_record_bounded_posix(
    discovery: Any, ref: str, ident: str, records_fd: int
) -> tuple[dict[str, Any], bytes] | None:
    filename = packet_id(ident) + ".json"
    full_path = discovery.records.work / "records" / filename

    try:
        fd = os.open(filename, _OPEN_RO_NOFOLLOW, dir_fd=records_fd)
    except FileNotFoundError:
        return _fallback_read_bounded(discovery, ref, ident)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
            raise ValueError(f"ordinary JSON file required: {full_path}") from None
        raise

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"ordinary JSON file required: {full_path}")
        byte_limit = info.st_size
        if not discovery._budget_bytes(ref, byte_limit):
            os.close(fd)
            fd = -1
            return None
        stream = os.fdopen(fd, "rb")
        fd = -1
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise

    with stream:
        raw = stream.read(byte_limit + 1)
    if len(raw) > byte_limit:
        raise ValueError(f"input exceeds {byte_limit} bytes: {full_path}")
    record = parse_input_bytes(raw, full_path)
    discovery.records._verify_record(ident, record, raw)
    return record, raw


def _read_record_bounded_win(
    discovery: Any, ref: str, ident: str, records_dir_str: str
) -> tuple[dict[str, Any], bytes] | None:
    filename = packet_id(ident) + ".json"
    full_str = records_dir_str + os.sep + filename

    try:
        info = os.lstat(full_str)
    except FileNotFoundError:
        return _fallback_read_bounded(discovery, ref, ident)
    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary JSON file required: {full_str}")

    byte_limit = info.st_size
    if not discovery._budget_bytes(ref, byte_limit):
        return None
    with open(full_str, "rb") as stream:
        raw = stream.read(byte_limit + 1)
    if len(raw) > byte_limit:
        raise ValueError(f"input exceeds {byte_limit} bytes: {full_str}")
    record = parse_input_bytes(raw, Path(full_str))
    discovery.records._verify_record(ident, record, raw)
    return record, raw


def read_record_bounded(
    discovery: Any, ref: str, ident: str
) -> tuple[dict[str, Any], bytes] | None:
    """Read and verify one hot record in a single open/stat under the discovery byte budget."""
    if discovery._records_fd is not None:
        return _read_record_bounded_posix(discovery, ref, ident, discovery._records_fd)
    if discovery._records_dir_str is not None:
        return _read_record_bounded_win(discovery, ref, ident, discovery._records_dir_str)
    return _fallback_read_bounded(discovery, ref, ident)


def verified_record_meta(
    discovery: Any, ident: str
) -> tuple[int, Any, str, int, str, str, bytes, bool] | None:
    """Return verified HeadIndex v3 metadata tuple for a hot record, or None if unavailable."""
    verified = discovery._verified_index
    if verified is None:
        return None

    entry = verified.overrides.get(ident)
    if entry is None:
        entry = verified.view.delta_puts.get(ident)
    if entry is not None:
        raw_sha_hex = entry["raw_sha256"]
        return (
            int(entry["size"]),
            entry.get("format"),
            entry["kind"],
            entry["revision"],
            entry["sha256"],
            raw_sha_hex,
            bytes.fromhex(raw_sha_hex),
            bool(entry.get("active", True)),
        )

    if ident in verified.removed or ident in verified.view.delta_dels:
        return None
    slot = verified.view.slot_for_id(ident)
    if slot < 0:
        return None

    tup = verified.view.slot_tuple(slot)
    raw_sha_bin = tup[19]
    return (
        tup[15],
        verified.view.record_format,
        verified.view.kind_names()[tup[7]],
        tup[9],
        tup[18].hex(),
        raw_sha_bin.hex(),
        raw_sha_bin,
        bool(tup[8] & FLAG_ACTIVE),
    )


def _read_raw_posix(
    records_root: Path, records_fd: int, filename: str, expected_size: int
) -> bytes:
    full_path = records_root / "records" / filename
    try:
        fd = os.open(filename, _OPEN_RO_NOFOLLOW, dir_fd=records_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
            raise ValueError(f"ordinary JSON file required: {full_path}") from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"ordinary JSON file required: {full_path}")
        stream = os.fdopen(fd, "rb")
        fd = -1
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise
    with stream:
        return stream.read(expected_size + 1)


def _read_raw_win(records_dir_str: str, filename: str, expected_size: int) -> bytes:
    full_str = records_dir_str + os.sep + filename
    try:
        info = os.lstat(full_str)
    except OSError:
        raise ValueError(f"ordinary JSON file required: {full_str}") from None
    if not is_ordinary_file_stat(info):
        raise ValueError(f"ordinary JSON file required: {full_str}")
    with open(full_str, "rb") as stream:
        return stream.read(expected_size + 1)


def read_verified_record_raw(
    discovery: Any, ident: str, expected_size: int, expected_raw_sha_bin: bytes
) -> tuple[bytes, dict[str, Any] | None]:
    """Read a hot record's raw bytes and verify SHA-256 against HeadIndex v3."""
    filename = packet_id(ident) + ".json"
    if discovery._records_fd is not None:
        raw = _read_raw_posix(
            discovery.records.work, discovery._records_fd, filename, expected_size
        )
    elif discovery._records_dir_str is not None:
        raw = _read_raw_win(discovery._records_dir_str, filename, expected_size)
    else:
        record, raw = discovery.records.read(ident, byte_limit=expected_size)
        return raw, record

    if len(raw) == expected_size and hmac.compare_digest(
        hashlib.sha256(raw).digest(), expected_raw_sha_bin
    ):
        return raw, None

    record, raw = discovery.records.read(ident, byte_limit=expected_size)
    return raw, record


def inspect_record(discovery: Any, ref: str, ident: str) -> dict[str, Any] | None:
    """Load bounded metadata (from HeadIndex v3 or direct read) for one hot record."""
    meta = discovery._verified_record_meta(ident)
    if meta is not None:
        size_val, fmt, kind, revision, sha_hex, raw_sha_hex, raw_sha_bin, active = meta
        if not discovery._budget_bytes(ref, size_val):
            return None
        discovery.body_info[ref] = {
            "bytes": size_val,
            "sha256": raw_sha_hex,
            "verified": True,
            "text": True,
        }
        if not discovery.req["include_inactive"] and not active:
            discovery._skip(ref, "inactive-record")
            return None
        return {
            "ref": ref,
            "scope": "records",
            "id": ident,
            "source": {
                "scope": "records",
                "id": ident,
                "format": fmt,
                "revision": revision,
                "record_sha256": sha_hex,
                "body_sha256": raw_sha_hex,
                "bytes": size_val,
            },
            "title": ident,
            "metadata": f"{ident} {kind}",
            "_raw_sha_bin": raw_sha_bin,
            "_size": size_val,
        }

    loaded = discovery._read_record_bounded(ref, ident)
    if loaded is None:
        return None
    record, raw = loaded
    raw_sha = digest(raw)
    discovery.bodies[ref] = raw.decode("utf-8")
    discovery.body_info[ref] = {
        "bytes": len(raw),
        "sha256": raw_sha,
        "verified": True,
        "text": True,
    }
    if not discovery.req["include_inactive"] and record.get("active") is False:
        discovery._skip(ref, "inactive-record")
        return None
    return {
        "ref": ref,
        "scope": "records",
        "id": ident,
        "source": {
            "scope": "records",
            "id": ident,
            "format": record.get("format"),
            "revision": record["revision"],
            "record_sha256": record["sha256"],
            "body_sha256": raw_sha,
            "bytes": len(raw),
        },
        "title": ident,
        "metadata": " ".join(str(record.get(k, "")) for k in ("id", "kind")),
        "record": record,
    }
