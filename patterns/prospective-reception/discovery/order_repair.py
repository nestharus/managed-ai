"""Find a minimum-fee synchronization contract that preserves a reference receiver execution."""
import argparse
from collections import deque
from functools import lru_cache
from itertools import combinations
import json
from pathlib import Path

from local_policy import name, unique_object


def require(condition, message):
    if not condition:
        raise ValueError(message)


def named_map(obj, limit, label, *, empty=False):
    require(isinstance(obj, dict) and (empty or obj) and len(obj) <= limit and
            all(name(k) for k in obj), 'invalid '+label)
    return obj


def bounded(value, low, high):
    require(type(value) is int and low <= value <= high, 'integer outside supported range')
    return value


class Limit(Exception):
    pass


class Receiver:
    def __init__(self, spec):
        self.states = named_map(spec['states'],64,'receiver states')
        self.initial = spec['initial']
        require(self.initial in self.states,'unknown initial state')
        self.commands = set(named_map(self.states[self.initial],64,'receiver commands'))
        for row in self.states.values():
            require(isinstance(row,dict) and set(row)==self.commands,'incomplete transition table')
            for transition in row.values():
                require(type(transition) is list and len(transition)==2 and name(transition[0]) and
                        name(transition[1]) and transition[1] in self.states,'invalid transition')
        follows = spec.get('follow', sorted(self.commands))
        require(type(follows) is list and all(name(x) for x in follows) and
                len(set(follows))==len(follows) and set(follows)<=self.commands,'invalid future commands')
        self.follow = sorted(follows)
        # Refine equivalence under the buyer's actual future vocabulary.
        blocks = dict.fromkeys(self.states,0)
        while True:
            ids = {}
            partition = {}
            for state,row in self.states.items():
                key = (blocks[state],tuple((row[a][0],blocks[row[a][1]]) for a in self.follow))
                partition[state] = ids.setdefault(key,len(ids))
            if len(set(partition.values()))==len(set(blocks.values())):
                break
            blocks = partition
        self.equivalence = partition

    def distinguishing(self, left, right):
        pending = deque([(left,right,())])
        seen = {(left,right)}
        while pending:
            a,b,path = pending.popleft()
            for command in self.follow:
                x,c = self.states[a][command]
                y,d = self.states[b][command]
                if x!=y:
                    return {'commands':[*path,command],'reference':x,'other':y}
                if (c,d) not in seen:
                    seen.add((c,d))
                    pending.append((c,d,(*path,command)))
        raise AssertionError('expected an observable difference')


