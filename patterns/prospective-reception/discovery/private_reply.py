"""Synthesize a bounded private record and public one-way reply for protected continuations."""
import argparse
from collections import defaultdict, deque
import json
from pathlib import Path
from scoped_return import design

MAX_STEPS = 40000


def named(value):
    return isinstance(value, str) and bool(value)


def construct(spec):
    worlds, moves, protected, bound = (spec[k] for k in ("worlds", "moves", "protect", "max_records"))
    if not isinstance(worlds, dict) or not 1 <= len(worlds) <= 8:
        raise ValueError("expected 1..8 named worlds")
    for w, row in worlds.items():
        if not named(w) or not isinstance(row, dict) or not all(
                named(row.get(k)) for k in ("sender", "port", "receiver", "action", "public")):
            raise ValueError("each world needs sender, port, receiver, action and public strings")
    if len({r["port"] for r in worlds.values()}) > 4 or type(bound) is not int or not 1 <= bound <= 3:
        raise ValueError("expected at most four ports and max_records 1..3")
    capacity = spec.get("max_symbols", 12)
    if type(capacity) is not int or not 1 <= capacity <= 12:
        raise ValueError("expected max_symbols 1..12")
    if not isinstance(moves, dict) or len(moves) > 32:
        raise ValueError("expected up to 32 named moves")
    by = {w: defaultdict(list) for w in worlds}
    for i, row in sorted(moves.items()):
        if (not named(i) or not isinstance(row, list) or len(row) != 5 or
                any(not named(row[k]) for k in (0,1,2)) or row[0] not in worlds or row[2] not in worlds or
                any(x is not None and not named(x) for x in row[3:])):
            raise ValueError("invalid move [source, command, target, private event, public event]")
        by[row[0]][row[1]].append(i)
    if not isinstance(protected, list) or len(protected) > 32:
        raise ValueError("expected up to 32 protected pairs of commissioning paths")
    for pair in protected:
        if not isinstance(pair, list) or len(pair)!=2:
            raise ValueError("expected a pair of paths")
        for path in pair:
            if (not isinstance(path, list) or not 1<=len(path)<=33 or
                    not all(named(k) for k in path) or path[0] not in worlds):
                raise ValueError("invalid commissioning path")
            w=path[0]
            for i in path[1:]:
                if i not in moves or moves[i][0]!=w:
                    raise ValueError("disconnected commissioning path")
                w=moves[i][2]

    def endpoint(path):
        return moves[path[-1]][2] if len(path)>1 else path[0]
    # Check the public environment before investing in any message design.
    queue=deque((endpoint(a),endpoint(b),[],i,[],[]) for i,(a,b) in enumerate(protected))
    seen=set()
    while queue:
        a,b,word,pair,left_path,right_path=queue.popleft()
        if (a,b) in seen:
            continue
        seen.add((a,b))
        difference = ("public-state" if worlds[a]["public"]!=worlds[b]["public"] else
                      "command-menu" if by[a].keys()!=by[b].keys() else None)
        if difference:
            return {"status":"impossible","reason":difference,"worlds":[a,b],"continuation":word,
                    "protected_pair":pair,"example_moves":[left_path,right_path]}
        for command in sorted(by[a]):
            for i in by[a][command]:
                for j in by[b][command]:
                    if moves[i][4]!=moves[j][4]:
                        return {"status":"impossible","reason":"public-event","moves":[i,j],"continuation":word+[command],
                                "protected_pair":pair,"example_moves":[left_path+[i],right_path+[j]]}
                    queue.append((moves[i][2],moves[j][2],word+[command],pair,left_path+[i],right_path+[j]))

    senders=sorted({r["sender"] for r in worlds.values()})
    demanded=defaultdict(set)
    for r in worlds.values():
        demanded[r["receiver"]].add(r["action"])
    reply_floor=max(map(len,demanded.values()))
    no_design={"status":"no-design-within-bound","max_records":bound}
    if "max_symbols" in spec:
        no_design["max_symbols"]=capacity
    if reply_floor>capacity:
        return no_design
    steps=0
    def tick():
        nonlocal steps
        steps+=1
        if steps>MAX_STEPS:
            raise OverflowError

    def evaluate(initial, update, reachable):
        def advance(node,i):
            w,s=node
            _,_,dest,event,_=moves[i]
            return dest, s if event is None else update[s,event]
        def start(path):
            node=(path[0],initial[worlds[path[0]]["sender"]])
            for i in path[1:]:
                node=advance(node,i)
            return node
        nodes=sorted(reachable)
        ids={node:str(i) for i,node in enumerate(nodes)}
        joint=deque((start(a),start(b)) for a,b in protected)
        paired=set()
        while joint:
            left,right=joint.popleft()
            if (left,right) in paired:
                continue
            paired.add((left,right))
            for cmd in by[left[0]]:
                for i in by[left[0]][cmd]:
                    for j in by[right[0]][cmd]:
                        joint.append((advance(left,i),advance(right,j)))
        rows={}
        for (w,s),i in ids.items():
            r=worlds[w]
            rows[i]=dict(sender=json.dumps([s,r["port"]]),receiver=r["receiver"],action=r["action"],public=r["public"])
        pairs=sorted({tuple(sorted((ids[a],ids[b]))) for a,b in paired if a!=b})
        code=design({"worlds":rows,"protect":[list(p) for p in pairs]})
        if code["status"]!="ready" or code["symbols"]>capacity:
            return None
        report={}
        for key,val in code["encoder"].items():
            state,port=json.loads(key)
            report.setdefault(str(state),{})[port]=val
        table={}
        for (state,event),dest in update.items():
            table.setdefault(str(state),{})[event]=str(dest)
        return dict(status="ready",records=len({s for w,s in nodes}),symbols=code["symbols"],
                    initial={v:str(s) for v,s in initial.items()},update=table,report=report,
                    decoder=code["decoder"],reachable_configurations=len(nodes),protected_configurations=len(paired))

    for limit in range(1,bound+1):
        best=None
        def optimum():
            return best is not None and best["symbols"]==reply_floor

        def extend(initial,update,used):
            nonlocal best
            tick()
            reached=set()
            queue=deque((w,initial[worlds[w]["sender"]]) for w in sorted(worlds))
            while queue:
                w,s=queue.popleft()
                if (w,s) in reached:
                    continue
                reached.add((w,s))
                for command in sorted(by[w]):
                    for i in by[w][command]:
                        _,_,dest,event,_=moves[i]
                        if event is None:
                            queue.append((dest,s))
                        elif (s,event) in update:
                            queue.append((dest,update[s,event]))
                        else:
                            for n in range(min(limit,used+1)):
                                update[s,event]=n
                                extend(initial,update,max(used,n+1))
                                del update[s,event]
                                if optimum():
                                    return
                            return
            result=evaluate(initial,update,reached)
            if result is not None and (best is None or result["symbols"]<best["symbols"]):
                best=result

        def seed(initial,used):
            tick()
            if len(initial)==len(senders):
                extend(initial,{},used)
                return
            sender=senders[len(initial)]
            for n in range(min(limit,used+1)):
                initial[sender]=n
                seed(initial,max(used,n+1))
                del initial[sender]
                if optimum():
                    break
        try:
            seed({},0)
        except OverflowError:
            return {"status":"resource-limit","examined_private_bound":limit,"feasible":best}
        if best is not None:
            return best
    return no_design


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec",type=Path)
    args=parser.parse_args()
    try:
        value=construct(json.loads(args.spec.read_bytes()))
    except (OSError,ValueError,KeyError,TypeError) as err:
        parser.exit(2,str(err)+"\n")
    print(json.dumps(value,indent=2,sort_keys=True))
