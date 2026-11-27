"""A bounded view of a caller-authored composition, without effect authority.

This optional profile exposes declared predictions, offered contributions,
premises and interactions. It neither discovers semantic truth nor turns open
record kinds or arbitrary relation labels into a universal task graph.
"""

from collections.abc import Callable, Iterator
from contextlib import nullcontext
from typing import Any

from managed_ai_information.core.formats import COMPOSITION_FORMAT as PROFILE
from managed_ai_information.core.formats import COMPOSITION_VIEW_FORMAT as FORMAT
from managed_ai_information.core.formats import INTERACTION_MAP_FORMAT

from .composition_eval import (
    COLLECTIONS,
    ViewBudget,
    bounded_int,
    bounded_profile_bytes,
    collect_interaction_pairs,
    exact_allowed_fields,
    observe_basis_row,
    select_focused_collections,
    string_list,
    text_field,
    validate_collection_row,
)

_integer, _strings, _text = bounded_int, string_list, text_field

MAX_ROWS = 2000
MAX_REFERENCES = 20000
MAX_PROFILE_BYTES = 2 * 1024 * 1024
MAX_BASIS_BYTES = 32 * 1024 * 1024
MAX_VIEW_BYTES = 1024 * 1024
PREFIXES = dict(zip(COLLECTIONS, ("element", "relation", "premise", "opportunity"), strict=True))
SCOPE = (
    "Caller-authored meanings and availability claims on nominated sources. "
    "No semantic completeness, effect authority, automatic dispatch or "
    "guarantee of actual parallel independence. Input/output resources are "
    "compared by exact declared identity; undeclared interactions remain unknown."
)


