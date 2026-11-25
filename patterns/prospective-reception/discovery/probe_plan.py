"""Choose a least-worst-cost inquiry for one decision in a finite deterministic model."""
import argparse
from functools import lru_cache
import json
from pathlib import Path

def plan(spec):
    costs=spec['costs']
    models=spec['models']
    observed=spec.get('observed',{})
    budget=spec['budget']
    if not isinstance(models,dict) or not 1<=len(models)<=12 or not isinstance(costs,dict) or len(costs)>32:
        raise ValueError('expected 1..12 candidates and up to 32 named questions')
    if type(budget) is not int or budget<0 or any(type(v) is not int or v<=0 for v in costs.values()):
        raise ValueError('expected a nonnegative integer budget and positive integer costs')
    if not isinstance(observed,dict) or not observed.keys()<=costs.keys():
        raise ValueError('observations must name offered questions')
    for model in models.values():
        if not isinstance(model,dict) or not isinstance(model.get('answers'),dict) or model['answers'].keys()!=costs.keys():
            raise ValueError('every candidate must predict every offered return')
        if not all(isinstance(v,str) and v for v in [model.get('action'),*model['answers'].values()]):
            raise ValueError('actions and predicted returns must be nonempty strings')
    if not all(isinstance(v,str) and v for v in observed.values()):
        raise ValueError('observed returns must be nonempty strings')
    remaining=tuple(sorted(k for k,m in models.items() if all(m['answers'][q]==v for q,v in observed.items())))
    def actions(ids):return sorted({models[i]['action'] for i in ids})

    @lru_cache(None)
    def solve(ids):
        if len(actions(ids))==1:return (0,None)
        best=None
        for q,cost in sorted(costs.items()):
            groups={}
            for i in ids:groups.setdefault(models[i]['answers'][q],[]).append(i)
            if len(groups)<2:continue
            children=[solve(tuple(group)) for group in groups.values()]
            if any(child is None for child in children):continue
            candidate=(cost+max(child[0] for child in children),q)
            if best is None or candidate<best:best=candidate
        return best

    result={'remaining':list(remaining),'actions':actions(remaining),'question':None,'worst_case_cost':None}
    if not remaining:return dict(result,status='outside-models')
    solution=solve(remaining)
    if solution is None:return dict(result,status='inseparable')
    cost,q=solution
    result['worst_case_cost']=cost
    if q is None:return dict(result,status='action-agrees')
    if cost>budget:return dict(result,status='over-budget')
    return dict(result,status='ask',question=q)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('problem',type=Path,help='JSON question costs, candidate returns and actions, observations and remaining budget')
    args=parser.parse_args()
    try: result=plan(json.loads(args.problem.read_bytes()))
    except (ValueError,TypeError,KeyError,OSError) as exc:parser.exit(2,str(exc)+'\n')
    print(json.dumps(result,indent=2,sort_keys=True))
