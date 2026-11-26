#!/usr/bin/env python3
"""Read canonical core or store-selected sources without a second source mirror.

Explicit top-level --library requests use the maintained complete-library reader.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

_SUBCOMMANDS = frozenset({
    'verify', 'show', 'discover', 'modules', 'prepare-index',
    'discover-batch', 'context', 'export', 'inspect', 'import',
})
_DISCOVER_FLAGS = frozenset({
    '--kind', '--limit', '--offset', '--content', '-h', '--help',
})


def _is_top_level_library_invocation(argv: list[str]) -> bool:
    """Return True only when --library is given as a top-level flag before the subcommand."""
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == '--':
            return False
        if token == '--library' or token.startswith('--library='):
            return True
        if token == '--store':
            i += 2
            continue
        if token.startswith('--store='):
            i += 1
            continue
        if token in _SUBCOMMANDS or not token.startswith('-'):
            return False
        i += 1
    return False


def _normalize_query_arg(argv: list[str]) -> list[str]:
    """Allow `discover --query --something` without argparse misreading the query value as a flag."""
    out = list(argv)
    for idx, token in enumerate(out):
        if token == '--query' and idx + 1 < len(out):
            nxt = out[idx + 1]
            if nxt.startswith('-') and nxt not in _DISCOVER_FLAGS and not any(
                    nxt.startswith(f + '=') for f in ('--kind', '--limit', '--offset')):
                out[idx] = '--query=' + nxt
                del out[idx + 1]
                break
    return out


def _execute_catalogue_action(catalogue, args):
    if args.action == 'show':
        result = catalogue.search(args.identity)
        if args.text:
            sys.stdout.write(result['body'])
            return None
        return result
    if args.action == 'discover':
        return catalogue.search(query=args.query, kind=args.kind, limit=args.limit,
                                offset=args.offset, content=args.content)
    verified, sealed = [], []
    for entry in catalogue.index()['entries']:
        if entry.get('availability') == 'sealed':
            sealed.append(entry['id'])
            continue
        raw = catalogue.body(entry)
        verified.append({'id': entry['id'], 'sha256': entry['sha256'], 'bytes': len(raw)})
    if getattr(args, 'summary', False):
        return {
            'status': 'verified',
            'verified_count': len(verified),
            'verified_bytes': sum(row['bytes'] for row in verified),
            'sealed_count': len(sealed),
            'sealed_without_body_read': sealed,
            'scope': 'Exact canonical byte custody; no participant acquisition or semantic acceptance is inferred.',
        }
    return {
        'status': 'verified',
        'sources': verified,
        'sealed_without_body_read': sealed,
        'scope': 'Exact canonical byte custody; no participant acquisition or semantic acceptance is inferred.',
    }


def main(argv: list[str] | None = None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if _is_top_level_library_invocation(raw_argv):
        from library.reader import knowledge
        saved = sys.argv
        try:
            sys.argv = [sys.argv[0], *raw_argv]
            knowledge.main()
        finally:
            sys.argv = saved
        return

    from core.ownership import NativeLayout, external_root
    from core.storage import AdmissionContended, admission_deadline, owner_admission, shared
    from store.selection.installed_catalogue import InstalledCatalogue

    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--store', type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    verify_cmd = sub.add_parser('verify', allow_abbrev=False)
    verify_cmd.add_argument('--summary', action='store_true',
                            help='Return verified/sealed counts and byte totals instead of listing every source')
    show = sub.add_parser('show', allow_abbrev=False)
    show.add_argument('identity')
    show.add_argument('--text', action='store_true')
    discover = sub.add_parser('discover', allow_abbrev=False)
    discover.add_argument('--query', default='')
    discover.add_argument('--kind')
    discover.add_argument('--limit', type=int, default=25)
    discover.add_argument('--offset', type=int, default=0)
    discover.add_argument('--content', action='store_true')
    args = parser.parse_args(_normalize_query_arg(raw_argv))

    if args.store:
        from store.system import System
        root = external_root(args.store)
        lock_path = external_root(NativeLayout(root).exchange) / 'owner.lock'
        try:
            with owner_admission(
                    lambda stack: stack.enter_context(shared(lock_path, allow_readonly_open=True)),
                    deadline=admission_deadline()):
                catalogue = System(root).techniques
                result = _execute_catalogue_action(catalogue, args)
        except AdmissionContended as exc:
            print(json.dumps(exc.result(args.action)), file=sys.stderr)
            raise SystemExit(75)
    else:
        catalogue = InstalledCatalogue(ENGINE / 'store')
        result = _execute_catalogue_action(catalogue, args)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'unresolved', 'error': str(exc)}), file=sys.stderr)
        raise SystemExit(2)
