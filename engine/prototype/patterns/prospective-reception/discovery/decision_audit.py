"""Audit a receiving cohort from uniformly sampled, distinct members of an earlier sealed frame."""
import argparse
from fractions import Fraction
from hashlib import sha256
import json
from math import comb
from pathlib import Path
import re


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key!r}')
        result[key] = value
    return result


def ratio(value):
    if type(value) is not int and (not isinstance(value,str) or not re.fullmatch(
            r'(?:0|[1-9][0-9]{0,8})(?:/[1-9][0-9]{0,8}|\.[0-9]{1,9})?',value)):
        raise ValueError('expected a short exact fraction, integer or fixed decimal')
    x=Fraction(value)
    if not 0<=x<=1:
        raise ValueError('ratio outside [0,1]')
    return x


def upper_rate(n, bad, drops, adds, flips):
    final_bad=min(n,bad+flips)
    return Fraction(final_bad+adds,n-min(drops,n-final_bad)+adds)


def boundaries(n, unsafe, risk, looks):
    """Count surviving ordered binary prefixes and their full-length completions."""
    if unsafe is None:
        return []
    denom=comb(n,unsafe)
    prefixes={0:1}
    absorbed=weight=0
    total=sum(looks.values())
    schedule=[]
    for t in range(1,max(looks)+1):
        following={}
        for bad,ways in prefixes.items():
            if t-bad<=n-unsafe:
                following[bad]=following.get(bad,0)+ways
            if bad<unsafe:
                following[bad+1]=following.get(bad+1,0)+ways
        prefixes=following
        if t not in looks:
            continue
        weight+=looks[t]
        allowed=risk*weight/total
        cutoff=-1
        for bad in range(t+1):
            ways=prefixes.get(bad,0)
            count=ways*comb(n-t,unsafe-bad) if ways else 0
            if absorbed+count>allowed*denom:
                break
            cutoff=bad
            absorbed+=count
            prefixes.pop(bad,None)
        schedule.append(dict(draws=t,accept_at_most=cutoff,cumulative_false_pass=str(Fraction(absorbed,denom))))
    return schedule


def disposition(t, good, bad, n, moves, limit, at, last):
    low=1-upper_rate(n,n-bad,*moves)
    high=upper_rate(n,n-good,*moves)
    if high<=limit: status='certain-pass'
    elif low>limit: status='certain-fail'
    elif t in at and t-good<=at[t]: status='risk-pass'
    elif t==last: status='inconclusive'
    else: status='draw'
    return status,low,high


def operating_curve(case,n,moves,limit,at,last):
    if not isinstance(case,dict) or set(case)!={'match','mismatch','unread'}:
        raise ValueError('a performance scenario needs match, mismatch and unread counts')
    good,bad,unread=(case[k] for k in ('match','mismatch','unread'))
    if any(type(v) is not int or v<0 for v in (good,bad,unread)) or good+bad+unread!=n:
        raise ValueError('scenario counts must partition the frame')
    def suffix_ways(g,b,u):
        return comb(g+b+u,g)*comb(b+u,b)
    total=suffix_ways(good,bad,unread)
    outcomes=dict.fromkeys(('certain-pass','risk-pass','certain-fail','inconclusive'),0)
    bill=0
    prefixes={(0,0):1}
    for t in range(last+1):
        following={}
        for (g,b),ways in prefixes.items():
            u=t-g-b
            decision,_,_=disposition(t,g,b,n,moves,limit,at,last)
            if decision!='draw':
                count=ways*suffix_ways(good-g,bad-b,unread-u)
                outcomes[decision]+=count
                bill+=t*count
                continue
            for allowed,key in ((g<good,(g+1,b)),(b<bad,(g,b+1)),(u<unread,(g,b))):
                if allowed:
                    following[key]=following.get(key,0)+ways
        prefixes=following
    return dict(composition=case,outcomes={k:str(Fraction(v,total)) for k,v in outcomes.items()},
                mean_draws=str(Fraction(bill,total)))


