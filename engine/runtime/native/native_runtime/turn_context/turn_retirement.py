"""Guarded two-phase retirement of completed turn working copies under work/.system/turns/.

Implements the MAINTENANCE.md working-copy retirement contract without touching
exchange/packets/, exchange/objects/, or native records.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import json
import os
from pathlib import Path
import posixpath
import stat
from typing import Any

from continuation_support.exchange.carrier_receipts import transport_outcome
from continuation_support.exchange.publication_receipts import (
    _publication_name,
    _validate_publication_record,
    publication_inventory,
    publication_matches,
)
from continuation_support.ownership import packet_id
from continuation_support.storage import (
    MAX_BYTES,
    _OPEN_RO_NOFOLLOW,
    encode,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    is_reparse_or_symlink,
    now,
    open_directory,
    read_bytes,
    sha,
    sync_dir,
    write_json,
)
from native_runtime.formats import (
    TURN_RETIRE_PLAN_FORMAT as PLAN_FORMAT,
    TURN_RETIRE_RECEIPT_FORMAT as RECEIPT_FORMAT,
    TURN_SELECTION_FORMAT,
    validate_optional_format,
)
from native_runtime.hot_refs import (
    extract_turn_ref as _extract_referenced_turn,
    scan_attention_refs,
    scan_interpretation_refs,
    scan_packet_refs,
    scan_uncommitted_turn_refs,
)
from native_runtime.identities import digest, exact_fields, nonempty, require, strict_json
from native_runtime.turn_context.preparation_journal import feedback_id

RECEIPT_DIRECTORY = '.system/retirements'
TURNS_RELATIVE = '.system/turns'

_PLAN_REQUIRED = {
    'format',
    'instance_id',
    'selection_mode',
    'nominated_turns',
    'eligible',
    'ineligible',
    'reclaimable_bytes',
    'reclaimable_files',
    'scope',
    'plan_sha256',
}
_ELIGIBLE_FIELDS = {
    'turn',
    'working_path',
    'request_sha256',
    'context_sha256',
    'return_sha256',
    'declared_targets',
    'published_targets',
    'files',
    'bytes',
    'working_dir_sha256',
}
_TURN_REQUEST_ALLOWED_FIELDS = {
    'task',
    'consumer',
    'records',
    'sources',
    'techniques',
    'targets',
    'returns',
    'artifacts',
    'attention',
    'record_limit',
    'attention_limit',
    'attention_max_bytes',
    'attention_groups',
    'attention_items',
    'project_sources',
    'discovery',
    'source_pins',
    'compositions',
    'feedback_uses',
}
_RECEIPT_REQUIRED = {
    'format',
    'instance_id',
    'plan_sha256',
    'reason',
    'complete',
    'retired_at',
    'retired_turns',
    'turns',
    'retired_count',
    'files_removed',
    'bytes_reclaimed',
    'post_verification',
    'receipt_path',
    'scope',
}


def _read_strict_file(path: Path, limit: int = MAX_BYTES) -> dict[str, Any]:
    return strict_json(read_bytes(path, limit))


def _scan_turn_directory(turn_dir: Path) -> tuple[int, int, str, list[str], dict[str, str], list[str]]:
    """Inspect a working turn directory without following symlinks or reparse points.

    Returns (file_count, total_bytes, inventory_sha256, structural_issues, file_hashes, dir_paths).
    """
    issues: list[str] = []
    entries: list[dict[str, Any]] = []
    file_hashes: dict[str, str] = {}
    dir_paths: list[str] = []
    file_count = 0
    total_bytes = 0

    def walk_dir(current: Path, rel_prefix: str) -> None:
        nonlocal file_count, total_bytes
        for child in sorted(current.iterdir(), key=lambda p: p.name):
            rel = f'{rel_prefix}/{child.name}' if rel_prefix else child.name
            info = child.lstat()
            mode = info.st_mode
            if is_reparse_or_symlink(info):
                issues.append(f'symlink:{rel}')
                continue
            if stat.S_ISDIR(mode):
                dir_paths.append(rel)
                entries.append({'path': rel, 'type': 'dir'})
                walk_dir(child, rel)
            elif stat.S_ISREG(mode):
                ctx = (open(child, 'rb', opener=lambda p, flags: os.open(p, _OPEN_RO_NOFOLLOW))
                       if hasattr(os, 'O_NOFOLLOW') else child.open('rb'))
                with ctx as stream:
                    finfo = os.fstat(stream.fileno())
                    if not stat.S_ISREG(finfo.st_mode):
                        issues.append(f'non_regular:{rel}')
                        continue
                    file_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
                    size = finfo.st_size
                file_count += 1
                total_bytes += size
                file_hashes[rel] = file_sha
                entries.append({'path': rel, 'type': 'file', 'bytes': size, 'sha256': file_sha})
            else:
                issues.append(f'special:{rel}')

    walk_dir(turn_dir, '')
    return file_count, total_bytes, sha(encode(entries)), issues, file_hashes, dir_paths


def _collect_record_references(system, candidate_set: set[str]) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Scan live work/records/*.json for structural references to any candidate turn working dir."""
    refs: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    if not candidate_set:
        return refs, errors
    records_dir = system.ex.path('records')
    if not records_dir.exists():
        return refs, errors
    require(not records_dir.is_symlink() and records_dir.is_dir(),
            'ordinary records directory required')
    filenames = sorted(p.name for p in records_dir.iterdir()
                       if p.name.endswith('.json') and not p.name.startswith('.'))
    if not filenames:
        return refs, errors

    marker = b'.system'
    unicode_ascii_escape = b'\\u00'

    def inspect_chunk(names: list[str], dir_fd: int | None) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
        found: list[tuple[str, dict[str, Any]]] = []
        chunk_errors: list[str] = []
        for name in names:
            full = records_dir / name
            if dir_fd is not None and hasattr(os, 'O_NOFOLLOW'):
                try:
                    fd = os.open(name, _OPEN_RO_NOFOLLOW, dir_fd=dir_fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                        raise ValueError(f'ordinary record file required: {full}') from None
                    chunk_errors.append(f'unreadable record {name}: {exc}')
                    continue
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise ValueError(f'ordinary record file required: {full}')
                    stream = os.fdopen(fd, 'rb')
                except BaseException:
                    os.close(fd)
                    raise
                with stream:
                    raw = stream.read()
            else:
                info = full.lstat()
                require(stat.S_ISREG(info.st_mode), f'ordinary record file required: {full}')
                raw = full.read_bytes()
            if marker not in raw and unicode_ascii_escape not in raw:
                continue
            try:
                record = strict_json(raw)
            except (ValueError, TypeError) as exc:
                chunk_errors.append(f'unreadable record {name}: {exc}')
                continue
            rec_id = record.get('id', full.stem)
            for item in record.get('basis', []):
                if isinstance(item, dict):
                    path = item.get('path')
                    target_turn = _extract_referenced_turn(path)
                    if target_turn in candidate_set:
                        found.append((target_turn, {'record': rec_id, 'field': 'basis', 'path': path}))
            for rel in record.get('relations', []):
                if isinstance(rel, dict):
                    for rel_field in ('target', 'origin'):
                        val = rel.get(rel_field)
                        target_turn = _extract_referenced_turn(val)
                        if target_turn in candidate_set:
                            found.append((target_turn, {
                                'record': rec_id,
                                'field': f'relations.{rel_field}',
                                'path': val,
                            }))
                    aliases = rel.get('aliases')
                    if isinstance(aliases, list):
                        for alias in aliases:
                            target_turn = _extract_referenced_turn(alias)
                            if target_turn in candidate_set:
                                found.append((target_turn, {
                                    'record': rec_id,
                                    'field': 'relations.aliases',
                                    'path': alias,
                                }))
            content = record.get('content')
            if isinstance(content, dict):
                origin = content.get('origin')
                target_turn = _extract_referenced_turn(origin)
                if target_turn in candidate_set:
                    found.append((target_turn, {
                        'record': rec_id,
                        'field': 'content.origin',
                        'path': origin,
                    }))
                obligations = content.get('obligations')
                if isinstance(obligations, list):
                    for ob in obligations:
                        if isinstance(ob, dict) and isinstance(ob.get('evidence'), list):
                            for ev in ob['evidence']:
                                target_turn = _extract_referenced_turn(ev)
                                if target_turn in candidate_set:
                                    found.append((target_turn, {
                                        'record': rec_id,
                                        'field': 'content.obligations.evidence',
                                        'path': ev,
                                    }))
                if isinstance(content.get('evidence'), list):
                    for ev in content['evidence']:
                        target_turn = _extract_referenced_turn(ev)
                        if target_turn in candidate_set:
                            found.append((target_turn, {
                                'record': rec_id,
                                'field': 'content.evidence',
                                'path': ev,
                            }))
        return found, chunk_errors

    if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
        with system.ex.open_workspace_dir('records') as records_fd:
            if len(filenames) >= 64:
                workers = min(8, len(filenames))
                chunk_size = (len(filenames) + workers - 1) // workers
                chunks = [filenames[i:i + chunk_size] for i in range(0, len(filenames), chunk_size)]
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for batch, chunk_errors in pool.map(lambda c: inspect_chunk(c, records_fd), chunks):
                        for target_turn, ref in batch:
                            refs.setdefault(target_turn, []).append(ref)
                        errors.extend(chunk_errors)
            else:
                batch, chunk_errors = inspect_chunk(filenames, records_fd)
                for target_turn, ref in batch:
                    refs.setdefault(target_turn, []).append(ref)
                errors.extend(chunk_errors)
    else:
        batch, chunk_errors = inspect_chunk(filenames, None)
        for target_turn, ref in batch:
            refs.setdefault(target_turn, []).append(ref)
        errors.extend(chunk_errors)

    for turn_refs in refs.values():
        turn_refs.sort(key=lambda r: (r['record'], r['field'], r['path']))
    errors.sort()
    return refs, errors


def _collect_cross_packet_references(system, candidate_set: set[str]) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    """Scan all exchange/packets/*/request.json (including staged .sealed-*/.prepare-*) for cross-turn refs."""
    if not candidate_set:
        return {}, []
    all_refs, _, scan_errors = scan_packet_refs(
        system,
        _extract_referenced_turn,
        open_only=False,
        skip_self_packet=True,
        fail_closed=False,
    )
    return {turn: rows for turn, rows in all_refs.items() if turn in candidate_set}, scan_errors


def _collect_attention_references(system, candidate_set: set[str]) -> tuple[dict[str, list[dict[str, str]]], str | None]:
    """Scan active native Attention field, item basis, and latest assessment products for working-dir refs."""
    if not candidate_set:
        return {}, None
    all_refs, err = scan_attention_refs(system, _extract_referenced_turn, fail_closed=False)
    return {turn: rows for turn, rows in all_refs.items() if turn in candidate_set}, err


def _collect_interpretation_references(system, candidate_set: set[str]) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    """Scan work/interpretations/*.json evidence paths (snapshot dicts or strings) for working-dir refs."""
    if not candidate_set:
        return {}, []
    all_refs, errors = scan_interpretation_refs(system, _extract_referenced_turn, fail_closed=False)
    return {turn: rows for turn, rows in all_refs.items() if turn in candidate_set}, errors


def _collect_uncommitted_turn_references(
    system,
    turns_root: Path,
    candidate_set: set[str],
) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    """Check active uncommitted turn staging requests under work/.system/turns/ for references."""
    if not candidate_set or not turns_root.is_dir():
        return {}, []
    packets_root = system.ex.root / 'packets'
    committed = (
        {p.name for p in packets_root.iterdir() if not p.name.startswith('.')}
        if packets_root.is_dir() else set()
    )
    all_refs, errors = scan_uncommitted_turn_refs(
        system,
        committed,
        _extract_referenced_turn,
        include_record_ids=False,
        skip_self_turn=True,
        fail_closed=False,
    )
    return {turn: rows for turn, rows in all_refs.items() if turn in candidate_set}, errors


def _evaluate_turn(
    system,
    turn: str,
    turn_dir: Path,
    *,
    record_refs: dict[str, list[dict[str, Any]]],
    record_scan_errors: list[str],
    packet_refs: dict[str, list[dict[str, str]]],
    packet_scan_errors: list[str],
    attention_refs: dict[str, list[dict[str, str]]],
    attention_error: str | None,
    interp_refs: dict[str, list[dict[str, str]]],
    interp_errors: list[str],
    uncommitted_refs: dict[str, list[dict[str, str]]],
    uncommitted_errors: list[str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    reasons: list[str] = []
    details: dict[str, Any] = {}

    def refuse(code: str, detail: Any = None) -> None:
        if code not in reasons:
            reasons.append(code)
        if detail is not None:
            details[code] = detail

    # Working directory structure check (no symlinks / special entries).
    file_count, total_bytes, dir_sha, dir_issues, file_hashes, dir_paths = _scan_turn_directory(turn_dir)
    if dir_issues:
        refuse('working_dir_unsafe_entries', dir_issues)

    # Guard (a): Packet exists, validates cleanly, and has a strict validated return.json.
    packet_dir = system.ex.directory(turn)
    staged_dir = system.ex.prepared_directory(turn)
    if staged_dir.exists() or staged_dir.is_symlink():
        refuse('staged_preparation_present', str(staged_dir.name))

    binding = None
    request = None
    returned = None
    if packet_dir.is_symlink() or not packet_dir.is_dir():
        refuse('packet_missing')
    else:
        try:
            binding = _read_strict_file(packet_dir / 'binding.json')
            request = _read_strict_file(packet_dir / 'request.json')
            loaded_dir, loaded_req = system.ex.load(turn)
            require(loaded_dir == packet_dir and loaded_req == request,
                    'loaded packet differs from strict request')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            refuse('packet_invalid', str(exc))
            binding = None
            request = None

        if request is not None:
            try:
                outcome = transport_outcome(system.ex, packet_dir, request)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                refuse('transport_outcome_unreadable', str(exc))
                outcome = None
            if outcome is not None:
                refuse('terminal_without_return', outcome.get('reason'))

            return_path = packet_dir / 'return.json'
            if return_path.is_symlink() or not return_path.is_file():
                refuse('missing_return')
            else:
                try:
                    returned = _read_strict_file(return_path)
                    system.ex.validate_return(packet_dir, returned)
                    require(returned.get('packet') == turn
                            and returned.get('request_sha256') == binding['request_sha256'],
                            'return binding differs from packet request')
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    refuse('invalid_return', str(exc))
                    returned = None

    # Guard (b): Clean preparation state AND 100% of declared targets returned and fully published.
    draft_dir = turn_dir / 'attention-draft'
    if draft_dir.exists() or draft_dir.is_symlink():
        refuse('uncommitted_attention_draft')

    prep_path = turn_dir / 'preparation.json'
    prep_state = None
    if prep_path.exists() or prep_path.is_symlink():
        try:
            prep_state = _read_strict_file(prep_path)
            phase = prep_state.get('phase')
            if phase != 'sealed':
                refuse('incomplete_preparation_phase', phase)
            elif binding is not None:
                if (prep_state.get('request_sha256') != binding['request_sha256']
                        or prep_state.get('context_sha256') != binding['context_sha256']):
                    refuse('preparation_binding_mismatch')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            refuse('unreadable_preparation_marker', str(exc))

    declared_targets: list[str] = []
    complete_targets: list[str] = []
    if request is not None and returned is not None:
        declared_targets = [row['path'] for row in request.get('targets', [])]
        proposal_paths = {row['path'] for row in returned.get('proposals', [])}
        unreturned = [t for t in declared_targets if t not in proposal_paths]
        if unreturned:
            refuse('unreturned_targets', unreturned)

        publications, reconciliations, unreadable = publication_inventory(packet_dir)
        if unreadable:
            refuse('unreadable_publication_records',
                   {name: str(err) for name, err in sorted(unreadable.items())})
        if reconciliations:
            refuse('reconciled_publication_unestablished', sorted(reconciliations))

        complete_set: set[str] = set()
        bad_pubs: list[str] = []
        for slot_name, (pub_record, _) in sorted(publications.items()):
            target = pub_record.get('path')
            try:
                require(isinstance(target, str) and slot_name == _publication_name(target),
                        f'non-canonical publication slot: {slot_name}')
                require(publication_matches(turn, request, pub_record, returned),
                        f'publication binding mismatch: {slot_name}')
                _validate_publication_record(pub_record)
            except (ValueError, KeyError, TypeError) as exc:
                bad_pubs.append(f'{slot_name}: {exc}')
                continue
            if pub_record.get('phase') != 'complete':
                bad_pubs.append(f'{target}: phase={pub_record.get("phase")}')
                continue
            complete_set.add(target)

        if bad_pubs:
            refuse('incomplete_or_invalid_publications', bad_pubs)

        unpublished = [t for t in declared_targets if t not in complete_set]
        if unpublished:
            refuse('unpublished_targets', unpublished)
        complete_targets = [t for t in declared_targets if t in complete_set]

    # Guard (c): No live structural reference from records, cross-turn packets, Attention, interpretations, or uncommitted turns.
    if turn in record_refs:
        refuse('referenced_by_records', record_refs[turn])
    if record_scan_errors:
        refuse('record_scan_uncertain', record_scan_errors)
    if turn in packet_refs:
        refuse('referenced_by_other_packets', packet_refs[turn])
    if packet_scan_errors:
        refuse('cross_packet_scan_uncertain', packet_scan_errors)
    if turn in attention_refs:
        refuse('referenced_by_attention', attention_refs[turn])
    if attention_error is not None:
        refuse('attention_scan_uncertain', attention_error)
    if turn in interp_refs:
        refuse('referenced_by_interpretations', interp_refs[turn])
    if interp_errors:
        refuse('interpretation_scan_uncertain', interp_errors)
    if turn in uncommitted_refs:
        refuse('referenced_by_uncommitted_turns', uncommitted_refs[turn])
    if uncommitted_errors:
        refuse('uncommitted_turn_scan_uncertain', uncommitted_errors)

    # Guard (d): 100% of captured bodies/objects remain recoverable in exchange/objects & exchange/packets/<turn>.
    if request is not None and returned is not None:
        try:
            view_doc = system.view(turn)
            require(isinstance(view_doc, dict), 'captured view must be an object')
            recovered = system.recover(turn)
            require(recovered.get('request_sha256') == binding['request_sha256']
                    and recovered.get('context_sha256') == binding['context_sha256'],
                    'recovered context binding differs')
            explained = system.explain_context(turn)
            require(explained.get('request_sha256') == binding['request_sha256']
                    and explained.get('context_sha256') == binding['context_sha256'],
                    'explained context binding differs')
            system.ex.objects.read(returned['reply_sha256'], retained=True,
                                   expected_bytes=returned['reply_bytes'])
            for proposal in returned.get('proposals', []):
                system.ex.objects.read(proposal['sha256'], retained=True,
                                       expected_bytes=proposal['bytes'])
            discovery_prefix = f'{TURNS_RELATIVE}/{turn}/discovery-summary.json'
            disc_entry = next((r for r in request.get('materials', []) if r.get('path') == discovery_prefix), None)
            if disc_entry is not None and disc_entry.get('sha256'):
                disc_summary = strict_json(system.ex.objects.read(disc_entry['sha256'], retained=True))
                if disc_summary.get('report_object'):
                    system.ex.objects.read(disc_summary['report_object'], retained=True)
            if isinstance(prep_state, dict) and prep_state.get('feedback_object'):
                system.ex.objects.read(prep_state['feedback_object'], retained=True)
            att_state = system.attention._load()
            fb_id = feedback_id(turn)
            if fb_id in att_state.get('feedbacks', {}):
                system.attention.inspect(fb_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            refuse('captured_recovery_failed', str(exc))

        # Verify every working file/dir under work/.system/turns/<turn>/ is either a byte-identical
        # captured packet/view object or a verified TurnPreparation/TurnCapture bookkeeping file.
        if not dir_issues:
            turn_prefix = f'{TURNS_RELATIVE}/{turn}/'
            expected_captured: dict[str, str] = {}
            brief_row = request.get('brief')
            for row in ([brief_row] if isinstance(brief_row, dict) else []) + list(request.get('methods', [])) + list(request.get('materials', [])):
                if not isinstance(row, dict):
                    continue
                row_path = row.get('path')
                row_sha = row.get('sha256')
                if isinstance(row_path, str) and row_path.startswith(turn_prefix) and isinstance(row_sha, str):
                    expected_captured[row_path[len(turn_prefix):]] = row_sha

            full_view_object = None
            exp_sha = expected_captured.get('exposure.json')
            if exp_sha:
                try:
                    exp_doc = strict_json(system.ex.objects.read(exp_sha, retained=True))
                    full_view_object = exp_doc.get('full_view_object')
                    if isinstance(full_view_object, str):
                        expected_captured['view.json'] = full_view_object
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    refuse('captured_recovery_failed', f'exposure.json verification failed: {exc}')

            modified_files: list[str] = []
            uncaptured_entries: list[str] = []
            allowed_files: set[str] = set()

            for rel, expected_sha in sorted(expected_captured.items()):
                if rel in file_hashes:
                    allowed_files.add(rel)
                    if file_hashes[rel] != expected_sha:
                        modified_files.append(rel)

            # Verify known uncaptured bookkeeping files written by TurnPreparation / TurnCapture.
            if 'preparation.json' in file_hashes:
                allowed_files.add('preparation.json')
                if not (isinstance(prep_state, dict)
                        and prep_state.get('phase') == 'sealed'
                        and prep_state.get('feedback') == feedback_id(turn)
                        and prep_state.get('request_sha256') == binding['request_sha256']
                        and prep_state.get('context_sha256') == binding['context_sha256']):
                    modified_files.append('preparation.json')

            work_request_doc = None
            if 'request.json' in file_hashes:
                allowed_files.add('request.json')
                try:
                    work_request_doc = _read_strict_file(turn_dir / 'request.json')
                    require(isinstance(work_request_doc, dict)
                            and set(work_request_doc) <= _TURN_REQUEST_ALLOWED_FIELDS
                            and work_request_doc.get('consumer') == request.get('consumer')
                            and work_request_doc.get('targets') == declared_targets
                            and work_request_doc.get('returns') == [r['origin']['packet'] for r in request.get('returns', [])]
                            and work_request_doc.get('artifacts') == [
                                f"{r['origin']['packet']}:{r['origin']['proposal_path']}"
                                for r in request.get('artifacts', [])
                            ],
                            'working request.json disagrees with packet request')
                    brief_sha = expected_captured.get('brief.md')
                    if brief_sha and isinstance(work_request_doc.get('task'), str):
                        brief_text = system.ex.objects.read(brief_sha, retained=True).decode('utf-8')
                        expected_prefix = '# Current receiving task\n\n' + work_request_doc['task'] + '\n'
                        require(brief_text.startswith(expected_prefix),
                                'working request.json task disagrees with captured brief.md')
                except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
                    modified_files.append('request.json')
                    work_request_doc = None

            if 'selection.json' in file_hashes:
                allowed_files.add('selection.json')
                try:
                    sel_doc = _read_strict_file(turn_dir / 'selection.json')
                    exact_fields(sel_doc, {'request', 'view', 'attention_feedback'}, {'format'}, 'selection.json')
                    validate_optional_format(sel_doc, TURN_SELECTION_FORMAT, 'selection.json')
                    require(sel_doc['attention_feedback'] == feedback_id(turn),
                            'selection.json feedback differs')
                    require(isinstance(full_view_object, str)
                            and sha(encode(sel_doc['view'])) == full_view_object,
                            'selection.json view differs from captured full_view_object')
                    if work_request_doc is not None:
                        require(encode(sel_doc['request']) == encode(work_request_doc),
                                'selection.json request differs from working request.json')
                except (OSError, ValueError, KeyError, TypeError):
                    modified_files.append('selection.json')
            elif 'request.json' in file_hashes:
                # Without selection.json tying request.json to full_view_object, refuse unretained edits.
                modified_files.append('request.json')

            if 'attention-spec.json' in file_hashes:
                if (isinstance(work_request_doc, dict) and 'attention' in work_request_doc):
                    allowed_files.add('attention-spec.json')
                    expected_att_sha = sha(encode(work_request_doc['attention']))
                    if file_hashes['attention-spec.json'] != expected_att_sha:
                        modified_files.append('attention-spec.json')
                    else:
                        try:
                            system.ex.objects.read(expected_att_sha, retained=True)
                        except (OSError, ValueError):
                            modified_files.append('attention-spec.json')
                else:
                    uncaptured_entries.append('attention-spec.json')

            for rel in sorted(file_hashes):
                if rel not in allowed_files:
                    uncaptured_entries.append(rel)

            allowed_dirs: set[str] = set()
            for rel in allowed_files:
                parent = posixpath.dirname(rel)
                while parent:
                    allowed_dirs.add(parent)
                    parent = posixpath.dirname(parent)
            for rel_dir in sorted(dir_paths):
                if rel_dir not in allowed_dirs:
                    uncaptured_entries.append(rel_dir + '/')

            if modified_files:
                refuse('working_dir_modified_after_capture', sorted(set(modified_files)))
            if uncaptured_entries:
                refuse('uncaptured_working_files', sorted(set(uncaptured_entries)))

    if reasons:
        ineligible_row: dict[str, Any] = {'turn': turn, 'reasons': sorted(reasons)}
        if details:
            ineligible_row['details'] = details
        return None, ineligible_row

    return {
        'turn': turn,
        'working_path': f'{TURNS_RELATIVE}/{turn}',
        'request_sha256': binding['request_sha256'],
        'context_sha256': binding['context_sha256'],
        'return_sha256': returned['return_sha256'],
        'declared_targets': declared_targets,
        'published_targets': complete_targets,
        'files': file_count,
        'bytes': total_bytes,
        'working_dir_sha256': dir_sha,
    }, None


def plan_turn_retirement(system, *, turns: list[str] | tuple[str, ...] | None = None,
                         all_eligible: bool = False) -> dict[str, Any]:
    """Audit candidate working copies under work/.system/turns/ and return a hashed retirement plan.

    Read-only. Caller should hold LOCK_SH or LOCK_EX on exchange/owner.lock.
    """
    require(type(all_eligible) is bool, 'all_eligible must be Boolean')
    if all_eligible:
        require(turns is None or len(turns) == 0,
                'supply either explicit turns or all_eligible=True, not both')
    else:
        require(isinstance(turns, (list, tuple)) and len(turns) > 0,
                'nominate at least one turn or pass all_eligible=True')

    turns_root = system.ex.path(TURNS_RELATIVE)
    if turns_root.exists() or turns_root.is_symlink():
        root_info = turns_root.lstat()
        require(not stat.S_ISLNK(root_info.st_mode) and stat.S_ISDIR(root_info.st_mode),
                'ordinary .system/turns directory required')

    candidates: list[tuple[str, Path]] = []
    ineligible: list[dict[str, Any]] = []
    nominated_turns: list[str] = []

    if all_eligible:
        if turns_root.is_dir():
            for entry in sorted(turns_root.iterdir(), key=lambda p: p.name):
                if entry.name.startswith('.'):
                    continue
                nominated_turns.append(entry.name)
                try:
                    valid_name = packet_id(entry.name)
                except ValueError as exc:
                    ineligible.append({
                        'turn': entry.name,
                        'reasons': ['invalid_turn_identity'],
                        'details': {'invalid_turn_identity': str(exc)},
                    })
                    continue
                info = entry.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    ineligible.append({
                        'turn': valid_name,
                        'reasons': ['symlink_or_non_directory'],
                    })
                    continue
                candidates.append((valid_name, entry))
    else:
        seen: set[str] = set()
        for raw_turn in turns:
            require(isinstance(raw_turn, str), 'turn identity must be a string')
            turn = packet_id(raw_turn)
            require(turn not in seen, f'duplicate nominated turn: {turn}')
            seen.add(turn)
            turn_dir = turns_root / turn
            try:
                info = turn_dir.lstat()
            except FileNotFoundError:
                raise ValueError(f'unknown turn working directory: {turn}') from None
            require(not stat.S_ISLNK(info.st_mode) and stat.S_ISDIR(info.st_mode),
                    f'ordinary turn working directory required (symlinks refused): {turn}')
            nominated_turns.append(turn)
            candidates.append((turn, turn_dir))

    candidate_set = {name for name, _ in candidates}
    record_refs, record_scan_errors = _collect_record_references(system, candidate_set)
    packet_refs, packet_scan_errors = _collect_cross_packet_references(system, candidate_set)
    attention_refs, attention_error = _collect_attention_references(system, candidate_set)
    interp_refs, interp_errors = _collect_interpretation_references(system, candidate_set)
    uncommitted_refs, uncommitted_errors = _collect_uncommitted_turn_references(system, turns_root, candidate_set)

    eligible: list[dict[str, Any]] = []
    for turn, turn_dir in sorted(candidates, key=lambda item: item[0]):
        ok_row, bad_row = _evaluate_turn(
            system,
            turn,
            turn_dir,
            record_refs=record_refs,
            record_scan_errors=record_scan_errors,
            packet_refs=packet_refs,
            packet_scan_errors=packet_scan_errors,
            attention_refs=attention_refs,
            attention_error=attention_error,
            interp_refs=interp_refs,
            interp_errors=interp_errors,
            uncommitted_refs=uncommitted_refs,
            uncommitted_errors=uncommitted_errors,
        )
        if ok_row is not None:
            eligible.append(ok_row)
        else:
            ineligible.append(bad_row)

    eligible.sort(key=lambda r: r['turn'])
    ineligible.sort(key=lambda r: r['turn'])
    plan_body = {
        'format': PLAN_FORMAT,
        'instance_id': system.instance_id,
        'selection_mode': 'all-eligible' if all_eligible else 'explicit',
        'nominated_turns': sorted(nominated_turns),
        'eligible': eligible,
        'ineligible': ineligible,
        'reclaimable_bytes': sum(row['bytes'] for row in eligible),
        'reclaimable_files': sum(row['files'] for row in eligible),
        'scope': (
            'Read-only working-copy retirement plan under MAINTENANCE.md guards; '
            'exchange packets, objects, and native records remain untouched.'
        ),
    }
    return dict(plan_body, plan_sha256=sha(encode(plan_body)))


def _validate_plan(system, plan: Any, expected_plan: Any) -> tuple[str, list[dict[str, Any]]]:
    require(isinstance(plan, dict), 'turn retirement plan must be an object')
    json.dumps(plan, allow_nan=False)
    exact_fields(plan, _PLAN_REQUIRED, set(), 'turn retirement plan')
    require(plan['format'] == PLAN_FORMAT, 'unsupported turn retirement plan format')
    require(plan['instance_id'] == system.instance_id, 'turn retirement plan targets a different instance')
    require(plan['selection_mode'] in ('all-eligible', 'explicit'), 'unsupported turn retirement selection_mode')
    require(isinstance(plan['nominated_turns'], list) and isinstance(plan['ineligible'], list),
            'turn retirement plan lists must be arrays')
    expected_sha = digest(expected_plan, 'expected_plan')
    declared_sha = digest(plan['plan_sha256'], 'plan_sha256')
    computed_sha = sha(encode({k: v for k, v in plan.items() if k != 'plan_sha256'}))
    require(declared_sha == computed_sha and computed_sha == expected_sha,
            'turn retirement plan digest mismatch')
    eligible = plan['eligible']
    require(isinstance(eligible, list) and len(eligible) > 0,
            'turn retirement plan has no eligible turns to retire')
    seen: set[str] = set()
    for row in eligible:
        exact_fields(row, _ELIGIBLE_FIELDS, set(), 'turn retirement plan eligible entry')
        turn = packet_id(row['turn'])
        require(turn not in seen, f'duplicate eligible turn in plan: {turn}')
        seen.add(turn)
        require(row['working_path'] == f'{TURNS_RELATIVE}/{turn}',
                f'invalid working_path for turn {turn}')
        digest(row['request_sha256'], 'request_sha256')
        digest(row['context_sha256'], 'context_sha256')
        digest(row['return_sha256'], 'return_sha256')
        digest(row['working_dir_sha256'], 'working_dir_sha256')
        require(type(row['files']) is int and row['files'] >= 0, 'invalid file count in plan')
        require(type(row['bytes']) is int and row['bytes'] >= 0, 'invalid byte count in plan')
    require(type(plan['reclaimable_files']) is int and type(plan['reclaimable_bytes']) is int
            and plan['reclaimable_files'] == sum(r['files'] for r in eligible)
            and plan['reclaimable_bytes'] == sum(r['bytes'] for r in eligible),
            'turn retirement plan totals differ from eligible entries')
    return computed_sha, eligible


def _remove_dir_fd(parent_fd: int, name: str, display_prefix: str) -> None:
    """Recursively remove a verified non-symlink directory subtree via dir_fd."""
    child_fd = open_directory(name, dir_fd=parent_fd)
    try:
        for entry in sorted(os.listdir(child_fd)):
            entry_info = os.lstat(entry, dir_fd=child_fd)
            mode = entry_info.st_mode
            if stat.S_ISLNK(mode):
                raise ValueError(f'refused symlink during turn removal: {display_prefix}/{entry}')
            if stat.S_ISDIR(mode):
                _remove_dir_fd(child_fd, entry, f'{display_prefix}/{entry}')
            elif stat.S_ISREG(mode):
                os.unlink(entry, dir_fd=child_fd)
            else:
                raise ValueError(f'refused special file during turn removal: {display_prefix}/{entry}')
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _safe_remove_turn_dir(system, turn: str) -> None:
    """Remove only work/.system/turns/<turn> without following any symlink."""
    turn = packet_id(turn)
    turns_root = system.ex.path(TURNS_RELATIVE)
    target = turns_root / turn
    require(target.parent == turns_root and target.is_relative_to(turns_root),
            f'turn path escapes {TURNS_RELATIVE}: {turn}')
    info = target.lstat()
    require(is_ordinary_dir_stat(info), f'ordinary turn working directory required: {turn}')

    if hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
        with system.ex.open_workspace_dir(TURNS_RELATIVE) as turns_fd:
            _remove_dir_fd(turns_fd, turn, f'{TURNS_RELATIVE}/{turn}')
        sync_dir(turns_root)
    else:
        def remove_fallback(current: Path) -> None:
            for child in sorted(current.iterdir(), key=lambda p: p.name):
                cinfo = child.lstat()
                if is_reparse_or_symlink(cinfo):
                    raise ValueError(f'refused symlink during turn removal: {child}')
                if stat.S_ISDIR(cinfo.st_mode):
                    remove_fallback(child)
                elif stat.S_ISREG(cinfo.st_mode):
                    child.unlink()
                else:
                    raise ValueError(f'refused special file during turn removal: {child}')
            current.rmdir()

        remove_fallback(target)


def execute_turn_retirement(system, *, plan: dict[str, Any], expected_plan: str,
                            reason: str) -> dict[str, Any]:
    """Verify plan and live MAINTENANCE.md guards under LOCK_EX, remove working copies, verify and record receipt.

    Mutating. Caller must hold LOCK_EX on exchange/owner.lock.
    """
    reason = nonempty(reason, 'turn retirement reason')
    plan_sha, planned_eligible = _validate_plan(system, plan, expected_plan)

    receipt_dir = system.ex.path(RECEIPT_DIRECTORY)
    receipt_path = system.ex.path(f'{RECEIPT_DIRECTORY}/{plan_sha}.json')
    if receipt_path.exists() or receipt_path.is_symlink():
        info = receipt_path.lstat()
        require(not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode),
                'ordinary turn retirement receipt required: ' + str(receipt_path))
        existing = _read_strict_file(receipt_path)
        if existing.get('complete'):
            raise ValueError('turn retirement plan already executed; inspect its receipt')
        raise ValueError('interrupted turn retirement detected; inspect receipt and re-plan remaining turns')

    # Re-run ALL 4 MAINTENANCE.md guards live under LOCK_EX to close TOCTOU drift.
    nominated = [row['turn'] for row in planned_eligible]
    fresh = plan_turn_retirement(system, turns=nominated, all_eligible=False)
    if fresh['ineligible']:
        details = '; '.join(f"{r['turn']} ({', '.join(r['reasons'])})" for r in fresh['ineligible'])
        raise ValueError('planned turns became ineligible before execution: ' + details)

    fresh_by_turn = {row['turn']: row for row in fresh['eligible']}
    for expected_row in planned_eligible:
        turn = expected_row['turn']
        observed = fresh_by_turn.get(turn)
        require(observed == expected_row,
                f'turn working copy or packet changed since planning: {turn}')

    pre_status = system.status()
    pre_turns = {row['packet']: row for row in pre_status['turns']}
    for expected_row in planned_eligible:
        turn = expected_row['turn']
        require(turn not in pre_status['incomplete_turns'],
                f'turn appears in incomplete_turns before retirement: {turn}')
        require(pre_turns.get(turn, {}).get('transport') == 'returned',
                f'turn transport is not returned in native status: {turn}')

    # Write durable in-progress receipt before removing working directories.
    if receipt_dir.exists() or receipt_dir.is_symlink():
        rinfo = receipt_dir.lstat()
        require(not stat.S_ISLNK(rinfo.st_mode) and stat.S_ISDIR(rinfo.st_mode),
                'ordinary turn retirement receipt directory required')
    receipt_dir.mkdir(parents=True, exist_ok=True)
    sync_dir(receipt_dir.parent)
    retired_rows: list[dict[str, Any]] = []
    receipt: dict[str, Any] = {
        'format': RECEIPT_FORMAT,
        'instance_id': system.instance_id,
        'plan_sha256': plan_sha,
        'reason': reason,
        'complete': False,
        'retired_at': now(),
        'retired_turns': [],
        'turns': [],
        'retired_count': 0,
        'files_removed': 0,
        'bytes_reclaimed': 0,
        'post_verification': {
            'all_views_verified': False,
            'all_recovers_verified': False,
            'all_explain_contexts_verified': False,
            'status_verified': False,
        },
        'receipt_path': f'{RECEIPT_DIRECTORY}/{plan_sha}.json',
        'scope': (
            'Completed working copies removed under work/.system/turns/ only; '
            'exchange packets, immutable objects, and native records remain intact and verified.'
        ),
    }
    write_json(receipt_path, receipt)

    turns_root = system.ex.path(TURNS_RELATIVE)
    for expected_row in planned_eligible:
        turn = expected_row['turn']
        _safe_remove_turn_dir(system, turn)
        remaining = turns_root / turn
        require(not remaining.exists() and not remaining.is_symlink(),
                f'turn working directory remained after removal: {turn}')

        view_doc = system.view(turn)
        require(isinstance(view_doc, dict), f'post-removal view failed for {turn}')
        recovered = system.recover(turn)
        require(recovered.get('request_sha256') == expected_row['request_sha256']
                and recovered.get('context_sha256') == expected_row['context_sha256'],
                f'post-removal recover binding failed for {turn}')
        explained = system.explain_context(turn)
        require(explained.get('request_sha256') == expected_row['request_sha256']
                and explained.get('context_sha256') == expected_row['context_sha256'],
                f'post-removal explain_context binding failed for {turn}')

        retired_rows.append({
            'turn': turn,
            'working_path': expected_row['working_path'],
            'files_removed': expected_row['files'],
            'bytes_reclaimed': expected_row['bytes'],
            'working_dir_sha256': expected_row['working_dir_sha256'],
            'request_sha256': expected_row['request_sha256'],
            'context_sha256': expected_row['context_sha256'],
            'return_sha256': expected_row['return_sha256'],
            'view_verified': True,
            'recover_verified': True,
            'explain_context_verified': True,
        })
        receipt.update(
            retired_turns=[r['turn'] for r in retired_rows],
            turns=list(retired_rows),
            retired_count=len(retired_rows),
            files_removed=sum(r['files_removed'] for r in retired_rows),
            bytes_reclaimed=sum(r['bytes_reclaimed'] for r in retired_rows),
        )
        write_json(receipt_path, receipt)

    post_status = system.status()
    post_turns = {row['packet']: row for row in post_status['turns']}
    require(post_status.get('current_goal', {}).get('sha256') == pre_status.get('current_goal', {}).get('sha256'),
            'current goal changed during turn retirement')
    require(post_status['incomplete_turns'] == pre_status['incomplete_turns'],
            'incomplete_turns changed during turn retirement')
    require(post_status['gaps']['changed_record_basis'] == pre_status['gaps']['changed_record_basis'],
            'changed_record_basis drifted during turn retirement')
    require(post_status['gaps']['pending_returns'] == pre_status['gaps']['pending_returns'],
            'pending_returns drifted during turn retirement')
    require(post_status['gaps']['whole_goal_requires_interpretation']
            == pre_status['gaps']['whole_goal_requires_interpretation'],
            'whole_goal_requires_interpretation drifted during turn retirement')
    require(post_status['current_use']['evidence_changes'] == pre_status['current_use']['evidence_changes']
            and post_status['current_use']['needs_interpretation'] == pre_status['current_use']['needs_interpretation']
            and post_status['current_use']['reassessment_reasons'] == pre_status['current_use']['reassessment_reasons'],
            'current_use interpretation state drifted during turn retirement')
    for row in retired_rows:
        require(post_turns.get(row['turn'], {}).get('transport') == 'returned',
                f'post-removal status lost returned transport for {row["turn"]}')

    receipt['post_verification'] = {
        'all_views_verified': True,
        'all_recovers_verified': True,
        'all_explain_contexts_verified': True,
        'status_verified': True,
    }
    receipt['complete'] = True
    receipt['retired_at'] = now()
    write_json(receipt_path, receipt)
    return receipt
