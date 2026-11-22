#!/usr/bin/env python3
"""Host-owned selected capture for an explicitly reviewed native implementation.

System.assess and Exchange.inspect retain their native behavior. Exact transport
reporting also uses the reviewed PacketTransport.has_attempt predicate. That
dependency is not a new public native API. Requalify this adapter after any
native Python program change; never fall back to a broad or repeated assessment.
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
PROFILE_ERROR = ('Selected native engine does not match the reviewed capture adapter; '
                 'requalify this adapter before capture')
RETURN_ERROR = 'Native return already exists; use its retained body or a new current occurrence'


def native_sources(entry: Path):
    entry = entry.resolve(strict=True)
    root = entry.parent
    runtime = root.parent / 'runtime'
    legacy = root.parent / 'prototype'
    if (entry in (runtime / 'manage_store.py', legacy / 'system.py')
            and (runtime / 'native/system.py').is_file()):
        roots = (runtime, legacy)
        base = root.parent
        native = runtime / 'native/system.py'
    else:
        if entry.name != 'system.py':
            raise ValueError(PROFILE_ERROR)
        roots = (root,)
        base = root
        native = root / 'agent-system/system.py'
    programs = {p.relative_to(base).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for folder in roots for p in folder.rglob('*.py') if '__pycache__' not in p.parts}
    return native, programs


def reviewed_engine(engine: Path):
    """Verify the selected native implementation before loading it."""
    path, programs = native_sources(engine)
    profile = hashlib.sha256(json.dumps(programs, sort_keys=True,
                                       separators=(',', ':')).encode()).hexdigest()
    if profile not in (REVIEWED_NATIVE_PROFILE, REORGANIZED_NATIVE_PROFILE):
        raise ValueError(PROFILE_ERROR)
    spec = importlib.util.spec_from_file_location('reviewed_capture_engine', path)
    if spec is None or spec.loader is None:
        raise ValueError(PROFILE_ERROR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, profile


def turn_summary(system, turn: str, observed=None) -> dict:
    """Match the reviewed Exchange.list row using only the requested packet.

    inspect() also checks current bases/targets; its observation errors do not
    become new status predicates. The same publication fields and transport
    precedence as the reviewed list() implementation determine this summary.
    """
    try:
        observed = system.ex.inspect(turn) if observed is None else observed
        publications = []
        for observation in observed['publications']:
            if 'record' not in observation:
                raise ValueError(observation['observation_error'])
            record = observation['record']
            publications.append({'path': record['path'], 'phase': record['phase']})
        transport = 'prepared'
        # Reviewed implementation dependency, qualified by the complete native
        # Python profile above; this is not a promised Exchange embedding API.
        if system.ex.transport.has_attempt(system.ex.directory(turn)):
            transport = 'dispatch-attempted'
        if observed['return'] is not None:
            transport = 'returned'
        outcome = observed.get('transport_outcome')
        if outcome is not None:
            transport = 'terminal-without-return'
        result = {'packet': turn, 'consumer': observed['consumer'],
                  'transport': transport, 'publications': publications,
                  'semantic_fulfillment': 'not-decided-by-exchange'}
        if outcome is not None:
            result['transport_outcome'] = {
                k: outcome[k] for k in ('reason', 'account', 'outcome_sha256')}
        return result
    except (ValueError, KeyError, OSError, TypeError) as exc:
        return {'packet': turn, 'transport': 'record-unresolved', 'error': str(exc)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path)
    commands = parser.add_subparsers(dest='action', required=True)
    capture = commands.add_parser('assess')
    capture.add_argument('turn')
    capture.add_argument('--receipt', required=True, type=Path)
    capture.add_argument('--producer', required=True)
    inspect = commands.add_parser('inspect-turn', help='read-only compatibility observation')
    inspect.add_argument('turn')
    args = parser.parse_args()
    try:
        native, profile = reviewed_engine(args.engine)
        system = native.System(args.root)
        with native.exclusive(system.ex.root / 'owner.lock'):
            if args.action == 'inspect-turn':
                result = {'turn': turn_summary(system, args.turn)}
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
                          'error': str(exc), **getattr(exc, 'details', {})}), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    main()
