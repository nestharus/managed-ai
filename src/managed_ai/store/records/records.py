"""Native record heads, revision checks, basis and retained history."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import errno
import hmac
import json
import os
from pathlib import Path
import re
import stat

from core.formats import COMPOSITION_FORMAT
from core.ownership import packet_id, relative_path
from core.storage import (
    MAX_BYTES,
    _OPEN_RO_NOFOLLOW,
    atomic_bytes,
    encode,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    now,
    read_bytes,
    sha,
)
from store.records.cold_archive import ColdArchive
from store.governance.feedback import KIND as FEEDBACK_KIND
from store.records.head_index import HeadIndex, build_entry
from store.records.head_receipts import HeadReceipts, MIN_BYTES
from store.protocol.identities import digest, exact_fields, identity, record_identity, require, strict_json
from store.protocol.inputs import accounted_closure, fields, integer, parse_input_bytes, read_input, strings, text


_SHARD_CONTENT_RE = re.compile(r'^([0-9a-f]{2})/([0-9a-f]{64})\.[A-Za-z0-9._-]+$')
_OBJECT_BASIS_RE = re.compile(r'^object:([0-9a-f]{64})$')


def content_addressed_sha(path: str, *, prefixes: tuple[str, ...] = ()) -> str | None:
    """Return the embedded 64-hex digest if path is an immutable content-addressed basis reference.

    Always recognizes generic `object:<64hex>`. Also recognizes domain-declared
    content-addressed workspace prefixes `<prefix><sha[:2]>/<sha>.<ext>`.
    """
    if not isinstance(path, str):
        return None
    obj_match = _OBJECT_BASIS_RE.fullmatch(path)
    if obj_match:
        return obj_match.group(1)
    for prefix in prefixes:
        norm = prefix if prefix.endswith('/') else prefix + '/'
        if path.startswith(norm):
            sub = path[len(norm):]
            orig_match = _SHARD_CONTENT_RE.fullmatch(sub)
            if orig_match and orig_match.group(1) == orig_match.group(2)[:2]:
                return orig_match.group(2)
    return None


def is_content_addressed_basis(item: dict, *, prefixes: tuple[str, ...] = ()) -> bool:
    """Return True when a captured basis pin matches its own content-addressed path digest."""
    if not isinstance(item, dict):
        return False
    expected = item.get('sha256')
    size = item.get('bytes')
    if not isinstance(expected, str) or type(size) is not int or size < 0:
        return False
    embedded = content_addressed_sha(item.get('path', ''), prefixes=prefixes)
    return embedded is not None and embedded == expected


class RecordStore:
    def __init__(self, exchange, record_format, envelope, *,
                 content_addressed_basis_prefixes: tuple[str, ...] = ()):
        self.ex = exchange
        self.work = exchange.workspace
        self.objects = exchange.objects
        self.receipts = HeadReceipts(exchange.root)
        self.index = HeadIndex(exchange.root, self.receipts)
        self.cold_archive = ColdArchive(exchange.root, record_format)
        self.format = record_format
        self.envelope = envelope
        self.payload_validator = None
        self.retention_policy = 'append-history'
        self.content_addressed_basis_prefixes = tuple(content_addressed_basis_prefixes)
        self._deferred_index_ids: list[str] | None = None
        self._deferred_index_entries: dict[str, dict] | None = None

    @contextmanager
    def batch_index_updates(self):
        """Defer incremental HeadIndex rewrites across a multi-record batch and flush once."""
        if self._deferred_index_ids is not None:
            yield
            return
        self._deferred_index_ids = []
        self._deferred_index_entries = {}
        try:
            yield
        finally:
            pending_ids = list(dict.fromkeys(self._deferred_index_ids))
            prebuilt = dict(self._deferred_index_entries)
            self._deferred_index_ids = None
            self._deferred_index_entries = None
            missing_ids = [ident for ident in pending_ids if ident not in prebuilt]
            if prebuilt or missing_ids:
                self.refresh_index_entries(missing_ids, prebuilt=prebuilt)

    @staticmethod
    def goal_record(goal: dict) -> dict:
        if 'id' not in goal:
            goal = {'id': 'goal', 'kind': 'goal', 'content': goal}
        if goal.get('id') != 'goal' or goal.get('kind') != 'goal':
            raise ValueError('the initial record must have id=goal and kind=goal')
        if not isinstance(goal.get('content'), dict):
            raise ValueError('goal content must be an object')
        text(goal['content'].get('contribution'), 'goal contribution')
        return goal

    @staticmethod
    def relative_name(ident: str) -> str:
        return 'records/' + packet_id(ident) + '.json'

    def path(self, ident: str) -> Path:
        return self.ex.path(self.relative_name(ident))

    def material(self, ident: str) -> str:
        return self.path(ident).relative_to(self.work).as_posix()

    def size(self, ident: str) -> int:
        path = self.path(ident)
        if hasattr(os, 'O_NOFOLLOW'):
            try:
                fd = os.open(path, _OPEN_RO_NOFOLLOW)
            except FileNotFoundError:
                if self.cold_archive.has(ident):
                    return len(self.cold_archive.read(ident)[1])
                raise
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                    raise ValueError(f'ordinary JSON file required: {path}') from None
                raise
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f'ordinary JSON file required: {path}')
                return info.st_size
            finally:
                os.close(fd)
        try:
            info = path.lstat()
        except FileNotFoundError:
            if self.cold_archive.has(ident):
                return len(self.cold_archive.read(ident)[1])
            raise
        if not is_ordinary_file_stat(info):
            raise ValueError(f'ordinary JSON file required: {path}')
        return info.st_size

    def has_record(self, ident: str) -> bool:
        try:
            packet_id(ident)
        except ValueError:
            return False
        hot = self.path(ident)
        if hot.is_file():
            if os.name == 'nt' or (hasattr(os, 'uname') and os.uname().sysname == 'Darwin'):
                try:
                    if hot.resolve().name != f'{ident}.json':
                        return self.cold_archive.has(ident)
                except OSError:
                    return False
            return True
        return self.cold_archive.has(ident)

    def show(self, ident: str) -> dict:
        return self.read(ident)[0]

    def read(self, ident: str, *, byte_limit: int | None = None,
             dir_fd: int | None = None) -> tuple[dict, bytes]:
        if dir_fd is not None:
            filename = packet_id(ident) + '.json'
            record, raw = read_input(self.work / 'records' / filename, byte_limit=byte_limit,
                                     dir_fd=dir_fd, filename=filename)
        else:
            hot = self.path(ident)
            if not hot.exists() and not hot.is_symlink() and self.cold_archive.has(ident):
                record, raw = self.cold_archive.read(ident)
                if byte_limit is not None and len(raw) > byte_limit:
                    raise ValueError(f'input exceeds {byte_limit} bytes: {ident}')
                return record, raw
            record, raw = read_input(hot, byte_limit=byte_limit)
        self._verify_record(ident, record, raw)
        return record, raw

    def _verify_record(self, ident, record, raw: bytes | None = None):
        if record.get('id') != ident or record.get('format') != self.format:
            raise ValueError('record identity or format changed')
        if record.get('sha256') != sha(encode({k: v for k, v in record.items() if k != 'sha256'})):
            raise ValueError('record digest changed')

    def captured_record(self, ident, basis):
        """Open the exact local record object named by an already qualified native basis."""
        require(basis['path'] == self.relative_name(ident), 'basis belongs to a different source path')
        raw = self.objects.read(basis['sha256'], retained=True, expected_bytes=basis['bytes'])
        record = strict_json(raw)
        self._verify_record(ident, record, raw)
        identity(record_identity(record))
        return record

    def _read_scoped_one(self, ident, needs_body, indexed, records_fd):
        filename = packet_id(ident) + '.json'
        full_path = self.work / 'records' / filename
        if indexed is not None and ident in indexed:
            entry = indexed[ident]
            if not needs_body(ident, entry['kind']):
                if hasattr(os, 'O_NOFOLLOW') and records_fd is not None:
                    try:
                        fd = os.open(filename, _OPEN_RO_NOFOLLOW, dir_fd=records_fd)
                    except OSError as exc:
                        if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                            raise ValueError(f'ordinary JSON file required: {full_path}') from None
                        raise
                    try:
                        if not stat.S_ISREG(os.fstat(fd).st_mode):
                            raise ValueError(f'ordinary JSON file required: {full_path}')
                        stream = os.fdopen(fd, 'rb')
                    except BaseException:
                        os.close(fd)
                        raise
                    with stream:
                        raw = stream.read()
                else:
                    try:
                        info = full_path.lstat()
                    except OSError:
                        raise ValueError(f'ordinary JSON file required: {full_path}') from None
                    if not is_ordinary_file_stat(info):
                        raise ValueError(f'ordinary JSON file required: {full_path}')
                    raw = full_path.read_bytes()
                if hmac.compare_digest(sha(raw), entry['raw_sha256']):
                    return {k: entry[k] for k in ('format', 'id', 'kind', 'sha256', 'revision', 'basis', 'active')}
                record = parse_input_bytes(raw, full_path)
                self._verify_record(ident, record, raw)
                return record
        if hasattr(os, 'O_NOFOLLOW') and records_fd is not None:
            try:
                fd = os.open(filename, _OPEN_RO_NOFOLLOW, dir_fd=records_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                    raise ValueError(f'ordinary JSON file required: {full_path}') from None
                raise
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f'ordinary JSON file required: {full_path}')
                stream = os.fdopen(fd, 'rb')
            except BaseException:
                os.close(fd)
                raise
            with stream:
                if info.st_size >= MIN_BYTES and not self.receipts._xattr_unsupported:
                    projection = self.receipts.projection(full_path, ident, self.format, size=info.st_size)
                    if projection is not None:
                        summary, raw_digest = projection
                        if not needs_body(ident, summary['kind']):
                            raw = stream.read()
                            if hmac.compare_digest(sha(raw), raw_digest):
                                return summary
                            record = parse_input_bytes(raw, full_path)
                            self._verify_record(ident, record, raw)
                            return record
                raw = stream.read()
            record = parse_input_bytes(raw, full_path)
            self._verify_record(ident, record, raw)
            return record
        path = self.path(ident)
        projection = self.receipts.projection(path, ident, self.format)
        if projection is not None:
            summary, raw_digest = projection
            if not needs_body(ident, summary['kind']) and self.receipts.matches(path, raw_digest):
                return summary
        return self.show(ident)

    @staticmethod
    def _chunk_map(items: list, fn, *, max_workers: int = 8, min_parallel: int = 16) -> list:
        if len(items) < min_parallel:
            return fn(items)
        workers = min(max_workers, len(items))
        step = (len(items) + workers - 1) // workers
        chunks = [items[i:i + step] for i in range(0, len(items), step)]
        if len(chunks) <= 1:
            return fn(items)
        out: list = []
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            for batch in pool.map(fn, chunks):
                out.extend(batch)
        return out

    def _validated_records_dir_win(self) -> tuple[Path, str]:
        records_dir = self.ex.path('records')
        try:
            dir_info = records_dir.lstat()
        except OSError:
            raise ValueError(f'ordinary records directory required: {records_dir}') from None
        if not is_ordinary_dir_stat(dir_info):
            raise ValueError(f'ordinary records directory required: {records_dir}')
        return records_dir, str(records_dir)

    def _read_verified_win(self, dir_str: str, ident: str, *, allow_cold: bool = True) -> tuple[dict, bytes, os.stat_result | None]:
        filename = packet_id(ident) + '.json'
        full_str = dir_str + os.sep + filename
        try:
            info = os.lstat(full_str)
        except FileNotFoundError:
            if allow_cold and self.cold_archive.has(ident):
                record, raw = self.cold_archive.read(ident)
                return record, raw, None
            raise ValueError(f'ordinary JSON file required: {full_str}') from None
        except OSError:
            raise ValueError(f'ordinary JSON file required: {full_str}') from None
        if not is_ordinary_file_stat(info):
            raise ValueError(f'ordinary JSON file required: {full_str}')
        with open(full_str, 'rb') as stream:
            raw = stream.read()
        record = parse_input_bytes(raw, Path(full_str))
        self._verify_record(ident, record, raw)
        return record, raw, info

    def scoped_heads(self, needs_body, *, max_workers: int = 16):
        """Yield cryptographically verified scope/status fields and full records for nominated roles."""
        idents = self.ids()
        if not idents:
            return
        indexed = self.index.load(self.format)
        if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
            with self.ex.open_workspace_dir('records') as records_fd:
                results = self._chunk_map(
                    idents,
                    lambda chunk: [self._read_scoped_one(i, needs_body, indexed, records_fd) for i in chunk],
                    max_workers=min(max_workers, 8),
                )
            yield from results
            return
        _, dir_str = self._validated_records_dir_win()

        def scoped_win_chunk(chunk):
            out = []
            for ident in chunk:
                entry = indexed.get(ident) if indexed is not None else None
                if entry is not None and not needs_body(ident, entry['kind']):
                    full_str = dir_str + os.sep + ident + '.json'
                    try:
                        info = os.lstat(full_str)
                    except OSError:
                        raise ValueError(f'ordinary JSON file required: {full_str}') from None
                    if not is_ordinary_file_stat(info):
                        raise ValueError(f'ordinary JSON file required: {full_str}')
                    with open(full_str, 'rb') as stream:
                        raw = stream.read()
                    if hmac.compare_digest(sha(raw), entry['raw_sha256']):
                        out.append({k: entry[k] for k in ('format', 'id', 'kind', 'sha256', 'revision', 'basis', 'active')})
                        continue
                    record = parse_input_bytes(raw, Path(full_str))
                    self._verify_record(ident, record, raw)
                    out.append(record)
                    continue
                record, _, _ = self._read_verified_win(dir_str, ident, allow_cold=False)
                out.append(record)
            return out

        yield from self._chunk_map(idents, scoped_win_chunk, max_workers=min(max_workers, 8))

    def indexed_summaries_with_format(self, *, max_workers: int = 16) -> tuple[dict[str, dict], str] | None:
        """Return (verified_entries, index_format) or None if unindexed (works on POSIX and Windows)."""
        loaded = self.index.load_with_format(self.format)
        if loaded is None:
            return None
        indexed, index_format = loaded
        idents = self.ids()
        if not idents:
            return {}, index_format

        if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
            with self.ex.open_workspace_dir('records') as records_fd:
                def check_chunk_posix(chunk):
                    out = []
                    for ident in chunk:
                        entry = indexed.get(ident)
                        filename = (ident if entry is not None else packet_id(ident)) + '.json'
                        full_path = self.work / 'records' / filename
                        try:
                            info = os.stat(filename, dir_fd=records_fd, follow_symlinks=False)
                        except OSError as exc:
                            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR):
                                raise ValueError(f'ordinary JSON file required: {full_path}') from None
                            raise
                        if not stat.S_ISREG(info.st_mode):
                            raise ValueError(f'ordinary JSON file required: {full_path}')
                        if (entry is not None
                                and entry.get('format') == self.format
                                and entry.get('id') == ident
                                and entry.get('size') == info.st_size
                                and entry.get('mtime_ns') == info.st_mtime_ns):
                            if entry.get('ino') == info.st_ino and entry.get('platform', os.name) == os.name:
                                out.append((ident, entry))
                                continue
                            # Cross-OS st_ino mismatch (e.g. Windows <-> WSL drvfs): verify raw SHA-256
                            # against the HMAC-authenticated index entry to skip JSON parse & re-encoding.
                            try:
                                fd = os.open(filename, _OPEN_RO_NOFOLLOW, dir_fd=records_fd)
                            except OSError as exc:
                                if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                                    raise ValueError(f'ordinary JSON file required: {full_path}') from None
                                raise
                            try:
                                if not stat.S_ISREG(os.fstat(fd).st_mode):
                                    raise ValueError(f'ordinary JSON file required: {full_path}')
                                stream = os.fdopen(fd, 'rb')
                            except BaseException:
                                os.close(fd)
                                raise
                            with stream:
                                raw = stream.read()
                            if hmac.compare_digest(sha(raw), entry['raw_sha256']):
                                out.append((ident, entry))
                                continue
                            record = parse_input_bytes(raw, full_path)
                            self._verify_record(ident, record, raw)
                            out.append((ident, build_entry(record, raw, info)))
                            continue
                        record, raw = self.read(ident, dir_fd=records_fd)
                        out.append((ident, build_entry(record, raw, info)))
                    return out

                pairs = self._chunk_map(idents, check_chunk_posix, max_workers=min(max_workers, 8))
            return dict(pairs), index_format

        # Native Windows: verify records/ directory once, then stat+match via str paths in 8 chunked workers.
        _, dir_str = self._validated_records_dir_win()

        def check_chunk_win(chunk):
            out = []
            for ident in chunk:
                entry = indexed.get(ident)
                filename = (ident if entry is not None else packet_id(ident)) + '.json'
                full_str = dir_str + os.sep + filename
                try:
                    info = os.lstat(full_str)
                except OSError:
                    raise ValueError(f'ordinary JSON file required: {full_str}') from None
                if not is_ordinary_file_stat(info):
                    raise ValueError(f'ordinary JSON file required: {full_str}')
                if (entry is not None
                        and entry.get('format') == self.format
                        and entry.get('id') == ident
                        and entry.get('size') == info.st_size
                        and entry.get('mtime_ns') == info.st_mtime_ns):
                    if entry.get('ino') == info.st_ino and entry.get('platform', os.name) == os.name:
                        out.append((ident, entry))
                        continue
                    with open(full_str, 'rb') as stream:
                        raw = stream.read()
                    if hmac.compare_digest(sha(raw), entry['raw_sha256']):
                        out.append((ident, entry))
                        continue
                    record = parse_input_bytes(raw, Path(full_str))
                    self._verify_record(ident, record, raw)
                    out.append((ident, build_entry(record, raw, info)))
                    continue
                record, raw, cur_info = self._read_verified_win(dir_str, ident, allow_cold=False)
                out.append((ident, build_entry(record, raw, cur_info or info)))
            return out

        pairs = self._chunk_map(idents, check_chunk_win, max_workers=min(max_workers, 8))
        return dict(pairs), index_format

    def indexed_summaries(self, *, max_workers: int = 16) -> dict[str, dict] | None:
        """Return HMAC + (size, mtime_ns, ino) verified head summaries, or None if unindexed.

        Any file absent from the index or whose filesystem metadata changed is
        verified through RecordStore.read() immediately. Never creates keys or writes files.
        """
        loaded = self.indexed_summaries_with_format(max_workers=max_workers)
        return None if loaded is None else loaded[0]

    def scan_records(self, idents=None, *, max_workers: int = 16) -> list[dict]:
        """Read and cryptographically verify nominated records (or all records) in parallel."""
        target_ids = self.ids() if idents is None else list(idents)
        if not target_ids:
            return []
        if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
            with self.ex.open_workspace_dir('records') as records_fd:
                return self._chunk_map(
                    target_ids,
                    lambda chunk: [self.read(ident, dir_fd=records_fd)[0] for ident in chunk],
                    max_workers=min(max_workers, 8),
                )
        _, dir_str = self._validated_records_dir_win()
        return self._chunk_map(
            target_ids,
            lambda chunk: [self._read_verified_win(dir_str, ident, allow_cold=True)[0] for ident in chunk],
            max_workers=min(max_workers, 8),
        )

    def reindex(self, *, max_workers: int = 16) -> dict:
        """Rebuild the HMAC-verified store-local head index under an exclusive owner lock."""
        idents = self.ids()
        entries: dict[str, dict] = {}
        if idents:
            if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
                with self.ex.open_workspace_dir('records') as records_fd:
                    def index_chunk_posix(chunk):
                        out = []
                        for ident in chunk:
                            filename = packet_id(ident) + '.json'
                            record, raw = self.read(ident, dir_fd=records_fd)
                            info = os.stat(filename, dir_fd=records_fd, follow_symlinks=False)
                            out.append((ident, build_entry(record, raw, info)))
                        return out

                    pairs = self._chunk_map(idents, index_chunk_posix, max_workers=min(max_workers, 8))
                    entries = dict(pairs)
            else:
                _, dir_str = self._validated_records_dir_win()

                def index_chunk_win(chunk):
                    out = []
                    for ident in chunk:
                        record, raw, info = self._read_verified_win(dir_str, ident, allow_cold=False)
                        out.append((ident, build_entry(record, raw, info)))
                    return out

                pairs = self._chunk_map(idents, index_chunk_win, max_workers=min(max_workers, 8))
                entries = dict(pairs)
        return self.index.save(self.format, entries, create_key=True)

    def refresh_index_entries(self, idents: list[str], *, prebuilt: dict[str, dict] | None = None) -> bool:
        """Batch-update existing HMAC head index entries for freshly committed records."""
        if (not idents and not prebuilt) or not self.index.path.exists():
            return False
        updates: dict[str, dict] = dict(prebuilt) if prebuilt else {}
        if idents:
            if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
                with self.ex.open_workspace_dir('records') as records_fd:
                    for ident in idents:
                        filename = packet_id(ident) + '.json'
                        record, raw = self.read(ident, dir_fd=records_fd)
                        info = os.stat(filename, dir_fd=records_fd, follow_symlinks=False)
                        updates[ident] = build_entry(record, raw, info)
            else:
                _, dir_str = self._validated_records_dir_win()
                for ident in idents:
                    record, raw, info = self._read_verified_win(dir_str, ident, allow_cold=False)
                    updates[ident] = build_entry(record, raw, info)
        return self.index.update_if_present(self.format, updates)

    def index_scope(self, ident):
        path = self.path(ident)
        if self.size(ident) < MIN_BYTES:
            return {'id': ident, 'indexed': False, 'reason': 'below size threshold'}
        record, raw = self.read(ident)
        return {'id': ident, 'revision': record['revision'],
                'indexed': self.receipts.issue(path, record, raw)}

    def all(self) -> list[dict]:
        return self.scan_records()

    def ids(self):
        records_dir = self.work / 'records'
        try:
            with os.scandir(records_dir) as entries:
                names = sorted(entry.name for entry in entries if entry.name.endswith('.json'))
        except FileNotFoundError:
            return []
        return [name[:-5] for name in names]

    def _obligations(self, content: dict, previous: dict | None):
        rows = content.get('obligations', [])
        if not isinstance(rows, list):
            raise ValueError('goal obligations must be a list of {id, text, disposition?} objects')
        merged = {r['id']: r for r in (previous or {}).get('content', {}).get('obligations', [])}
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('each goal obligation must be an object')
            ident = packet_id(row.get('id', ''))
            if ident in seen:
                raise ValueError('duplicate goal obligation')
            seen.add(ident)
            text(row.get('text'), 'obligation text')
            disposition = text(row.get('disposition', 'open'), 'obligation disposition')
            if disposition in {'fulfilled', 'released'}:
                text(row.get('account'), 'closed obligation account')
            merged[ident] = dict(row, disposition=disposition)
        content['obligations'] = list(merged.values())

    def _normalize_record(self, record: dict, previous: dict | None, *, retain_basis=True,
                          preserve_basis: list | None = None,
                          verify_preserved_objects: bool = True) -> dict:
        json.dumps(record, allow_nan=False)
        fields(record, {'id', 'kind', 'content', 'relations', 'basis', 'active'}, 'record')
        ident, kind = packet_id(record.get('id', '')), text(record.get('kind'), 'record kind')
        if 'content' not in record:
            raise ValueError('record content is required')
        active = record.get('active', True)
        if type(active) is not bool:
            raise ValueError('active must be Boolean navigation metadata')
        relations = record.get('relations', [])
        if not isinstance(relations, list):
            raise ValueError('relations must be a list of objects')
        for relation in relations:
            if not isinstance(relation, dict):
                raise ValueError('relation must be an object')
            text(relation.get('relation'), 'relation label')
            text(relation.get('target'), 'relation target')
        content = record['content']
        if kind == 'obligation' and isinstance(content, dict) and content.get('disposition') in ('fulfilled', 'released'):
            text(content.get('account'), 'closed obligation account')
        prior_owed = previous and previous['kind'] == 'obligation' and not accounted_closure(previous['content'])
        owed = kind == 'obligation' and not accounted_closure(content)
        if prior_owed and kind != 'obligation':
            raise ValueError('account for the owed obligation before reclassification')
        if ident == 'goal':
            self.goal_record(record)
            if not active:
                raise ValueError('the current goal cannot be hidden by navigation metadata')
            content = dict(content)
            self._obligations(content, previous)
        if preserve_basis is not None:
            if verify_preserved_objects:
                require(all(row['sha256'] is None or self.objects.has(row['sha256'])
                            for row in preserve_basis), 'preserved basis object is unavailable')
            basis = preserve_basis
        elif prior_owed and owed and 'basis' not in record:
            basis = previous['basis']
        else:
            basis = [self._capture_basis_item(item, retain_basis=retain_basis)
                     for item in strings(record.get('basis', []), 'record basis')]
        if prior_owed and owed:
            current = {r['path']: r['sha256'] for r in basis}
            if any(r['path'] not in current or current[r['path']] != r['sha256'] for r in previous['basis']):
                change = content.get('basis_change') if isinstance(content, dict) else None
                if (not isinstance(change, dict) or type(change.get('from_revision')) is not int or
                        change['from_revision'] != previous['revision']):
                    raise ValueError('changed obligation basis requires an account for the previous revision')
                text(change.get('account'), 'account for changed obligation basis')
        return {'id': ident, 'kind': kind, 'content': content, 'relations': relations,
                'basis': basis, 'active': active}

    def _capture_basis_item(self, source: str, *, retain_basis: bool) -> dict:
        if source.startswith('object:'):
            key = digest(source[7:], 'object basis sha256')
            raw = self.objects.read(key, retained=True)
            return {'path': source, 'sha256': key, 'bytes': len(raw)}
        rel = relative_path(source)
        path = self.ex.path(rel)
        if not path.exists():
            embedded = content_addressed_sha(rel, prefixes=self.content_addressed_basis_prefixes)
            if embedded is not None and self.objects.has(embedded):
                raw = self.objects.read(embedded, retained=True)
                return {'path': rel, 'sha256': embedded, 'bytes': len(raw)}
        return self.ex.snapshot(rel, optional=True) if retain_basis else self._preview_basis(rel)

    def _preview_basis(self, source: str) -> dict:
        path = self.ex.path(source)
        if not path.exists():
            return {'path': source, 'sha256': None, 'bytes': 0}
        raw = read_bytes(path)
        key = sha(raw)
        if self.objects.has(key) and self.objects.read(key, retained=True, expected_bytes=len(raw)) != raw:
            raise ValueError('content object changed')
        return {'path': source, 'sha256': key, 'bytes': len(raw)}

    def validate_import(self, record: dict) -> None:
        """Qualify a new write or a matching current head without retaining evidence."""
        path = self.path(record['id'])
        if not path.exists():
            require(not self.cold_archive.has(record['id']),
                    'record exists in cold archive; restore explicitly before importing: ' + record['id'])
            self._prepare_write(record, None, None, retain_basis=False)
            return
        previous = self.show(record['id'])
        self._validate_payload(record, previous)
        normalized = self._normalize_record(record, previous, retain_basis=False)
        if encode(normalized) != encode({key: previous[key] for key in normalized}):
            raise ValueError('current import conflicts with existing record: ' + record['id'])

    def put(self, record: dict, expected: int) -> dict:
        return self._replace(record, expected)

    def put_reviewed(self, record: dict, expected: int, basis: list, *, verify=None) -> dict:
        """Admit only the composition body and basis actually prepared for this write."""
        return self._replace(record, expected, reviewed_basis=basis, verify=verify)

    def put_feedback(self, record: dict, expected: int) -> dict:
        return self._replace(record, expected, feedback_write=True)

    def put_information(self, record: dict, expected: int, mode: str) -> dict:
        return self._replace(record, expected, information_mode=mode)

    def _replace(self, record: dict, expected: int, *, information_mode: str | None = None,
                 feedback_write: bool = False, observation_write: bool = False, reviewed_basis: list | None = None,
                 preserve_basis: list | None = None, verify=None, _defer_index_update: bool = False,
                 _preloaded: tuple[dict | None, bytes | None] | None = None,
                 _prepared: tuple[dict, bytes] | None = None) -> dict:
        integer(expected, 'expected revision')
        path = self.path(record.get('id', ''))
        if _preloaded is not None:
            previous, previous_raw = _preloaded
        else:
            if not path.exists() and self.cold_archive.has(record.get('id', '')):
                raise ValueError(f"record {record['id']} is in cold archive; restore explicitly before modifying")
            previous, previous_raw = self.read(record['id']) if path.exists() else (None, None)
        revision = previous['revision'] if previous else 0
        if revision != expected:
            raise ValueError(f'record revision conflict: expected {expected}, current {revision}')
        if _prepared is not None:
            body, raw = _prepared
        else:
            body, raw = self._prepare_write(record, previous, previous_raw, information_mode=information_mode,
                                            feedback_write=feedback_write, observation_write=observation_write,
                                            retain_basis=reviewed_basis is None, preserve_basis=preserve_basis)
        if reviewed_basis is not None:
            require(body['basis'] == reviewed_basis, 'captured workspace basis changed at publication')
            # Reuse the incumbent's already captured objects. A rejected write
            # must not retain newly encountered workspace material.
            require(all(row['sha256'] is None or self.objects.has(row['sha256'])
                        for row in reviewed_basis), 'reviewed basis object is unavailable')
            require(encode(body['content']) == encode(record['content']), 'normalized composition differs from reviewed body')
        if verify is not None:
            verify()
        old = body['previous_object']
        if old is not None:
            if len(previous_raw) <= MAX_BYTES:
                self.objects.put(previous_raw)
            else:
                # Preserve a verified legacy head larger than today's transport limit.
                self.objects.put_retained(previous_raw, max_bytes=len(previous_raw))
        atomic_bytes(path, raw)
        self.receipts.issue(path, body, raw)
        if not _defer_index_update:
            if self._deferred_index_ids is not None:
                self._deferred_index_ids.append(body['id'])
                if self._deferred_index_entries is not None and self.index.path.exists():
                    self._deferred_index_entries[body['id']] = build_entry(body, raw, path.lstat())
            elif self.index.path.exists():
                self.index.update_if_present(self.format, {body['id']: build_entry(body, raw, path.lstat())})
        return {'id': body['id'], 'revision': body['revision'], 'sha256': body['sha256'],
                'path': str(path), 'previous_object': old}

    def _validate_payload(self, record, previous, *, information_mode=None):
        self.envelope.validate_write(record, previous, mode=information_mode)
        content = record.get('content')
        if isinstance(content, dict) and content.get('format') == COMPOSITION_FORMAT:
            from store.composition.composition import Composition
            Composition(None).profile(record)
        if self.payload_validator is not None:
            self.payload_validator(record, previous)

    def _prepare_write(self, record, previous, previous_raw, *, information_mode=None,
                       feedback_write=False, observation_write=False, retain_basis=True,
                       preserve_basis: list | None = None, verify_preserved_objects: bool = True):
        if previous is None and self.cold_archive.has(record.get('id', '')):
            raise ValueError(f"record {record['id']} is in cold archive; restore explicitly before modifying")
        if (record.get('kind') == FEEDBACK_KIND or (previous and previous['kind'] == FEEDBACK_KIND)) and not feedback_write:
            raise ValueError('feedback records must be changed through feedback operations')
        if (record.get('kind') == 'source-observation' or (previous and previous['kind'] == 'source-observation')) and not observation_write:
            raise ValueError('cross-instance observations must be received through transfer operations')
        self._validate_payload(record, previous, information_mode=information_mode)
        normalized = self._normalize_record(record, previous, retain_basis=retain_basis,
                                            preserve_basis=preserve_basis,
                                            verify_preserved_objects=verify_preserved_objects)
        revision = previous['revision'] if previous else 0
        if normalized['kind'] == FEEDBACK_KIND and self.retention_policy == 'current-use':
            if any(row.get('route') not in {'work', 'hold'} for row in normalized['content'].get('carried_returns', [])):
                raise ValueError('resolve legacy carried feedback dispositions before ending predecessor retention')
        retain = self.retention_policy == 'append-history'
        old = sha(previous_raw) if previous_raw is not None and retain else None
        body = {'format': self.format, **normalized, 'revision': revision + 1,
                'previous_object': old, 'updated_at': now(),
                'retention': {'policy': self.retention_policy,
                              'retained_from_revision': (previous.get('retention', {}).get('retained_from_revision', 1)
                                                         if previous and retain else revision + 1)}}
        body['sha256'] = sha(encode(body))
        raw = encode(body)
        if len(raw) > MAX_BYTES:
            raise ValueError(f'complete encoded record exceeds {MAX_BYTES} bytes; reduce the record or nominate external evidence')
        return body, raw

    def basis_changes(self, record: dict, *, resolved_hashes: dict[str, str | None] | None = None,
                      verified_objects: set[tuple[str, int]] | None = None) -> list[dict]:
        basis = record['basis']
        if not basis:
            return []
        if resolved_hashes is not None:
            hashes = resolved_hashes
        else:
            workspace_paths = [item['path'] for item in basis if not item['path'].startswith('object:')]
            hashes = self.ex.hash_workspace_paths(workspace_paths) if workspace_paths else {}
        changes = []
        for item in basis:
            path = item['path']
            current = None if path.startswith('object:') else hashes.get(path)
            if (current is None
                    and is_content_addressed_basis(item, prefixes=self.content_addressed_basis_prefixes)
                    and self.objects.has(item['sha256'])):
                obj_key = (item['sha256'], item['bytes'])
                if verified_objects is not None and obj_key in verified_objects:
                    current = item['sha256']
                else:
                    try:
                        self.objects.read(item['sha256'], retained=True, expected_bytes=item['bytes'])
                    except FileNotFoundError:
                        current = None
                    else:
                        current = item['sha256']
                        if verified_objects is not None:
                            verified_objects.add(obj_key)
            if current != item['sha256']:
                changes.append({'path': path, 'captured': item['sha256'], 'current': current})
        return changes

    def head(self, wanted: dict) -> dict:
        identity(wanted)
        current = self.show(wanted['id'])
        require(record_identity(current) == wanted, 'information source or contract revision conflict')
        return current

    def pin(self, record: dict) -> dict:
        parsed, data = self.read(record['id'])
        require(parsed == record, 'information record changed during pinning')
        if len(data) <= MAX_BYTES:
            obj = self.objects.put(data)
        else:
            obj = self.objects.put_retained(data, max_bytes=len(data))
        return dict(record_identity(record), object=obj)

    def _historical_object(self, ident, revision, key):
        # Older releases could retain larger heads than today's transport maximum.
        digest(key, 'retained record object')
        record = strict_json(self.objects.read(key, retained=True))
        require(record.get('format') == self.format and record.get('id') == ident and
                type(record.get('revision')) is int and record['revision'] == revision,
                'broken record lineage')
        require(record.get('sha256') == sha(encode({k: v for k, v in record.items() if k != 'sha256'})),
                'retained record digest changed')
        return record

    def history_pages(self, ident, *, limit=8, cursor=None):
        """Yield descending verified pages. Reauthenticate the prefix once on external restart."""
        require(type(limit) is int and 1 <= limit <= 32, 'history page limit must be 1..32')
        current, raw = self.read(ident)
        head = dict(record_identity(current), object=sha(raw))
        identity(head, object_pin=True)
        if cursor is not None:
            exact_fields(cursor, {'head', 'next_revision', 'next_object'}, set(), 'history cursor')
            captured = identity(cursor['head'], object_pin=True)
            require(captured['id'] == ident, 'history cursor record changed')
            target = cursor['next_revision']
            require(type(target) is int and 1 <= target < captured['revision'], 'invalid cursor revision')
            digest(cursor['next_object'], 'cursor next object')
            if head != captured:
                current = self._historical_object(ident, captured['revision'], captured['object'])
                require(record_identity(current) == record_identity(captured),
                        'history cursor head changed')
            head = captured
            while current['revision'] > target:
                key = current['previous_object']
                if current['revision'] == target + 1:
                    require(key == cursor['next_object'], 'history cursor is off the captured chain')
                current = self._historical_object(ident, current['revision'] - 1, key)
        page = []
        while True:
            revision = current['revision']
            key = current['previous_object']
            boundary = key is None and revision > 1 and current.get('retention', {}).get('policy') == 'current-use'
            if revision == 1:
                require(key is None, 'unexpected predecessor on first revision')
            elif not boundary:
                digest(key, 'record predecessor')
            page.append(current)
            if len(page) == limit or revision == 1 or boundary:
                continuation = None if revision == 1 or boundary else dict(head=head, next_revision=revision - 1, next_object=key)
                yield dict(head=head, records=page, cursor=continuation, complete=revision == 1,
                           retention_boundary=({'policy': 'current-use', 'retained_from_revision': revision,
                                                'scope': 'Earlier ordinary revisions were not retained by this chain; separately selected evidence pins remain independent.'}
                                               if boundary else None))
                if revision == 1 or boundary:
                    return
                page = []
            current = self._historical_object(ident, revision - 1, key)
