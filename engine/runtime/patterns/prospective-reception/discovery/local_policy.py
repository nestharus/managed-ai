"""Check finite buyer promises against the joint decisions implementable from local observations."""
import argparse
from fractions import Fraction
from itertools import product
import json
from math import prod
from pathlib import Path
import re

MAX_TABLES = 256
MAX_PIVOTS = 1024


def number(value):
    if type(value) is int and abs(value)<=999999999:
        return Fraction(value)
    if not isinstance(value,str) or not re.fullmatch(
            r'-?(?:0|[1-9][0-9]{0,8})(?:/[1-9][0-9]{0,8}|\.[0-9]{1,9})?',value):
        raise ValueError('use a bounded integer, fraction or fixed decimal')
    return Fraction(value)


def name(value):
    return isinstance(value,str) and 0<len(value)<=160


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field: '+key)
        result[key]=value
    return result


def game(columns):
    """Solve a positive matrix game by a rational packing LP, with Bland pivot choices."""
    n=len(columns);m=len(columns[0])
    shift=1-min(min(c) for c in columns)
    rows=[[x+shift for x in col]+[Fraction(i==j) for j in range(n)]+[Fraction(1)]
          for i,col in enumerate(columns)]
    objective=[Fraction(-1)]*m+[Fraction(0)]*(n+1)
    basis=list(range(m,m+n))
    for _ in range(MAX_PIVOTS):
        enter=next((i for i,coef in enumerate(objective[:-1]) if coef<0),None)
        if enter is None:
            total=objective[-1]
            prices=[Fraction(0)]*m
            for k,row in zip(basis,rows):
                if k<m:
                    prices[k]=row[-1]/total
            return 1/total-shift,[x/total for x in objective[m:-1]],prices
        choices=[(row[-1]/row[enter],basis[i],i) for i,row in enumerate(rows) if row[enter]>0]
        _,_,leave=min(choices)
        pivot=rows[leave]
        pivot=[v/pivot[enter] for v in pivot]
        rows[leave]=pivot
        basis[leave]=enter
        for i,row in enumerate(rows):
            if i!=leave and row[enter]:
                rows[i]=[v-row[enter]*p for v,p in zip(row,pivot)]
        objective=[v-objective[enter]*p for v,p in zip(objective,pivot)]
    return None


