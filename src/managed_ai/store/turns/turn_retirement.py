"""Guarded two-phase retirement of completed turn working copies under work/.system/turns/.

Implements the MAINTENANCE.md working-copy retirement contract without touching
exchange/packets/, exchange/objects/, or native records.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import posixpath
import stat
from typing import Any

from managed_ai.core.exchange.carrier_receipts import transport_outcome
from managed_ai.core.exchange.publication_receipts import (
    publication_inventory,
    publication_matches,
    publication_name,
    validate_publication_record,
)
from managed_ai.core.formats import (
    TURN_RETIRE_PLAN_FORMAT as PLAN_FORMAT,
    TURN_RETIRE_RECEIPT_FORMAT as RECEIPT_FORMAT,
    TURN_SELECTION_FORMAT,
    validate_optional_format,
)
from managed_ai.core.ownership import packet_id
from managed_ai.core.storage import (
    MAX_BYTES,
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
from managed_ai.store.protocol.identities import digest, exact_fields, nonempty, require, strict_json
from managed_ai.store.turns.preparation_journal import feedback_id
from managed_ai.store.turns.turn_retirement_scan import (
    _collect_attention_references,
    _collect_cross_packet_references,
    _collect_interpretation_references,
    _collect_record_references,
    _collect_uncommitted_turn_references,
    _scan_turn_directory,
)

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


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _check_packet_and_return(system, turn: str, refuse) -> tuple[Path, dict | None, dict | None, dict | None]:
    """Guard (a): Packet exists, validates cleanly, and has a strict validated return.json."""
    packet_dir = system.ex.directory(turn)
    staged_dir = system.ex.prepared_directory(turn)
    if _lstat_or_none(staged_dir) is not None:
        refuse('staged_preparation_present', str(staged_dir.name))

    binding = None
    request = None
    returned = None
    pkt_info = _lstat_or_none(packet_dir)
    if pkt_info is None or not is_ordinary_dir_stat(pkt_info):
        refuse('packet_missing')
        return packet_dir, binding, request, returned

    try:
        binding = _read_strict_file(packet_dir / 'binding.json')
        request = _read_strict_file(packet_dir / 'request.json')
        loaded_dir, loaded_req = system.ex.load(turn)
        require(loaded_dir == packet_dir and loaded_req == request,
                'loaded packet differs from strict request')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        refuse('packet_invalid', str(exc))
        return packet_dir, None, None, None

    try:
        outcome = transport_outcome(system.ex, packet_dir, request)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        refuse('transport_outcome_unreadable', str(exc))
        outcome = None
    if outcome is not None:
        refuse('terminal_without_return', outcome.get('reason'))

    return_path = packet_dir / 'return.json'
    ret_info = _lstat_or_none(return_path)
    if ret_info is None or not is_ordinary_file_stat(ret_info):
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
    return packet_dir, binding, request, returned


def _check_publications(turn: str, turn_dir: Path, packet_dir: Path, binding, request, returned, refuse):
    """Guard (b): Clean preparation state AND 100% of declared targets returned and fully published."""
    if _lstat_or_none(turn_dir / 'attention-draft') is not None:
        refuse('uncommitted_attention_draft')

    prep_path = turn_dir / 'preparation.json'
    prep_state = None
    if _lstat_or_none(prep_path) is not None:
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
                require(isinstance(target, str) and slot_name == publication_name(target),
                        f'non-canonical publication slot: {slot_name}')
                require(publication_matches(turn, request, pub_record, returned),
                        f'publication binding mismatch: {slot_name}')
                validate_publication_record(pub_record)
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
    return prep_state, declared_targets, complete_targets


def _check_live_references(
    turn: str,
    refuse,
    *,
    record_refs, record_scan_errors,
    packet_refs, packet_scan_errors,
    attention_refs, attention_error,
    interp_refs, interp_errors,
    uncommitted_refs, uncommitted_errors,
) -> None:
    """Guard (c): No live structural reference from records, packets, Attention, interps, or uncommitted turns."""
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


def _check_working_copy_integrity(
    system, turn: str, turn_dir: Path, binding, request, returned,
    prep_state, declared_targets: list[str],
    dir_issues: list[str], file_hashes: dict[str, str], dir_paths: list[str], refuse,
) -> None:
    """Guard (d): Verify 100% captured recoverability and working-copy byte correspondence."""
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

    if dir_issues:
        return

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
        modified_files.append('request.json')

    if 'attention-spec.json' in file_hashes:
        if isinstance(work_request_doc, dict) and 'attention' in work_request_doc:
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

    file_count, total_bytes, dir_sha, dir_issues, file_hashes, dir_paths = _scan_turn_directory(turn_dir)
    if dir_issues:
        refuse('working_dir_unsafe_entries', dir_issues)

    packet_dir, binding, request, returned = _check_packet_and_return(system, turn, refuse)
    prep_state, declared_targets, complete_targets = _check_publications(
        turn, turn_dir, packet_dir, binding, request, returned, refuse,
    )
    _check_live_references(
        turn, refuse,
        record_refs=record_refs, record_scan_errors=record_scan_errors,
        packet_refs=packet_refs, packet_scan_errors=packet_scan_errors,
        attention_refs=attention_refs, attention_error=attention_error,
        interp_refs=interp_refs, interp_errors=interp_errors,
        uncommitted_refs=uncommitted_refs, uncommitted_errors=uncommitted_errors,
    )
    if request is not None and returned is not None:
        _check_working_copy_integrity(
            system, turn, turn_dir, binding, request, returned,
            prep_state, declared_targets, dir_issues, file_hashes, dir_paths, refuse,
        )

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
                'supply either --turn TURN or --all-eligible, not both')
    else:
        require(isinstance(turns, (list, tuple)) and len(turns) > 0,
                'nominate at least one turn (--turn TURN) or pass --all-eligible')

    turns_root = system.ex.path(TURNS_RELATIVE)
    root_info = _lstat_or_none(turns_root)
    if root_info is not None:
        require(is_ordinary_dir_stat(root_info),
                'ordinary .system/turns directory required')

    candidates: list[tuple[str, Path]] = []
    ineligible: list[dict[str, Any]] = []
    nominated_turns: list[str] = []

    if all_eligible:
        if root_info is not None:
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
                if not is_ordinary_dir_stat(info):
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
            require(is_ordinary_dir_stat(info),
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
    receipt_info = _lstat_or_none(receipt_path)
    if receipt_info is not None:
        require(is_ordinary_file_stat(receipt_info),
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
    rdir_info = _lstat_or_none(receipt_dir)
    if rdir_info is not None:
        require(is_ordinary_dir_stat(rdir_info),
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
        require(_lstat_or_none(remaining) is None,
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
