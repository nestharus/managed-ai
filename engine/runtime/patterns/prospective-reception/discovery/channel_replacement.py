"""Bound directional replacement loss for a finite, timed source and target experiment."""
import argparse
from fractions import Fraction as F
from functools import lru_cache
import json
from pathlib import Path

from local_policy import game, name, number, unique_object


def tape(value):
    if not isinstance(value,list) or not 1<=len(value)<=8 or not all(name(k) for k in value):
        raise ValueError("expected a tape of one to eight named symbols")
    return tuple(value)


def law(rows):
    if not isinstance(rows,list) or not 1<=len(rows)<=64:
        raise ValueError("expected 1..64 weighted tapes per law")
    weights={}
    for row in rows:
        key=tape(row['tape'])
        w=row['weight']
        if key in weights or type(w) is not int or not 1<=w<=1000000:
            raise ValueError("expected distinct tapes with positive bounded integer weights")
        weights[key]=w
    total=sum(weights.values())
    return {k:F(v,total) for k,v in weights.items()}


def children(paths):
    out={}
    for path in paths:
        for j in range(len(path)):
            out.setdefault(path[:j],set()).add(path[:j+1])
    return {k:sorted(v) for k,v in out.items()}


def construct(spec):
    worlds=spec['worlds'];mode=spec.get('timing','online');limit=spec.get('round_limit',64)
    if not isinstance(worlds,dict) or not 1<=len(worlds)<=16 or not all(map(name,worlds)):
        raise ValueError("expected 1..16 named worlds")
    if mode not in ('online','after') or type(limit) is not int or not 1<=limit<=96:
        raise ValueError("timing must be online or after; round_limit must be 1..96")
    tol=number(spec.get('tolerance',0));stop=spec.get('stop_on_decision',False)
    if not 0<=tol<=1 or type(stop) is not bool:raise ValueError("invalid tolerance or stop flag")
    states=sorted(worlds)
    P=[law(worlds[w]['source']) for w in states]
    Q=[law(worlds[w]['target']) for w in states]
    S=sorted(set().union(*P));T=sorted(set().union(*Q))
    if len(S)>64 or len(T)>64 or len({len(p) for p in S+T})!=1:
        raise ValueError("use a common horizon and at most 64 distinct tapes per side")
    horizon=len(S[0])
    si={p:i for i,p in enumerate(S)};ti={p:i for i,p in enumerate(T)}
    s_children=children(S);t_children=children(T)
    forbid=spec.get('forbid',[])
    if not isinstance(forbid,list) or len(forbid)>4096:raise ValueError("invalid forbidden receipt list")
    blocked=set()
    for row in forbid:
        s,t=tape(row['source']),tape(row['target'])
        if s not in si or t not in ti or (s,t) in blocked:raise ValueError("invalid or repeated forbidden pair")
        blocked.add((s,t))

    def best_reply(reward):
        # Reconstruct one optimal path map without enumerating all possible maps.
        if mode=='after':
            plan=[]
            for s in S:
                viable=[t for t in T if (s,t) not in blocked]
                if not viable:return None
                plan.append(ti[max(viable,key=lambda t:reward[si[s]][ti[t]])])
            return tuple(plan)
        choice={}
        @lru_cache(None)
        def best(s,prefix):
            value=None
            for nxt in t_children[prefix]:
                if len(s)==horizon:
                    if (s,nxt) in blocked:continue
                    score=reward[si[s]][ti[nxt]]
                else:
                    parts=[best(child,nxt) for child in s_children[s]]
                    if None in parts:continue
                    score=sum(parts)
                if value is None or score>value:
                    value=score;choice[s,prefix]=nxt
            return value
        plan=[0]*len(S)
        def recover(s,prefix):
            selected=choice[s,prefix]
            if len(s)==horizon:
                plan[si[s]]=ti[selected]
            else:
                for child in s_children[s]:recover(child,selected)
        for s in s_children[()]:
            if best(s,()) is None:return None
            recover(s,())
        return tuple(plan)

    n,m=len(S),len(T)
    first=best_reply([[F(0)]*m for _ in S])
    if first is None:return dict(status='no-admissible-converter',timing=mode)
    p=[[law_i.get(s,F(0)) for s in S] for law_i in P]
    q=[[law_i.get(t,F(0)) for t in T] for law_i in Q]
    plans=[first];tests=[(0,())]
    @lru_cache(None)
    def emitted(plan,w):
        row=[F(0)]*m
        for i,j in enumerate(plan):row[j]+=p[w][i]
        return tuple(row)
    @lru_cache(None)
    def value(plan,test):
        w,A=test
        row=emitted(plan,w)
        return sum((row[j]-q[w][j] for j in A),F(0))
    def display(plan):
        rules={}
        for s,j in zip(S,plan):
            if mode=='after':
                rules[s]=T[j]
            else:
                for k in range(1,horizon+1):
                    assert rules.setdefault(s[:k],T[j][:k])==T[j][:k]
        return [dict(after=list(s),emit=list(t)) for s,t in sorted(rules.items())]
    lower=F(-1);upper=F(2)
    reason='round-limit'
    for iteration in range(1,limit+1):
        matrix=[[value(plan,test) for test in tests] for plan in plans]
        solved=game(matrix)
        if solved is None:
            reason='matrix-pivot-limit'
            break
        _,mix,prices=solved
        supported=[(plan,prob) for plan,prob in zip(plans,mix) if prob]
        gaps=[]
        for w in range(len(states)):
            probs=[sum(prob*emitted(plan,w)[j] for plan,prob in supported) for j in range(m)]
            A=tuple(j for j in range(m) if q[w][j]>probs[j])
            gaps.append((sum((q[w][j]-probs[j] for j in A),F(0)),(w,A)))
        gap,test=max(gaps,key=lambda v:v[0])
        if gap<upper:
            upper=gap
            converter=[dict(probability=str(prob),rules=display(plan)) for plan,prob in supported]
            world_errors={w:str(v[0]) for w,v in zip(states,gaps)}
        # Aggregate the tests into an ordinary bounded buyer payoff for each full receipt.
        prior=[F(0)]*len(states)
        gains=[[F(0)]*m for _ in states]
        for (w,A),v in zip(tests,prices):
            prior[w]+=v
            for j in A:gains[w][j]+=v
        reward=[[sum((p[w][i]*gains[w][j] for w in range(len(states))),F(0))
                 for j in range(m)] for i in range(n)]
        response=best_reply(reward)
        source_value=sum(reward[i][j] for i,j in enumerate(response))
        target_value=sum(q[w][j]*gains[w][j] for w in range(len(states)) for j in range(m))
        bound=max(F(0),target_value-source_value)
        if bound>lower:
            lower=bound
            counter=dict(target_follow=str(target_value),source_best=str(source_value),
                source_rule=display(response),
                terms=[dict(world=w,probability=str(prior[i]),rewards=[
                    dict(tape=list(t),value=str(v/prior[i])) for t,v in zip(T,gains[i]) if v])
                    for i,w in enumerate(states) if prior[i]])
        if upper==lower:reason='matching-certificates';break
        if stop and (upper<=tol or lower>tol):reason='tolerance-decided';break
        if iteration<limit:
            if test not in tests:tests.append(test)
            if response not in plans:plans.append(response)
    if lower<0: return dict(status='resource-limit',reason=reason)
    if lower>upper:raise ArithmeticError("inconsistent replacement certificates")
    return dict(status='exact' if lower==upper else 'bounded',reason=reason,timing=mode,
                lower=str(lower),upper=str(upper),tolerance=str(tol),
                decision='replace' if upper<=tol else 'cannot-replace' if lower>tol else 'unresolved',
                converter=converter,world_errors=world_errors,countercommission=counter,
                iterations=iteration,generated_maps=len(plans),generated_tests=len(tests))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec',type=Path)
    args=parser.parse_args()
    try:
        result=construct(json.loads(args.spec.read_bytes(),object_pairs_hook=unique_object))
    except (KeyError,TypeError,ValueError,OSError,ZeroDivisionError) as exc:
        parser.exit(2,str(exc)+'\n')
    print(json.dumps(result,indent=2,sort_keys=True))
