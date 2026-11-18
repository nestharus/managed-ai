"""Construct a smallest deterministic reply for a finite, explicitly scoped receiver."""
import argparse
import json
from collections import deque
from pathlib import Path


def design(spec):
    rows = spec["worlds"]
    pairs = spec["protect"]
    if not isinstance(rows, dict) or not 1 <= len(rows) <= 128:
        raise ValueError("expected 1..128 named worlds")
    fields = ("sender", "receiver", "action", "public")
    for name, row in rows.items():
        if not isinstance(name, str) or not name or not isinstance(row, dict):
            raise ValueError("expected named world records")
        if any(not isinstance(row.get(k), str) or not row[k] for k in fields):
            raise ValueError("each world needs sender, receiver, action and public strings")
    if not isinstance(pairs, list) or len(pairs) > 512:
        raise ValueError("expected at most 512 protected pairs")
    for pair in pairs:
        if (not isinstance(pair, list) or len(pair) != 2 or
                any(not isinstance(x, str) or x not in rows for x in pair)):
            raise ValueError("unknown protected pair")
    symbols = sorted({r["sender"] for r in rows.values()})
    equal = {k: [] for k in symbols}
    for i, j in pairs:
        left, right = rows[i], rows[j]
        if left["public"] != right["public"]:
            return {"status": "impossible", "reason": "already-visible", "worlds": [i, j]}
        a, b = left["sender"], right["sender"]
        equal[a].append((b, [i, j]))
        equal[b].append((a, [j, i]))

    def paths(start):
        reached = {start: []}
        queue = deque([start])
        while queue:
            here = queue.popleft()
            for other, witness in equal[here]:
                if other not in reached:
                    reached[other] = reached[here] + [witness]
                    queue.append(other)
        return reached

    reachable = {k: paths(k) for k in symbols}
    group = {k: min(reachable[k]) for k in symbols}
    edges = {g: set() for g in group.values()}
    names = sorted(rows)
    for idx, i in enumerate(names):
        for j in names[idx+1:]:
            left, right = rows[i], rows[j]
            if left["receiver"] != right["receiver"] or left["action"] == right["action"]:
                continue
            a, b = left["sender"], right["sender"]
            if group[a] == group[b]:
                return {"status": "impossible", "reason": "forced-collision", "worlds": [i, j],
                        "protected_chain": reachable[a][b]}
            edges[group[a]].add(group[b])
            edges[group[b]].add(group[a])

    if len(edges) > 12:
        return {"status": "resource-limit", "remaining_classes": len(edges)}

    assigned = {}
    def color(limit):
        if len(assigned) == len(edges):
            return True
        available = [g for g in edges if g not in assigned]
        def rank(g):
            used = {assigned[n] for n in edges[g] if n in assigned}
            return -len(used), -len(edges[g]), g
        g = min(available, key=rank)
        used = {assigned[n] for n in edges[g] if n in assigned}
        for c in range(min(limit, 1 + len(set(assigned.values())))):
            if c in used:
                continue
            assigned[g] = c
            if color(limit):
                return True
            del assigned[g]
        return False
    for count in range(1, len(edges)+1):
        if color(count):
            break
    encoder = {k: str(assigned[group[k]]) for k in symbols}
    decoder = {}
    for i in names:
        row = rows[i]
        code = encoder[row["sender"]]
        decoder.setdefault(row["receiver"], {})[code] = row["action"]
    return {"status": "ready", "symbols": count, "encoder": encoder, "decoder": decoder}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("spec", type=Path, help="JSON worlds and protected pairs for a trusted, finite interface")
    args = p.parse_args()
    try:
        result = design(json.loads(args.spec.read_bytes()))
    except (OSError, TypeError, KeyError, ValueError) as exc:
        p.exit(2, str(exc) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
