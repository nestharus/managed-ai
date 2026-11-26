"""Public command syntax for collective artifact work."""
import argparse
import json
from pathlib import Path
import sqlite3

from . import Collective, PRODUCT
from .contract import CONTEXT_LIMIT, loads_strict
from .database import _require_ordinary_path


def main(argv=None, *, product=PRODUCT):
    parser=argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--db',required=True,type=Path)
    sub=parser.add_subparsers(dest='operation',required=True)
    sub.add_parser('init', allow_abbrev=False)
    sub.add_parser('upgrade', allow_abbrev=False)
    review=sub.add_parser('next-review', allow_abbrev=False); review.add_argument('proposal')
    relation=sub.add_parser('use-source', allow_abbrev=False); relation.add_argument('target'); relation.add_argument('source', nargs='?')
    call=sub.add_parser('call', allow_abbrev=False); call.add_argument('--request',required=True,type=Path)
    show=sub.add_parser('show', allow_abbrev=False); show.add_argument('id'); show.add_argument('--revision',type=int); show.add_argument('--content',action='store_true')
    for name_ in ('context','proposal'):
        command=sub.add_parser(name_, allow_abbrev=False); command.add_argument('id')
    for name_ in ('inbox','changes'):
        command=sub.add_parser(name_, allow_abbrev=False); command.add_argument('--after',type=int,default=0); command.add_argument('--limit',type=int,default=20)
        if name_=='inbox': command.add_argument('--actor',required=True)
    encounter=sub.add_parser('encounters', allow_abbrev=False)
    encounter.add_argument('--after',type=int,default=0)
    encounter.add_argument('--limit',type=int,default=20)
    encounter.add_argument('--scan-limit',type=int,default=256)
    receipt=sub.add_parser('receipt', allow_abbrev=False); receipt.add_argument('--actor',required=True); receipt.add_argument('--command',required=True)
    notice=sub.add_parser('notice', allow_abbrev=False); notice.add_argument('id',type=int)
    args=parser.parse_args(argv)
    try:
        if args.operation=='init': result=Collective.initialize(args.db, product=product)
        elif args.operation=='upgrade': result=Collective.upgrade(args.db, product=product)
        else:
            store=Collective(args.db, product=product)
            if args.operation=='call':
                _require_ordinary_path(args.request, 'request file', must_exist=True)
                raw=args.request.read_bytes()
                if len(raw)>CONTEXT_LIMIT: raise ValueError('Request file exceeds 2 MiB')
                result=store.call(loads_strict(raw))
            elif args.operation=='show': result=store.show(args.id,args.revision,content=args.content)
            elif args.operation in {'context','proposal'}: result=store.retained(args.operation,args.id)
            elif args.operation=='receipt': result=store.receipt(args.actor,args.command)
            elif args.operation=='notice': result=store.notice(args.id)
            elif args.operation=='next-review': result=store.next_review(args.proposal)
            elif args.operation=='use-source': result=store.use_source(args.target,args.source)
            elif args.operation=='encounters':
                result=store.encounters(after=args.after,limit=args.limit,scan_limit=args.scan_limit)
            else: result=store.view(args.operation,actor=getattr(args,'actor',None),after=args.after,limit=args.limit)
        print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
        return 0 if not isinstance(result,dict) or result.get('status')!='refused' else 2
    except (ValueError,sqlite3.Error,OSError) as exc:
        print(json.dumps({'status':'unresolved','error':str(exc)}, allow_nan=False))
        return 3
