"""Cold-tier compressed record archive with verified on-demand readback and strict safety guards."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import zlib

from continuation_support.ownership import packet_id
from continuation_support.storage import (
    _OPEN_RO_NOFOLLOW,
    atomic_bytes,
    encode,
    now,
    read_json,
    sha,
    sync_dir,
    write_json,
)
from feedback import KIND as FEEDBACK_KIND
from native_runtime.identities import digest, exact_fields, require, strict_json
from native_runtime.inputs import strings, text


MANIFEST_FORMAT = 'managed-ai-cold-archive/1'
PLAN_FORMAT = 'managed-ai-archive-records-plan/1'
RECEIPT_FORMAT = 'managed-ai-archive-records-receipt/1'
PROTECTED_IDS = frozenset({'goal', 'research-current'})
PROTECTED_KINDS = frozenset({'goal', 'obligation', 'received-duty-reference', 'research-current', FEEDBACK_KIND})


def _decompress_exact(path: Path, expected_bytes: int, expected_sha256: str) -> bytes:
    """Open an ordinary non-symlink deflate blob and stream-decompress within exact raw byte bounds."""
    if hasattr(os, 'O_NOFOLLOW'):
        try:
            fd = os.open(path, _OPEN_RO_NOFOLLOW)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                raise ValueError(f'ordinary archived record blob required: {path}') from None
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f'ordinary archived record blob required: {path}')
            stream = os.fdopen(fd, 'rb')
        except BaseException:
            os.close(fd)
            raise
    else:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'ordinary archived record blob required: {path}')
        stream = path.open('rb')
    with stream:
        decompressor = zlib.decompressobj()
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                part = stream.read(65536)
                if not part:
                    break
                remaining = (expected_bytes + 1) - total
                out = decompressor.decompress(part, remaining)
                if out:
                    chunks.append(out)
                    total += len(out)
                    if total > expected_bytes:
                        raise ValueError(f'archived record exceeds declared size: {path}')
                if decompressor.unconsumed_tail:
                    raise ValueError(f'archived record exceeds declared size: {path}')
                if decompressor.eof:
                    if decompressor.unused_data or stream.read(1):
                        raise ValueError(f'trailing bytes after archived record: {path}')
                    break
            tail = decompressor.flush((expected_bytes + 1) - total)
            if tail:
                chunks.append(tail)
                total += len(tail)
                if total > expected_bytes:
                    raise ValueError(f'archived record exceeds declared size: {path}')
            if not decompressor.eof:
                raise ValueError(f'truncated archived record: {path}')
        except zlib.error as exc:
            raise ValueError(f'corrupt archived record {path}: {exc}') from None
    raw = b''.join(chunks)
    if len(raw) != expected_bytes or sha(raw) != expected_sha256:
        raise ValueError(f'archived record integrity changed: {path}')
    return raw


class ColdArchive:
    """Verified compressed cold record store under exchange/cold-records/."""

    def __init__(self, exchange_root: Path, record_format: str):
        self.directory = exchange_root / 'cold-records'
        self.blobs_dir = self.directory / 'records'
        self.manifest_path = self.directory / 'manifest.json'
        self.record_format = record_format

    def _load_manifest(self) -> dict | None:
        if not self.manifest_path.exists() and not self.manifest_path.is_symlink():
            return None
        doc = read_json(self.manifest_path)
        require(doc.get('format') == MANIFEST_FORMAT, 'unsupported cold archive manifest format')
        require(doc.get('record_format') == self.record_format, 'cold archive record format differs')
        records = doc.get('records')
        require(isinstance(records, dict), 'cold archive manifest records must be an object')
        return doc

    def entries(self) -> dict[str, dict]:
        doc = self._load_manifest()
        return dict(doc['records']) if doc is not None else {}

    def ids(self) -> list[str]:
        return sorted(self.entries().keys())

    def has(self, ident: str) -> bool:
        try:
            packet_id(ident)
        except ValueError:
            return False
        doc = self._load_manifest()
        return doc is not None and ident in doc['records']

    def blob_path(self, ident: str) -> Path:
        return self.blobs_dir / (packet_id(ident) + '.json.deflate')

    def read(self, ident: str) -> tuple[dict, bytes]:
        ident = packet_id(ident)
        doc = self._load_manifest()
        if doc is None or ident not in doc['records']:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(self.blob_path(ident)))
        entry = doc['records'][ident]
        require(isinstance(entry, dict), f'invalid cold archive entry: {ident}')
        raw_bytes = entry.get('raw_bytes')
        raw_sha256 = entry.get('raw_sha256')
        require(type(raw_bytes) is int and raw_bytes >= 0, f'invalid cold archive byte count: {ident}')
        digest(raw_sha256, 'cold archive raw_sha256')
        raw = _decompress_exact(self.blob_path(ident), raw_bytes, raw_sha256)
        record = strict_json(raw)
        require(record.get('id') == ident and record.get('format') == self.record_format,
                'archived record identity or format changed')
        require(record.get('sha256') == sha(encode({k: v for k, v in record.items() if k != 'sha256'})),
                'archived record digest changed')
        require(record.get('revision') == entry.get('revision') and record.get('sha256') == entry.get('sha256'),
                'archived record head differs from cold archive manifest')
        return record, raw

    def save_manifest(self, instance_id: str, records: dict[str, dict]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.manifest_path, {
            'format': MANIFEST_FORMAT,
            'instance_id': instance_id,
            'record_format': self.record_format,
            'records': records,
        })


def _sealed_markers(system) -> tuple[set[str], set[str]]:
    """Collect sealed catalogue entry IDs and body paths strictly from metadata (never opens bodies)."""
    sealed_ids: set[str] = set()
    sealed_paths: set[str] = set()
    for entry in system.domain_selection.entries:
        if entry.get('availability') == 'sealed':
            if isinstance(entry.get('id'), str) and entry['id']:
                sealed_ids.add(entry['id'])
            if isinstance(entry.get('body_path'), str) and entry['body_path']:
                sealed_paths.add(entry['body_path'])
    return sealed_ids, sealed_paths


def _collect_strings(value, out: set[str]) -> None:
    if isinstance(value, str):
        out.add(value)
    elif isinstance(value, list):
        for item in value:
            _collect_strings(item, out)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_strings(v, out)


def _references_sealed(record: dict, sealed_ids: set[str], sealed_paths: set[str]) -> bool:
    if not sealed_ids and not sealed_paths:
        return False
    tokens: set[str] = set()
    _collect_strings(record.get('content'), tokens)
    _collect_strings(record.get('relations'), tokens)
    _collect_strings(record.get('basis'), tokens)
    for token in tokens:
        if token in sealed_ids or token in sealed_paths:
            return True
        for sp in sealed_paths:
            if sp in token:
                return True
        for sid in sealed_ids:
            if sid in token:
                return True
    return False


def _active_outbound_targets(records: list[dict]) -> set[str]:
    targets: set[str] = set()
    for rec in records:
        if not rec.get('active', True):
            continue
        for rel in rec.get('relations', ()):
            if isinstance(rel, dict) and isinstance(rel.get('target'), str):
                targets.add(rel['target'])
        content = rec.get('content')
        if isinstance(content, dict):
            for key, value in content.items():
                if key == 'id':
                    continue
                if not (key.endswith(('_id', '_ids')) or key in ('directions', 'selected_question')):
                    continue
                if isinstance(value, str):
                    targets.add(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            targets.add(item)
    return targets


def _external_hot_record_refs(system) -> set[str]:
    """Find hot record IDs directly pinned on disk by interpretations or staged packets."""
    pinned: set[str] = set()
    interp_dir = system.work / 'interpretations'
    if interp_dir.is_dir():
        for path in sorted(interp_dir.glob('*.json')):
            doc = read_json(path)
            for item in doc.get('evidence', ()):
                if isinstance(item, dict) and isinstance(item.get('path'), str):
                    p = item['path']
                    if p.startswith('records/') and p.endswith('.json'):
                        pinned.add(p[len('records/'):-5])
    packets_dir = system.ex.root / 'packets'
    if packets_dir.is_dir():
        for child in sorted(packets_dir.iterdir()):
            if not child.name.startswith('.sealed-') or not child.is_dir():
                continue
            req_path = child / 'request.json'
            if req_path.is_file():
                req = read_json(req_path)
                for role in ('methods', 'materials', 'targets'):
                    for item in req.get(role, ()):
                        if isinstance(item, dict) and isinstance(item.get('path'), str):
                            p = item['path']
                            if p.startswith('records/') and p.endswith('.json'):
                                pinned.add(p[len('records/'):-5])
    return pinned


def _evaluate_candidates(system, candidate_ids: list[str]) -> tuple[list[dict], list[dict]]:
    all_records = system.record_store.scan_records()
    by_id = {rec['id']: rec for rec in all_records}
    active_targets = _active_outbound_targets(all_records)
    external_pins = _external_hot_record_refs(system)
    sealed_ids, sealed_paths = _sealed_markers(system)
    already_archived = set(system.record_store.cold_archive.ids())

    eligible: list[dict] = []
    ineligible: list[dict] = []
    for ident in sorted(dict.fromkeys(candidate_ids)):
        reasons: list[str] = []
        if ident in already_archived:
            reasons.append('already-archived')
        rec = by_id.get(ident)
        if rec is None:
            if not reasons:
                reasons.append('missing-hot-record')
            ineligible.append({'id': ident, 'reasons': reasons})
            continue
        if ident in PROTECTED_IDS:
            reasons.append('protected-governing-id')
        if rec['kind'] in PROTECTED_KINDS:
            reasons.append('protected-record-kind:' + rec['kind'])
        content = rec.get('content')
        if isinstance(content, dict) and content.get('format') == 'g60-composition/1':
            reasons.append('protected-composition-record')
        if rec.get('active', True):
            reasons.append('active-record')
        if ident in active_targets:
            reasons.append('referenced-by-active-record')
        if ident in external_pins:
            reasons.append('referenced-by-interpretation-or-staged-packet')
        if _references_sealed(rec, sealed_ids, sealed_paths):
            reasons.append('references-sealed-source')
        if reasons:
            ineligible.append({'id': ident, 'reasons': reasons})
        else:
            raw_bytes = system.record_store.size(ident)
            eligible.append({
                'id': ident,
                'kind': rec['kind'],
                'revision': rec['revision'],
                'sha256': rec['sha256'],
                'raw_bytes': raw_bytes,
            })
    return eligible, ineligible


def plan_archive_records(system, *, ids: list[str] | None = None, kinds: list[str] | None = None) -> dict:
    """Read-only audit nominating eligible active=False cold records for compressed archival."""
    nominated: list[str] = []
    if ids:
        for ident in strings(list(ids), 'ids'):
            nominated.append(packet_id(ident))
    if kinds:
        wanted_kinds = set(strings(list(kinds), 'kinds'))
        summaries = system.record_store.indexed_summaries()
        if summaries is not None:
            for ident, entry in summaries.items():
                if entry['kind'] in wanted_kinds and not entry.get('active', True):
                    nominated.append(ident)
        else:
            for rec in system.record_store.scan_records():
                if rec['kind'] in wanted_kinds and not rec.get('active', True):
                    nominated.append(rec['id'])
    require(bool(nominated), 'nominate at least one record --id or inactive --kind to archive')

    eligible, ineligible = _evaluate_candidates(system, nominated)
    reclaimable = sum(row['raw_bytes'] for row in eligible)
    body = {
        'format': PLAN_FORMAT,
        'instance_id': system.instance_id,
        'eligible': eligible,
        'ineligible': ineligible,
        'reclaimable_hot_bytes': reclaimable,
        'reclaimable_records': len(eligible),
    }
    return dict(body, plan_sha256=sha(encode(body)))


def execute_archive_records(system, *, plan: dict, expected_plan: str, reason: str) -> dict:
    """Move verified eligible cold records into exchange/cold-records/ under owner.lock."""
    text(reason, 'archive reason')
    digest(expected_plan, 'expected plan sha256')
    exact_fields(
        plan,
        {'format', 'instance_id', 'eligible', 'ineligible', 'reclaimable_hot_bytes',
         'reclaimable_records', 'plan_sha256'},
        set(),
        'archive-records plan',
    )
    require(plan['format'] == PLAN_FORMAT, 'unsupported archive-records plan format')
    require(plan['instance_id'] == system.instance_id, 'archive-records plan belongs to another store')
    body = {k: v for k, v in plan.items() if k != 'plan_sha256'}
    computed_sha = sha(encode(body))
    require(plan['plan_sha256'] == computed_sha and computed_sha == expected_plan,
            'archive-records plan digest differs from expected plan')
    require(isinstance(plan['eligible'], list) and len(plan['eligible']) > 0,
            'archive-records plan has no eligible records')

    nominated_ids = [packet_id(row['id']) for row in plan['eligible']]
    require(len(nominated_ids) == len(set(nominated_ids)), 'duplicate record IDs in archive plan')

    # Re-verify ALL guards and exact (revision, sha256) live under exclusive owner.lock before any write.
    live_eligible, live_ineligible = _evaluate_candidates(system, nominated_ids)
    require(not live_ineligible,
            'records became ineligible since planning: ' + ', '.join(
                f"{r['id']} ({','.join(r['reasons'])})" for r in live_ineligible))
    live_by_id = {row['id']: row for row in live_eligible}
    for planned in plan['eligible']:
        current = live_by_id.get(planned['id'])
        require(current is not None
                and current['revision'] == planned['revision']
                and current['sha256'] == planned['sha256']
                and current['kind'] == planned['kind'],
                'planned record head changed since planning: ' + planned['id'])

    archive = system.record_store.cold_archive
    archive.directory.mkdir(parents=True, exist_ok=True)
    archive.blobs_dir.mkdir(parents=True, exist_ok=True)
    manifest_records = archive.entries()

    archived_rows: list[dict] = []
    compressed_total = 0
    raw_total = 0
    staged_reads: list[tuple[str, dict, bytes]] = []
    for planned in plan['eligible']:
        ident = planned['id']
        record, raw = system.record_store.read(ident)
        require(record['revision'] == planned['revision'] and record['sha256'] == planned['sha256'],
                'record changed during archive read: ' + ident)
        staged_reads.append((ident, record, raw))

    # Phase 1: Write and round-trip verify every compressed blob before updating manifest or unlinking hot heads.
    archived_at = now()
    for ident, record, raw in staged_reads:
        raw_sha = sha(raw)
        compressed = zlib.compress(raw, level=6)
        blob = archive.blob_path(ident)
        atomic_bytes(blob, compressed)
        # Immediate round-trip integrity check
        verified_raw = _decompress_exact(blob, len(raw), raw_sha)
        require(verified_raw == raw, 'archived blob verification failed: ' + ident)
        compressed_total += len(compressed)
        raw_total += len(raw)
        basis_objects = sorted({item['sha256'] for item in record.get('basis', ()) if item.get('sha256')})
        manifest_records[ident] = {
            'id': ident,
            'kind': record['kind'],
            'revision': record['revision'],
            'sha256': record['sha256'],
            'active': record['active'],
            'raw_bytes': len(raw),
            'compressed_bytes': len(compressed),
            'raw_sha256': raw_sha,
            'basis_objects': basis_objects,
            'archived_at': archived_at,
            'reason': reason,
        }
        archived_rows.append({
            'id': ident,
            'kind': record['kind'],
            'revision': record['revision'],
            'sha256': record['sha256'],
            'raw_bytes': len(raw),
            'compressed_bytes': len(compressed),
        })

    # Phase 2: Atomically commit updated manifest.
    archive.save_manifest(system.instance_id, manifest_records)
    sync_dir(archive.blobs_dir)

    # Phase 3: Remove hot files, sync records dir, update HeadIndex, and verify transparent readback.
    records_dir = system.work / 'records'
    for ident, _, _ in staged_reads:
        hot_path = system.record_store.path(ident)
        hot_path.unlink()
    sync_dir(records_dir)
    system.record_store.index.remove_if_present(system.record_store.format, nominated_ids)

    # Post-archive readback verification
    for planned in plan['eligible']:
        reloaded = system.record_store.show(planned['id'])
        require(reloaded['revision'] == planned['revision'] and reloaded['sha256'] == planned['sha256'],
                'post-archive readback mismatch: ' + planned['id'])

    receipt_dir = system.ex.path('.system/maintenance')
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / f'archive-records-{expected_plan[:16]}.json'
    receipt = {
        'format': RECEIPT_FORMAT,
        'instance_id': system.instance_id,
        'plan_sha256': expected_plan,
        'reason': reason,
        'archived_at': archived_at,
        'archived_count': len(archived_rows),
        'reclaimed_hot_bytes': raw_total,
        'cold_compressed_bytes': compressed_total,
        'records': archived_rows,
    }
    write_json(receipt_path, receipt)
    return dict(receipt, receipt_path=str(receipt_path))
