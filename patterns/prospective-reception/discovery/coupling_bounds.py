"""Sharp bounds on any public discrepancy under prescribed conditional laws of two finite trees."""
import argparse
from fractions import Fraction
from itertools import product
import json
import re
from pathlib import Path


def tree(spec):
    root, nodes = spec['root'], spec['nodes']
    if not isinstance(nodes, dict) or not 1 <= len(nodes) <= 48 or not isinstance(root, str) or root not in nodes:
        raise ValueError('expected a named root and 1..48 nodes')
    layers, laws, seen = [[root]], {}, {root}
    for level in layers:
        following = []
        for name in level:
            row = nodes[name]
            if not isinstance(row,dict) or not isinstance(row.get('public'),str):
                raise ValueError('each node needs a public string')
            children = row.get('next', {})
            if not isinstance(children,dict) or len(children)>4:
                raise ValueError('expected at most four children per node')
            for child, weight in sorted(children.items()):
                if not child or child not in nodes or child in seen or type(weight) is not int or not 1<=weight<=10000:
                    raise ValueError('expected fresh named children with positive integer weights up to 10000')
                seen.add(child)
                following.append(child)
            laws[name] = {c:Fraction(n,sum(children.values())) for c,n in sorted(children.items())}
        if following:
            if not all(laws[n] for n in level) or len(layers)>=9:
                raise ValueError('use a common terminal depth of at most eight on every path')
            layers.append(sorted(following))
    if seen!=set(nodes):
        raise ValueError('unreachable nodes')
    return root, nodes, layers, laws


def mass(value):
    if type(value) is not int and (not isinstance(value,str) or not re.fullmatch(
            r'(?:0|[1-9][0-9]{0,8})(?:/[1-9][0-9]{0,8}|\.[0-9]{1,9})?', value)):
        raise ValueError('use an integer, a fraction with up to nine digits per part, or a short fixed decimal')
    q=Fraction(value)
    if not 0<=q<=1:
        raise ValueError('conditional mass must be in [0,1]')
    return q


def transport(left, right, bounds, costs):
    """Send residual row mass to columns at minimum cost, with reversible residual edges."""
    rows, columns = list(left),list(right)
    supply, demand = dict(left),dict(right)
    source, sink = len(rows)+len(columns),len(rows)+len(columns)+1
    graph=[[] for _ in range(sink+1)]
    def edge(a,b,capacity,cost):
        forward=[b,len(graph[b]),capacity,cost]
        back=[a,len(graph[a]),0,-cost]
        graph[a].append(forward)
        graph[b].append(back)
        return back
    flows={}
    for i,a in enumerate(rows):
        for j,b in enumerate(columns):
            lo,hi=bounds.get((a,b),(0,1))
            if (a,b) not in costs:
                if lo: return None
                continue
            supply[a]-=lo
            demand[b]-=lo
            back=edge(i,len(rows)+j,hi-lo,costs[a,b])
            flows[a,b]=(lo,back)
    if min([*supply.values(),*demand.values()])<0:
        return None
    for i,a in enumerate(rows): edge(source,i,supply[a],0)
    for j,b in enumerate(columns): edge(len(rows)+j,sink,demand[b],0)
    remaining=sum(supply.values())
    while remaining:
        distance=[None]*len(graph)
        parent=[None]*len(graph)
        distance[source]=0
        for _ in range(len(graph)-1):
            changed=False
            for a,edges in enumerate(graph):
                if distance[a] is None: continue
                for k,(b,_,cap,cost) in enumerate(edges):
                    price=distance[a]+cost
                    if cap and (distance[b] is None or price<distance[b]):
                        distance[b]=price
                        parent[b]=(a,k)
                        changed=True
            if not changed: break
        if parent[sink] is None: return None
        path=[]
        b=sink
        while b!=source:
            a,k=parent[b]
            path.append((a,k))
            b=a
        step=min(graph[a][k][2] for a,k in path)
        for a,k in path:
            b,rev,_,_=graph[a][k]
            graph[a][k][2]-=step
            graph[b][rev][2]+=step
        remaining-=step
    return {cell:lo+back[2] for cell,(lo,back) in flows.items() if lo+back[2]}


def construct(spec):
    l, left, ll, lp = tree(spec['left'])
    r, right, rl, rp = tree(spec['right'])
    if len(ll)!=len(rl):
        raise ValueError('trees must share a terminal depth')
    conditions=spec.get('conditions',[])
    if not isinstance(conditions,list) or len(conditions)>512:
        raise ValueError('expected up to 512 conditional cell bounds')
    layers=[list(product(a,b)) for a,b in zip(ll,rl)]
    pairs={pair for layer in layers for pair in layer}
    limits={}
    references=set()
    for row in conditions:
        if not isinstance(row,dict) or not isinstance(row.get('basis'),str) or not row['basis']:
            raise ValueError('name the evidence or assumption for each constraint')
        at, to = row['at'],row['to']
        if any(not isinstance(p,list) or len(p)!=2 or not all(isinstance(n,str) for n in p) for p in (at,to)):
            raise ValueError('expected two node names for at and to')
        a,b=at; c,d=to
        if (a,b) not in pairs or c not in lp[a] or d not in rp[b]:
            raise ValueError('conditional bound must refer to children of a same-depth pair')
        lo,hi=mass(row.get('lower',0)),mass(row.get('upper',1))
        if lo>hi or not ('lower' in row or 'upper' in row):
            raise ValueError('invalid conditional interval')
        table=limits.setdefault((a,b),{})
        previous=table.get((c,d),(0,1))
        table[c,d]=(max(previous[0],lo),min(previous[1],hi))
        references.add(row['basis'])
    best,worst={},{}
    decisions=[{},{}]
    blocked=[]
    for layer in reversed(layers):
        for a,b in layer:
            pair=(a,b)
            mismatch=left[a]['public']!=right[b]['public']
            if not lp[a]:
                best[pair]=worst[pair]=int(mismatch)
                continue
            bounds=limits.get(pair,{})
            children=[p for p in product(lp[a],rp[b]) if p in best]
            lo_cost={p:1 if mismatch else best[p] for p in children}
            hi_cost={p:1 if mismatch else worst[p] for p in children}
            fit=None if any(lo>hi for lo,hi in bounds.values()) else transport(lp[a],rp[b],bounds,lo_cost)
            if fit is None:
                blocked.append([a,b])
                continue
            alt=transport(lp[a],rp[b],bounds,{p:-v for p,v in hi_cost.items()})
            decisions[0][pair],decisions[1][pair]=fit,alt
            best[pair]=sum(v*lo_cost[p] for p,v in fit.items())
            worst[pair]=sum(v*hi_cost[p] for p,v in alt.items())
    root=(l,r)
    if root not in best:
        return {'status':'infeasible','blocked_pairs':blocked,'basis':sorted(references)}
    result={'status':'bounded','basis':sorted(references)}
    for kind,values,tables in zip(('minimum','maximum'),(best,worst),decisions):
        pending=[root];plan=[]
        while pending:
            p=pending.pop()
            if p in tables:
                m=tables[p]
                plan.append({'at':list(p),'mass':[[a,b,str(q)] for (a,b),q in sorted(m.items())]})
                pending.extend(sorted(m,reverse=True))
        result[kind]={'probability':str(values[root]),'steps':plan}
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec',type=Path)
    args=parser.parse_args()
    try:
        result=construct(json.loads(args.spec.read_bytes()))
    except (OSError,ValueError,TypeError,KeyError,ZeroDivisionError) as exc:
        parser.exit(2,str(exc)+'\n')
    print(json.dumps(result,indent=2,sort_keys=True))