class Composition:
    def __init__(self, system: Any) -> None:
        self.system = system

    def profile(
        self,
        record: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, dict[str, dict[str, Any]]], int, int]:
        body = record.get("content")
        if not isinstance(body, dict) or body.get("format") != PROFILE:
            raise ValueError(
                "compose requires ordinary record.content with format "
                + PROFILE
                + "; information envelopes are not implicitly unwrapped"
            )
        exact_allowed_fields(
            body,
            {"format", "purpose", *COLLECTIONS, "unknowns", "coverage", "content"},
            "composition",
        )
        text_field(body.get("purpose"), "composition purpose")
        input_bytes = bounded_profile_bytes(body, MAX_PROFILE_BYTES)
        string_list(body.get("unknowns", []), "composition unknowns")

        if "coverage" in body and not isinstance(body["coverage"], (str, dict, list)):
            raise ValueError("composition coverage must be text, an object or a list")

        rows: dict[str, dict[str, dict[str, Any]]] = {}
        references = 0
        allowed = {
            "elements": {"id", "account", "content"},
            "relations": {
                "id",
                "from",
                "to",
                "prediction",
                "observation",
                "account",
                "basis",
                "content",
            },
            "premises": {"id", "account", "disposition", "basis", "content"},
            "opportunities": {
                "id",
                "at",
                "contribution",
                "requires",
                "disposition",
                "reads",
                "writes",
                "footprint",
                "conflicts",
                "phase",
                "account",
                "content",
            },
        }

        for collection in COLLECTIONS:
            source = body.get(collection, [])
            if not isinstance(source, list):
                raise ValueError(collection + " must be a list")

            rows[collection] = {}
            for row in source:
                exact_allowed_fields(row, allowed[collection], collection + " row")
                ident = text_field(row.get("id"), collection + " id")
                if ident in rows[collection]:
                    raise ValueError("duplicate " + collection + " id: " + ident)
                rows[collection][ident] = row
                references += validate_collection_row(collection, ident, row)

        if sum(len(table) for table in rows.values()) > MAX_ROWS:
            raise ValueError("composition exceeds the 2000 total declared-row bound")
        if references > MAX_REFERENCES:
            raise ValueError("composition exceeds the 20000 declared-reference bound")
        return body, rows, input_bytes, references

    def _basis(self, record: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], int]:
        captured: dict[str, dict[str, Any]] = {}
        used = 0
        declared = record.get("basis", [])
        if not isinstance(declared, list) or len(declared) > MAX_ROWS:
            raise ValueError("record basis must be a list of at most 2000 captured paths")

        objects = getattr(self.system.ex, "objects", None)
        prefixes = getattr(
            getattr(self.system, "record_store", None),
            "content_addressed_basis_prefixes",
            (),
        )
        with (
            objects.pack_session()
            if objects is not None and hasattr(objects, "pack_session")
            else nullcontext()
        ):
            for row in declared:
                if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                    raise ValueError("record basis must contain captured path objects")
                name = row["path"]
                if name in captured:
                    raise ValueError("duplicate captured record basis path: " + name)
                captured[name], used = observe_basis_row(
                    self.system.ex, row, objects, prefixes, used, MAX_PROFILE_BYTES, MAX_BASIS_BYTES
                )

        return captured, used

    @staticmethod
    def _support(names: list[str], basis: dict[str, dict[str, Any]]) -> dict[str, Any]:
        observed = [basis.get(name, {"path": name, "status": "unbound"}) for name in names]
        return {
            "status": (
                "no-declared-evidence"
                if not observed
                else "current"
                if all(row["status"] == "current" for row in observed)
                else "needs-qualification"
            ),
            "sources": observed,
            "scope": "Byte correspondence of nominated evidence; not assessment of its meaning.",
        }

    def interaction_map(self, record_id: str, limit: int = 20) -> dict[str, Any]:
        """Factor declared positive interactions by resource or explicit conflict."""
        bounded_int(limit, "interaction group limit", 200)
        record = self.system.show(record_id)
        _, rows, _, references = self.profile(record)
        opportunities = rows["opportunities"]
        resources: dict[str, dict[str, list[str]]] = {}
        explicit: dict[str, set[str]] = {}
        unknown = unresolved = self_refs = 0
        for ident, row in opportunities.items():
            unknown += row.get("footprint", "unknown") == "unknown"
            for mode in ("reads", "writes"):
                for name in row.get(mode, []):
                    resources.setdefault(name, {"reads": [], "writes": []})[mode].append(ident)
            for other in row.get("conflicts", []):
                if other not in opportunities:
                    unresolved += 1
                elif other == ident:
                    self_refs += 1
                else:
                    left, right = sorted((ident, other))
                    explicit.setdefault(left, set()).add(right)

        def groups() -> Iterator[dict[str, Any]]:
            for name, users in resources.items():
                writers = users["writes"]
                if writers and (
                    len(writers) > 1 or any(reader != writers[0] for reader in users["reads"])
                ):
                    yield {"kind": "resource", "resource": name, **users}
            for left, others in explicit.items():
                yield {"kind": "explicit-conflict", "left": left, "others": sorted(others)}

        coverage: dict[str, int] = {
            "opportunities": len(opportunities),
            "declared_references": references,
            "unknown_footprints": unknown,
            "unresolved_conflict_refs": unresolved,
            "self_conflict_refs": self_refs,
            "total_groups": 0,
            "omitted_by_limit": 0,
            "omitted_by_bytes": 0,
        }
        result_groups: list[dict[str, Any]] = []
        result: dict[str, Any] = {
            "format": INTERACTION_MAP_FORMAT,
            "record": {key: record[key] for key in ("id", "revision", "sha256")},
            "groups": result_groups,
            "coverage": coverage,
            "scope": (
                "Current declarations only. Resource groups relate each writer to every other "
                "writer and reader of that name. Explicit edges are unordered. This view does "
                "not check physical identity, evidence currency, hidden costs or authority. "
                "Recover omitted groups and original opportunities with show."
            ),
        }
        budget = ViewBudget(MAX_VIEW_BYTES)
        if not budget.include(result):
            raise ValueError("interaction map header exceeds the view bound")

        for group in groups():
            coverage["total_groups"] += 1
            if len(result_groups) >= limit:
                coverage["omitted_by_limit"] += 1
            elif budget.include({"groups": [group]}):
                result_groups.append(group)
            else:
                coverage["omitted_by_bytes"] += 1

        return result

    def _evaluate_premises(
        self,
        rows: dict[str, dict[str, dict[str, Any]]],
        basis: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        premises: dict[str, dict[str, Any]] = {}

        for ident, row in rows["premises"].items():
            support = self._support(row.get("basis", []), basis)
            declared = row.get("disposition", "unassessed")
            availability = "unassessed" if support["status"] == "needs-qualification" else declared
            premises[ident] = dict(
                row,
                ref="premise:" + ident,
                disposition=declared,
                availability=availability,
                support=support,
                scope=(
                    "Caller-supplied availability, qualified by nominated current source bindings."
                ),
            )

        return premises

    def _evaluate_relations(
        self,
        rows: dict[str, dict[str, dict[str, Any]]],
        basis: dict[str, dict[str, Any]],
        known: set[str],
        missing: Callable[[str, str], str],
    ) -> dict[str, dict[str, Any]]:
        relations: dict[str, dict[str, Any]] = {}

        for ident, row in rows["relations"].items():
            ref = "relation:" + ident
            endpoints = ["element:" + value for key in ("from", "to") for value in row.get(key, [])]
            unknown = [missing(value, ref) for value in endpoints if value not in known]
            relations[ident] = dict(
                row,
                ref=ref,
                observation=row.get("observation", "unassessed"),
                support=self._support(row.get("basis", []), basis),
                unresolved_refs=unknown,
                observation_scope=(
                    "Caller-authored observation; no truth or alignment judgment inferred."
                ),
            )

        return relations

    def _evaluate_opportunities(
        self,
        rows: dict[str, dict[str, dict[str, Any]]],
        premises: dict[str, dict[str, Any]],
        known: set[str],
        missing: Callable[[str, str], str],
    ) -> dict[str, dict[str, Any]]:
        opportunities: dict[str, dict[str, Any]] = {}

        for ident, row in rows["opportunities"].items():
            ref = "opportunity:" + ident
            unknown = [
                missing(value, ref)
                for value in row.get("at", [])
                if value not in known or value.partition(":")[0] not in ("element", "relation")
            ]

            for other in row.get("conflicts", []):
                if other not in rows["opportunities"]:
                    missing("opportunity:" + other, ref)

            alternatives: list[dict[str, Any]] = []
            for group in row.get("requires", []):
                inputs: list[dict[str, Any]] = []
                for premise in group:
                    if premise not in premises:
                        missing("premise:" + premise, ref)
                        inputs.append(
                            {"ref": "premise:" + premise, "availability": "unresolved-reference"}
                        )
                    else:
                        known_premise = premises[premise]
                        inputs.append(
                            {
                                "ref": known_premise["ref"],
                                "availability": known_premise["availability"],
                                "support_status": known_premise["support"]["status"],
                            }
                        )
                states = [value["availability"] for value in inputs]
                status = (
                    "available"
                    if all(s == "available" for s in states)
                    else "unavailable"
                    if "unavailable" in states
                    else "unassessed"
                )
                alternatives.append({"premises": inputs, "availability": status})

            states = [alternative["availability"] for alternative in alternatives]
            availability = (
                "available-under-declared-premises"
                if not states or "available" in states
                else "unavailable-under-declared-premises"
                if all(s == "unavailable" for s in states)
                else "needs-qualification"
            )
            if unknown:
                availability = "needs-qualification"

            opportunities[ident] = dict(
                row,
                ref=ref,
                disposition=row.get("disposition", "open"),
                phase=row.get("phase", "unknown"),
                footprint=row.get("footprint", "unknown"),
                availability=availability,
                premise_alternatives=alternatives,
                unresolved_refs=unknown,
                footprint_scope=(
                    "Declared resources are partial claims; "
                    "implicit context/publication dependencies are not inferred."
                ),
            )

        return opportunities

    def inspect(
        self,
        record_id: str,
        focus: str | None = None,
        limit: int = 20,
        pair_limit: int = 200,
    ) -> dict[str, Any]:
        bounded_int(limit, "composition limit", 200)
        bounded_int(pair_limit, "composition pair_limit", 20000, minimum=0)
        if focus is not None:
            text_field(focus, "composition focus")
        record = self.system.show(record_id)
        body, rows, input_bytes, reference_count = self.profile(record)
        basis, basis_bytes = self._basis(record)
        known = {PREFIXES[c] + ":" + ident for c in COLLECTIONS for ident in rows[c]}
        # Unambiguously resolve a bare ID supplied to --focus
        # (e.g. 'pkg-engine-store' -> 'element:pkg-engine-store').
        if focus is not None and ":" not in focus:
            matches = [PREFIXES[c] + ":" + focus for c in COLLECTIONS if focus in rows[c]]
            if len(matches) == 1:
                focus = matches[0]
        unresolved: list[dict[str, str]] = []
        unresolved_keys: set[tuple[str, str]] = set()

        def missing(ref: str, owner: str) -> str:
            if (ref, owner) not in unresolved_keys:
                unresolved_keys.add((ref, owner))
                unresolved.append({"ref": ref, "declared_in": owner})
            return ref

        if focus is not None and focus not in known:
            missing(focus, "requested-focus")
        premises = self._evaluate_premises(rows, basis)
        relations = self._evaluate_relations(rows, basis, known, missing)
        opportunities = self._evaluate_opportunities(rows, premises, known, missing)
        all_opportunities, collections, coverage = select_focused_collections(
            rows, relations, premises, opportunities, focus, limit
        )

        pairs: list[dict[str, Any]] = []
        count = len(all_opportunities) * (len(all_opportunities) - 1) // 2
        saturated: list[str] = []
        pair_coverage: dict[str, Any] = {
            "total": count,
            "examined": 0,
            "omitted": count,
            "saturated": saturated,
            "ordering": "Focus-first opportunity order, then unordered pairs in that order.",
        }
        result: dict[str, Any] = {
            "format": FORMAT,
            "profile": PROFILE,
            "record": {key: record[key] for key in ("id", "revision", "sha256")},
            "purpose": body["purpose"],
            "focus": focus,
            **collections,
            "basis": list(basis.values()),
            "interactions": pairs,
            "unresolved_refs": unresolved,
            "unknowns": body.get("unknowns", []),
            "authored_coverage": body.get("coverage"),
            "coverage": {
                "collections": coverage,
                "total_declared_rows": sum(len(table) for table in rows.values()),
                "declared_reference_count": reference_count,
                "profile_bytes": input_bytes,
                "examined_basis_bytes": basis_bytes,
                "limits": {
                    "detail_per_collection": limit,
                    "pair_limit": pair_limit,
                    "total_rows": MAX_ROWS,
                    "total_references": MAX_REFERENCES,
                    "profile_bytes": MAX_PROFILE_BYTES,
                    "basis_bytes": MAX_BASIS_BYTES,
                    "view_bytes": MAX_VIEW_BYTES,
                },
                "pairs": pair_coverage,
                "recovery": {
                    "record": record_id,
                    "account": (
                        "Show the exact captured composition body; a current compose call is a new "
                        "observation and may use changed basis. Focus opportunity:ID to bring its "
                        "pairs first, or increase declared bounds when useful."
                    ),
                },
                "scope": (
                    "Complete bounded profile evaluated; detail and pair omissions are explicit. "
                    "Cycles in authored relations are allowed, not treated as task deadlocks. "
                    "No outside composition or semantic relation census."
                ),
            },
            "scope": SCOPE,
        }
        budget = ViewBudget(MAX_VIEW_BYTES)
        if not budget.include(result):
            raise ValueError(
                "composition base view exceeds the 1 MiB returned-view byte bound; "
                "reduce limit, narrow the nominated composition, or read its exact body with show"
            )
        collected, byte_saturated = collect_interaction_pairs(all_opportunities, pair_limit, budget)
        pairs.extend(collected)
        pair_coverage.update(examined=len(pairs), omitted=count - len(pairs))
        if byte_saturated:
            saturated.append("view-byte-bound")
        if len(pairs) == pair_limit and len(pairs) < count:
            saturated.append("pair-limit")
        return result
