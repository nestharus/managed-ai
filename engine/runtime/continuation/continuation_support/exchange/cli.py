"""Explicit context, return, and single-file publication across local invocations.

The caller supplies meaning and authority. This module supplies byte-bound
transport and optional publication, not a cognitive schema or semantic selector.
Trusted local POSIX use only; it is not a filesystem or process sandbox."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..storage import exclusive
from .api import Exchange


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True, type=Path)
    sub = p.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init'); init.add_argument('--workspace', type=Path, required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('packet'); prep.add_argument('--consumer', required=True); prep.add_argument('--brief', required=True)
    for flag in ('method', 'material', 'target', 'return'):
        prep.add_argument('--' + flag, dest=flag + 's', action='append', default=[])
    prep.add_argument('--artifact', dest='artifacts', action='append', default=[],
                      metavar='PACKET:TARGET', help='include a recorded proposal as material, without publication')
    dis = sub.add_parser('dispatch'); dis.add_argument('packet'); dis.add_argument('argv', nargs=argparse.REMAINDER)
    rec = sub.add_parser('receive'); rec.add_argument('packet'); rec.add_argument('--reply', type=Path)
    rec.add_argument('--producer', required=True); rec.add_argument('--proposal', dest='proposals', action='append', default=[])
    rec.add_argument('--wait', type=float, default=0)
    listing = sub.add_parser('list'); listing.add_argument('--consumer')
    ins = sub.add_parser('inspect'); ins.add_argument('packet')
    context = sub.add_parser('context', help='read the validated originating context')
    context.add_argument('packet'); context.add_argument('--text', action='store_true')
    recovery = sub.add_parser('recover-transport', help='recover the captured carrier view from retained evidence')
    recovery.add_argument('packet'); recovery.set_defaults(action='recover_transport')
    closure = sub.add_parser('close-transport', help='record an owner-accounted terminal failure without a contribution')
    closure.add_argument('packet'); closure.add_argument('--account', required=True)
    closure.set_defaults(action='close_transport')
    reconciliation = sub.add_parser('reconcile-publication', help='acknowledge observed target bytes without replaying publication')
    reconciliation.add_argument('packet'); reconciliation.add_argument('--target', required=True)
    reconciliation.add_argument('--expected-sha256', required=True, metavar='SHA256_OR_absent')
    reconciliation.add_argument('--account', required=True)
    reconciliation.set_defaults(action='reconcile_publication')
    pub = sub.add_parser('publish'); pub.add_argument('packet'); pub.add_argument('--target', required=True); pub.add_argument('--reason', required=True)
    args = vars(p.parse_args())
    root, action = args.pop('root'), args.pop('action')
    text = args.pop('text', False)
    try:
        if action == 'init':
            result = Exchange.initialize(root, **args)
        else:
            ex = Exchange(root)
            with exclusive(ex.root / 'owner.lock', allow_readonly_open=(action == 'inspect')):
                if action == 'dispatch' and args['argv'][:1] == ['--']:
                    args['argv'] = args['argv'][1:]
                result = getattr(ex, action)(**args)
        if text:
            sys.stdout.write(result['context'])
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, RuntimeError, StopIteration) as exc:
        print(json.dumps({'error': str(exc), 'operation': action}), file=sys.stderr)
        raise SystemExit(2)
