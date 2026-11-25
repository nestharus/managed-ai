"""Issue conditional price messages and route an accepted row back to its supplying peers."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from local_policy import name, unique_object


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def ordered_keys(keys, domains):
    require(isinstance(keys,list) and len(keys)==len(set(keys)) and all(k in domains for k in keys),
            'invalid or repeated decision field')
    return keys


def normal_rows(rows, scope, domains, limit, *, source=False):
    require(isinstance(rows,list) and len(rows)<=limit,'invalid row count')
    table={}
    for r in rows:
        at=r['at']
        require(isinstance(at,list) and len(at)==len(scope) and
                all(a in domains[v] for v,a in zip(scope,at)),'invalid row coordinates')
        price=r['cost']
        require(type(price) is int and (not source or abs(price)<=1000000),'invalid price')
        key=tuple(at)
        require(key not in table,'duplicate price row')
        table[key]=price
    return [dict(at=list(k),cost=table[k]) for k in sorted(table)]


def catalog(spec):
    space=spec['space']
    require(name(space),'expected a named semantic edition')
    domains=spec['domains']
    require(isinstance(domains,dict) and len(domains)<=512 and all(name(v) for v in domains),'invalid domains')
    for choices in domains.values():
        require(isinstance(choices,list) and 1<=len(choices)<=128 and all(name(v) for v in choices)
                and len(choices)==len(set(choices)),'invalid decision values')
    owners=spec['factors']
    require(isinstance(owners,dict) and 1<=len(owners)<=256 and all(name(k) for k in owners),'invalid factor roster')
    declarations={}
    for owner,f in owners.items():
        scope=ordered_keys(f['scope'],domains)
        require(len(scope)<=32 and name(f['version']),'invalid owner scope or authority version')
        rows=normal_rows(f['rows'],scope,domains,16384,source=True)
        declarations[owner]=dict(scope=scope,version=f['version'],
            pin=digest([space,owner,f['version'],[(v,domains[v]) for v in scope],rows]))
    open_fields=ordered_keys(spec.get('open',[]),domains)
    require(set(domains)=={v for f in declarations.values() for v in f['scope']},'unused or unowned decision field')
    return dict(space=space,domains=domains,factors=declarations,open=open_fields)


def boundary(h,members):
    group=set(members)
    inside=set();outside=set(h['open'])
    for k,f in h['factors'].items():
        (inside if k in group else outside).update(f['scope'])
    return sorted(inside & outside)


class Reopen(ValueError):
    pass


class Limit(ValueError):
    pass


class Meter:
    def __init__(self,limit):
        require(type(limit) is int and 1<=limit<=10000000,'invalid candidate-row limit')
        self.limit=limit
        self.count=0
    def consume(self):
        if self.count>=self.limit:
            raise Limit('candidate-row limit')
        self.count+=1


def seal(h,members,ports,prices,actions):
    rows=[{'at':list(key),'cost':prices[key]} for key in sorted(prices)]
    body=dict(space=h['space'],members={k:h['factors'][k]['pin'] for k in sorted(members)},ports=ports,axes={v:h['domains'][v] for v in ports},rows=rows)
    public=dict(body,seal=digest(body))
    private=dict(message=public['seal'],ports=ports,rows=[dict(at=list(k),choice=actions[k]) for k in sorted(prices)])
    private['seal']=digest(private)
    return public,private


def leaf(h, owner, raw, meter, retain=()):
    f=h['factors'][owner];scope=f['scope'];domains=h['domains']
    rows=normal_rows(raw,scope,domains,16384,source=True)
    require(f['pin']==digest([h['space'],owner,f['version'],[(v,domains[v]) for v in scope],rows]),
            'source price or authority differs from commission')
    require(set(retain)<=set(scope),'cannot retain unavailable private fields')
    ports=sorted(set(boundary(h,[owner]))|set(retain))
    positions=[scope.index(v) for v in ports]
    prices={};actions={}
    for r in rows:
        meter.consume()
        key=tuple(r['at'][i] for i in positions)
        if key not in prices or r['cost']<prices[key]:
            prices[key]=r['cost'];actions[key]=dict(owner=owner,assignment=dict(zip(scope,r['at'])),cost=r['cost'])
    return seal(h,[owner],ports,prices,actions)


def validate(h,msg):
    body={k:msg[k] for k in ('space','members','ports','axes','rows')}
    require(digest(body)==msg['seal'],'corrupt public price message')
    require(msg['space']==h['space'],'incompatible semantic editions')
    members=msg['members']
    require(isinstance(members,dict) and members,'empty message')
    for k,pin in members.items():
        if k not in h['factors'] or h['factors'][k]['pin']!=pin:
            raise Reopen('owner changed or left: '+k)
    ports=ordered_keys(msg['ports'],h['domains'])
    require(msg['axes']=={v:h['domains'][v] for v in ports},'changed field domain')
    require(set(ports)<={v for k in members for v in h['factors'][k]['scope']},'unowned field in message')
    missing=set(boundary(h,members))-set(ports)
    if missing:
        raise Reopen('new external use of forgotten fields: '+', '.join(sorted(missing)))
    normal_rows(msg['rows'],ports,h['domains'],10000000)


def join(h, left, right, meter, retain=()):
    for msg in (left,right):
        validate(h,msg)
    owners=set(left['members'])|set(right['members'])
    require(len(owners)==len(left['members'])+len(right['members']),'factor ownership overlaps')
    lp,rp=left['ports'],right['ports']
    overlap=sorted(set(lp)&set(rp))
    li=[lp.index(v) for v in overlap];ri=[rp.index(v) for v in overlap]
    buckets=defaultdict(list)
    for r in right['rows']:
        buckets[tuple(r['at'][i] for i in ri)].append(r)
    require(set(retain)<=set(lp+rp),'retained field needs reopened inputs')
    ports=sorted(set(boundary(h,owners))|set(retain))
    prices={};choices={}
    for l in left['rows']:
        for r in buckets.get(tuple(l['at'][i] for i in li),()):
            meter.consume()
            values=dict(zip(lp,l['at']));values.update(zip(rp,r['at']))
            key=tuple(values[v] for v in ports)
            cost=l['cost']+r['cost']
            if key not in prices or cost<prices[key]:
                prices[key]=cost
                choices[key]=dict(children=[dict(message=left['seal'],at=l['at']),
                                             dict(message=right['seal'],at=r['at'])])
    return seal(h,owners,ports,prices,choices)


def compare(left,right):
    for msg in (left,right):
        require(msg['seal']==digest({k:v for k,v in msg.items() if k!='seal'}),'corrupt comparison input')
    require(all(left[k]==right[k] for k in ('space','ports','axes')),'different comparison interfaces')
    l,r=({tuple(row['at']):row['cost'] for row in msg['rows']} for msg in (left,right))
    witnesses={}
    for key in sorted(l.keys()|r.keys()):
        if l.get(key)==r.get(key):
            continue
        better='left' if key not in r or key in l and l[key]<r[key] else 'right'
        witnesses.setdefault(better,dict(at=list(key),left=l.get(key),right=r.get(key),
            selector=dict(scope=left['ports'],rows=[dict(at=list(key),cost=0)])))
    relation=('crossing' if len(witnesses)==2 else next(iter(witnesses))+'-dominates' if witnesses else 'equal-prices')
    return dict(relation=relation,witnesses=witnesses)


def route(private, message, binding):
    require(private['seal']==digest({k:v for k,v in private.items() if k!='seal'}),'corrupt local witness')
    require(private['message']==message,'witness belongs to another price message')
    require(isinstance(binding,list) and len(binding)==len(private['ports']),'wrong award coordinates')
    for r in private['rows']:
        if r['at']==binding:
            return r['choice']
    raise ValueError('award is infeasible or absent')


def read(p):
    return json.loads(p.read_bytes(),object_pairs_hook=unique_object)


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    sub=cli.add_subparsers(dest='cmd',required=True)
    a=sub.add_parser('catalog');a.add_argument('spec',type=Path)
    for cmd in ('leaf','join'):
        a=sub.add_parser(cmd);a.add_argument('catalog',type=Path)
        if cmd=='leaf':a.add_argument('owner');a.add_argument('rows',type=Path)
        else:a.add_argument('left',type=Path);a.add_argument('right',type=Path)
        a.add_argument('--limit',type=int,default=300000)
    a=sub.add_parser('compare');a.add_argument('left',type=Path);a.add_argument('right',type=Path)
    a=sub.add_parser('route');a.add_argument('journal',type=Path);a.add_argument('award',type=Path)
    a=cli.parse_args()
    try:
        if a.cmd=='catalog':out=catalog(read(a.spec))
        elif a.cmd=='compare':out=compare(read(a.left),read(a.right))
        elif a.cmd=='route':
            award=read(a.award);out=route(read(a.journal),award['message'],award['at'])
        elif a.cmd=='leaf':
            public,private=leaf(read(a.catalog),a.owner,read(a.rows),Meter(a.limit));out=dict(public=public,private=private)
        else:
            public,private=join(read(a.catalog),read(a.left),read(a.right),Meter(a.limit));out=dict(public=public,private=private)
    except (Limit,Reopen) as e:
        out=dict(status='resource-limit' if isinstance(e,Limit) else 'reopen',reason=str(e))
    except (ValueError,KeyError,TypeError,AttributeError,OSError) as e:
        cli.exit(2,str(e)+'\n')
    print(json.dumps(out,indent=2,sort_keys=True))
