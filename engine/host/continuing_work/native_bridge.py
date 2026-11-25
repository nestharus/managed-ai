"""Native subprocess bridge, durable event/object custody, and candidate capture reconciliation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile

from occurrence_files import (
    atomic_bytes,
    digest,
    file_lock,
    ident,
    loads_strict,
    nonempty,
    now,
    read,
    sync_dir,
    write,
)

HERE = Path(__file__).resolve().parent

MAX_EVENT_STREAM_BYTES = 16 * 1024
MAX_EVENT_LINE_BYTES = 64 * 1024

SAFE_CAPTURE_ERRORS = frozenset({
    'disposition must be considered or deferred',
    'each assessment must name a distinct actually delivered item',
    'receipt identity already used',
    'receipt identity not recorded in native Attention',
    'receipt must contain id and assessments',
    'invalid assessment fields',
    'nonempty bounded assessments required',
    'Native return already exists; use its retained body or a new current occurrence',
    'Selected native engine does not match the reviewed capture adapter; requalify this adapter before capture',
})

_PRECOMMIT_CLEANUP_ERRORS = frozenset({
    'disposition must be considered or deferred',
    'each assessment must name a distinct actually delivered item',
    'receipt must contain id and assessments',
    'invalid assessment fields',
    'nonempty bounded assessments required',
    'Native return already exists; use its retained body or a new current occurrence',
    'Selected native engine does not match the reviewed capture adapter; requalify this adapter before capture',
})


def _safe_jsonl(row) -> bytes:
    encoded = json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    # Escape Unicode line separators that str.splitlines() splits on.
    encoded = (encoded.replace('\u2028', '\\u2028')
                      .replace('\u2029', '\\u2029')
                      .replace('\u0085', '\\u0085'))
    if '\n' in encoded or '\r' in encoded:
        raise ValueError('event line framing contains unescaped newline')
    return (encoded + '\n').encode('utf-8')


def capture_error(raw):
    """Preserve known selected-capture errors, not arbitrary owner history."""
    try:
        parsed = loads_strict(raw)
        detail = parsed.get('error') if isinstance(parsed, dict) else None
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str) and detail in SAFE_CAPTURE_ERRORS:
        return detail
    return ('Candidate capture could not be confirmed; raw failure details remain in owner evidence. '
            'Do not blindly repeat the receipt.')


def _temp_receipt_file(directory: Path, body: bytes) -> Path:
    """Write a closed temporary JSON file so child processes can read it on all platforms."""
    directory.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=directory, suffix='.json', delete=False) as selected:
        selected.write(body)
        selected.flush()
        return Path(selected.name)


def resolve_sibling_cli(engine_entry: Path, canonical_rel: str, legacy_name: str) -> Path:
    """Resolve an engine CLI script relative to store/manage_store.py (or historical runtime/manage_store.py)."""
    entry = Path(engine_entry).resolve()
    if entry.parent.name == 'store':
        candidate = entry.parents[1] / canonical_rel
        if candidate.is_file():
            return candidate
    return entry.with_name(legacy_name)


class NativeBridge:
    """Composed collaborator for durable object/event custody, native subprocess calls, and capture reconciliation."""

    def __init__(self, host):
        self._host = host

    def _put_object_bytes(self, raw: bytes) -> tuple[str, str]:
        sha = digest(raw)
        obj = self._host.root / 'objects' / sha
        if obj.is_symlink() or obj.exists():
            if obj.is_symlink() or not obj.is_file():
                raise ValueError(f'Retained object must be an ordinary file: {obj}')
            existing = obj.read_bytes()
            if existing != raw or digest(existing) != sha:
                raise ValueError(f'Retained object integrity mismatch for {sha}')
        else:
            atomic_bytes(obj, raw)
        return obj.relative_to(self._host.root).as_posix(), sha

    def _bound_event_value(self, value: dict) -> dict:
        bounded = dict(value)
        for field in ('stdout', 'stderr'):
            text = bounded.get(field)
            if isinstance(text, str):
                raw = text.encode('utf-8', errors='replace')
                if len(raw) > MAX_EVENT_STREAM_BYTES:
                    obj_rel, sha = self._put_object_bytes(raw)
                    bounded[field] = raw[:MAX_EVENT_STREAM_BYTES].decode('utf-8', errors='replace')
                    bounded[field + '_truncated'] = True
                    bounded[field + '_bytes'] = len(raw)
                    bounded[field + '_sha256'] = sha
                    bounded[field + '_object'] = obj_rel
        return bounded

    def event(self, action, value):
        self._host.root.mkdir(parents=True, exist_ok=True)
        bounded = self._bound_event_value(value)
        row = {'at_utc': now(), 'action': action, **bounded}
        line = _safe_jsonl(row)
        written_row = row
        if len(line) > MAX_EVENT_LINE_BYTES:
            obj_rel, sha = self._put_object_bytes(line)
            summary = {'at_utc': row['at_utc'], 'action': action, 'truncated': True,
                       'event_bytes': len(line), 'event_sha256': sha, 'event_object': obj_rel}
            for key in ('id', 'receipt', 'exit'):
                if key in row and len(_safe_jsonl(row[key])) <= 1024:
                    summary[key] = row[key]
            if isinstance(row.get('argv'), list) and len(_safe_jsonl(row['argv'])) <= 4096:
                summary['argv'] = row['argv']
            line = _safe_jsonl(summary)
            written_row = summary
        events_path = self._host.root / 'EVENTS.jsonl'
        events_lock = self._host.root / '.events.lock'
        with file_lock(events_lock):
            if events_path.is_symlink():
                raise ValueError('EVENTS.jsonl must be an ordinary file')
            created = not events_path.exists()
            with events_path.open('ab') as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    raise ValueError('EVENTS.jsonl must be an ordinary file')
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            if created:
                sync_dir(self._host.root)
        return written_row

    def call(self, *args, scoped_errors=False):
        if (self._host.config.get('attached_instance_id')
                and read(self._host.native / 'system.json').get('instance_id') != self._host.config['attached_instance_id']):
            raise ValueError('attached native store identity changed')
        argv = [sys.executable, '-B', self._host.engine, '--root', str(self._host.native), *map(str, args)]
        p = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8')
        self._host.event('native-call', {'argv': argv, 'exit': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr})
        if p.returncode:
            raise ValueError(capture_error(p.stderr or p.stdout) if scoped_errors else p.stderr or p.stdout)
        return loads_strict(p.stdout)

    def inspect_native(self, sid, scoped_errors=False):
        cli = resolve_sibling_cli(Path(self._host.engine), 'core/cli/exchange_packets.py', 'exchange_packets.py')
        argv = [sys.executable, '-B', str(cli), '--root', str(self._host.native / 'exchange'), 'inspect', ident(sid)]
        p = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8')
        self._host.event('native-inspection', {'argv': argv, 'exit': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr})
        if p.returncode:
            raise ValueError(capture_error(p.stderr or p.stdout) if scoped_errors else p.stderr or p.stdout)
        return loads_strict(p.stdout)

    def capture_native(self, sid, receipt, producer):
        """Use the qualified host adapter over the unchanged selected engine."""
        adapter = HERE / 'capture_native.py'
        argv = [sys.executable, '-B', str(adapter), '--engine', self._host.engine,
                '--root', str(self._host.native), 'assess', sid, '--receipt', str(receipt), '--producer', producer]
        p = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8')
        self._host.event('native-call', {'argv': argv, 'exit': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr,
                                         'capture_adapter': self._host.pin(adapter)})
        if p.returncode:
            raise ValueError(capture_error(p.stderr or p.stdout))
        return loads_strict(p.stdout)

    def reconcile_capture_native(self, sid, receipt, producer):
        """Verify an already-committed native Attention receipt for crash recovery."""
        adapter = HERE / 'capture_native.py'
        argv = [sys.executable, '-B', str(adapter), '--engine', self._host.engine,
                '--root', str(self._host.native), 'reconcile-assess', sid, '--receipt', str(receipt), '--producer', producer]
        p = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8')
        self._host.event('native-capture-reconciliation', {'argv': argv, 'exit': p.returncode, 'stdout': p.stdout,
                                                           'stderr': p.stderr, 'capture_adapter': self._host.pin(adapter)})
        if p.returncode:
            raise ValueError(capture_error(p.stderr or p.stdout))
        return loads_strict(p.stdout)

    def temp(self, name, value):
        p = self._host.root / 'requests' / name
        self._host._write(p, value)
        return p

    def pin(self, path):
        p = Path(path).resolve()
        raw = p.read_bytes()
        obj_rel, sha = self._put_object_bytes(raw)
        return {'path': str(p), 'sha256': sha, 'bytes': len(raw), 'object': obj_rel}

    @staticmethod
    def _capture_record(sid, result):
        attention = result['attention']
        status = result['status']
        matching = [r for r in status.get('turns', []) if r.get('packet') == sid]
        turn = ({'id': sid, 'transport': matching[0]['transport'],
                 'publication_count': len(matching[0].get('publications', []))}
                if len(matching) == 1 else {'id': sid, 'transport': 'unavailable', 'publication_count': None})
        # Use an explicit outward allowlist. The selected adapter result remains
        # in owner evidence; it is never this constructor's unfiltered response.
        return {'id': sid, 'attention': {k: attention[k] for k in ('receipt', 'object', 'recorded', 'boundary')},
                'turn': turn, 'parent_duties_closed': False,
                'scope': 'Candidate custody and requested-turn status only; no unrelated history or parent-duty inventory. This is not a completed return, semantic verdict or access-control boundary.'}

    def _pending_capture_paths(self, sid):
        folder = self._host.root / 'captures' / sid
        if not folder.is_dir():
            return []
        return sorted(folder.glob('.pending-*.json'))

    def _reconcile_pending_captures(self, sid):
        reconciled = []
        requests_dir = self._host.root / 'requests'
        requests_dir.mkdir(parents=True, exist_ok=True)
        for pending_path in self._pending_capture_paths(sid):
            pending = read(pending_path)
            rid = ident(pending['receipt'])
            producer = nonempty(pending['producer'], 'producer')
            target = self._host.root / 'captures' / sid / f'{rid}.json'
            if target.exists():
                pending_path.unlink(missing_ok=True)
                continue
            obj_path = self._host.root / pending['receipt_object']
            if obj_path.is_symlink() or not obj_path.is_file():
                raise ValueError(f'Retained pending receipt object is unavailable: {rid}')
            body = obj_path.read_bytes()
            if digest(body) != pending['receipt_sha256']:
                raise ValueError(f'Retained pending receipt object integrity mismatch: {rid}')
            selected_path = _temp_receipt_file(requests_dir, body)
            try:
                try:
                    result = self._host.reconcile_capture_native(sid, selected_path, producer)
                except ValueError as exc:
                    if str(exc) == 'receipt identity not recorded in native Attention':
                        pending_path.unlink(missing_ok=True)
                        self._host.event('pending-capture-uncommitted', {'id': sid, 'receipt': rid})
                        continue
                    raise
            finally:
                selected_path.unlink(missing_ok=True)
            capture = self._capture_record(sid, result)
            self._host._write(target, capture)
            pending_path.unlink(missing_ok=True)
            self._host.event('candidate-captured', {'id': sid, 'receipt': rid, 'capture': self._host.pin(target),
                                                    'reconciled_from_native': True})
            reconciled.append(capture)
        return reconciled

    def capture(self, sid, spec):
        """Retain products through native Attention without exposing global status."""
        sid = ident(sid)
        scope = read(self._host.root / 'scopes' / f'{sid}.json')
        if set(spec) != {'receipt', 'producer'}:
            raise ValueError('Candidate capture requires receipt and producer only')
        if 'result' in scope or 'superseded' in scope:
            raise ValueError('Candidate capture uses an open producing scope, not a completed or superseded one')
        producer = nonempty(spec['producer'], 'producer')
        receipt = Path(spec['receipt']).resolve()
        with receipt.open('rb') as submitted:
            if not stat.S_ISREG(os.fstat(submitted.fileno()).st_mode):
                raise ValueError('An ordinary assessment receipt file is required')
            body = submitted.read()
        rid = ident(loads_strict(body)['id'])
        target = self._host.root / 'captures' / sid / f'{rid}.json'
        if target.exists():
            raise ValueError('Retain the existing candidate capture; use a fresh receipt for a new occurrence')
        receipt_obj, receipt_sha = self._put_object_bytes(body)
        pending_path = self._host.root / 'captures' / sid / f'.pending-{rid}.json'
        requests_dir = self._host.root / 'requests'
        requests_dir.mkdir(parents=True, exist_ok=True)
        created_pending = not pending_path.exists()
        if not created_pending:
            existing_pending = read(pending_path)
            if (existing_pending.get('receipt_sha256') != receipt_sha
                    or existing_pending.get('producer') != producer):
                old_obj = self._host.root / existing_pending['receipt_object']
                old_body = old_obj.read_bytes() if old_obj.is_file() and not old_obj.is_symlink() else None
                if old_body is not None and digest(old_body) == existing_pending.get('receipt_sha256'):
                    prior_path = _temp_receipt_file(requests_dir, old_body)
                    try:
                        try:
                            self._host.reconcile_capture_native(sid, prior_path, existing_pending['producer'])
                        except ValueError as prior_exc:
                            if str(prior_exc) == 'receipt identity not recorded in native Attention':
                                created_pending = True
                        else:
                            raise ValueError('receipt identity already used')
                    finally:
                        prior_path.unlink(missing_ok=True)
        if created_pending:
            self._host._write(pending_path, {'id': sid, 'receipt': rid, 'producer': producer,
                                             'receipt_object': receipt_obj, 'receipt_sha256': receipt_sha,
                                             'started_at_utc': now()})
        reconciled = False
        selected_path = _temp_receipt_file(requests_dir, body)
        try:
            try:
                result = self._host.capture_native(sid, selected_path, producer)
            except ValueError as exc:
                if str(exc) != 'receipt identity already used':
                    if created_pending:
                        if str(exc) in _PRECOMMIT_CLEANUP_ERRORS:
                            pending_path.unlink(missing_ok=True)
                        else:
                            try:
                                self._host.reconcile_capture_native(sid, selected_path, producer)
                            except ValueError as probe_exc:
                                if str(probe_exc) == 'receipt identity not recorded in native Attention':
                                    pending_path.unlink(missing_ok=True)
                    raise
                try:
                    result = self._host.reconcile_capture_native(sid, selected_path, producer)
                except ValueError as reconcile_exc:
                    if created_pending and str(reconcile_exc) in {
                        'receipt identity already used',
                        'receipt must contain id and assessments',
                    }:
                        pending_path.unlink(missing_ok=True)
                    raise
                reconciled = True
        finally:
            selected_path.unlink(missing_ok=True)
        capture = self._capture_record(sid, result)
        self._host._write(target, capture)
        pending_path.unlink(missing_ok=True)
        event_data = {'id': sid, 'receipt': rid, 'capture': self._host.pin(target)}
        if reconciled:
            event_data['reconciled_from_native'] = True
        self._host.event('candidate-captured', event_data)
        return capture
