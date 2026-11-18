"""Compare declared source, selection, and accepted-output identities.

A result nominates receiving work. It does not read source paths, infer
semantic dependencies, establish truth, or release independent obligations.
"""

import argparse as _argparse
import copy as _copy
import json as _json
import sys as _sys


_SELECTION_FIELDS = ("query_sha256", "catalogue_sha256", "policy_sha256")


def _check_inputs(previous, current):
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise ValueError("previous and current must be JSON objects")
    for key in ("artifacts", "obligations"):
        if not isinstance(previous.get(key), list):
            raise ValueError("previous." + key + " must be a list")
    for key in ("requirements", "sources", "artifacts"):
        if not isinstance(current.get(key), dict):
            raise ValueError("current." + key + " must be an object")
    if "selections" in current:
        if not isinstance(current["selections"], dict):
            raise ValueError("current.selections must be an object")
        for observed in current["selections"].values():
            if observed is not None and not isinstance(observed, dict):
                raise ValueError("each current selection must be an object or null")
    seen = set()
    for receipt in previous["artifacts"]:
        if not isinstance(receipt, dict):
            raise ValueError("each artifact receipt must be an object")
        if not isinstance(receipt.get("id"), str):
            raise ValueError("each artifact receipt requires a string id")
        if receipt["id"] in seen:
            raise ValueError("artifact ids must be unique")
        seen.add(receipt["id"])
        if not isinstance(receipt.get("artifact_sha256"), str):
            raise ValueError("each artifact receipt requires artifact_sha256")
        for key in ("requirements", "sources"):
            if not isinstance(receipt.get(key), dict):
                raise ValueError("receipt." + key + " must be an object")
        if receipt.get("scope", "fixed") not in ("fixed", "discovery"):
            raise ValueError("receipt.scope must be fixed or discovery")
        for key in ("selections", "accepted_inputs"):
            if key in receipt and not isinstance(receipt[key], dict):
                raise ValueError("receipt." + key + " must be an object")
        for basis in receipt.get("selections", {}).values():
            if not isinstance(basis, dict):
                raise ValueError("each previous selection must be an object")
    if any(not isinstance(item, dict) for item in previous["obligations"]):
        raise ValueError("each obligation must be an object")


def _compare(kind, identity, expected, observations):
    if identity not in observations:
        suffix = "unobserved"
    elif observations[identity] is None:
        suffix = "absent"
    elif observations[identity] != expected:
        suffix = "changed"
    else:
        return None
    return {"kind": kind + "_" + suffix, "id": identity}


def _selection_reasons(receipt, current):
    """Compare declared fields and retain unsupported discovery currentness."""
    selections = receipt.get("selections", {})
    reasons = []
    if receipt.get("scope", "fixed") == "discovery" and (
        not selections or any(
            any(field not in basis for field in _SELECTION_FIELDS)
            for basis in selections.values()
        )
    ):
        reasons.append({"kind": "selection_basis_missing", "id": receipt["id"]})
    observations = current.get("selections", {})
    for selection_id, basis in selections.items():
        if selection_id in observations and observations[selection_id] is None:
            # A null selection explicitly observes every declared field absent.
            observed_fields = {field: None for field in basis}
        else:
            observed_fields = observations.get(selection_id, {})
        for field, expected in basis.items():
            reason = _compare("selection", field, expected, observed_fields)
            if reason is not None:
                reason["id"] = selection_id + "/" + field
                reasons.append(reason)
    return reasons


def assess(previous: dict, current: dict) -> dict:
    """Return ordered current/recheck/unknown decisions without mutating inputs.

    Missing keys mean unobserved; explicit None means observed absent.
    A known change outranks an unavailable observation, retaining all reasons.
    Selection dependencies apply to every receipt; discovery also requires a
    complete declared selection basis. Accepted inputs pin exact output bytes.
    Open obligations are deep-copied independently of artifact decisions.
    """
    _check_inputs(previous, current)
    results = []
    for receipt in previous["artifacts"]:
        reasons = []
        for singular, plural in (("requirement", "requirements"),
                                 ("source", "sources")):
            for identity, expected in receipt[plural].items():
                reason = _compare(singular, identity, expected, current[plural])
                if reason is not None:
                    reasons.append(reason)
        reason = _compare("artifact", receipt["id"], receipt["artifact_sha256"],
                          current["artifacts"])
        if reason is not None:
            reasons.append(reason)
        reasons.extend(_selection_reasons(receipt, current))
        for identity, expected in receipt.get("accepted_inputs", {}).items():
            reason = _compare("accepted_input", identity, expected,
                              current["artifacts"])
            if reason is not None:
                reasons.append(reason)
        known_change = any(reason["kind"].endswith(("_changed", "_absent"))
                           for reason in reasons)
        status = "recheck" if known_change else ("unknown" if reasons else "current")
        results.append({"id": receipt["id"], "status": status, "reasons": reasons})
    return {
        "scope": "declared-input-currentness",
        "artifacts": results,
        "open_obligations": _copy.deepcopy([
            item for item in previous["obligations"] if item.get("status") == "open"
        ]),
    }


def _main(argv=None):
    parser = _argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", required=True, help="previous receipts JSON")
    parser.add_argument("--current", required=True, help="current observations JSON")
    args = parser.parse_args(argv)
    try:
        with open(args.previous, "r", encoding="utf-8") as stream:
            previous = _json.load(stream)
        with open(args.current, "r", encoding="utf-8") as stream:
            current = _json.load(stream)
        result = assess(previous, current)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        print("continuation_assessor: " + str(exc), file=_sys.stderr)
        return 2
    print(_json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
