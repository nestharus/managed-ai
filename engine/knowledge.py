#!/usr/bin/env python3
"""Read canonical core or store-selected sources without a second source mirror.

Explicit --library requests still use the maintained complete-library reader.
"""
from pathlib import Path
import argparse
import json
import runpy
import sys

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
if '--library' in sys.argv or any(a.startswith('--library=') for a in sys.argv):
    runpy.run_path(str(ROOT / 'runtime/knowledge-system/knowledge.py'), run_name='__main__')
    raise SystemExit(0)

sys.path.insert(0, str(ROOT / 'runtime/store'))
sys.path.insert(0, str(ROOT / 'runtime'))
from native_runtime.catalogue import InstalledCatalogue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('verify')
    show = sub.add_parser('show'); show.add_argument('identity'); show.add_argument('--text', action='store_true')
    discover = sub.add_parser('discover'); discover.add_argument('--query', default='')
    discover.add_argument('--kind'); discover.add_argument('--limit', type=int, default=25)
    discover.add_argument('--offset', type=int, default=0); discover.add_argument('--content', action='store_true')
    args = parser.parse_args()
    if args.store:
        api = runpy.run_path(str(ROOT / 'runtime/store/system.py'))
        catalogue = api['System'](args.store).techniques
    else:
        catalogue = InstalledCatalogue(ROOT / 'runtime/store')
    if args.action == 'show':
        result = catalogue.search(args.identity)
        if args.text:
            sys.stdout.write(result['body']); return
    elif args.action == 'discover':
        result = catalogue.search(query=args.query, kind=args.kind, limit=args.limit,
                                  offset=args.offset, content=args.content)
    else:
        verified, sealed = [], []
        for entry in catalogue.index()['entries']:
            if entry.get('availability') == 'sealed':
                sealed.append(entry['id']); continue
            raw = catalogue.body(entry)
            verified.append({'id': entry['id'], 'sha256': entry['sha256'], 'bytes': len(raw)})
        result = {'status': 'verified', 'sources': verified, 'sealed_without_body_read': sealed,
                  'scope': 'Exact canonical byte custody; no participant acquisition or semantic acceptance is inferred.'}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'unresolved', 'error': str(exc)}), file=sys.stderr)
        raise SystemExit(2)
