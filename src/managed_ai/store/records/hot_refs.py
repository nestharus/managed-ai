"""Shared fail-closed structural reference scanners for turn retirement and cold archival.

Single canonical owner for scanning live hot dependencies across:
  1. work/interpretations/*.json
  2. exchange/packets/** (staged .sealed-*/.prepare-* and committed packets)
  3. exchange/attention/index.json (field, active item basis, active latest assessment products)
  4. work/.system/turns/<turn>/ (uncommitted active turn staging requests & selections)
"""
from __future__ import annotations

from pathlib import Path
import posixpath
from typing import Any, Callable

from managed_ai.core.exchange.carrier_receipts import transport_outcome
from managed_ai.core.exchange.publication_receipts import (
    publication_name,
    publication_inventory,
    publication_matches,
)
from managed_ai.core.storage import (
    MAX_BYTES,
    is_ordinary_dir_stat,
    is_ordinary_file_stat,
    read_bytes,
    read_json,
)
from managed_ai.store.protocol.identities import require, strict_json


def normalize_work_relpath(path_value: Any) -> str | None:
    """Normalize a workspace-relative path string (stripping leading ./ or work/)."""
    if not isinstance(path_value, str):
        return None
    raw = path_value.strip().replace('\\', '/')
    if not raw:
        return None
    normalized = posixpath.normpath(raw)
    while normalized.startswith('./'):
        normalized = normalized[2:]
    if normalized.startswith('work/'):
        normalized = normalized[len('work/'):]
    return normalized


def extract_turn_ref(path_value: Any) -> str | None:
    """Return the turn name if a structural path points into .system/turns/<turn>."""
    normalized = normalize_work_relpath(path_value)
    if normalized is None or not normalized.startswith('.system/turns/'):
        return None
    rest = normalized[len('.system/turns/'):]
    turn_name = rest.split('/', 1)[0]
    if turn_name and turn_name not in ('.', '..'):
        return turn_name
    return None


def extract_hot_record_id(path_value: Any) -> str | None:
    """Return record ID if a structural path points to hot records/<id>.json."""
    normalized = normalize_work_relpath(path_value)
    if normalized is None:
        return None
    if normalized.startswith('records/') and normalized.endswith('.json'):
        leaf = normalized[len('records/'):-5]
        if leaf and '/' not in leaf and leaf not in ('.', '..'):
            return leaf
    return None


def packet_is_open(system, pkt_dir: Path, pkt_name: str, req: dict[str, Any]) -> bool:
    """Return True when a committed packet can still observe workspace files on disk at publish()."""
    ret_path = pkt_dir / 'return.json'
    if not ret_path.exists() and not ret_path.is_symlink():
        outcome = transport_outcome(system.ex, pkt_dir, req)
        return outcome is None
    ret = read_json(ret_path)
    publications, reconciliations, unreadable = publication_inventory(pkt_dir)
    if unreadable:
        return True
    declared_targets = {
        row['path'] for row in req.get('targets', ())
        if isinstance(row, dict) and isinstance(row.get('path'), str)
    }
    proposed_targets = {
        row['path'] for row in ret.get('proposals', ())
        if isinstance(row, dict) and isinstance(row.get('path'), str)
    }
    reconciled_intents = {
        rec.get('intent_sha256') for rec, _ in reconciliations.values()
        if isinstance(rec, dict) and isinstance(rec.get('intent_sha256'), str)
    }
    completed_targets: set[str] = set()
    for _, (pub, pub_digest) in publications.items():
        if not isinstance(pub, dict):
            return True
        phase = pub.get('phase')
        target = pub.get('path')
        if phase == 'complete':
            if isinstance(target, str) and publication_matches(pkt_name, req, pub, ret):
                completed_targets.add(target)
        else:
            canonical = publication_name(target, 'reconcile') if isinstance(target, str) else None
            if pub_digest in reconciled_intents or (canonical and canonical in reconciliations):
                if isinstance(target, str):
                    completed_targets.add(target)
            else:
                return True
    return bool((declared_targets & proposed_targets) - completed_targets)


