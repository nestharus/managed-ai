"""Buy authorized routing signals sufficient for agents to realize a conditional price."""
import argparse
from fractions import Fraction
from itertools import count
import heapq
from copy import deepcopy
import json
from pathlib import Path
from boundary_pricing import construct as price
from boundary_messages import read, require
from shared_inquiry import construct as plan
from inquiry_desk import project, step


def account(result,query,tariffs):
    dispatch=project(result)
    owners=query['owners']
    desks={owner:project(result,owner) for owner in owners}
    records={};fees=materials=mass=0
    for w,case in query['worlds'].items():
        public=[];private={owner:{} for owner in owners};bill=0
        while True:
            decision=step(dispatch,dict(public=public))
            if decision['status']=='complete':
                break
            q=decision['offer'];source=case['offers'][q]
            public.append([q,source['public']]);bill+=query['costs'][q]
            for owner in owners:
                private[owner][q]=source.get('private',{}).get(owner)
        acts=[step(desks[o],dict(public=public,view=case['views'][o],private=private[o]))['action'] for o in owners]
        cost=next(row['cost'] for row in tariffs[w]['acceptable'] if row['actions']==acts)
        weight=case['weight'];mass+=weight;fees+=bill*weight;materials+=cost*weight
        records[w]=dict(actions=acts,public=public,fees=bill,material=cost,total=bill+cost)
    return Fraction(fees,mass),Fraction(materials,mass),records


def total_price(query, tariffs, limit):
    # A box contains possible vectors of per-context material ceilings.
    # Its low material corner and most permissive high-corner inquiry give a lower bound.
    require(type(limit) is int and 1<=limit<=4096,'invalid outer search limit')
    require(query.get('objective','mean')=='mean','total pricing uses the weighted mean')
    worlds=list(tariffs);levels=[sorted({r['cost'] for r in tariffs[w]['acceptable']}) for w in worlds]
    weights=[query['worlds'][w]['weight'] for w in worlds];mass=sum(weights)
    require(all(type(w) is int and 1<=w<=1000000 for w in weights),'invalid context weight')
    def material_bound(corner):
        return Fraction(sum(w*level[i] for w,level,i in zip(weights,levels,corner)),mass)
    def ask(corner):
        request=dict(query,worlds={})
        for i,(w,top) in enumerate(zip(worlds,corner)):
            case=query['worlds'][w]
            rows=[r['actions'] for r in tariffs[w]['acceptable'] if r['cost']<=levels[i][top]]
            request['worlds'][w]=dict(case,joint={'witness':rows},
                allowed={o:sorted({r[j] for r in rows}) for j,o in enumerate(query['owners'])})
        return plan(request)
    lo=tuple(0 for w in worlds);hi=tuple(len(v)-1 for v in levels)
    serial=count();queue=[(material_bound(lo),next(serial),lo,hi)];cache={};best=None;solution=None;unresolved=[]
    while queue:
        lower,_,low,high=heapq.heappop(queue)
        if best is not None and lower>=best:
            break
        if high not in cache:
            if len(cache)>=limit:
                unresolved.append(lower);break
            cache[high]=ask(high)
        policy=cache[high]
        if policy['status']=='infeasible':
            continue
        if policy['status']!='optimal':
            unresolved.append(lower);continue
        fee=Fraction(policy['mean_cost'])
        lower=max(lower,material_bound(low)+fee)
        acquisition,material,records=account(policy,query,tariffs)
        require(fee==acquisition,'issued schedule disagrees with fee')
        if best is None or fee+material<best:
            best=fee+material
            solution=dict(inquiry=policy,mean_fee=str(fee),mean_material=str(material),records=records)
        if lower>=best or low==high:
            continue
        axis=max(range(len(worlds)),key=lambda i:weights[i]*(levels[i][high[i]]-levels[i][low[i]]))
        middle=(low[axis]+high[axis])//2
        left=high[:axis]+(middle,)+high[axis+1:]
        right=low[:axis]+(middle+1,)+low[axis+1:]
        heapq.heappush(queue,(lower,next(serial),low,left))
        heapq.heappush(queue,(max(lower,material_bound(right)+fee),next(serial),right,high))
    active=unresolved+[q[0] for q in queue]
    if best is None and not active:
        return dict(status='infeasible',inquiries=len(cache))
    lower=min(active+([best] if best is not None else []))
    return dict(status='optimal' if best==lower else 'bounded',lower=str(lower),
                upper=str(best) if best is not None else None,inquiries=len(cache),solution=solution)


def commission(job):
    spec=deepcopy(job['pricing'])
    require('award' not in spec,'supply a pricing problem with an open boundary')
    ports=sorted(spec.get('open',[]))
    roles=job['roles'];actions=list(roles.values())
    require(isinstance(roles,dict) and 1<=len(roles)<=8 and len(actions)==len(set(actions))
            and set(actions)<=spec['domains'].keys() and not set(actions)&set(ports),'invalid action fields')
    total=job.get('mode','threshold')=='total'
    require(job.get('mode','threshold') in ('total','threshold'),'unknown pricing mode')
    overhead=job.get('max_extra',None if total else 0)
    require(total and overhead is None or type(overhead) is int and overhead>=0,'invalid permitted excess cost')
    spec['open']=ports+actions
    expanded=price(spec)
    if expanded['status']!='priced':
        return dict(status=expanded['status'],candidate_rows=expanded['candidate_rows'])
    msg=expanded['message'];idx={v:i for i,v in enumerate(msg['ports'])}
    choices={}
    for row in msg['rows']:
        key=tuple(row['at'][idx[p]] for p in ports)
        acts=tuple(row['at'][idx[v]] for v in actions)
        choices.setdefault(key,{})[acts]=row['cost']
    cases=job['cases']
    require(type(cases) is list and len(cases)==len(choices),'context roster must cover the feasible boundary')
    worlds={};seen=set();tariffs={}
    for n,c in enumerate(cases):
        require(isinstance(c['at'],dict) and set(c['at'])==set(ports),'invalid boundary context')
        key=tuple(c['at'][p] for p in ports)
        require(key in choices and key not in seen,'duplicate or unpriced context')
        seen.add(key);name='case-'+str(n);prices=choices[key];best=min(prices.values())
        allowed=sorted(a for a,v in prices.items() if overhead is None or v<=best+overhead)
        worlds[name]=dict(weight=c.get('weight',1),views=c['views'],offers=c['offers'],
            allowed={owner:sorted({a[i] for a in allowed}) for i,owner in enumerate(roles)},
            joint={'witness':[list(a) for a in allowed]})
        tariffs[name]=dict(at=c['at'],minimum=best,acceptable=[dict(actions=list(a),cost=prices[a]) for a in allowed])
    query=dict(owners=list(roles),costs=job['costs'],scopes={'witness':list(roles)},worlds=worlds)
    for opt in ('objective','budget','state_limit','assignment_limit','custody'):
        if opt in job:query[opt]=job[opt]
    if total:
        return total_price(query,tariffs,job.get('inquiry_limit',128))
    result=plan(query)
    return dict(status=result['status'],roles=roles,max_extra=overhead,contexts=tariffs,inquiry=result)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('job',type=Path)
    args=parser.parse_args()
    try:out=commission(read(args.job))
    except (ValueError,KeyError,TypeError,OSError,AttributeError,RecursionError) as e:parser.exit(2,str(e)+'\n')
    print(json.dumps(out,sort_keys=True,indent=2))
