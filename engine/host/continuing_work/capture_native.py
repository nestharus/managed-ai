#!/usr/bin/env python3
"""Host-owned selected capture for an explicitly reviewed native implementation.

Exchange owns the current packet summary. Earlier pinned engines require their
original single-packet projection. Requalify after any native program change.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


REVIEWED_NATIVE_PROFILE = 'f65119d9a2751f7927ecfb502df1f26c07ee1c958c9b219aeb13f6efc1c6565e'
REORGANIZED_NATIVE_PROFILE = '65d21e3482ae5ca9a4a9a3060e8d2c60e98bc95e224f6969dbed198d317910aa'
REPAIRED_NATIVE_PROFILE = '7b6176300e8802b9c2714bf9cd9ebdfa8d2741b41d21c680c39f5bf5a32784e3'
COMMAND_ROUTES_NATIVE_PROFILE = 'fb535b60e0d22b6fc482f84dcd4482ecce2dda52fa6e06ee5915268a4cc3f621'
BODY_REPAIRS_NATIVE_PROFILE = 'e92245c09bc33a22e7aa086be2ae141093dd19bf2302f69d53c771e5e8e23cc9'
SCHEMA_REPAIRS_NATIVE_PROFILE = '5f0830029a210dd73fd3299a3a6f5c170a2802dadf85b3076b5640831774d836'
LIFECYCLE_NATIVE_PROFILE = 'e031f4e246801e7ea5d41306c7356891872af6444f7fd9b998bacd48ea67945a'
OUTLIER_REPAIRS_NATIVE_PROFILE = '9b879473fc1a7a46b530027d5f7045101b05183b9742027861af15d3f145e6ea'
SCOPED_COMPOSITION_NATIVE_PROFILE = '6bb020ad0a1feacaab040b7b8bfe448e9f472525902bad1203344d2ec2f436f4'
JOINED_NATIVE_PROFILE = '27c5c10a3c1a0016fb5fb9e86454025103aa1a22e9889044ad65bd9f8932316d'
PACKET_LIFECYCLE_NATIVE_PROFILE = 'db6b9c8a443483e99932b8970b5abbc506622d1a2f337a1fe9b452a98cec706a'
BOUNDED_ADMISSION_NATIVE_PROFILE = 'cc7fb2cdb1c88b5ecff826bac7088e93d11f13a0317fd673f5291778736f1c44'
EXPONENTIAL_ADMISSION_NATIVE_PROFILE = 'e772c4c3a51d8db2937a5cbcb991dc0a5c2433f70082a7b02d32cda5aabdd5e4'
WAVE1_NATIVE_PROFILE = '2eb453170ca260aa32575b628480ec529d71f78d80dcbe052ebe5f807569945f'
WAVE2_NATIVE_PROFILE = 'a61df8f59eadde2f1631f7002e062770ae68c577de973717e9f4997748fee1d4'
PRE_WAVE3_NATIVE_PROFILE = 'dbfaee1c623984cbd60833413de6c4aab1033214d4ddf6300c415b04bfbeefce'
WAVE3_NATIVE_PROFILE = '23c5c06540959fe6cbdd0e9e58e3bb69c328616f879caa6fd4604b22d790cf82'
WAVE3_5_NATIVE_PROFILE = '82f1d0421b9cb9c134e5d0fb97c14e530407c409ac3716730a7f758e16ddd609'
CURRENT_NATIVE_PROFILE = WAVE3_5_NATIVE_PROFILE

ADMITTED_NATIVE_PROFILES = frozenset({
    REVIEWED_NATIVE_PROFILE,
    REORGANIZED_NATIVE_PROFILE,
    REPAIRED_NATIVE_PROFILE,
    COMMAND_ROUTES_NATIVE_PROFILE,
    BODY_REPAIRS_NATIVE_PROFILE,
    SCHEMA_REPAIRS_NATIVE_PROFILE,
    LIFECYCLE_NATIVE_PROFILE,
    OUTLIER_REPAIRS_NATIVE_PROFILE,
    SCOPED_COMPOSITION_NATIVE_PROFILE,
    JOINED_NATIVE_PROFILE,
    PACKET_LIFECYCLE_NATIVE_PROFILE,
    BOUNDED_ADMISSION_NATIVE_PROFILE,
    EXPONENTIAL_ADMISSION_NATIVE_PROFILE,
    WAVE1_NATIVE_PROFILE,
    WAVE2_NATIVE_PROFILE,
    PRE_WAVE3_NATIVE_PROFILE,
    WAVE3_NATIVE_PROFILE,
    WAVE3_5_NATIVE_PROFILE,
    CURRENT_NATIVE_PROFILE,
})

PROFILE_ERROR = ('Selected native engine does not match the reviewed capture adapter; '
                 'requalify this adapter before capture')
RETURN_ERROR = 'Native return already exists; use its retained body or a new current occurrence'


def _is_test_module(name: str) -> bool:
    lower = name.lower()
    return lower.endswith('.py') and (lower.startswith('test_') or lower.endswith('_test.py'))


def native_sources(entry: Path):
    """Collect the exact executable native engine source map for profile qualification.

    Wave 4 canonical engines hash only engine/{deployment,core,store} non-test modules.
    Pre-Wave-4 historical engines hash engine/runtime/ to preserve historical profile pins.
    """
    entry = entry.resolve(strict=True)
    engine = entry.parents[1]
    if entry == engine / 'store/manage_store.py' and (engine / 'store/system.py').is_file():
        roots = (engine / 'deployment', engine / 'core', engine / 'store')
        base = engine
        native = engine / 'store/system.py'
        exclude_tests = True
    elif entry == engine / 'runtime/manage_store.py' and (engine / 'runtime/native/system.py').is_file():
        roots = (engine / 'runtime',)
        base = engine
        native = engine / 'runtime/native/system.py'
        exclude_tests = False
    else:
        raise ValueError(PROFILE_ERROR)
    programs = {}
    for folder in roots:
        if not folder.is_dir():
            continue
        for p in folder.rglob('*.py'):
            if '__pycache__' in p.parts:
                continue
            if exclude_tests and ('tests' in p.relative_to(base).parts or _is_test_module(p.name)):
                continue
            programs[p.relative_to(base).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return native, programs


def reviewed_engine(engine: Path):
    """Verify the selected native implementation before loading it."""
    path, programs = native_sources(engine)
    profile = hashlib.sha256(json.dumps(programs, sort_keys=True,
                                       separators=(',', ':')).encode()).hexdigest()
    if profile not in ADMITTED_NATIVE_PROFILES:
        raise ValueError(PROFILE_ERROR)
    engine_root = str(path.parents[1])
    if path.parent.name == 'store' and engine_root not in sys.path:
        sys.path.insert(0, engine_root)
    spec = importlib.util.spec_from_file_location('reviewed_capture_engine', path)
    if spec is None or spec.loader is None:
        raise ValueError(PROFILE_ERROR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, profile


def legacy_packet_summary(exchange, directory, consumer, publications, returned, outcome):
    """The admitted predecessor engines predate Exchange.packet_summary."""
    attempted = exchange.transport.has_attempt(directory)
    transport = 'dispatch-attempted' if attempted else 'prepared'
    if returned:
        transport = 'returned'
    row = {'packet': directory.name, 'consumer': consumer, 'transport': transport,
           'publications': publications, 'semantic_fulfillment': 'not-decided-by-exchange'}
    if outcome is not None:
        row['transport'] = 'terminal-without-return'
        row['transport_outcome'] = {k: outcome[k] for k in ('reason', 'account', 'outcome_sha256')}
    return row


def turn_summary(system, turn: str, observed=None) -> dict:
    """Project only the inspected packet. Current basis observations do not decide transport."""
    try:
        observed = system.ex.inspect(turn) if observed is None else observed
        publications = []
        for observation in observed['publications']:
            if 'record' not in observation:
                raise ValueError(observation['observation_error'])
            record = observation['record']
            publications.append({'path': record['path'], 'phase': record['phase']})
        directory = system.ex.directory(turn)
        returned = observed['return'] is not None
        outcome = observed.get('transport_outcome')
        if hasattr(system.ex, 'packet_summary'):
            return system.ex.packet_summary(directory, observed['consumer'], publications,
                                            returned=returned, outcome=outcome)
        return legacy_packet_summary(system.ex, directory, observed['consumer'], publications, returned, outcome)
    except (ValueError, KeyError, OSError, TypeError) as exc:
        return {'packet': turn, 'transport': 'record-unresolved', 'error': str(exc)}


def _exchange_root(native, root: Path) -> tuple[Path, Path]:
    """Resolve and validate the native root and exchange lock root before locking."""
    resolved_root = native.external(root) if hasattr(native, 'external') else root.expanduser().resolve()
    raw_exchange = native.NativeLayout(resolved_root).exchange if hasattr(native, 'NativeLayout') else resolved_root / 'exchange'
    exchange_root = native.external(raw_exchange) if hasattr(native, 'external') else raw_exchange.expanduser().resolve()
    return resolved_root, exchange_root


def _lookup_attention_receipt(system, raw_id: str) -> tuple[str, dict, str | None]:
    """Inspect a recorded Attention receipt via public API (with pre-Wave-4 historical fallback)."""
    if hasattr(system.attention, 'inspect_receipt'):
        inspected = system.attention.inspect_receipt(raw_id)
        if inspected is None:
            raise ValueError('receipt identity not recorded in native Attention')
        return inspected['object'], inspected['record'], inspected.get('feedback_object')
    # Historical pre-Wave-4 engines predate Attention.inspect_receipt.
    state = system.attention._load()
    current = system.attention._current(state)
    position = current['receipt_ids'].get(raw_id)
    if position is None:
        raise ValueError('receipt identity not recorded in native Attention')
    obj_key = state['receipts'][position]
    record = system.attention._get(obj_key)
    return obj_key, record, state['feedbacks'].get(record.get('feedback'))


def _reconcile_assessment(native, system, turn: str, receipt: Path, producer: str, observed: dict) -> dict:
    """Verify an already-committed native Attention receipt matches this exact capture request."""
    supplied = native.read(receipt)
    if not isinstance(supplied, dict) or set(supplied) != {'id', 'assessments'}:
        raise ValueError('receipt must contain id and assessments')
    raw_id = supplied['id']
    if not isinstance(raw_id, str):
        raise ValueError('receipt identity not recorded in native Attention')
    expected_source_sha = native.sha(native.encode(supplied))
    expected_feedback = native.feedback_id(turn)
    obj_key, record, current_feedback_obj = _lookup_attention_receipt(system, raw_id)
    rid = native.packet_id(raw_id)
    if (record.get('id') != rid
            or record.get('feedback') != expected_feedback
            or record.get('feedback_object') != current_feedback_obj
            or record.get('producer') != producer
            or record.get('source', {}).get('sha256') != expected_source_sha):
        raise ValueError('receipt identity already used')
    attention = {
        'receipt': record['id'],
        'object': obj_key,
        'recorded': [item['item'] for item in record['assessments']],
        'boundary': record['boundary'],
    }
    return {
        'attention': attention,
        'status': {'turns': [turn_summary(system, turn, observed)]},
        'reconciled': True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--engine', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path)
    commands = parser.add_subparsers(dest='action', required=True)
    capture = commands.add_parser('assess', allow_abbrev=False)
    capture.add_argument('turn')
    capture.add_argument('--receipt', required=True, type=Path)
    capture.add_argument('--producer', required=True)
    reconcile = commands.add_parser('reconcile-assess', allow_abbrev=False,
                                    help='verify and recover an already-committed assessment receipt')
    reconcile.add_argument('turn')
    reconcile.add_argument('--receipt', required=True, type=Path)
    reconcile.add_argument('--producer', required=True)
    inspect = commands.add_parser('inspect-turn', allow_abbrev=False, help='read-only compatibility observation')
    inspect.add_argument('turn')
    args = parser.parse_args()
    try:
        native, profile = reviewed_engine(args.engine)
        root, exchange_root = _exchange_root(native, args.root)
        with native.exclusive(exchange_root / 'owner.lock'):
            system = native.System(root)
            if system.ex.root.resolve() != exchange_root.resolve():
                raise ValueError('native exchange lock root differs from selected system exchange')
            if args.action == 'inspect-turn':
                result = {'turn': turn_summary(system, args.turn)}
            elif args.action == 'reconcile-assess':
                observed = system.ex.inspect(args.turn)
                result = _reconcile_assessment(native, system, args.turn, args.receipt, args.producer, observed)
            else:
                # Keep the open-turn check and retention in one cooperative
                # owner invocation. assess only changes Attention, so this
                # selected transport observation remains applicable afterward.
                observed = system.ex.inspect(args.turn)
                if observed['return'] is not None:
                    raise ValueError(RETURN_ERROR)
                attention = system.assess(args.turn, args.receipt, args.producer)
                result = {'attention': attention,
                          'status': {'turns': [turn_summary(system, args.turn, observed)]}}
            result['capture_adapter'] = {
                'native_profile_sha256': profile,
                'scope': 'Selected native assessment and turn only; no global status computation.'}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({'status': 'unresolved', 'operation': args.action,
                          'error': str(exc), **getattr(exc, 'details', {})}, allow_nan=False), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    main()