def scan_interpretation_refs(
    system,
    extractor: Callable[[Any], str | None],
    *,
    fail_closed: bool = False,
) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    """Scan work/interpretations/*.json evidence entries (snapshot dicts or strings)."""
    refs: dict[str, list[dict[str, str]]] = {}
    errors: list[str] = []
    interp_dir = system.work / 'interpretations'
    if not interp_dir.exists() and not interp_dir.is_symlink():
        return refs, errors
    info = interp_dir.lstat()
    require(is_ordinary_dir_stat(info), 'ordinary interpretations directory required')
    for path in sorted(interp_dir.iterdir(), key=lambda p: p.name):
        if path.name.startswith('.') or not path.name.endswith('.json'):
            continue
        try:
            p_info = path.lstat()
            if not is_ordinary_file_stat(p_info):
                raise ValueError(f'symlink interpretation file: {path.name}')
            doc = strict_json(read_bytes(path, MAX_BYTES))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if fail_closed:
                raise
            errors.append(f'unreadable interpretation {path.name}: {exc}')
            continue
        evidence = doc.get('evidence', [])
        if not isinstance(evidence, list):
            msg = f'malformed evidence list in interpretation {path.name}'
            if fail_closed:
                raise ValueError(msg)
            errors.append(msg)
            continue
        for ev in evidence:
            if isinstance(ev, dict):
                ev_path = ev.get('path')
                if not isinstance(ev_path, str):
                    msg = f'malformed evidence entry in interpretation {path.name}'
                    if fail_closed:
                        raise ValueError(msg)
                    errors.append(msg)
                    continue
            elif isinstance(ev, str):
                ev_path = ev
            else:
                msg = f'malformed evidence entry in interpretation {path.name}'
                if fail_closed:
                    raise ValueError(msg)
                errors.append(msg)
                continue
            target = extractor(ev_path)
            if target is not None:
                refs.setdefault(target, []).append({'interpretation': path.name, 'path': ev_path})
    for rows in refs.values():
        rows.sort(key=lambda r: (r['interpretation'], r['path']))
    return refs, errors


def scan_packet_refs(
    system,
    extractor: Callable[[Any], str | None],
    *,
    open_only: bool = False,
    skip_self_packet: bool = False,
    fail_closed: bool = False,
) -> tuple[dict[str, list[dict[str, str]]], set[str], list[str]]:
    """Scan exchange/packets/** (staged .sealed-*/.prepare-* and committed packets)."""
    refs: dict[str, list[dict[str, str]]] = {}
    committed: set[str] = set()
    errors: list[str] = []
    packets_root = system.ex.root / 'packets'
    if not packets_root.exists() and not packets_root.is_symlink():
        return refs, committed, errors
    require(is_ordinary_dir_stat(packets_root.lstat()), 'ordinary packets directory required')
    for pkt_dir in sorted(packets_root.iterdir(), key=lambda p: p.name):
        entry_name = pkt_dir.name
        staged = False
        if entry_name.startswith('.sealed-'):
            pkt_name = entry_name[len('.sealed-'):]
            staged = True
        elif entry_name.startswith('.prepare-'):
            pkt_name = entry_name
            staged = True
        elif entry_name.startswith('.'):
            continue
        else:
            pkt_name = entry_name
        info = pkt_dir.lstat()
        if not is_ordinary_dir_stat(info):
            msg = f'non-directory packet entry: {entry_name}'
            if fail_closed:
                raise ValueError(msg)
            errors.append(msg)
            continue
        req_path = pkt_dir / 'request.json'
        if entry_name.startswith('.prepare-') and not req_path.exists() and not req_path.is_symlink():
            msg = f'in-flight packet staging directory present: {entry_name}'
            if fail_closed:
                raise ValueError(msg)
            errors.append(msg)
            continue
        try:
            request = strict_json(read_bytes(req_path, MAX_BYTES))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if fail_closed:
                raise
            errors.append(f'unreadable packet request {entry_name}: {exc}')
            continue
        if not staged:
            committed.add(entry_name)
        elif isinstance(request.get('packet'), str) and request['packet']:
            pkt_name = request['packet']
        if open_only and not staged and not packet_is_open(system, pkt_dir, entry_name, request):
            continue
        for role in ('brief', 'methods', 'materials', 'targets'):
            rows = [request[role]] if role == 'brief' and isinstance(request.get('brief'), dict) else request.get(role, [])
            if not isinstance(rows, list):
                msg = f'malformed {role} in packet {entry_name}'
                if fail_closed:
                    raise ValueError(msg)
                errors.append(msg)
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                raw_path = row.get('path')
                target = extractor(raw_path)
                if target is not None and (not skip_self_packet or target != pkt_name):
                    refs.setdefault(target, []).append({
                        'packet': entry_name,
                        'role': role,
                        'path': raw_path,
                    })
    for rows in refs.values():
        rows.sort(key=lambda r: (r['packet'], r['role'], r['path']))
    return refs, committed, errors


