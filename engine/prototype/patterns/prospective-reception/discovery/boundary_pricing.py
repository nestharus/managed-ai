"""Price a finite, open commission by passing conditional values and returning local awards."""
import argparse
from itertools import combinations
import json
from math import prod
from pathlib import Path
from boundary_messages import catalog, leaf, join, route, Meter, Limit, read, require


def arrange(h):
    # Prefer nearby owners while shared decisions remain; join disconnected closed results afterwards.
    nodes={k:({k},set(f['scope']),k) for k,f in h['factors'].items()}
    all_uses={v:{k for k,f in h['factors'].items() if v in f['scope']} for v in h['domains']}
    n=0
    while len(nodes)>1:
        best=None
        for l,r in combinations(nodes,2):
            lm,lp,_=nodes[l];rm,rp,_=nodes[r]
            whole=lm|rm
            inputs=lp|rp
            live={v for v in inputs if v in h['open'] or not all_uses[v]<=whole}
            score=(not bool(lp&rp),prod(len(h['domains'][v]) for v in inputs),
                   prod(len(h['domains'][v]) for v in live),len(whole),l,r)
            if best is None or score<best[0]:
                best=(score,l,r,whole,live)
        _,l,r,whole,live=best
        a=nodes.pop(l)[2];b=nodes.pop(r)[2]
        n+=1;key='@'+str(n)
        while key in nodes:key='@'+key
        nodes[key]=(whole,live,[a,b])
    return next(iter(nodes.values()))[2]


def construct(spec):
    h=catalog(spec)
    tree=spec['tree'] if 'tree' in spec else arrange(h)
    def roster(node):
        if isinstance(node,str):return [node]
        require(isinstance(node,list) and len(node)==2,'invalid binary purchase tree')
        return roster(node[0])+roster(node[1])
    names=roster(tree)
    require(len(names)==len(set(names)) and set(names)==h['factors'].keys(),'tree must cover every owner exactly once')
    meter=Meter(spec.get('work_limit',300000));journals={};max_ports=0;max_rows=0
    def evaluate(node):
        nonlocal max_ports,max_rows
        if isinstance(node,str):msg,private=leaf(h,node,spec['factors'][node]['rows'],meter)
        else:msg,private=join(h,evaluate(node[0]),evaluate(node[1]),meter)
        journals[msg['seal']]=private
        max_ports=max(max_ports,len(msg['ports']));max_rows=max(max_rows,len(msg['rows']))
        return msg
    try:
        result=evaluate(tree)
    except Limit:
        return dict(status='resource-limit',candidate_rows=meter.count)
    out=dict(status='priced' if result['rows'] else 'infeasible',message=result,tree=tree,
             candidate_rows=meter.count,peak_ports=max_ports,peak_rows=max_rows)
    if 'award' in spec or not h['open'] and result['rows']:
        award=spec.get('award',{})
        require(isinstance(award,dict) and set(award)==set(result['ports']),'award must bind all and only the public fields')
        at=[award[v] for v in result['ports']];actions={}
        stack=[(result['seal'],at)]
        while stack:
            sig,key=stack.pop()
            chosen=route(journals[sig],sig,key)
            if 'owner' in chosen:actions[chosen['owner']]=chosen
            else:stack.extend((r['message'],r['at']) for r in chosen['children'])
        out.update(award=actions,cost=sum(v['cost'] for v in actions.values()))
    return out


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__);cli.add_argument('spec',type=Path)
    cli.add_argument('--operation',choices=('price','award','recover','retain'),default='price')
    a=cli.parse_args()
    try:
        operation=construct
        if a.operation=='award':
            from boundary_award import commission as operation
        elif a.operation=='recover':
            from boundary_recover import choose as operation
        elif a.operation=='retain':
            from boundary_retention import design as operation
        out=operation(read(a.spec))
    except (ValueError,KeyError,TypeError,OSError,AttributeError,RecursionError) as e:cli.exit(2,str(e)+'\n')
    print(json.dumps(out,indent=2,sort_keys=True))
