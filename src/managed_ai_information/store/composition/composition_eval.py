"""Validation, basis observation, interaction pairing, and focus selection for Composition."""

import hashlib
import json
from collections.abc import Iterable
from typing import Any

COLLECTIONS = ("elements", "relations", "premises", "opportunities")
_VIEW_ENCODER = json.JSONEncoder(ensure_ascii=False, indent=2, allow_nan=False)
_PROFILE_ENCODER = json.JSONEncoder(ensure_ascii=False, allow_nan=False)


def text_field(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " must be nonempty text")
    return value


def string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(label + " must be a list of nonempty strings")
    if len(value) != len(set(value)):
        raise ValueError(label + " contains duplicate references")
    return value


def exact_allowed_fields(value: Any, allowed: Iterable[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(label + " must be an object")
    extra = set(value) - set(allowed)
    if extra:
        raise ValueError("unknown " + label + " fields: " + ", ".join(sorted(extra)))


def bounded_int(value: Any, name: str, maximum: int, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


class ViewBudget:
    """Measure UTF-8 encoded JSON bytes with C-accelerated json.JSONEncoder.encode()."""

    def __init__(self, max_view_bytes: int) -> None:
        self.used = 1024
        self.max_view_bytes = max_view_bytes

    def include(self, value: Any) -> bool:
        size = len(_VIEW_ENCODER.encode(value).encode("utf-8"))
        if self.used + size > self.max_view_bytes:
            return False
        self.used += size
        return True


def bounded_profile_bytes(body: dict[str, Any], max_profile_bytes: int) -> int:
    """Measure profile UTF-8 JSON bytes via C-accelerated encoding with early ceiling check."""
    total = 2
    for key, val in body.items():
        if key in COLLECTIONS and isinstance(val, list):
            total += len(key.encode("utf-8")) + 6
            for row in val:
                total += len(_PROFILE_ENCODER.encode(row).encode("utf-8")) + 2
                if total > max_profile_bytes + 4096:
                    raise ValueError("composition profile exceeds the 2 MiB view input bound")
        else:
            total += (
                len(key.encode("utf-8")) + len(_PROFILE_ENCODER.encode(val).encode("utf-8")) + 4
            )
            if total > max_profile_bytes + 4096:
                raise ValueError("composition profile exceeds the 2 MiB view input bound")
    exact = len(_PROFILE_ENCODER.encode(body).encode("utf-8"))
    if exact > max_profile_bytes:
        raise ValueError("composition profile exceeds the 2 MiB view input bound")
    return exact


def validate_collection_row(collection: str, ident: str, row: dict[str, Any]) -> int:
    if "account" in row:
        text_field(row["account"], collection + " account")
    if collection == "elements":
        text_field(row.get("account"), "element account")
        return 0
    if collection == "relations":
        refs = sum(
            len(string_list(row.get(k, []), "relation " + k)) for k in ("from", "to", "basis")
        )
        text_field(row.get("prediction"), "relation prediction")
        observation = row.get("observation", "unassessed")
        if observation not in ("unassessed", "supported", "contradicted"):
            raise ValueError(
                f"relation {ident!r} field 'observation' must be unassessed, "
                "supported or contradicted; "
                "put the explanatory observation in this relation's 'account' field"
            )
        if observation != "unassessed":
            text_field(
                row.get("account"),
                f"relation {ident!r} field 'account' for an asserted observation",
            )
        return refs
    if collection == "premises":
        text_field(row.get("account"), "premise account")
        if row.get("disposition", "unassessed") not in ("available", "unavailable", "unassessed"):
            raise ValueError("premise disposition must be available, unavailable or unassessed")
        return len(string_list(row.get("basis", []), "premise basis"))

    text_field(row.get("contribution"), "opportunity contribution")
    refs = sum(
        len(string_list(row.get(k, []), "opportunity " + k))
        for k in ("at", "reads", "writes", "conflicts")
    )
    if row.get("disposition", "open") not in ("open", "held", "complete"):
        raise ValueError("opportunity disposition must be open, held or complete")
    if row.get("footprint", "unknown") not in ("declared", "unknown"):
        raise ValueError("opportunity footprint must be declared or unknown")
    text_field(row.get("phase", "unknown"), "opportunity phase")
    alternatives = row.get("requires", [])
    if not isinstance(alternatives, list):
        raise ValueError("opportunity requires must be a list of alternative premise lists")
    return refs + sum(len(string_list(g, "opportunity premise alternative")) for g in alternatives)


def finish_basis_observation(
    raw: bytes, row: dict[str, Any], used: int, observed: dict[str, Any]
) -> int:
    observed["current_sha256"] = hashlib.sha256(raw).hexdigest()
    observed["current_bytes"] = len(raw)
    observed["status"] = "current" if row.get("sha256") == observed["current_sha256"] else "changed"
    return used + len(raw)


def _check_basis_bound(
    size: int, used: int, max_profile: int, max_basis: int, observed: dict[str, Any]
) -> bool:
    if size > max_profile or used + size > max_basis:
        observed["status"] = (
            "per-source-byte-bound" if size > max_profile else "total-source-byte-bound"
        )
        return False
    return True


def observe_exchange_basis(
    objects: Any,
    row: dict[str, Any],
    prefixes: tuple[str, ...],
    used: int,
    observed: dict[str, Any],
    max_profile_bytes: int,
    max_basis_bytes: int,
) -> int:
    from ..protocol.basis_pins import is_content_addressed_basis

    if not (
        objects is not None
        and is_content_addressed_basis(row, prefixes=prefixes)
        and objects.has(row["sha256"])
    ):
        observed["status"] = "missing"
        return used
    size = row["bytes"]
    if not _check_basis_bound(size, used, max_profile_bytes, max_basis_bytes, observed):
        return used
    raw = objects.read(row["sha256"], retained=True, expected_bytes=size)
    observed["custody"] = "exchange-object"
    return finish_basis_observation(raw, row, used, observed)


def observe_file_basis(
    path: Any,
    info: Any,
    row: dict[str, Any],
    used: int,
    observed: dict[str, Any],
    max_profile_bytes: int,
    max_basis_bytes: int,
) -> int:
    if not _check_basis_bound(info.st_size, used, max_profile_bytes, max_basis_bytes, observed):
        return used
    with path.open("rb") as source:
        raw = source.read(max_profile_bytes + 1)
    if len(raw) > max_profile_bytes or used + len(raw) > max_basis_bytes:
        observed["status"] = "source-byte-bound"
        return used
    return finish_basis_observation(raw, row, used, observed)


def observe_basis_row(
    system_ex: Any,
    row: dict[str, Any],
    objects: Any,
    prefixes: tuple[str, ...],
    used: int,
    max_profile_bytes: int,
    max_basis_bytes: int,
) -> tuple[dict[str, Any], int]:
    from managed_ai_information.core.storage import is_ordinary_file_stat

    name = row["path"]
    observed: dict[str, Any] = {
        "path": name,
        "captured_sha256": row.get("sha256"),
        "current_sha256": None,
        "status": "unavailable",
    }
    try:
        path = None if name.startswith("object:") else system_ex.path(name)
        info = None
        if path is not None:
            try:
                st = path.lstat()
                info = st if is_ordinary_file_stat(st) else None
            except OSError:
                info = None
        if info is None or path is None:
            used = observe_exchange_basis(
                objects, row, prefixes, used, observed, max_profile_bytes, max_basis_bytes
            )
        else:
            used = observe_file_basis(
                path, info, row, used, observed, max_profile_bytes, max_basis_bytes
            )
    except (OSError, ValueError) as exc:
        observed["error"] = str(exc)
    return observed, used


def pair_interaction_reasons(
    l_id: str,
    l_sets: tuple[frozenset[str], frozenset[str], frozenset[str]],
    r_id: str,
    r_sets: tuple[frozenset[str], frozenset[str], frozenset[str]],
) -> list[dict[str, Any]]:
    l_reads, l_writes, l_conflicts = l_sets
    r_reads, r_writes, r_conflicts = r_sets
    reasons: list[dict[str, Any]] = []
    if r_id in l_conflicts or l_id in r_conflicts:
        reasons.append({"kind": "explicit-conflict"})
    for kind, a, b in (
        ("write-write", l_writes, r_writes),
        ("left-write-right-read", l_writes, r_reads),
        ("left-read-right-write", l_reads, r_writes),
    ):
        if a and b and (common := a & b):
            reasons.append({"kind": kind, "resources": sorted(common)})
    return reasons


def collect_interaction_pairs(
    all_opportunities: list[dict[str, Any]],
    pair_limit: int,
    budget: ViewBudget,
) -> tuple[list[dict[str, Any]], bool]:
    pairs: list[dict[str, Any]] = []
    opp_sets = [
        (
            frozenset(o.get("reads", ())),
            frozenset(o.get("writes", ())),
            frozenset(o.get("conflicts", ())),
        )
        for o in all_opportunities
    ]
    byte_saturated = False

    for idx, left in enumerate(all_opportunities):
        if len(pairs) >= pair_limit or byte_saturated:
            break
        l_id, l_ref, l_sets = left["id"], left["ref"], opp_sets[idx]
        l_unknown = left["footprint"] == "unknown"
        for j in range(idx + 1, len(all_opportunities)):
            if len(pairs) >= pair_limit:
                break
            right = all_opportunities[j]
            reasons = pair_interaction_reasons(l_id, l_sets, right["id"], opp_sets[j])
            unknown = l_unknown or right["footprint"] == "unknown"
            pair = {
                "left": l_ref,
                "right": right["ref"],
                "phase_refs": [
                    {"opportunity": l_ref, "field": "phase"},
                    {"opportunity": right["ref"], "field": "phase"},
                ],
                "interactions": reasons,
                "status": (
                    "declared-interaction"
                    if reasons
                    else "unknown-footprint"
                    if unknown
                    else "no-declared-interaction"
                ),
                "unknown_footprint": unknown,
                "scope": (
                    "No declared interaction is not actual independence; "
                    "context and effect contracts still apply."
                ),
            }
            if not budget.include({"interactions": [pair]}):
                byte_saturated = True
                break
            pairs.append(pair)
    return pairs, byte_saturated


def select_focused_collections(
    rows: dict[str, dict[str, dict[str, Any]]],
    relations: dict[str, dict[str, Any]],
    premises: dict[str, dict[str, Any]],
    opportunities: dict[str, dict[str, Any]],
    focus: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    focus_refs = {focus} if focus is not None else set()
    if focus is not None and focus.startswith("element:"):
        element = focus.partition(":")[2]
        focus_refs.update(
            "relation:" + ident
            for ident, relation in rows["relations"].items()
            if element in relation.get("from", []) or element in relation.get("to", [])
        )
    all_opportunities = sorted(
        opportunities.values(),
        key=lambda row: (
            row["ref"] != focus,
            not (row["ref"] in focus_refs or bool(set(row.get("at", [])) & focus_refs)),
        ),
    )
    displayed_opportunities = all_opportunities[:limit]
    related_refs = set(focus_refs)
    for row in displayed_opportunities:
        related_refs.update(row.get("at", []))
        related_refs.update(
            "premise:" + name for group in row.get("requires", []) for name in group
        )
    for relation in relations.values():
        if relation["ref"] in related_refs:
            related_refs.update(
                "element:" + value for key in ("from", "to") for value in relation.get(key, [])
            )

    collections: dict[str, list[dict[str, Any]]] = {"opportunities": displayed_opportunities}
    complete = {
        "elements": [dict(row, ref="element:" + ident) for ident, row in rows["elements"].items()],
        "relations": list(relations.values()),
        "premises": list(premises.values()),
        "opportunities": all_opportunities,
    }
    coverage: dict[str, dict[str, Any]] = {}
    for collection, items in complete.items():
        if collection != "opportunities":
            collections[collection] = sorted(
                items, key=lambda row: (row["ref"] != focus, row["ref"] not in related_refs)
            )[:limit]
        shown = {row["ref"] for row in collections[collection]}
        coverage[collection] = {
            "total": len(items),
            "shown": len(shown),
            "omitted_refs": [row["ref"] for row in items if row["ref"] not in shown],
        }
    return all_opportunities, collections, coverage