def scan_attention_refs(
    system,
    extractor: Callable[[Any], str | None],
    *,
    fail_closed: bool = False,
) -> tuple[dict[str, list[dict[str, str]]], str | None]:
    """Scan active native Attention field, item basis, and latest assessment products."""
    refs: dict[str, list[dict[str, str]]] = {}
    att_index = system.ex.root / 'attention/index.json'
    if not att_index.exists() and not att_index.is_symlink():
        return refs, None
    try:
        state = system.attention._load()
        field_target = extractor(state.get('field'))
        if field_target is not None:
            refs.setdefault(field_target, []).append({'owner': 'attention.field', 'path': state['field']})
        active_items: set[str] = set()
        for item_id, obj_key in sorted(state.get('items', {}).items()):
            spec = system.attention._get(obj_key)
            if not spec.get('active', True):
                continue
            active_items.add(item_id)
            for path in spec.get('basis', []):
                target = extractor(path)
                if target is not None:
                    refs.setdefault(target, []).append({
                        'owner': 'attention.item',
                        'item': item_id,
                        'path': path,
                    })
        if active_items and state.get('receipts'):
            current = system.attention._current(state)
            receipt_cache: dict[int, dict[str, Any]] = {}
            latest_map = current.get('latest', {})
            for item_id in sorted(active_items):
                bases = latest_map.get(item_id, {})
                if not isinstance(bases, dict):
                    raise ValueError(f'malformed attention latest entries for {item_id}')
                for basis_key in sorted(bases):
                    pos, offset = bases[basis_key]
                    if pos not in receipt_cache:
                        receipt_cache[pos] = system.attention._get(state['receipts'][pos])
                    receipt_doc = receipt_cache[pos]
                    assessment = receipt_doc['assessments'][offset]
                    products = assessment.get('products', [])
                    if not isinstance(products, list):
                        raise ValueError(f'malformed attention assessment products for {item_id}')
                    for product in products:
                        prod_path = product.get('path') if isinstance(product, dict) else product
                        target = extractor(prod_path)
                        if target is not None:
                            refs.setdefault(target, []).append({
                                'owner': 'attention.assessment.product',
                                'item': item_id,
                                'receipt': str(receipt_doc.get('id', '')),
                                'path': prod_path,
                            })
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        if fail_closed:
            raise
        return refs, str(exc)
    for rows in refs.values():
        rows.sort(key=lambda r: (r['owner'], r.get('item', ''), r.get('receipt', ''), r['path']))
    return refs, None


