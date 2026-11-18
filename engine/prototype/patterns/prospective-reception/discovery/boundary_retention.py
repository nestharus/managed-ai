"""Price a sufficient retained menu against bounded future tariff differences."""
import argparse
from functools import lru_cache
import json
from pathlib import Path
from boundary_messages import digest, read, require, Limit


def design(job):
    msg=job['message']
    require(msg['seal']==digest({k:v for k,v in msg.items() if k!='seal'}),'corrupt boundary message')
    rows=msg['rows'];n=len(rows)
    require(1<=n<=96,'retention supports one to 96 feasible boundary rows')
    index={tuple(row['at']):j for j,row in enumerate(rows)}
    costs=[r['cost'] for r in rows]
    def integer(x):
        require(type(x) is int and abs(x)<=1000000000,'invalid integer bound or fee')
        return x
    tol=integer(job.get('tolerance',0));require(tol>=0,'negative tolerance')
    limits=job['limits']
    require(isinstance(limits,list) and len(limits)==len(index)==n,'warrant must cover the priced menu')
    # d[i][j] bounds the future surcharge on j minus the surcharge on i.
    anchor=n
    d=[[None]*(n+1) for _ in range(n+1)]
    for i in range(n+1):d[i][i]=0
    keep={};seen=set()
    for v in limits:
        i=index[tuple(v['at'])]
        low=integer(v['low']);high=integer(v['high'])
        require(i not in seen and low<=high,'overlapping or invalid per-row bounds')
        seen.add(i);d[anchor][i]=high;d[i][anchor]=-low
        if v['keep'] is not None:
            fee=integer(v['keep']);require(fee>=0,'negative retention fee');keep[i]=fee
    constraints=job.get('differences',[])
    require(isinstance(constraints,list) and len(constraints)<=20000,'invalid difference constraints')
    for c in constraints:
        a=index[tuple(c['from'])];b=index[tuple(c['to'])];v=integer(c['upper'])
        d[a][b]=v if d[a][b] is None else min(d[a][b],v)
    for k in range(n+1):
        for i in range(n+1):
            if d[i][k] is None:continue
            for j in range(n+1):
                if d[k][j] is None:continue
                through=d[i][k]+d[k][j]
                if d[i][j] is None or through<d[i][j]:
                    d[i][j]=through
        if any(d[i][i]<0 for i in range(n+1)):
            return dict(status='inconsistent-warrant')
    def assess(chosen):
        if not chosen:return dict(worst_loss=None,reason='empty menu')
        loss,j=max((min(costs[i]-costs[j]+d[j][i] for i in chosen),j) for j in range(n))
        tariffs=[d[j][i]-d[j][anchor] for i in range(n)]
        invoices=[c+f for c,f in zip(costs,tariffs)]
        original=min(range(n),key=lambda i:invoices[i]);retained=min(chosen,key=lambda i:invoices[i])
        require(invoices[retained]-invoices[original]==loss,'inconsistent separating price book')
        return dict(worst_loss=loss,full_winner=rows[original]['at'],retained_winner=rows[retained]['at'],
            full_invoice=invoices[original],retained_invoice=invoices[retained],
            future=[dict(at=r['at'],surcharge=f) for r,f in zip(rows,tariffs)])
    coverage={i:sum(1<<j for j in range(n) if costs[i]-costs[j]+d[j][i]<=tol) for i in keep}
    available=sorted(keep);all_targets=(1<<n)-1
    if any(not any(mask&(1<<j) for mask in coverage.values()) for j in range(n)):
        return dict(status='infeasible',all_available=assess(available))
    work=job.get('state_limit',100000)
    require(type(work) is int and 1<=work<=1000000,'invalid state limit')
    states=0
    @lru_cache(None)
    def solve(t):
        nonlocal states
        if states>=work:raise Limit('retention state limit')
        states+=1
        if not t:return 0,()
        candidates=min(([i for i in available if coverage[i]&(1<<j)] for j in range(n) if t&(1<<j)),key=len)
        best=None
        for i in candidates:
            value,rest=solve(t & ~coverage[i])
            candidate=keep[i]+value,tuple(sorted((i,*rest)))
            if best is None or candidate<best:best=candidate
        return best
    try:
        fee,selected=solve(all_targets);status='optimal'
    except Limit:
        fee,selected=sum(keep.values()),available;status='resource-limit'
    out=dict(status=status,fee=fee,retained=[rows[i]['at'] for i in selected],states=states,
             guarantee=assess(selected))
    if 'evaluate' in job:
        out['evaluated']=assess([index[tuple(a)] for a in job['evaluate']])
    return out


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__);cli.add_argument('job',type=Path);a=cli.parse_args()
    try:out=design(read(a.job))
    except (ValueError,KeyError,TypeError,OSError,AttributeError) as err:cli.exit(2,str(err)+'\n')
    print(json.dumps(out,indent=2,sort_keys=True))