def construct(spec):
    names=('epoch','duty','measure','draw_owner')
    if not isinstance(spec,dict) or any(not isinstance(spec.get(k),str) or not spec[k] for k in names):
        raise ValueError('expected epoch, duty, measure and draw_owner names')
    frame=spec['frame']
    if (not isinstance(frame,list) or not 1<=len(frame)<=256 or
            not all(isinstance(x,str) and x for x in frame) or len(set(frame))!=len(frame)):
        raise ValueError('expected 1..256 distinct unit names in the sealed frame')
    n=len(frame)
    limit,risk=ratio(spec['limit']),ratio(spec['risk'])
    if risk==1:
        raise ValueError('risk must be less than one')
    change=spec['change']
    if not isinstance(change,dict) or not isinstance(change.get('basis'),str) or not change['basis']:
        raise ValueError('name the basis of the receiving change allowances')
    drops,adds,flips=(change[k] for k in ('drop','add','flip'))
    if (not all(type(v) is int and v>=0 for v in (drops,adds,flips)) or
            drops>=n or flips>n or adds>256):
        raise ValueError('invalid change allowances; retain at least one original unit')
    looks=spec['looks']
    if not isinstance(looks,list) or not 1<=len(looks)<=32:
        raise ValueError('expected 1..32 precommitted looks')
    schedule={}
    prev=0
    for row in looks:
        if (not isinstance(row,list) or len(row)!=2 or any(type(i) is not int for i in row)
                or not prev<row[0]<=n or not 1<=row[1]<=10000):
            raise ValueError('each look needs an increasing draw count and positive integer weight')
        prev,w=row
        schedule[prev]=w
    plan={k:spec[k] for k in names}
    plan.update(contract='g60.decision-audit.v1',frame=sorted(frame),limit=str(limit),risk=str(risk),
                looks=looks,change=dict(drop=drops,add=adds,flip=flips,basis=change['basis']))
    plan_id=sha256(json.dumps(plan,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    null=next((m for m in range(n+1) if upper_rate(n,m,drops,adds,flips)>limit),None)
    table=boundaries(n,null,risk,schedule)
    at={row['draws']:row['accept_at_most'] for row in table}
    records=spec.get('reads',[])
    if not isinstance(records,list) or len(records)>prev:
        raise ValueError('invalid number of draw receipts')
    cases=spec.get('evaluate',[])
    if (not isinstance(cases,list) or len(cases)>4 or
            (cases and (records or n>64))):
        raise ValueError('evaluate up to four scenarios before drawing on frames of at most 64')
    moves=(drops,adds,flips)
    curves=[operating_curve(c,n,moves,limit,at,prev) for c in cases]
    valid=set(frame);opened=set()
    bad=good=0
    for t in range(len(records)+1):
        status,low,high=disposition(t,good,bad,n,moves,limit,at,prev)
        if t==len(records):
            result=dict(status=status,plan=plan_id,draws=t,unread=t-bad-good,
                        possible_rate=[str(low),str(high)],first_unsafe_frame_count=null,looks=table,
                        false_pass_bound=table[-1]['cumulative_false_pass'] if table else '0')
            if curves:
                result['before_draw_performance']=curves
            return result
        if status!='draw':
            raise ValueError('receipt supplied after this audit already stopped')
        row=records[t]
        if not isinstance(row,dict) or row.get('epoch')!=spec['epoch'] or row.get('plan')!=plan_id:
            raise ValueError('receipt epoch or audit plan differs')
        unit=row.get('id')
        if not isinstance(unit,str) or unit not in valid or unit in opened:
            raise ValueError('unrecognized or repeated draw')
        opened.add(unit)
        answer=row.get('result')
        if answer not in ('match','mismatch','unread'):
            raise ValueError('invalid draw result')
        bad+=answer=='mismatch'
        good+=answer=='match'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec',type=Path)
    args=parser.parse_args()
    try:
        result=construct(json.loads(args.spec.read_bytes(), object_pairs_hook=unique_object))
    except (OSError,ValueError,TypeError,KeyError,ZeroDivisionError) as exc:
        parser.exit(2,str(exc)+'\n')
    print(json.dumps(result,sort_keys=True,indent=2))
