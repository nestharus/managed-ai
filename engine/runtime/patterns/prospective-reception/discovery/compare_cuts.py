"""Compare authorized, ordered label readings at each holder's own cut."""
import argparse
import json
from pathlib import Path

def compare(request, expected):
    holders = request["required"]
    scope = request["scope"]
    code = request["code"]
    snapshots = request["snapshots"]
    if (not isinstance(holders, list) or not holders or not all(isinstance(h, str) and h for h in holders)
            or len(set(holders)) != len(holders) or not isinstance(scope, list) or not scope
            or not all(isinstance(s, str) and s for s in scope)
            or not isinstance(code, str) or not code or not isinstance(expected, str) or not expected):
        raise ValueError("invalid roster, scope, code or requested label")
    if not isinstance(snapshots, dict) or set(snapshots) - set(holders):
        raise ValueError("received snapshot outside the requested roster")
    seen = {}
    gaps = []
    labels = {}
    for holder in holders:
        if holder not in snapshots:
            gaps.append(holder)
            continue
        snap = snapshots[holder]
        if snap["holder"] != holder or snap["scope"] != scope:
            raise ValueError("wrong holder or use")
        if snap["complete"] is not True or snap.get("next") is not None:
            gaps.append(holder)
            continue
        applied = set()
        latest = None
        for event in snap["events"]:
            key = event["occurrence"]
            printed = event["printed"]
            observation = event["observation"]
            if (not isinstance(key, list) or len(key) != 2 or not isinstance(key[0], str) or not key[0]
                    or type(key[1]) != int or key[1] < 0 or not isinstance(printed, dict)
                    or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in printed.items())
                    or not isinstance(observation, str) or not observation):
                raise ValueError("malformed occurrence")
            identity = tuple(key)
            body = (observation, printed)
            if identity in seen and seen[identity] != body:
                raise ValueError("inconsistent representation of one occurrence")
            seen[identity] = body
            if identity not in applied:
                applied.add(identity)
                if code in printed:
                    latest = printed[code]
        if latest is None:
            gaps.append(holder)
        else:
            labels[holder] = latest
    distinct = set(labels.values())
    if len(distinct) > 1:
        status = "disagree"
    elif gaps:
        status = "incomplete"
    elif distinct == {expected}:
        status = "match-at-reported-cuts"
    else:
        status = "other-label"
    return {"status":status,"expected":expected,"by_holder":labels,
            "gaps":sorted(gaps),"occurrences":len(seen)}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("expected", help="Label required by this particular order")
    args = parser.parse_args()
    try:
        result = compare(json.loads(args.request.read_text()), args.expected)
    except (KeyError, TypeError, ValueError) as exc:
        parser.exit(2, "Input refused: " + str(exc) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
