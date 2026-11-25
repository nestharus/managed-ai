"""Synthesize a finite partial-observation supplier for a deterministic buyer."""
from collections import defaultdict, deque
from heapq import heappop, heappush
from itertools import product
import json
import sys


def demand(condition, message):
    if not condition:
        raise ValueError(message)


def strings(xs):
    return isinstance(xs, list) and all(isinstance(x, str) and x for x in xs)


def validate(spec):
    source, target = spec["source"], spec["target"]
    demand(isinstance(source, dict) and isinstance(target, dict) and source and target, "nonempty state tables required")
    demand(spec["start"] in source, "unknown buyer start")
    seeds = spec["possible"]
    demand(strings(seeds) and seeds and len(seeds) == len(set(seeds)) and set(seeds) <= target.keys(), "invalid possible states")
    for row in source.values():
        demand(isinstance(row, dict), "buyer row must be an object")
        for dest, word in row.values():
            demand(dest in source and strings(word), "invalid buyer transition")
    fees = spec["fees"]
    demand(isinstance(fees, dict) and all(type(f) is int and f > 0 for f in fees.values()), "positive integer operating fees required")
    for row in target.values():
        demand(isinstance(row, dict), "supplier row must be an object")
        for action, (dest, word, signal) in row.items():
            demand(action in fees and dest in target and strings(word) and isinstance(signal, str), "invalid supplier transition")


def information_states(spec):
    """A public output and a private signal are both visible to the adapter."""
    targets = spec["target"]
    start = tuple(sorted(spec["possible"]))
    pending = deque([start])
    transitions = {}
    seen = {start}
    while pending:
        belief = pending.popleft()
        choices = {}
        for action in sorted(set.intersection(*(set(targets[t]) for t in belief))):
            groups = defaultdict(set)
            for t in belief:
                dest, word, signal = targets[t][action]
                groups[(tuple(word), signal)].add(dest)
            choices[action] = {key: tuple(sorted(g)) for key, g in sorted(groups.items())}
            for g in choices[action].values():
                if g not in seen:
                    seen.add(g)
                    demand(len(seen) <= 256, "more than 256 reachable information states")
                    pending.append(g)
        transitions[belief] = choices
    return start, transitions


def path_values(transitions, fees, word, goal, on_cancel):
    """Compute least worst-case fee to finish a request. A positive-fee cycle cannot help."""
    size = len(word)
    # An action becomes usable when every distinct observed successor is settled.
    parents = defaultdict(list)
    replies = {}
    missing = {}
    height = {}
    for u in product(transitions, range(size + 1)):
        b, i = u
        for action, groups in transitions[b].items():
            arcs = []
            for (effects, signal), following in groups.items():
                j = i + len(effects)
                if word[i:j] != effects:
                    break
                terminal = j == size and following in goal
                if on_cancel is not None and not terminal and (j or following not in on_cancel):
                    break
                arcs.append((effects, signal, (following, j)))
            else:
                edge = (u, action)
                replies[edge] = arcs
                children = {v for _, _, v in arcs}
                missing[edge] = len(children)
                height[edge] = 0
                for v in children:
                    parents[v].append(edge)

    # Positive fees make every child of an optimal action strictly cheaper than
    # its parent. The last child popped therefore sets the action's worst fee.
    best = {}
    pending = []
    for b in goal:
        u = (b, size)
        best[u] = (0, 0, "")
        heappush(pending, (0, u))
    values, choices = {}, {}
    while pending:
        fee, v = heappop(pending)
        if v in values:
            continue
        values[v] = fee
        _, depth, picked = best[v]
        if fee:
            choices[v] = (picked, replies[v, picked])
        for edge in parents[v]:
            u, action = edge
            missing[edge] -= 1
            height[edge] = max(height[edge], depth + 1)
            if missing[edge] == 0:
                candidate = (fees[action] + fee, height[edge], action)
                if u not in best or candidate < best[u]:
                    best[u] = candidate
                    heappush(pending, (candidate[0], u))
    return values, choices


