"""Find uses whose recorded reads differ from new, explicitly named input observations."""
import argparse
import json
import re
from pathlib import Path

def pins(values):
    if not isinstance(values, dict):
        raise ValueError("expected a map of named inputs to digests")
    for key, value in values.items():
        if not isinstance(key, str) or not key or (value is not None and
                (not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None)):
            raise ValueError("expected a named input and its SHA-256 or an observed absence")
    return values

class ReadRoutes:
    """Reverse index over the reads each use recorded on its last evaluation."""
    def __init__(self, receipts):
        self.by_input = {}
        self.by_use = {}
        for use, reads in receipts.items():
            self.replace(use, reads)

    def replace(self, use, reads):
        if not isinstance(use, str) or not use:
            raise ValueError("expected a named use")
        observed = dict(pins(reads))
        for key in self.by_use.get(use, {}):
            del self.by_input[key][use]
            if not self.by_input[key]:
                del self.by_input[key]
        self.by_use[use] = observed
        for key, value in observed.items():
            self.by_input.setdefault(key, {})[use] = value

    def affected(self, changes):
        reasons = {}
        for key, current in pins(changes).items():
            for use, previous in self.by_input.get(key, {}).items():
                if previous != current:
                    reasons.setdefault(use, []).append(key)
        return {use:sorted(keys) for use,keys in sorted(reasons.items())}

if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts",type=Path,help="JSON map from use to recorded input digests")
    parser.add_argument("changes",type=Path,help="JSON map of observations, including null for known absence")
    args=parser.parse_args()
    try:
        receipts=json.loads(args.receipts.read_bytes())
        if not isinstance(receipts,dict):
            raise ValueError("expected receipts by use")
        result=ReadRoutes(receipts).affected(json.loads(args.changes.read_bytes()))
    except (TypeError,ValueError) as exc:
        parser.exit(2,"Input refused: "+str(exc)+"\n")
    print(json.dumps(result,sort_keys=True,indent=2))