def construct(spec):
    events = named_map(spec['events'],256,'events')
    raw = named_map(spec['receivers'],128,'receivers')
    machines = {k:Receiver(v) for k,v in raw.items()}
    ref = spec['reference']
    require(type(ref) is list and len(ref)==len(events) and set(ref)==events.keys(),'reference must order every event once')
    index = {v:i for i,v in enumerate(ref)}
    for row in events.values():
        named_map(row,len(machines),'event operations')
        require(all(k in machines and name(a) and a in machines[k].commands for k,a in row.items()),
                'unknown receiver command')
    def edges(rows):
        require(type(rows) is list and len(rows)<=4096,'invalid ordering pairs')
        out=[]
        for edge in rows:
            require(type(edge) is list and len(edge)==2 and all(name(x) and x in index for x in edge),
                    'invalid ordering pair')
            a,b=map(index.get,edge)
            require(a<b,'each ordering right must admit the reference')
            out.append((a,b))
        return out
    given=edges(spec.get('before',[]))
    offers=named_map(spec.get('offers',{}),192,'ordering offers',empty=True)
    costs={k:bounded(v['cost'],1,1000000) for k,v in offers.items()}
    fences={k:edges(v.get('edges',[])) for k,v in offers.items()}
    gates={}
    for offer,body in offers.items():
        rules=body.get('gates',[])
        require(type(rules) is list and len(rules)<=64,'invalid receiving gates')
        gates[offer]=[]
        for rule in rules:
            members,target=rule['before'],rule['event']
            require(name(target) and target in index and type(members) is list and
                    1<=len(members)<=128 and all(name(e) and e in index for e in members) and
                    len(set(members))==len(members) and all(index[e]<index[target] for e in members),
                    'gate needs distinct earlier reference occurrences')
            gates[offer].append((tuple(index[e] for e in members),index[target],
                                 bounded(rule['need'],1,len(members))))
        require(fences[offer] or gates[offer],'each offer needs an ordering condition')
    require(sum(map(len,gates.values()))<=128,'up to 128 gates per commission')
    preserve=spec.get('preserve',[])
    require(type(preserve) is list and len(preserve)<=256,'invalid progress prefixes')
    excluded={}
    def first_denial(prefix, fixed, quorums):
        seen=set()
        for i in prefix:
            if any(b==i and a not in seen for a,b in fixed) or any(
                b==i and len(seen.intersection(members))<need for members,b,need in quorums):
                return ref[i]
            seen.add(i)
        return None
    for n,prefix in enumerate(preserve):
        require(type(prefix) is list and all(name(e) and e in index for e in prefix) and
                len(set(prefix))==len(prefix),'invalid progress prefix')
        word=[index[e] for e in prefix]
        denied=first_denial(word,given,[])
        if denied is not None:
            return dict(status='infeasible',reason='preexisting-order-blocks-progress',prefix=n,event=denied)
        for q in offers:
            denied=first_denial(word,fences[q],gates[q])
            if denied is not None:
                excluded.setdefault(q,dict(prefix=n,event=denied))
    candidates=sorted(q for q in offers if q not in excluded)
    limit=bounded(spec.get('work_limit',250000),1,2000000)
    counts={'schedule_states':0,'cover_states':0,'diamond_states':0,'proven_projections':0,'checks':0}
    def tick(k):
        if counts['schedule_states']+counts['cover_states']+counts['diamond_states']>=limit:
            raise Limit()
        counts[k]+=1

    positions={k:[i for i,e in enumerate(ref) if k in events[e]] for k in machines}
    targets={}
    for k,m in machines.items():
        state=m.initial
        receipts={}
        for i in positions[k]:
            receipt,state=m.states[state][events[ref[i]][k]]
            receipts[i]=receipt
        targets[k]=(receipts,state)

    def closure(pairs):
        parents=[0]*len(ref)
        for a,b in pairs:
            parents[b]|=1<<a
        for b in range(len(ref)):
            mask=parents[b]
            while mask:
                bit=mask & -mask
                parents[b]|=parents[bit.bit_length()-1]
                mask^=bit
        return parents

    def lift(parents, local):
        # All linear extensions of an induced subposet extend to the entire poset.
        pred=parents.copy()
        for a,b in zip(local,local[1:]):
            pred[b]|=1<<a
        done=0
        order=[]
        while len(order)<len(ref):
            i=next(i for i in range(len(ref)) if not done & (1<<i) and not pred[i] & ~done)
            done|=1<<i
            order.append(ref[i])
        return order

    @lru_cache(None)
    def commutes(receiver, a, b):
        states=machines[receiver].states
        for row in states.values():
            tick('diamond_states')
            a_first,after_a=row[a]
            b_first,after_b=row[b]
            b_second,end_ab=states[after_a][b]
            a_second,end_ba=states[after_b][a]
            if a_first!=a_second or b_first!=b_second or end_ab!=end_ba:
                return False
        return True

    def counterexample(parents, receivers):
        for k in receivers:
            m=machines[k]
            expected,end=targets[k]
            items=positions[k]
            if all(parents[j] & (1<<i) or
                   commutes(k,*sorted((events[ref[i]][k],events[ref[j]][k])))
                   for i,j in combinations(items,2)):
                # Adjacent swaps of incomparable actions connect every linear extension.
                # Their own receipts and the resulting state survive every such swap.
                counts['proven_projections']+=1
                continue
            mask=sum(1<<i for i in items)
            seen=set()
            def explore(done,state,prefix):
                tag=(done,state)
                if tag in seen:
                    return None
                tick('schedule_states')
                seen.add(tag)
                if done==mask:
                    if m.equivalence[state]!=m.equivalence[end]:
                        return prefix,dict(kind='future',receiver=k,
                                 distinguishing=m.distinguishing(end,state))
                    return None
                for i in reversed(items):
                    bit=1<<i
                    if done&bit or parents[i]&mask&~done:
                        continue
                    output,nxt=m.states[state][events[ref[i]][k]]
                    after=prefix+(i,)
                    if output!=expected[i]:
                        rest=tuple(j for j in items if j!=i and not done & (1<<j))
                        return after+rest,dict(kind='receipt',receiver=k,event=ref[i],
                                               reference=expected[i],other=output)
                    failed=explore(done|bit,nxt,after)
                    if failed is not None:
                        return failed
                return None
            failed=explore(0,m.initial,())
            if failed is not None:
                partial,reason=failed
                schedule=lift(parents,partial)
                rank={index[e]:j for j,e in enumerate(schedule)}
                blockers=sorted(q for q in candidates if any(rank[a]>rank[b] for a,b in fences[q]) or
                                any(sum(rank[a]<rank[b] for a in members)<need for members,b,need in gates[q]))
                return dict(order=schedule,difference=reason,excluded_by=blockers)
        return None

    certified=set()
    def check(chosen):
        counts['checks']+=1
        base=given+[edge for q in chosen for edge in fences[q]]
        requirements=[g for q in chosen for g in gates[q]]
        # Receiver effects, active edges and each quorum define the communicating components.
        # A bundle may price edges on different components jointly without coupling their execution.
        parent=list(range(len(ref)))
        def find(x):
            while x!=parent[x]:
                parent[x]=parent[parent[x]]
                x=parent[x]
            return x
        def join(xs):
            if xs:
                head=find(xs[0])
                for x in xs[1:]:
                    parent[find(x)]=head
        for xs in positions.values():
            join(xs)
        for pair in base:
            join(pair)
        for members,target,need in requirements:
            join((target,*members))
        components={}
        for k,items in positions.items():
            if items:
                components.setdefault(find(items[0]),[]).append(k)

        def verify(pairs, remaining, receivers):
            tick('schedule_states')
            bad=counterexample(closure(pairs),receivers)
            if bad is None:
                return None
            rank={index[e]:j for j,e in enumerate(bad['order'])}
            violated=next((g for g in remaining if sum(rank[a]<rank[g[1]] for a in g[0])<g[2]),None)
            if violated is None:
                return bad
            members,target,need=violated
            others=[g for g in remaining if g!=violated]
            for group in combinations(members,need):
                witness=verify(pairs+[(a,target) for a in group],others,receivers)
                if witness is not None:
                    return witness
            return None

        for component,receivers in components.items():
            local=[edge for edge in base if find(edge[0])==component]
            quorums=[g for g in requirements if find(g[1])==component]
            key=(tuple(receivers),tuple(sorted(set(local))),tuple(sorted(set(quorums))))
            if key in certified:
                continue
            # Other components run in reference order in the lifted witness.
            bad=verify(local,quorums,receivers)
            if bad is not None:
                return bad
            certified.add(key)
        return None

    def cheapest(clauses):
        @lru_cache(None)
        def solve(remaining):
            tick('cover_states')
            if not remaining:
                return 0,()
            forced={clause[0] for clause in remaining if len(clause)==1}
            if forced:
                rest=tuple(c for c in remaining if not forced.intersection(c))
                expense,chosen=solve(rest)
                return expense+sum(costs[q] for q in forced),tuple(sorted((*forced,*chosen)))
            pivot=min(remaining,key=lambda x:(len(x),x))
            best=None
            for offer in pivot:
                rest=tuple(c for c in remaining if offer not in c)
                expense,chosen=solve(rest)
                candidate=(costs[offer]+expense,tuple(sorted((offer,*chosen))))
                if best is None or candidate<best:
                    best=candidate
            return best
        # A subset clause already implies each of its supersets.
        unique=sorted(set(clauses),key=lambda c:(len(c),c))
        minimal=[]
        for clause in unique:
            if not any(set(old)<=set(clause) for old in minimal):
                minimal.append(clause)
        return solve(tuple(minimal))

    lower=0
    upper=None
    incumbent=None
    witnesses=[]
    try:
        all_offers=candidates
        unrepaired=check(all_offers)
        if unrepaired is not None:
            return dict(status='infeasible',witness=unrepaired,excluded_offers=excluded,counts=counts)
        incumbent=all_offers
        upper=sum(costs[q] for q in candidates)
        while lower<upper:
            cost,chosen=cheapest([tuple(w['excluded_by']) for w in witnesses])
            lower=cost
            if lower==upper:
                break
            bad=check(chosen)
            if bad is None:
                incumbent=list(chosen)
                upper=cost
                break
            witnesses.append(bad)
    except Limit:
        return dict(status='resource-limit',lower=lower,upper=upper,offers=incumbent,
                    witnesses=witnesses,excluded_offers=excluded,counts=counts)
    return dict(status='optimal',cost=upper,offers=incumbent,witnesses=witnesses,excluded_offers=excluded,counts=counts)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('spec',type=Path)
    args=parser.parse_args()
    try:
        result=construct(json.loads(args.spec.read_bytes(),object_pairs_hook=unique_object))
    except (ValueError,KeyError,TypeError,OSError,AttributeError) as e:
        parser.exit(2,str(e)+'\n')
    print(json.dumps(result,sort_keys=True,indent=2))
