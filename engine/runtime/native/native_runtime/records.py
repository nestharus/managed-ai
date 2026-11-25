"""Native record heads, revision checks, basis and retained history."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import errno
import hmac
import json
import os
from pathlib import Path
import stat

from continuation_support.ownership import packet_id, relative_path
from continuation_support.storage import MAX_BYTES, _OPEN_RO_NOFOLLOW, atomic_bytes, encode, now, read_bytes, sha
from feedback import KIND as FEEDBACK_KIND
from native_runtime.head_index import HeadIndex, build_entry
from native_runtime.head_receipts import HeadReceipts, MIN_BYTES
from native_runtime.identities import digest, exact_fields, identity, record_identity, require, strict_json
from native_runtime.inputs import accounted_closure, fields, integer, parse_input_bytes, read_input, strings, text


class RecordStore:
    def __init__(self, exchange, record_format, envelope):
        self.ex = exchange
        self.work = exchange.workspace
        self.objects = exchange.objects
        self.receipts = HeadReceipts(exchange.root)
        self.index = HeadIndex(exchange.root, self.receipts)
        self.format = record_format
        self.envelope = envelope
        self.payload_validator = None
        self.retention_policy = 'append-history'
        self._deferred_index_ids: list[str] | None = None

    @contextmanager
    def batch_index_updates(self):
        """Defer incremental HeadIndex rewrites across a multi-record batch and flush once."""
        if self._deferred_index_ids is not None:
            yield
            return
        self._deferred_index_ids = []
        try:
            yield
        finally:
            pending = list(dict.fromkeys(self._deferred_index_ids))
            self._deferred_index_ids = None
            if pending:
                self.refresh_index_entries(pending)

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
        return self.path(ident).stat().st_size

    def show(self, ident: str) -> dict:
        return self.read(ident)[0]

    def read(self, ident: str, *, byte_limit: int | None = None,
             dir_fd: int | None = None) -> tuple[dict, bytes]:
        if dir_fd is not None:
            filename = packet_id(ident) + '.json'
            record, raw = read_input(self.work / 'records' / filename, byte_limit=byte_limit,
                                     dir_fd=dir_fd, filename=filename)
        else:
            record, raw = read_input(self.path(ident), byte_limit=byte_limit)
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
                    raw = self.path(ident).read_bytes()
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

    def scoped_heads(self, needs_body, *, max_workers: int = 16):
        """Yield cryptographically verified scope/status fields and full records for nominated roles."""
        idents = self.ids()
        if not idents:
            return
        indexed = self.index.load(self.format)
        if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
            with self.ex.open_workspace_dir('records') as records_fd:
                if len(idents) >= 16:
                    with ThreadPoolExecutor(max_workers=min(max_workers, len(idents))) as pool:
                        results = list(pool.map(
                            lambda ident: self._read_scoped_one(ident, needs_body, indexed, records_fd),
                            idents,
                        ))
                    yield from results
                else:
                    for ident in idents:
                        yield self._read_scoped_one(ident, needs_body, indexed, records_fd)
            return
        for ident in idents:
            yield self._read_scoped_one(ident, needs_body, indexed, None)

    def indexed_summaries(self, *, max_workers: int = 16) -> dict[str, dict] | None:
        """Return HMAC + (size, mtime_ns, ino) verified head summaries, or None if unindexed.

        Any file absent from the index or whose filesystem metadata changed is
        verified through RecordStore.read() immediately. Never creates keys or writes files.
        """
        indexed = self.index.load(self.format)
        if indexed is None:
            return None
        idents = self.ids()
        if not idents:
            return {}
        if not (hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY')):
            return None
        with self.ex.open_workspace_dir('records') as records_fd:
            def check_one(ident):
                filename = packet_id(ident) + '.json'
                full_path = self.work / 'records' / filename
                try:
                    info = os.stat(filename, dir_fd=records_fd, follow_symlinks=False)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR):
                        raise ValueError(f'ordinary JSON file required: {full_path}') from None
                    raise
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError(f'ordinary JSON file required: {full_path}')
                entry = indexed.get(ident)
                if (entry is not None
                        and entry.get('format') == self.format
                        and entry.get('id') == ident
                        and entry.get('size') == info.st_size
                        and entry.get('mtime_ns') == info.st_mtime_ns
                        and entry.get('ino') == info.st_ino):
                    return ident, entry
                record, raw = self.read(ident, dir_fd=records_fd)
                return ident, build_entry(record, raw, info)

            if len(idents) >= 16:
                with ThreadPoolExecutor(max_workers=min(max_workers, len(idents))) as pool:
                    pairs = list(pool.map(check_one, idents))
            else:
                pairs = [check_one(ident) for ident in idents]
        return dict(pairs)

    def scan_records(self, idents=None, *, max_workers: int = 16) -> list[dict]:
        """Read and cryptographically verify nominated records (or all records) via openat."""
        target_ids = self.ids() if idents is None else list(idents)
        if not target_ids:
            return []
        if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
            with self.ex.open_workspace_dir('records') as records_fd:
                if len(target_ids) >= 16:
                    with ThreadPoolExecutor(max_workers=min(max_workers, len(target_ids))) as pool:
                        return list(pool.map(lambda ident: self.read(ident, dir_fd=records_fd)[0], target_ids))
                return [self.read(ident, dir_fd=records_fd)[0] for ident in target_ids]
        return [self.show(ident) for ident in target_ids]

    def reindex(self, *, max_workers: int = 16) -> dict:
        """Rebuild the HMAC-verified store-local head index under an exclusive owner lock."""
        idents = self.ids()
        entries: dict[str, dict] = {}
        if idents:
            if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
                with self.ex.open_workspace_dir('records') as records_fd:
                    def index_one(ident):
                        filename = packet_id(ident) + '.json'
                        record, raw = self.read(ident, dir_fd=records_fd)
                        info = os.stat(filename, dir_fd=records_fd, follow_symlinks=False)
                        return ident, build_entry(record, raw, info)

                    if len(idents) >= 16:
                        with ThreadPoolExecutor(max_workers=min(max_workers, len(idents))) as pool:
                            pairs = list(pool.map(index_one, idents))
                    else:
                        pairs = [index_one(ident) for ident in idents]
                    entries = dict(pairs)
            else:
                for ident in idents:
                    record, raw = self.read(ident)
                    entries[ident] = build_entry(record, raw, self.path(ident).lstat())
        return self.index.save(self.format, entries, create_key=True)

    def refresh_index_entries(self, idents: list[str]) -> bool:
        """Batch-update existing HMAC head index entries for freshly committed records."""
        if not idents or not self.index.path.exists():
            return False
        updates: dict[str, dict] = {}
        if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
            with self.ex.open_workspace_dir('records') as records_fd:
                for ident in idents:
                    filename = packet_id(ident) + '.json'
                    record, raw = self.read(ident, dir_fd=records_fd)
                    info = os.stat(filename, dir_fd=records_fd, follow_symlinks=False)
                    updates[ident] = build_entry(record, raw, info)
        else:
            for ident in idents:
                record, raw = self.read(ident)
                updates[ident] = build_entry(record, raw, self.path(ident).lstat())
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
                          preserve_basis: list | None = None) -> dict:
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
            require(all(row['sha256'] is None or (self.objects.directory / row['sha256']).is_file()
                        for row in preserve_basis), 'preserved basis object is unavailable')
            basis = preserve_basis
        elif prior_owed and owed and 'basis' not in record:
            basis = previous['basis']
        else:
            paths = map(relative_path, strings(record.get('basis', []), 'record basis'))
            basis = [self.ex.snapshot(path, optional=True) if retain_basis else self._preview_basis(path)
                     for path in paths]
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

    def _preview_basis(self, source: str) -> dict:
        path = self.ex.path(source)
        if not path.exists():
            return {'path': source, 'sha256': None, 'bytes': 0}
        raw = read_bytes(path)
        digest = sha(raw)
        if (self.objects.directory / digest).exists() and self.objects.read(digest) != raw:
            raise ValueError('content object changed')
        return {'path': source, 'sha256': digest, 'bytes': len(raw)}

    def validate_import(self, record: dict) -> None:
        """Qualify a new write or a matching current head without retaining evidence."""
        path = self.path(record['id'])
        if not path.exists():
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
                 preserve_basis: list | None = None, verify=None, _defer_index_update: bool = False) -> dict:
        integer(expected, 'expected revision')
        path = self.path(record.get('id', ''))
        previous, previous_raw = self.read(record['id']) if path.exists() else (None, None)
        revision = previous['revision'] if previous else 0
        if revision != expected:
            raise ValueError(f'record revision conflict: expected {expected}, current {revision}')
        body, raw = self._prepare_write(record, previous, previous_raw, information_mode=information_mode,
                                        feedback_write=feedback_write, observation_write=observation_write,
                                        retain_basis=reviewed_basis is None, preserve_basis=preserve_basis)
        if reviewed_basis is not None:
            require(body['basis'] == reviewed_basis, 'captured workspace basis changed at publication')
            # Reuse the incumbent's already captured objects. A rejected write
            # must not retain newly encountered workspace material.
            require(all(row['sha256'] is None or (self.objects.directory / row['sha256']).is_file()
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
            elif self.index.path.exists():
                self.index.update_if_present(self.format, {body['id']: build_entry(body, raw, path.lstat())})
        return {'id': body['id'], 'revision': body['revision'], 'sha256': body['sha256'],
                'path': str(path), 'previous_object': old}

    def _validate_payload(self, record, previous, *, information_mode=None):
        self.envelope.validate_write(record, previous, mode=information_mode)
        if self.payload_validator is not None:
            self.payload_validator(record, previous)

    def _prepare_write(self, record, previous, previous_raw, *, information_mode=None,
                       feedback_write=False, observation_write=False, retain_basis=True,
                       preserve_basis: list | None = None):
        if (record.get('kind') == FEEDBACK_KIND or (previous and previous['kind'] == FEEDBACK_KIND)) and not feedback_write:
            raise ValueError('feedback records must be changed through feedback operations')
        if (record.get('kind') == 'source-observation' or (previous and previous['kind'] == 'source-observation')) and not observation_write:
            raise ValueError('cross-instance observations must be received through transfer operations')
        self._validate_payload(record, previous, information_mode=information_mode)
        normalized = self._normalize_record(record, previous, retain_basis=retain_basis,
                                            preserve_basis=preserve_basis)
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

    def basis_changes(self, record: dict, *, resolved_hashes: dict[str, str | None] | None = None) -> list[dict]:
        basis = record['basis']
        if not basis:
            return []
        hashes = resolved_hashes if resolved_hashes is not None else self.ex.hash_workspace_paths([item['path'] for item in basis])
        changes = []
        for item in basis:
            current = hashes[item['path']]
            if current != item['sha256']:
                changes.append({'path': item['path'], 'captured': item['sha256'], 'current': current})
        return changes

    def head(self, wanted: dict) -> dict:
        identity(wanted)
        current = self.show(wanted['id'])
        require(record_identity(current) == wanted, 'information source or contract revision conflict')
        return current

    def pin(self, record: dict) -> dict:
        data = self.path(record['id']).read_bytes()
        parsed = strict_json(data)
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