def solve(spec):
    validate(spec)
    source, fees = spec["source"], spec["fees"]
    cancel = spec.get("cancellation", False)
    demand(type(cancel) is bool, "cancellation must be boolean")
    first, transitions = information_states(spec)
    bs = sorted(transitions)
    names = {b: i for i, b in enumerate(bs)}
    pair = (spec["start"], first)
    universe = set(product(source, bs))

    def fixed(cap):
        region = universe.copy()
        layers = []
        while True:
            cache = {}
            deletions = {}
            for s, b in sorted(region):
                for cmd, (dest, effects) in sorted(source[s].items()):
                    key = (s if cancel else None, dest, tuple(effects))
                    if key not in cache:
                        goal = {g for g in bs if (dest, g) in region}
                        on_cancel = {g for g in bs if (s, g) in region} if cancel else None
                        cache[key] = path_values(transitions, fees, key[2], goal, on_cancel)
                    cost = cache[key][0].get((b, 0))
                    if cost is None or cap is not None and cost > cap:
                        deletions[s, b] = cmd
                        break
            if not deletions:
                return region, cache, layers
            layers.append(deletions)
            region.difference_update(deletions)

    def loss(layers):
        return [[{"buyer": s, "belief": names[b], "challenge": cmd}
                 for (s, b), cmd in sorted(layer.items())] for layer in layers]

    whole, _, layers = fixed(None)
    receipt = {"contract": "g60.receiver-adapter.v1", "beliefs": list(bs), "initial_belief": names[first]}
    if pair not in whole:
        return {**receipt, "status": "impossible", "elimination": loss(layers)}
    width = max((len(word) for row in source.values() for _, word in row.values()), default=0)
    lo, hi = -1, len(bs) * (width + 1) * max(fees.values(), default=0)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if pair in fixed(mid)[0]:
            hi = mid
        else:
            lo = mid
    region, cache, _ = fixed(hi)
    # Serialize a shared contingent program, including stop decisions.
    nodes, entries = [], []
    ids = {}

    def emit(key, root):
        values, policy = cache[key]
        pending = []

        def intern(u):
            mark = (key, u)
            if mark not in ids:
                ids[mark] = len(nodes)
                nodes.append(None)
                pending.append(u)
            return ids[mark]

        first_node = intern(root)
        while pending:
            u = pending.pop()
            b, i = u
            node = {"belief": names[b], "offset": i, "remaining_fee": values[u]}
            if u in policy:
                action, arcs = policy[u]
                node.update(action=action, replies=[
                    {"effects": list(e), "signal": sig, "node": intern(v)} for e, sig, v in arcs])
            else:
                node["stop"] = True
            nodes[ids[key, u]] = node
        return first_node

    # Only externally reachable idle states and their actual policies are needed.
    queue = deque([pair])
    reached = {pair}
    while queue:
        s, b = queue.popleft()
        for cmd, (dest, word) in sorted(source[s].items()):
            key = (s if cancel else None, dest, tuple(word))
            n = emit(key, (b, 0))
            entries.append({"buyer": s, "belief": names[b], "command": cmd, "node": n, "next_buyer": dest})
            frontier, visited = [n], set()
            while frontier:
                k = frontier.pop()
                if k in visited:
                    continue
                visited.add(k)
                leaf = nodes[k]
                if leaf.get("stop"):
                    q = (dest, bs[leaf["belief"]])
                    if q not in reached:
                        reached.add(q)
                        queue.append(q)
                else:
                    for arc in leaf["replies"]:
                        child = nodes[arc["node"]]
                        if cancel and not child.get("stop"):
                            interrupted = (s, bs[child["belief"]])
                            if interrupted not in reached:
                                reached.add(interrupted)
                                queue.append(interrupted)
                        frontier.append(arc["node"])
    witness = [] if hi == 0 else loss(fixed(hi - 1)[2])
    return {**receipt, "status": "qualified", "peak_fee": hi, "entries": entries, "nodes": nodes,
            "below_peak_elimination": witness}


if __name__ == "__main__":
    spec = json.load(sys.stdin) if len(sys.argv) == 1 else json.load(open(sys.argv[1], encoding="utf8"))
    print(json.dumps(solve(spec), indent=2))

