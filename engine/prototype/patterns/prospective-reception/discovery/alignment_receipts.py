"""Bind a peer-alignment commission to an epoch and consume a paid observation path."""
import argparse
import hashlib
import json
from pathlib import Path
from local_policy import name, unique_object
from alignment_space import require
from peer_alignment import construct


def issue(spec,job,epoch):
    require(name(job) and name(epoch),'job and source epoch required')
    require(spec.get('format')!='network','use a peer chart for an observation desk')
    out=construct(spec)
    digest=hashlib.sha256(json.dumps([spec,job,epoch],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    result=dict(job=job,epoch=epoch,commission=digest,status=out['status'])
    for field in ('root','nodes','effects','required_answers','probes','cost','worst_cost'):
        if field in out:result[field]=out[field]
    return result


def advance(desk,receipts):
    require(isinstance(receipts,list) and len(receipts)<=512,'invalid receipt stream')
    known={}
    for row in receipts:
        require(all(desk[k]==row[k] for k in ('job','epoch','commission')),'foreign or expired observation')
        q,v=row['probe'],row['value'];require(name(q) and name(v),'malformed probe receipt')
        if q in known:require(known[q]==v,'incompatible receipts for one probe')
        else:known[q]=v
    status=desk['status']
    if status=='qualified':
        require(not known,'unexpected observations');return dict(status='act',effects=desk['effects'])
    if status=='certified-panel':
        required=desk['required_answers'];require(known.keys()<=required.keys(),'unscheduled panel observation')
        if any(v!=required[k] for k,v in known.items()):return dict(status='reopen',known=known)
        pending=[q for q in desk['probes'] if q not in known]
        return dict(status='ask',probe=pending[0]) if pending else dict(status='act',effects=desk['effects'])
    if status!='policy':return dict(status='hold',reason=status)
    node=desk['nodes'][desk['root']]
    for q,v in known.items():
        require(node.get('ask')==q,'observation outside the scheduled path')
        if v not in node['next']:return dict(status='reopen',known=known)
        node=desk['nodes'][node['next'][v]]
    return dict(status='ask',probe=node['ask']) if 'ask' in node else dict(status='act',effects=node['effects'])


def read(p):return json.loads(p.read_bytes(),object_pairs_hook=unique_object)


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    sub=cli.add_subparsers(dest='cmd',required=True)
    a=sub.add_parser('issue');a.add_argument('spec',type=Path);a.add_argument('job');a.add_argument('epoch')
    a=sub.add_parser('advance');a.add_argument('desk',type=Path);a.add_argument('receipts',type=Path)
    a=cli.parse_args()
    try:out=issue(read(a.spec),a.job,a.epoch) if a.cmd=='issue' else advance(read(a.desk),read(a.receipts))
    except (ValueError,KeyError,TypeError,AttributeError,OSError) as e:cli.exit(2,str(e)+'\n')
    print(json.dumps(out,sort_keys=True,indent=2))
