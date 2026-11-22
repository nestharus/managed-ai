"""Issue and consult a job-bound ordering plan on committed occurrence receipts."""
import argparse
import hashlib
import json
from pathlib import Path

from order_repair import construct, require
from local_policy import name, unique_object


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def issue(spec, job):
    require(name(job),'missing job identity')
    result=construct(spec)
    if result['status']!='optimal':
        return result
    claims=result['offers']
    rules={e:[] for e in spec['reference']}
    def add(edge):
        a,b=edge
        gate=dict(before=[a],need=1)
        if gate not in rules[b]:
            rules[b].append(gate)
    for edge in spec.get('before',[]):
        add(edge)
    for q in claims:
        body=spec['offers'][q]
        for edge in body.get('edges',[]):
            add(edge)
        for gate in body.get('gates',[]):
            rules[gate['event']].append(dict(before=gate['before'],need=gate['need']))
    return dict(status='issued',job=job,commission=digest([job,spec]),rules=rules,offers=claims,cost=result['cost'])


def ready(plan, event, receipts):
    require(plan['status']=='issued' and event in plan['rules'],'unknown order or occurrence')
    require(type(receipts) is list and len(receipts)<=4096,'invalid receipt list')
    seen={}
    for r in receipts:
        require(r['job']==plan['job'] and r['commission']==plan['commission'] and
                r['event'] in plan['rules'] and name(r['effect']), 'receipt is outside the order')
        prior=seen.setdefault(r['event'],r['effect'])
        require(prior==r['effect'],'conflicting effects for one occurrence')
    if event in seen:
        return dict(status='already-committed',effect=seen[event])
    pending=[]
    for gate in plan['rules'][event]:
        eligible=set(gate['before'])
        missing=gate['need']-len(eligible.intersection(seen))
        if missing>0:
            pending.append(dict(remaining=sorted(eligible-seen.keys()),need=missing))
    return dict(status='waiting',pending=pending) if pending else dict(status='ready')


def read(path):
    return json.loads(path.read_bytes(),object_pairs_hook=unique_object)


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    commands=cli.add_subparsers(required=True,dest='command')
    p=commands.add_parser('issue');p.add_argument('spec',type=Path);p.add_argument('job')
    p=commands.add_parser('ready');p.add_argument('plan',type=Path);p.add_argument('event');p.add_argument('receipts',type=Path)
    args=cli.parse_args()
    try:
        out=issue(read(args.spec),args.job) if args.command=='issue' else ready(read(args.plan),args.event,read(args.receipts))
    except (ValueError,KeyError,TypeError,OSError,AttributeError) as e:
        cli.exit(2,str(e)+'\n')
    print(json.dumps(out,sort_keys=True,indent=2))