def scan_uncommitted_turn_refs(
    system,
    committed_packets: set[str],
    extractor: Callable[[Any], str | None],
    *,
    include_record_ids: bool = False,
    skip_self_turn: bool = False,
    fail_closed: bool = False,
) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    """Scan active uncommitted turn directories under work/.system/turns/."""
    refs: dict[str, list[dict[str, str]]] = {}
    errors: list[str] = []
    turns_root = system.work / '.system/turns'
    if not turns_root.exists() and not turns_root.is_symlink():
        return refs, errors
    require(is_ordinary_dir_stat(turns_root.lstat()), 'ordinary turns working directory required')
    for entry in sorted(turns_root.iterdir(), key=lambda p: p.name):
        if entry.name.startswith('.'):
            continue
        other = entry.name
        try:
            info = entry.lstat()
        except OSError as exc:
            if fail_closed:
                raise
            errors.append(f'unreadable turn directory entry {other}: {exc}')
            continue
        if not is_ordinary_dir_stat(info):
            msg = f'non-directory turn working entry: {other}'
            if fail_closed:
                raise ValueError(msg)
            errors.append(msg)
            continue
        if other in committed_packets:
            continue
        prep_file = entry / 'preparation.json'
        if prep_file.exists() or prep_file.is_symlink():
            try:
                prep = strict_json(read_bytes(prep_file, MAX_BYTES))
            except (OSError, ValueError) as exc:
                if fail_closed:
                    raise
                errors.append(f'unreadable preparation in uncommitted turn {other}: {exc}')
                continue
            if prep.get('phase') == 'abandoned':
                continue
        req_file = entry / 'request.json'
        if req_file.exists() or req_file.is_symlink():
            try:
                if not is_ordinary_file_stat(req_file.lstat()):
                    raise ValueError(f'symlink request file in uncommitted turn {other}')
                req = strict_json(read_bytes(req_file, MAX_BYTES))
            except (OSError, ValueError) as exc:
                if fail_closed:
                    raise
                errors.append(f'unreadable request in uncommitted turn {other}: {exc}')
                continue
            if include_record_ids:
                for rid in req.get('records', ()):
                    if isinstance(rid, str) and rid:
                        refs.setdefault(rid, []).append({
                            'uncommitted_turn': other, 'field': 'records', 'path': f'records/{rid}.json',
                        })
                for comp in req.get('compositions', ()):
                    if isinstance(comp, dict) and isinstance(comp.get('record'), str) and comp['record']:
                        rid = comp['record']
                        refs.setdefault(rid, []).append({
                            'uncommitted_turn': other, 'field': 'compositions', 'path': f'records/{rid}.json',
                        })
            for field in ('sources', 'targets', 'methods', 'materials'):
                values = req.get(field, [])
                if isinstance(values, list):
                    for val in values:
                        raw_path = val.get('path') if isinstance(val, dict) else val
                        target = extractor(raw_path)
                        if target is not None and (not skip_self_turn or target != other):
                            refs.setdefault(target, []).append({
                                'uncommitted_turn': other, 'field': field, 'path': raw_path,
                            })
            brief_val = req.get('brief')
            brief_path = brief_val.get('path') if isinstance(brief_val, dict) else brief_val
            brief_target = extractor(brief_path)
            if brief_target is not None and (not skip_self_turn or brief_target != other):
                refs.setdefault(brief_target, []).append({
                    'uncommitted_turn': other, 'field': 'brief', 'path': brief_path,
                })
        if include_record_ids:
            sel_file = entry / 'selection.json'
            if sel_file.exists() or sel_file.is_symlink():
                try:
                    sel = strict_json(read_bytes(sel_file, MAX_BYTES))
                except (OSError, ValueError) as exc:
                    if fail_closed:
                        raise
                    errors.append(f'unreadable selection in uncommitted turn {other}: {exc}')
                    continue
                view = sel.get('view')
                if isinstance(view, dict):
                    for rid in view.get('selected_records', ()):
                        if isinstance(rid, str) and rid:
                            refs.setdefault(rid, []).append({
                                'uncommitted_turn': other, 'field': 'selection.view.selected_records',
                                'path': f'records/{rid}.json',
                            })
    for rows in refs.values():
        rows.sort(key=lambda r: (r['uncommitted_turn'], r['field'], r['path']))
    return refs, errors
