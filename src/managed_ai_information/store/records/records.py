"""Native record heads, revision checks, basis and retained history."""

import errno
import os
import stat
from collections.abc import Callable, Generator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from managed_ai_information.core.formats import COMPOSITION_FORMAT
from managed_ai_information.core.ownership import packet_id
from managed_ai_information.core.storage import (
    MAX_BYTES,
    OPEN_RO_NOFOLLOW,
    atomic_bytes,
    encode,
    is_ordinary_file_stat,
    now,
    read_bytes,
    sha,
)
from managed_ai_information.platform import PLATFORM

from ..governance.feedback import KIND as FEEDBACK_KIND
from ..protocol.identities import identity, record_identity, require, strict_json
from ..protocol.inputs import integer, read_input
from . import record_scanner, record_validation
from .cold_archive import ColdArchive
from .head_index import HeadIndex, build_entry
from .head_receipts import MIN_BYTES, HeadReceipts
from .record_validation import (
    content_addressed_sha as content_addressed_sha,
)
from .record_validation import (
    is_content_addressed_basis as is_content_addressed_basis,
)


class RecordStore:
    def __init__(
        self,
        exchange: Any,
        record_format: str,
        envelope: Any,
        *,
        content_addressed_basis_prefixes: tuple[str, ...] = (),
        composition_validator: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.ex = exchange
        self.work = exchange.workspace
        self.objects = exchange.objects
        self.receipts = HeadReceipts(exchange.root)
        self.index = HeadIndex(exchange.root, self.receipts)
        self.cold_archive = ColdArchive(exchange.root, record_format)
        self.format = record_format
        self.envelope = envelope
        self.payload_validator: Callable[[dict[str, Any], dict[str, Any] | None], Any] | None = None
        self.composition_validator: Callable[[dict[str, Any]], Any] | None = composition_validator
        self.retention_policy = "append-history"
        self.content_addressed_basis_prefixes = tuple(content_addressed_basis_prefixes)
        self._deferred_index_ids: list[str] | None = None
        self._deferred_index_entries: dict[str, dict[str, Any]] | None = None

    @contextmanager
    def batch_index_updates(self) -> Generator[None]:
        """Defer incremental HeadIndex rewrites across a multi-record batch and flush once."""
        if self._deferred_index_ids is not None:
            yield
            return

        deferred_ids: list[str] = []
        deferred_entries: dict[str, dict[str, Any]] = {}
        self._deferred_index_ids = deferred_ids
        self._deferred_index_entries = deferred_entries
        try:
            yield
        finally:
            pending_ids = list(dict.fromkeys(deferred_ids))
            prebuilt = dict(deferred_entries)
            self._deferred_index_ids = None
            self._deferred_index_entries = None
            missing_ids = [ident for ident in pending_ids if ident not in prebuilt]
            if prebuilt or missing_ids:
                self.refresh_index_entries(missing_ids, prebuilt=prebuilt)

    @staticmethod
    def goal_record(goal: dict) -> dict:
        return record_validation.validate_goal_record(goal)

    @staticmethod
    def relative_name(ident: str) -> str:
        return "records/" + packet_id(ident) + ".json"

    def path(self, ident: str) -> Path:
        return self.ex.path(self.relative_name(ident))

    def material(self, ident: str) -> str:
        return self.path(ident).relative_to(self.work).as_posix()

    def _cold_size(self, ident: str) -> int:
        if self.cold_archive.has(ident):
            return len(self.cold_archive.read(ident)[1])
        raise FileNotFoundError(ident)

    def size(self, ident: str) -> int:
        path = self.path(ident)
        if hasattr(os, "O_NOFOLLOW"):
            try:
                fd = os.open(path, OPEN_RO_NOFOLLOW)
            except FileNotFoundError:
                return self._cold_size(ident)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                    raise ValueError(f"ordinary JSON file required: {path}") from None
                raise
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f"ordinary JSON file required: {path}")
                return info.st_size
            finally:
                os.close(fd)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return self._cold_size(ident)
        if not is_ordinary_file_stat(info):
            raise ValueError(f"ordinary JSON file required: {path}")
        return info.st_size

    def _check_no_hot_collision(self, ident: str, path: Path) -> None:
        require(
            not path.exists() and not path.is_symlink(),
            f"case-only record identity collision on filesystem: {ident}",
        )

    def _has_exact_hot_record(self, ident: str, hot: Path) -> bool:
        if not hot.exists() and not hot.is_symlink():
            return False
        if PLATFORM.is_case_insensitive_fs and hot.exists() and not hot.is_symlink():
            return PLATFORM.verify_exact_filename(hot, f"{ident}.json")
        return True

    def has_record(self, ident: str) -> bool:
        try:
            packet_id(ident)
        except ValueError:
            return False
        hot = self.path(ident)
        if hot.is_file() and self._has_exact_hot_record(ident, hot):
            return True
        return self.cold_archive.has(ident)

    def show(self, ident: str) -> dict:
        return self.read(ident)[0]

    def read(
        self, ident: str, *, byte_limit: int | None = None, dir_fd: int | None = None
    ) -> tuple[dict, bytes]:
        if dir_fd is not None:
            filename = packet_id(ident) + ".json"
            record, raw = read_input(
                self.work / "records" / filename,
                byte_limit=byte_limit,
                dir_fd=dir_fd,
                filename=filename,
            )
        else:
            hot = self.path(ident)
            if not self._has_exact_hot_record(ident, hot):
                if self.cold_archive.has(ident):
                    record, raw = self.cold_archive.read(ident)
                    if byte_limit is not None and len(raw) > byte_limit:
                        raise ValueError(f"input exceeds {byte_limit} bytes: {ident}")
                    return record, raw
                raise FileNotFoundError(f"record not found: {ident}")
            record, raw = read_input(hot, byte_limit=byte_limit)
        self._verify_record(ident, record, raw)
        return record, raw

    def _verify_record(self, ident: str, record: dict[str, Any], raw: bytes | None = None) -> None:
        if record.get("id") != ident or record.get("format") != self.format:
            raise ValueError("record identity or format changed")
        if record.get("sha256") != sha(encode({k: v for k, v in record.items() if k != "sha256"})):
            raise ValueError("record digest changed")

    def captured_record(self, ident: str, basis: dict[str, Any]) -> dict[str, Any]:
        """Open the exact local record object named by an already qualified native basis."""
        require(
            basis["path"] == self.relative_name(ident), "basis belongs to a different source path"
        )
        raw = self.objects.read(basis["sha256"], retained=True, expected_bytes=basis["bytes"])
        record = strict_json(raw)
        self._verify_record(ident, record, raw)
        identity(record_identity(record))
        return record

    def _read_scoped_one(
        self,
        ident: str,
        needs_body: Callable[[str, str], bool],
        indexed: dict[str, dict[str, Any]] | None,
        records_fd: int | None,
    ) -> dict[str, Any]:
        return record_scanner.read_scoped_one(self, ident, needs_body, indexed, records_fd)

    @staticmethod
    def _chunk_map[T, R](
        items: list[T],
        fn: Callable[[list[T]], list[R]],
        *,
        max_workers: int = 8,
        min_parallel: int = 16,
    ) -> list[R]:
        return record_scanner.chunk_map(
            items, fn, max_workers=max_workers, min_parallel=min_parallel
        )

    def _validated_records_dir_win(self) -> tuple[Path, str]:
        return record_scanner.validated_records_dir_win(self)

    def _read_verified_win(
        self,
        dir_str: str,
        ident: str,
        *,
        allow_cold: bool = True,
    ) -> tuple[dict[str, Any], bytes, os.stat_result | None]:
        return record_scanner.read_verified_win(self, dir_str, ident, allow_cold=allow_cold)

    def scoped_heads(
        self, needs_body: Callable[[str, str], bool], *, max_workers: int = 16
    ) -> Iterator[dict[str, Any]]:
        return record_scanner.scoped_heads(self, needs_body, max_workers=max_workers)

    def indexed_summaries_with_format(
        self, *, max_workers: int = 16
    ) -> tuple[dict[str, dict[str, Any]], str] | None:
        return record_scanner.indexed_summaries_with_format(self, max_workers=max_workers)

    def open_verified_index_view(
        self, *, max_workers: int = 16
    ) -> record_scanner.VerifiedIndexView | None:
        return record_scanner.open_verified_index_view(self, max_workers=max_workers)

    def get_indexed_entry(self, ident: str) -> dict[str, Any] | None:
        return record_scanner.get_verified_entry(self, ident)

    def indexed_summaries(self, *, max_workers: int = 16) -> dict[str, dict[str, Any]] | None:
        loaded = self.indexed_summaries_with_format(max_workers=max_workers)
        return None if loaded is None else loaded[0]

    def scan_records(
        self, idents: Iterable[str] | None = None, *, max_workers: int = 16
    ) -> list[dict[str, Any]]:
        return record_scanner.scan_records(self, idents, max_workers=max_workers)

    def reindex(self, *, max_workers: int = 16) -> dict[str, Any]:
        return record_scanner.reindex(self, max_workers=max_workers)

    def refresh_index_entries(
        self,
        idents: list[str],
        *,
        prebuilt: dict[str, dict[str, Any]] | None = None,
    ) -> bool:
        return record_scanner.refresh_index_entries(self, idents, prebuilt=prebuilt)

    def index_scope(self, ident: str) -> dict[str, Any]:
        path = self.path(ident)
        if self.size(ident) < MIN_BYTES:
            return {"id": ident, "indexed": False, "reason": "below size threshold"}
        record, raw = self.read(ident)
        return {
            "id": ident,
            "revision": record["revision"],
            "indexed": self.receipts.issue(path, record, raw),
        }

    def all(self) -> list[dict[str, Any]]:
        return self.scan_records()

    def ids(self) -> list[str]:
        records_dir = self.work / "records"
        try:
            with os.scandir(records_dir) as entries:
                # Preserve <id>.json filename sort order
                # (pinned by continuing_state_support census/obligation_basis).
                names = sorted(entry.name for entry in entries if entry.name.endswith(".json"))
        except FileNotFoundError:
            return []
        return [name[:-5] for name in names]

    def _preview_basis(self, source: str) -> dict:
        path = self.ex.path(source)
        if not path.exists():
            return {"path": source, "sha256": None, "bytes": 0}
        raw = read_bytes(path)
        key = sha(raw)
        if (
            self.objects.has(key)
            and self.objects.read(key, retained=True, expected_bytes=len(raw)) != raw
        ):
            raise ValueError("content object changed")
        return {"path": source, "sha256": key, "bytes": len(raw)}

    def validate_import(self, record: dict) -> None:
        """Qualify a new write or a matching current head without retaining evidence."""
        if not isinstance(record, dict):
            raise ValueError("record must be an object")

        ident = packet_id(record.get("id", ""))
        path = self.path(ident)
        if not self._has_exact_hot_record(ident, path):
            self._check_no_hot_collision(ident, path)
            require(
                not self.cold_archive.has(ident),
                "record exists in cold archive; restore explicitly before importing: " + ident,
            )
            self._prepare_write(record, None, None, retain_basis=False)
            return

        previous = self.show(ident)
        self._validate_payload(record, previous)
        normalized = record_validation.normalize_record(self, record, previous, retain_basis=False)
        if encode(normalized) != encode({key: previous[key] for key in normalized}):
            raise ValueError("current import conflicts with existing record: " + ident)

    def put(self, record: dict, expected: int) -> dict:
        return self._replace(record, expected)

    def put_reviewed(
        self,
        record: dict[str, Any],
        expected: int,
        basis: list[dict[str, Any]],
        *,
        verify: Callable[[], Any] | None = None,
    ) -> dict[str, Any]:
        """Admit only the composition body and basis actually prepared for this write."""
        return self._replace(record, expected, reviewed_basis=basis, verify=verify)

    def put_feedback(self, record: dict[str, Any], expected: int) -> dict[str, Any]:
        return self._replace(record, expected, feedback_write=True)

    def put_information(self, record: dict[str, Any], expected: int, mode: str) -> dict[str, Any]:
        return self._replace(record, expected, information_mode=mode)

    def _replace(
        self,
        record: dict[str, Any],
        expected: int,
        *,
        information_mode: str | None = None,
        feedback_write: bool = False,
        observation_write: bool = False,
        reviewed_basis: list[dict[str, Any]] | None = None,
        preserve_basis: list[dict[str, Any]] | None = None,
        verify: Callable[[], Any] | None = None,
        _defer_index_update: bool = False,
        _preloaded: tuple[dict[str, Any] | None, bytes | None] | None = None,
        _prepared: tuple[dict[str, Any], bytes] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise ValueError("record must be an object")

        integer(expected, "expected revision")
        ident = packet_id(record.get("id", ""))
        path = self.path(ident)

        if _preloaded is not None:
            previous, previous_raw = _preloaded
        elif not self._has_exact_hot_record(ident, path):
            self._check_no_hot_collision(ident, path)
            if self.cold_archive.has(ident):
                raise ValueError(
                    f"record {ident} is in cold archive; restore explicitly before modifying"
                )
            previous, previous_raw = None, None
        else:
            previous, previous_raw = self.read(ident)

        revision = previous["revision"] if previous else 0
        if revision != expected:
            raise ValueError(f"record revision conflict: expected {expected}, current {revision}")

        if _prepared is not None:
            body, raw = _prepared
        else:
            body, raw = self._prepare_write(
                record,
                previous,
                previous_raw,
                information_mode=information_mode,
                feedback_write=feedback_write,
                observation_write=observation_write,
                retain_basis=reviewed_basis is None,
                preserve_basis=preserve_basis,
            )

        if reviewed_basis is not None:
            require(
                body["basis"] == reviewed_basis, "captured workspace basis changed at publication"
            )
            with self.objects.pack_session():
                require(
                    all(
                        row["sha256"] is None or self.objects.has(row["sha256"])
                        for row in reviewed_basis
                    ),
                    "reviewed basis object is unavailable",
                )
            require(
                encode(body["content"]) == encode(record["content"]),
                "normalized composition differs from reviewed body",
            )

        if verify is not None:
            verify()

        old = body["previous_object"]
        if old is not None:
            if previous_raw is None:
                raise RuntimeError(f"missing previous raw bytes for retained object: {ident}")
            if len(previous_raw) <= MAX_BYTES:
                self.objects.put(previous_raw)
            else:
                self.objects.put_retained(previous_raw, max_bytes=len(previous_raw))

        atomic_bytes(path, raw)
        self.receipts.issue(path, body, raw)

        if not _defer_index_update:
            if self._deferred_index_ids is not None:
                self._deferred_index_ids.append(body["id"])
                if self._deferred_index_entries is not None and self.index.exists():
                    self._deferred_index_entries[body["id"]] = build_entry(body, raw, path.lstat())
            elif self.index.exists():
                self.index.update_if_present(
                    self.format, {body["id"]: build_entry(body, raw, path.lstat())}
                )

        return {
            "id": body["id"],
            "revision": body["revision"],
            "sha256": body["sha256"],
            "path": str(path),
            "previous_object": old,
        }

    def _validate_payload(
        self,
        record: dict[str, Any],
        previous: dict[str, Any] | None,
        *,
        information_mode: str | None = None,
    ) -> None:
        self.envelope.validate_write(record, previous, mode=information_mode)
        content = record.get("content")
        if (
            isinstance(content, dict)
            and content.get("format") == COMPOSITION_FORMAT
            and self.composition_validator is not None
        ):
            self.composition_validator(record)
        if self.payload_validator is not None:
            self.payload_validator(record, previous)

    def _prepare_write(
        self,
        record: dict[str, Any],
        previous: dict[str, Any] | None,
        previous_raw: bytes | None,
        *,
        information_mode: str | None = None,
        feedback_write: bool = False,
        observation_write: bool = False,
        retain_basis: bool = True,
        preserve_basis: list[dict[str, Any]] | None = None,
        verify_preserved_objects: bool = True,
    ) -> tuple[dict[str, Any], bytes]:
        if not isinstance(record, dict):
            raise ValueError("record must be an object")

        ident = packet_id(record.get("id", ""))
        if previous is None and self.cold_archive.has(ident):
            raise ValueError(
                f"record {ident} is in cold archive; restore explicitly before modifying"
            )
        if (
            record.get("kind") == FEEDBACK_KIND or (previous and previous["kind"] == FEEDBACK_KIND)
        ) and not feedback_write:
            raise ValueError("feedback records must be changed through feedback operations")
        if (
            record.get("kind") == "source-observation"
            or (previous and previous["kind"] == "source-observation")
        ) and not observation_write:
            raise ValueError(
                "cross-instance observations must be received through transfer operations"
            )

        self._validate_payload(record, previous, information_mode=information_mode)
        normalized = record_validation.normalize_record(
            self,
            record,
            previous,
            retain_basis=retain_basis,
            preserve_basis=preserve_basis,
            verify_preserved_objects=verify_preserved_objects,
        )
        revision = previous["revision"] if previous else 0

        if (
            normalized["kind"] == FEEDBACK_KIND
            and self.retention_policy == "current-use"
            and any(
                row.get("route") not in {"work", "hold"}
                for row in normalized["content"].get("carried_returns", [])
            )
        ):
            raise ValueError(
                "resolve legacy carried feedback dispositions before ending predecessor retention"
            )

        retain = self.retention_policy == "append-history"
        old = sha(previous_raw) if previous_raw is not None and retain else None
        body = {
            "format": self.format,
            **normalized,
            "revision": revision + 1,
            "previous_object": old,
            "updated_at": now(),
            "retention": {
                "policy": self.retention_policy,
                "retained_from_revision": (
                    previous.get("retention", {}).get("retained_from_revision", 1)
                    if previous and retain
                    else revision + 1
                ),
            },
        }
        body["sha256"] = sha(encode(body))
        raw = encode(body)
        if len(raw) > MAX_BYTES:
            raise ValueError(
                f"complete encoded record exceeds {MAX_BYTES} bytes; "
                "reduce the record or nominate external evidence"
            )
        return body, raw

    def basis_changes(
        self,
        record: dict,
        *,
        resolved_hashes: dict[str, str | None] | None = None,
        verified_objects: set[tuple[str, int]] | None = None,
    ) -> list[dict]:
        return record_validation.compute_basis_changes(
            self, record, resolved_hashes=resolved_hashes, verified_objects=verified_objects
        )

    def head(self, wanted: dict) -> dict:
        identity(wanted)
        current = self.show(wanted["id"])
        require(
            record_identity(current) == wanted, "information source or contract revision conflict"
        )
        return current

    def pin(self, record: dict) -> dict:
        parsed, data = self.read(record["id"])
        require(parsed == record, "information record changed during pinning")
        if len(data) <= MAX_BYTES:
            obj = self.objects.put(data)
        else:
            obj = self.objects.put_retained(data, max_bytes=len(data))
        return dict(record_identity(record), object=obj)

    def history_pages(
        self, ident: str, *, limit: int = 8, cursor: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield descending verified pages. Reauthenticate the prefix once on external restart."""
        return record_validation.iter_history_pages(self, ident, limit=limit, cursor=cursor)
