"""Project a compiled inquiry onto one desk and advance it with the observations that desk owns."""
import argparse
import json
from pathlib import Path

from local_policy import unique_object


def project(plan, owner=None):
    if plan['status'] != 'optimal':
        raise ValueError('a completed purchase policy is required')
    nodes={}
    for key,node in plan['nodes'].items():
        if 'buy' in node:
            nodes[key]={'buy':node['buy'],'next':node['next']}
        elif owner is None:
            nodes[key]={'complete':True}
        else:
            nodes[key]={'acquired':node['acquired'],'rules':node['rules'][owner]}
    return dict(owner=owner,root=plan['root'],nodes=nodes)


def step(desk, record):
    trace=record['public']
    if not isinstance(trace,list):
        raise ValueError('public trace must be an ordered list')
    node=desk['nodes'][desk['root']]
    for row in trace:
        if not isinstance(row,list) or len(row)!=2 or 'buy' not in node or row[0]!=node['buy']:
            raise ValueError('observation arrived outside its scheduled purchase')
        label=row[1]
        if label not in node['next']:
            raise ValueError('unrecognized return')
        node=desk['nodes'][node['next'][label]]
    if desk['owner'] is None and set(record)!={'public'}:
        raise ValueError('dispatcher accepts only public receipts')
    if 'buy' in node:
        return {'status':'buy' if desk['owner'] is None else 'await','offer':node['buy']}
    if desk['owner'] is None:
        return {'status':'complete'}
    private=record['private']
    if not isinstance(private,dict) or set(private)!=set(node['acquired']):
        raise ValueError('each paid offer requires an explicit role receipt')
    view=[record['view'],*[private[q] for q in node['acquired']]]
    for row in node['rules']:
        if row['view']==view:
            return {'status':'act','action':row['action']}
    raise ValueError('private history has no authorized action in this policy')


def read(p):
    return json.loads(p.read_bytes(),object_pairs_hook=unique_object)


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    sub=cli.add_subparsers(dest='command',required=True)
    a=sub.add_parser('project');a.add_argument('policy',type=Path);a.add_argument('--owner')
    a=sub.add_parser('step');a.add_argument('desk',type=Path);a.add_argument('receipts',type=Path)
    args=cli.parse_args()
    try:
        out=project(read(args.policy),args.owner) if args.command=='project' else step(read(args.desk),read(args.receipts))
    except (KeyError,ValueError,TypeError,OSError) as exc:
        cli.exit(2,str(exc)+'\n')
    print(json.dumps(out,indent=2,sort_keys=True))
