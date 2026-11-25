"""Find a smallest finite handoff record the recipient can update without the sender."""
import argparse
import json
from itertools import combinations
from pathlib import Path

from scoped_return import design


def overlapping(rows, moves):
    """Cover the readings by viable, possibly overlapping recipient records."""
    worlds = sorted(rows)
    if len(worlds) > 5:
        return {"status": "resource-limit", "worlds": len(worlds)}
    initial = {}
    for name, row in rows.items():
        initial.setdefault(row["sender"], set()).add(name)
    seeds = {frozenset(v) for v in initial.values()}
    choices, following = {}, {}
    for size in range(1, len(worlds) + 1):
        for members in combinations(worlds, size):
            block = frozenset(members)
            actions, successors = {}, {}
            for name in block:
                row = rows[name]
                if actions.setdefault(row["receiver"], row["action"]) != row["action"]:
                    break
            else:
                for a, event, b in moves:
                    if a not in block:
                        continue
                    if event is None:
                        if b not in block:
                            break
                    else:
                        successors.setdefault(event, set()).add(b)
                else:
                    choices[block] = actions
                    following[block] = successors
    # Eliminate blocks whose successor obligation has no possible destination.
    viable = set(choices)
    while True:
        kept = {b for b in viable if all(any(t <= dest for dest in viable)
                                        for t in following[b].values())}
        if kept == viable:
            break
        viable = kept
    if not all(any(seed <= dest for dest in viable) for seed in seeds):
        return {"status": "impossible", "reason": "no-closed-cover"}
    blocks = sorted(viable, key=lambda b: (-len(b), sorted(b)))
    maximal = [b for b in blocks if not any(b < c for c in blocks)]
    # All maximal viable blocks together cover every requirement. Try smaller covers first.
    obligations = set(seeds)
    for b in blocks:
        obligations.update(frozenset(v) for v in following[b].values())
    options = {t: [i for i,b in enumerate(blocks) if t <= b] for t in obligations}
    checks = 0
    def complete(chosen, limit, seen):
        nonlocal checks
        if chosen in seen:
            return None
        seen.add(chosen)
        checks += 1
        if checks > 120000:
            raise OverflowError
        needs = set(seeds)
        for i in chosen:
            needs.update(frozenset(v) for v in following[blocks[i]].values())
        missing = [t for t in needs if chosen.isdisjoint(options[t])]
        if not missing:
            return [blocks[i] for i in sorted(chosen)]
        if len(chosen) == limit:
            return None
        goal = min(missing, key=lambda t: (len(options[t]), sorted(t)))
        for i in options[goal]:
            solution = complete(chosen | {i}, limit, seen)
            if solution is not None:
                return solution
        return None
    cover = maximal
    try:
        for limit in range(1, len(maximal)):
            result = complete(frozenset(), limit, set())
            if result is not None:
                cover = result
                break
    except OverflowError:
        return {"status": "resource-limit", "reason": "cover-search", "feasible_upper_bound": len(maximal)}
    update, decoder = {}, {}
    for i, block in enumerate(cover):
        for event, targets in following[block].items():
            j = next(k for k, dest in enumerate(cover) if targets <= dest)
            update.setdefault(str(i), {})[event] = str(j)
        for receiver, action in choices[block].items():
            decoder.setdefault(receiver, {})[str(i)] = action
    return {"status": "ready", "symbols": len(cover),
            "encoder": {s: str(next(i for i,b in enumerate(cover) if names <= b)) for s,names in initial.items()},
            "decoder": decoder, "update": update,
            "members": {str(i): sorted(b) for i,b in enumerate(cover)}}


def synthesize(spec):
    rows = spec["worlds"]
    single = design(spec)
    moves = spec["moves"]
    if not isinstance(moves, list) or len(moves) > 256:
        raise ValueError("expected up to 256 moves")
    for move in moves:
        if (not isinstance(move, list) or len(move) != 3 or
                any(not isinstance(i, str) or i not in rows for i in (move[0], move[2])) or
                (move[1] is not None and (not isinstance(move[1], str) or not move[1]))):
            raise ValueError("a move needs named source and target worlds and a visible label or null")
    placement = spec.get("placement", "fixed")
    if placement not in ("fixed", "history"):
        raise ValueError("placement must be fixed or history")
    if placement == "history" and spec["protect"]:
        return {"status": "unsupported-scope", "reason": "history-sensitive-protection"}
    if single["status"] == "impossible":
        return single
    if placement == "history":
        return overlapping(rows, moves)

    readings = sorted({v["sender"] for v in rows.values()})
    group = {s: s for s in readings}
    proof = []

    def merge(left, right, cause):
        a, b = group[rows[left]["sender"]], group[rows[right]["sender"]]
        if a == b:
            return False
        for s in readings:
            if group[s] == max(a, b):
                group[s] = min(a, b)
        proof.append({"joined": [left, right], **cause})
        return True

    for a, b in spec["protect"]:
        merge(a, b, {"protect": [a, b]})
    for a, event, b in moves:
        if event is None:
            merge(a, b, {"silent": [a, b]})
    while True:
        first = {}
        changed = False
        for a, event, b in moves:
            if event is None:
                continue
            key = (group[rows[a]["sender"]], event)
            if key in first:
                prior_a, prior_b = first[key]
                changed |= merge(prior_b, b, {"after": event, "sources": [prior_a, a]})
            else:
                first[key] = a, b
        if not changed:
            break
    demands = {}
    for name, row in sorted(rows.items()):
        key = (group[row["sender"]], row["receiver"])
        old = demands.get(key)
        if old is not None and rows[old]["action"] != row["action"]:
            return {"status": "impossible", "reason": "continuation-collision",
                    "worlds": [old, name], "forced_joins": proof}
        demands[key] = name
    classes = sorted(set(group.values()))
    if len(classes) > 8:
        return {"status": "resource-limit", "remaining_classes": len(classes)}

    index = {s: classes.index(g) for s, g in group.items()}
    decisions = [(index[r["sender"]], r["receiver"], r["action"]) for r in rows.values()]
    updates = [(index[rows[a]["sender"]], event, index[rows[b]["sender"]])
               for a, event, b in moves if event is not None]

    def tables(labels):
        decoder, update = {}, {}
        for i, receiver, action in decisions:
            choices = decoder.setdefault(receiver, {})
            if choices.setdefault(str(labels[i]), action) != action:
                return None
        for i, event, j in updates:
            following = update.setdefault(str(labels[i]), {})
            if following.setdefault(event, str(labels[j])) != str(labels[j]):
                return None
        return decoder, update

    # Canonical restricted-growth strings enumerate each partition once.
    best = list(range(len(classes)))
    def visit(labels, used):
        nonlocal best
        if used >= max(best) + 1:
            return
        if len(labels) == len(classes):
            if tables(labels) is not None:
                best = labels
            return
        for code in range(used + 1):
            visit(labels + [code], max(used, code + 1))
    visit([0], 1)
    decoder, update = tables(best)
    return {"status": "ready", "symbols": max(best) + 1,
            "encoder": {s: str(best[i]) for s, i in index.items()},
            "decoder": decoder, "update": update}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    args = parser.parse_args()
    try:
        out = synthesize(json.loads(args.spec.read_bytes()))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(out, indent=2, sort_keys=True))
