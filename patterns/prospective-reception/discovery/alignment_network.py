"""Price and check a binary correspondence between peers under grouped issuer failures."""
from heapq import heappop,heappush
from itertools import product
from alignment_space import named_map, integer, require
from boundary_pricing import construct as price
from local_policy import name


class PriceLimit(Exception):
    pass


def analyze(spec):
    peers=spec['peers'];request=spec['request']
    require(type(peers) is list and 2<=len(peers)<=40 and all(name(v) for v in peers)
            and len(set(peers))==len(peers),'invalid binary-frame peers')
    require(type(request) is list and len(request)==2 and request[0]!=request[1] and set(request)<=set(peers),
            'invalid requested peer pair')
    links=named_map(spec['links'],96,'invalid comparisons')
    offers=named_map(spec.get('offers',{}),18,'invalid comparison offers')
    require(not links.keys()&offers.keys(),'duplicate comparison occurrence')
    readings={}
    for k,row in (links|offers).items():
        a,b=row['ends'];require(a!=b and a in peers and b in peers and name(row['issuer']),'invalid comparison endpoints')
        if k in offers:integer(row['cost'],1,1000000)
        elif 'bit' in row:readings[k]=integer(row['bit'],0,1)
    faults=integer(spec.get('faults',0),0,96);erasures=integer(spec.get('erasures',0),0,96)
    budget=integer(spec.get('work_limit',300000),1,10000000)
    mode=spec.get('mode','qualify')
    require(mode in ('qualify','design'),'unknown network commission')
    def solve(rows,observed=None):
        # Names are measured relative to the first requested peer.
        # Only one global reference is physically free: unconstrained other peer frames keep both choices.
        d={'use':['0','1']};f={}
        incident=set(request)|{p for row in rows.values() for p in row['ends']}
        for i,p in enumerate(peers):
            if p in incident:d['p'+str(i)]=['0'] if p==request[0] else ['0','1']
        var={p:'p'+str(i) for i,p in enumerate(peers)}
        owners=sorted({v['issuer'] for v in rows.values()})
        flags={o:'c'+str(i) for i,o in enumerate(owners)}
        for o,c in flags.items():
            d[c]=['0','1'];f['fee'+c]=dict(scope=[c],version='1',rows=[dict(at=[b],cost=int(b)) for b in d[c]])
        for i,(k,v) in enumerate(sorted(rows.items())):
            a,b=[var[x] for x in v['ends']];bad=flags[v['issuer']];bit=0 if observed is None else observed[k]
            allowed=[dict(at=[x,y,z],cost=0) for x,y,z in product(d[a],d[b],d[bad])
                     if z=='1' or int(x)^int(y)==bit]
            f['link'+str(i)]=dict(scope=[a,b,bad],version='1',rows=allowed)
        u,v=[var[x] for x in request];f['reading']=dict(scope=[u,v,'use'],version='1',
                    rows=[dict(at=[a,b,str(int(a)^int(b))],cost=0) for a,b in product(d[u],d[v])])
        case=dict(space='binary-peer-frames',domains=d,factors=f,open=['use'],work_limit=budget)
        def run(extra):
            ans=price(case|extra)
            if ans['status']=='resource-limit':raise PriceLimit()
            return ans
        # Both truth values are possible if enough comparisons may be wrong.
        ans=run({});costs={r['at'][0]:r['cost'] for r in ans['message']['rows']}
        def witness(value):
            solution=run(dict(award={'use':value}))['award']
            assignment={}
            for r in solution.values():assignment.update(r['assignment'])
            frames={p:int(assignment.get(v,'0')) for p,v in var.items()}
            wrong=sorted({row['issuer'] for k,row in rows.items() if frames[row['ends'][0]]^frames[row['ends'][1]]
                          !=(0 if observed is None else observed[k])})
            require(len(wrong)==costs[value],'grouped disagreement witness differed')
            return dict(frames=frames,issuers=wrong)
        return costs,witness

    try:
        if mode=='qualify':
            active={k:v for k,v in links.items() if k in readings}
            missing={row['issuer'] for k,row in links.items() if k not in readings}
            require(not missing&{r['issuer'] for r in active.values()},'issuer erasures apply to whole reporting groups')
            costs,w=solve(active,readings)
            possible=[k for k in ('0','1') if costs[k]<=faults]
            return dict(status='qualified' if len(possible)==1 else 'ambiguous' if possible else 'inconsistent',
                        possible=possible,minimum_wrong=costs,witnesses={k:w(k) for k in ('0','1')},erased=sorted(missing))
        target=2*faults+erasures+1;limit=integer(spec.get('portfolio_limit',8192),1,100000)
        keys=sorted(offers);cuts=[]
        def check(chosen):
            active=links|{k:offers[k] for k in chosen}
            costs,w=solve(active)
            return costs['1'],w('1'),active
        strongest,cut,active=check(keys)
        if strongest<target:
            different=cut['issuers'];lost=set(different[:erasures])
            rest=[o for o in different if o not in lost]
            faulty0=set(rest[:faults])
            record={}
            for k,r in active.items():
                if r['issuer'] in lost:continue
                bit=cut['frames'][r['ends'][0]]^cut['frames'][r['ends'][1]]
                record[k]=bit if r['issuer'] in faulty0 else 0
            return dict(status='insufficient',max_distance=strongest,required=target,cut=cut,erased=sorted(lost),
                        shared_record=record,truth0_faults=sorted(faulty0),truth1_faults=sorted(set(rest)-faulty0))
        feasible_cost=sum(v['cost'] for v in offers.values())
        heap=[(0,())];checked=0;calls=1
        while heap:
            fee,indices=heappop(heap)
            chosen=[keys[i] for i in indices]
            if fee>=feasible_cost:
                return dict(status='optimal',cost=feasible_cost,offers=keys,distance=strongest,
                            required=target,portfolios=checked,cut_calls=calls,witnesses=cuts)
            if checked>=limit:
                return dict(status='resource-limit',lower=fee,upper=feasible_cost,offers=keys,
                            required=target,portfolios=checked,cut_calls=calls)
            checked+=1;bundle=links|{k:offers[k] for k in chosen}
            def crosses(w):
                return len({r['issuer'] for r in bundle.values() if w[r['ends'][0]]!=w[r['ends'][1]]})
            if all(crosses(c['frames'])>=target for c in cuts):
                distance,w,_=check(chosen);calls+=1
                if distance>=target:
                    return dict(status='optimal',cost=fee,offers=chosen,distance=distance,required=target,
                                portfolios=checked,cut_calls=calls,witnesses=cuts)
                cuts.append(w)
            for i in range(indices[-1]+1 if indices else 0,len(keys)):
                heappush(heap,(fee+offers[keys[i]]['cost'],indices+(i,)))
    except PriceLimit:
        return dict(status='resource-limit',reason='conditional-price-search')
