"""Purchase public and privately delivered observations for several decision owners."""
import argparse
from fractions import Fraction
from functools import lru_cache
import json
from pathlib import Path

from local_policy import name, unique_object


class Limit(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ValueError(message)


def names(xs, maximum):
    require(type(xs) is list and 1 <= len(xs) <= maximum and all(name(x) for x in xs)
            and len(set(xs)) == len(xs), 'invalid or duplicate names')
    return xs


def integer(n, lo, hi):
    require(type(n) is int and lo <= n <= hi, 'integer outside allowed range')
    return n


def construct(spec):
    owners = names(spec['owners'], 128)
    owner_set = set(owners)
    fees = spec['costs']
    require(type(fees) is dict and len(fees) <= 16 and all(map(name, fees)), 'invalid offers')
    tests = sorted(fees)
    for v in fees.values():
        integer(v, 1, 1000000)
    worlds = spec['worlds']
    require(type(worlds) is dict and 1 <= len(worlds) <= 128 and all(map(name, worlds)), 'invalid worlds')
    scopes = spec.get('scopes', {})
    require(type(scopes) is dict and len(scopes) <= 32 and all(map(name, scopes)), 'invalid joint scopes')
    for scope in scopes.values():
        require(set(names(scope, 8)) <= owner_set, 'unknown joint owner')
    for world in worlds.values():
        integer(world.get('weight', 1), 1, 1000000)
        require(set(world['views']) == set(world['allowed']) == owner_set and
                all(map(name, world['views'].values())), 'missing local view or action set')
        for actions in world['allowed'].values():
            names(actions, 32)
        require(isinstance(world['offers'], dict) and world['offers'].keys() <= fees.keys(), 'unknown offer')
        for row in world['offers'].values():
            private = row.get('private', {})
            require(name(row['public']) and type(private) is dict and private.keys() <= owner_set and
                    all(map(name, private.values())), 'invalid public or private return')
        joint = world.get('joint', {})
        require(type(joint) is dict and joint.keys() == scopes.keys(), 'missing joint clause')
        for k, rows in joint.items():
            require(type(rows) is list and 1 <= len(rows) <= 256, 'invalid joint alternatives')
            for row in rows:
                require(type(row) is list and len(row) == len(scopes[k]) and
                        all(a in world['allowed'][o] for o,a in zip(scopes[k], row)), 'invalid joint action')
    custody = spec.get('custody', {'initial':'ready','accept':['ready'],
                                  'moves':{'ready':{q:'ready' for q in tests}}})
    moves = custody['moves']
    require(type(moves) is dict and 1 <= len(moves) <= 64 and all(map(name,moves)), 'invalid custody states')
    initial = custody['initial']
    accepting = set(names(custody['accept'],64))
    require(initial in moves and accepting <= moves.keys(), 'unknown custody state')
    for edges in moves.values():
        require(type(edges) is dict and edges.keys() <= fees.keys() and
                all(name(v) and v in moves for v in edges.values()), 'invalid custody transition')
    cap = integer(spec.get('budget', sum(fees.values())), 0, 16000000)
    objective = spec.get('objective', 'mean')
    requested_objective, requested_cap = objective, cap
    require(objective in ('mean','worst'), 'objective must be mean or worst')
    remaining = {'states':integer(spec.get('state_limit', 30000), 1, 100000),
                 'assignments':integer(spec.get('assignment_limit', 100000), 1, 500000)}
    counts = dict.fromkeys(remaining,0)

    def tick(k):
        if counts[k] >= remaining[k]:
            raise Limit(k)
        counts[k] += 1

    def keys(w, acquired):
        row = worlds[w]
        return {o:(o, (row['views'][o], *(row['offers'][q].get('private',{}).get(o) for q in acquired)))
                for o in owners}

    def settle(ids, acquired):
        domains = {}
        constraints = []
        for w in ids:
            row = worlds[w]
            variables = keys(w, acquired)
            for o, var in variables.items():
                options = set(row['allowed'][o])
                if var in domains:
                    options &= domains[var]
                if not options:
                    return None
                domains[var] = options
            for k, scope in scopes.items():
                constraints.append(([variables[o] for o in scope], row['joint'][k]))
        # Support propagation followed by exhaustive branching only for still-conflicting local choices.
        def propagate(current):
            changed = True
            while changed:
                changed = False
                for variables, rows in constraints:
                    live = [r for r in rows if all(a in current[v] for v,a in zip(variables,r))]
                    if not live:
                        return False
                    for i,v in enumerate(variables):
                        narrowed = {r[i] for r in live}
                        if narrowed != current[v]:
                            current[v] = narrowed
                            changed = True
            return True

        stack = [domains]
        while stack:
            tick('assignments')
            current = stack.pop()
            if not propagate(current):
                continue
            selected = {v:min(opts) for v,opts in current.items()}
            conflict = next((vs for vs,rows in constraints if [selected[v] for v in vs] not in rows),None)
            if conflict is None:
                return selected
            var = min((v for v in conflict if len(current[v])>1),key=lambda v:len(current[v]))
            for a in sorted(current[var], reverse=True):
                successor = current.copy()
                successor[var] = {a}
                stack.append(successor)
        return None

    @lru_cache(None)
    def solve(ids, acquired, custody_state):
        tick('states')
        local = settle(ids, acquired) if custody_state in accepting else None
        if local is not None:
            return (0,0,None,local)
        mass = sum(worlds[w].get('weight',1) for w in ids)
        spent = sum(fees[q] for q in acquired)
        best = None
        for q in tests:
            if q not in moves[custody_state] or q in acquired or spent+fees[q]>cap or any(q not in worlds[w]['offers'] for w in ids):
                continue
            # Strictly positive prices allow pruning once the first step already exceeds the incumbent.
            if best and ((fees[q]*mass > best[0]) if objective=='mean' else (fees[q]>best[1])):
                continue
            groups = {}
            for w in ids:
                groups.setdefault(worlds[w]['offers'][q]['public'],[]).append(w)
            next_custody = moves[custody_state][q]
            if len(groups)==1 and next_custody==custody_state:
                # A private return is useful only if it splits an existing recipient information class.
                symbols = {}
                refined = False
                for w in ids:
                    private = worlds[w]['offers'][q].get('private',{})
                    for owner,var in keys(w,acquired).items():
                        symbol = private.get(owner)
                        if symbols.setdefault(var,symbol) != symbol:
                            refined = True
                            break
                    if refined:
                        break
                if not refined:
                    continue
            enlarged = tuple(sorted((*acquired,q)))
            total = mass*fees[q]
            peak = 0
            children = {}
            for label, branch in sorted(groups.items()):
                child = (tuple(branch),enlarged,next_custody)
                result = solve(*child)
                if result is None:
                    break
                children[label] = child
                total += result[0]
                peak = max(peak,result[1])
            else:
                cost = (total,fees[q]+peak)
                key = cost if objective=='mean' else cost[::-1]
                if best is None or key < (best[:2] if objective=='mean' else best[:2][::-1]):
                    best = (*cost,q,children)
        return best

    first = (tuple(sorted(worlds)), (), initial)
    try:
        result = solve(*first)
        if result is not None and objective == 'worst' and result[1] > 0:
            # A cheap policy in a slack branch can spend up to the global peak.
            # Reoptimize its mean at that peak instead of imposing branchwise minimax tie breaks.
            cap, objective = result[1], 'mean'
            solve.cache_clear()
            result = solve(*first)
    except Limit as err:
        return dict(status='resource-limit',reason=str(err),counts=counts)
    if result is None:
        return dict(status='infeasible',budget=cap,counts=counts)
    nodes = {}
    labels = {}
    def export(state):
        if state in labels:
            return labels[state]
        node = labels[state] = 'n'+str(len(labels))
        total, peak, ask, info = solve(*state)
        acquired = list(state[1])
        if ask is None:
            rules = {o:[] for o in owners}
            for (owner,view),action in info.items():
                rules[owner].append(dict(view=list(view),action=action))
            nodes[node] = dict(acquired=acquired,rules=rules)
        else:
            nodes[node] = dict(buy=ask,next={k:export(v) for k,v in info.items()})
        return node
    root = export(first)
    mass = sum(w.get('weight',1) for w in worlds.values())
    return dict(status='optimal',objective=requested_objective,budget=requested_cap,mean_cost=str(Fraction(result[0],mass)),
                worst_cost=result[1],root=root,nodes=nodes,counts=counts)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('spec',type=Path)
    a=p.parse_args()
    try:
        result=construct(json.loads(a.spec.read_bytes(),object_pairs_hook=unique_object))
    except (KeyError,TypeError,ValueError,OSError,AttributeError) as exc:
        p.exit(2,str(exc)+'\n')
    print(json.dumps(result,indent=2,sort_keys=True))