def construct(spec):
    if not isinstance(spec,dict):raise ValueError('expected a policy commission')
    roles,worlds,requirements=(spec[k] for k in ('roles','worlds','requirements'))
    if not isinstance(roles,list) or not 1<=len(roles)<=3:raise ValueError('expected 1..3 roles')
    if not isinstance(worlds,dict) or not 1<=len(worlds)<=32:raise ValueError('expected 1..32 worlds')
    if not isinstance(requirements,list) or not 1<=len(requirements)<=24:
        raise ValueError('expected 1..24 receiving requirements')
    role_names=[]; keys=[]; menus=[]
    for role in roles:
        who,views=role['id'],role['views']
        if not name(who) or who in role_names or not isinstance(views,dict) or not 1<=len(views)<=32:
            raise ValueError('invalid role or its views')
        role_names.append(who)
        for view,actions in sorted(views.items()):
            if (not name(view) or not isinstance(actions,list) or not 1<=len(actions)<=4 or
                    not all(name(a) for a in actions) or len(set(actions))!=len(actions)):
                raise ValueError('each view needs 1..4 distinct legal action names')
            keys.append((who,view))
            menus.append(sorted(actions))
    positions={key:i for i,key in enumerate(keys)}
    seen=set(); indices={}; outcomes={}
    for w,world in sorted(worlds.items()):
        local=world['views']
        if not name(w) or not isinstance(local,list) or len(local)!=len(roles) or not all(name(v) for v in local):
            raise ValueError('expected one local view per role in each world')
        entries=list(zip(role_names,local))
        if any(e not in positions for e in entries):raise ValueError('unknown local view')
        seen.update(entries)
        ids=indices[w]=[positions[e] for e in entries]
        expected=set(product(*(menus[i] for i in ids)))
        table=outcomes[w]={}
        results=world['outcomes']
        if not isinstance(results,list) or len(results)!=len(expected):raise ValueError('incomplete outcome table')
        for row in results:
            acts=row['actions'];scores=row['scores']
            if not isinstance(acts,list) or not all(name(a) for a in acts):raise ValueError('invalid action tuple')
            code=tuple(acts)
            if code not in expected or code in table:raise ValueError('invalid or repeated joint action')
            if not isinstance(scores,dict) or not 1<=len(scores)<=16 or not all(name(k) for k in scores):
                raise ValueError('invalid score record')
            table[code]={k:number(v) for k,v in scores.items()}
    if seen!=set(keys):raise ValueError('remove uncommissioned local views')
    clauses=[]; labels=set()
    for req in requirements:
        label,metric,mix=req['id'],req['metric'],req['mix']
        if not name(label) or label in labels or not name(metric):raise ValueError('invalid requirement identity')
        labels.add(label)
        if not isinstance(mix,dict) or not mix or not set(mix)<=set(worlds):raise ValueError('invalid scenario mixture')
        weights={w:number(v) for w,v in mix.items()}
        if min(weights.values())<=0 or any(metric not in o for w in mix for o in outcomes[w].values()):
            raise ValueError('each weighted world needs positive weight and the complete requested metric')
        total=sum(weights.values());weights={w:v/total for w,v in weights.items()}
        if ('at_least' in req)==('at_most' in req):raise ValueError('specify exactly one bound')
        sense=1 if 'at_least' in req else -1
        target=number(req['at_least'] if sense==1 else req['at_most'])
        scale=number(req.get('scale',1))
        if scale<=0:raise ValueError('scale must be positive')
        clauses.append((label,metric,weights,sense,target,scale))
    expose=spec.get('expose',[])
    if not isinstance(expose,list) or len(expose)>len(keys):
        raise ValueError('invalid exposed local entries')
    public_keys=[]
    for item in expose:
        if (not isinstance(item,list) or len(item)!=2 or not all(name(v) for v in item)
                or tuple(item) not in positions or tuple(item) in public_keys):
            raise ValueError('each exposed entry must be a distinct role and local view')
        public_keys.append(tuple(item))
    count=prod(map(len,menus))
    if count>MAX_TABLES:
        return dict(status='resource-limit',reason='local-tables',tables=count,limit=MAX_TABLES)
    groups={}
    for actions in product(*menus):
        scores={w:outcomes[w][tuple(actions[i] for i in ids)] for w,ids in indices.items()}
        col=tuple(sense*(sum(p*scores[w][metric] for w,p in weights.items())-target)/scale
                  for _,metric,weights,sense,target,scale in clauses)
        signal=tuple(actions[positions[k]] for k in public_keys)
        groups.setdefault(signal,{}).setdefault(col,actions)
    best=None
    bounds=[]
    for signal,representatives in groups.items():
        result=solve_group(representatives,role_names,keys,clauses)
        if result is None:return dict(status='resource-limit',reason='pivots',limit=MAX_PIVOTS)
        if public_keys:
            bounds.append(dict(signal=list(signal),margin=result['margin'],clause_prices=result['clause_prices']))
        if best is None or Fraction(result['margin'])>Fraction(best['margin']):
            best=result
            if public_keys:best['chosen_signal']=list(signal)
    best['local_tables']=count
    if public_keys:best['signal_bounds']=bounds
    return best


def solve_group(representatives,role_names,keys,clauses):
    cols=list(representatives)
    solved=game(cols)
    if solved is None:return None
    margin,chances,prices=solved
    # A feasible primal distribution and a matching universal upper bound certify optimality.
    if min(chances)<0 or min(prices)<0 or sum(chances)!=1 or sum(prices)!=1:
        raise ArithmeticError('invalid game weights')
    lower=[sum(p*c[i] for p,c in zip(chances,cols)) for i in range(len(clauses))]
    upper=max(sum(v*z for v,z in zip(col,prices)) for col in cols)
    if min(lower)!=margin or upper!=margin:raise ArithmeticError('policy and obstruction certificates disagree')
    plans=[]
    for col,p in zip(cols,chances):
        if p:
            policy={who:{} for who in role_names}
            for (who,view),act in zip(keys,representatives[col]):policy[who][view]=act
            plans.append(dict(probability=str(p),local=policy))
    achieved={label:str(target+sense*scale*v) for (label,_,_,sense,target,scale),v in zip(clauses,lower)}
    return dict(status='feasible' if margin>=0 else 'infeasible',margin=str(margin),
                pure_margin=str(max(min(c) for c in cols)),distinct_scores=len(cols),
                plans=plans,achieved=achieved,clause_prices={c[0]:str(p) for c,p in zip(clauses,prices) if p})

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec',type=Path)
    args=parser.parse_args()
    try:
        answer=construct(json.loads(args.spec.read_bytes(),object_pairs_hook=unique_object))
    except (OSError,KeyError,TypeError,ValueError,ZeroDivisionError) as exc:
        parser.exit(2,str(exc)+'\n')
    print(json.dumps(answer,indent=2,sort_keys=True))
