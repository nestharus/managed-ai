"""A bounded view of a caller-authored composition, without effect authority.

This optional profile exposes declared predictions, offered contributions,
premises and interactions. It neither discovers semantic truth nor turns open
record kinds or arbitrary relation labels into a universal task graph.
"""

import hashlib
import json
from collections.abc import Callable, Iterable, Iterator
from contextlib import nullcontext
from typing import Any

from managed_ai_information.core.formats import COMPOSITION_FORMAT as PROFILE
from managed_ai_information.core.formats import COMPOSITION_VIEW_FORMAT as FORMAT
from managed_ai_information.core.formats import INTERACTION_MAP_FORMAT

MAX_ROWS = 2000
MAX_REFERENCES = 20000
MAX_PROFILE_BYTES = 2 * 1024 * 1024
MAX_BASIS_BYTES = 32 * 1024 * 1024
MAX_VIEW_BYTES = 1024 * 1024
COLLECTIONS = ("elements", "relations", "premises", "opportunities")
PREFIXES = dict(zip(COLLECTIONS, ("element", "relation", "premise", "opportunity"), strict=True))
SCOPE = (
    "Caller-authored meanings and availability claims on nominated sources. "
    "No semantic completeness, effect authority, automatic dispatch or "
    "guarantee of actual parallel independence. Input/output resources are "
    "compared by exact declared identity; undeclared interactions remain unknown."
)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " must be nonempty text")
    return value


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(label + " must be a list of nonempty strings")
    if len(value) != len(set(value)):
        raise ValueError(label + " contains duplicate references")
    return value


def _fields(value: Any, allowed: Iterable[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(label + " must be an object")
    extra = set(value) - set(allowed)
    if extra:
        raise ValueError("unknown " + label + " fields: " + ", ".join(sorted(extra)))


def _integer(value: Any, name: str, maximum: int, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


_VIEW_ENCODER = json.JSONEncoder(ensure_ascii=False, indent=2, allow_nan=False)
_PROFILE_ENCODER = json.JSONEncoder(ensure_ascii=False, allow_nan=False)


class _ViewBudget:
    """Measure UTF-8 encoded JSON bytes with C-accelerated json.JSONEncoder.encode()."""

    def __init__(self) -> None:
        self.used = 1024

    def include(self, value: Any) -> bool:
        size = len(_VIEW_ENCODER.encode(value).encode("utf-8"))
        if self.used + size > MAX_VIEW_BYTES:
            return False
        self.used += size
        return True


def _bounded_profile_bytes(body: dict[str, Any]) -> int:
    """Measure profile UTF-8 JSON bytes via C-accelerated encoding with early ceiling check."""
    # Check each top-level collection item boundedly before full encode to prevent huge allocations.
    total = 2
    for key, val in body.items():
        if key in COLLECTIONS and isinstance(val, list):
            total += len(key.encode("utf-8")) + 6
            for row in val:
                total += len(_PROFILE_ENCODER.encode(row).encode("utf-8")) + 2
                if total > MAX_PROFILE_BYTES + 4096:
                    raise ValueError("composition profile exceeds the 2 MiB view input bound")
        else:
            total += (
                len(key.encode("utf-8")) + len(_PROFILE_ENCODER.encode(val).encode("utf-8")) + 4
            )
            if total > MAX_PROFILE_BYTES + 4096:
                raise ValueError("composition profile exceeds the 2 MiB view input bound")
    exact = len(_PROFILE_ENCODER.encode(body).encode("utf-8"))
    if exact > MAX_PROFILE_BYTES:
        raise ValueError("composition profile exceeds the 2 MiB view input bound")
    return exact


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
        _fields(
            body,
            {"format", "purpose", *COLLECTIONS, "unknowns", "coverage", "content"},
            "composition",
        )
        _text(body.get("purpose"), "composition purpose")
        input_bytes = _bounded_profile_bytes(body)
        _strings(body.get("unknowns", []), "composition unknowns")

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
                _fields(row, allowed[collection], collection + " row")
                ident = _text(row.get("id"), collection + " id")
                if ident in rows[collection]:
                    raise ValueError("duplicate " + collection + " id: " + ident)

                rows[collection][ident] = row
                if "account" in row:
                    _text(row["account"], collection + " account")

                if collection == "elements":
                    _text(row.get("account"), "element account")
                elif collection == "relations":
                    for key in ("from", "to", "basis"):
                        references += len(_strings(row.get(key, []), "relation " + key))
                    _text(row.get("prediction"), "relation prediction")
                    observation = row.get("observation", "unassessed")
                    if observation not in ("unassessed", "supported", "contradicted"):
                        raise ValueError(
                            f"relation {ident!r} field 'observation' must be unassessed, "
                            "supported or contradicted; "
                            "put the explanatory observation in this relation's 'account' field"
                        )
                    if observation != "unassessed":
                        _text(
                            row.get("account"),
                            f"relation {ident!r} field 'account' for an asserted observation",
                        )
                elif collection == "premises":
                    _text(row.get("account"), "premise account")
                    if row.get("disposition", "unassessed") not in (
                        "available",
                        "unavailable",
                        "unassessed",
                    ):
                        raise ValueError(
                            "premise disposition must be available, unavailable or unassessed"
                        )
                    references += len(_strings(row.get("basis", []), "premise basis"))
                else:
                    _text(row.get("contribution"), "opportunity contribution")
                    for key in ("at", "reads", "writes", "conflicts"):
                        references += len(_strings(row.get(key, []), "opportunity " + key))
                    if row.get("disposition", "open") not in ("open", "held", "complete"):
                        raise ValueError("opportunity disposition must be open, held or complete")
                    if row.get("footprint", "unknown") not in ("declared", "unknown"):
                        raise ValueError("opportunity footprint must be declared or unknown")
                    _text(row.get("phase", "unknown"), "opportunity phase")
                    alternatives = row.get("requires", [])
                    if not isinstance(alternatives, list):
                        raise ValueError(
                            "opportunity requires must be a list of alternative premise lists"
                        )
                    for group in alternatives:
                        references += len(_strings(group, "opportunity premise alternative"))

        if sum(len(table) for table in rows.values()) > MAX_ROWS:
            raise ValueError("composition exceeds the 2000 total declared-row bound")
        if references > MAX_REFERENCES:
            raise ValueError("composition exceeds the 20000 declared-reference bound")
        return body, rows, input_bytes, references

    def _basis(self, record: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], int]:
        from managed_ai_information.core.storage import is_ordinary_file_stat
        from managed_ai_information.store.records.records import is_content_addressed_basis

        captured: dict[str, dict[str, Any]] = {}
        used = 0
        declared = record.get("basis", [])
        if not isinstance(declared, list) or len(declared) > MAX_ROWS:
            raise ValueError("record basis must be a list of at most 2000 captured paths")

        objects = getattr(self.system.ex, "objects", None)
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

                observed: dict[str, Any] = {
                    "path": name,
                    "captured_sha256": row.get("sha256"),
                    "current_sha256": None,
                    "status": "unavailable",
                }

                try:
                    path = None if name.startswith("object:") else self.system.ex.path(name)
                    info = None
                    if path is not None:
                        try:
                            st = path.lstat()
                            if is_ordinary_file_stat(st):
                                info = st
                        except OSError:
                            info = None

                    if info is None or path is None:
                        prefixes = getattr(
                            getattr(self.system, "record_store", None),
                            "content_addressed_basis_prefixes",
                            (),
                        )
                        if (
                            objects is not None
                            and is_content_addressed_basis(row, prefixes=prefixes)
                            and objects.has(row["sha256"])
                        ):
                            size = row["bytes"]
                            if size > MAX_PROFILE_BYTES:
                                observed["status"] = "per-source-byte-bound"
                            elif used + size > MAX_BASIS_BYTES:
                                observed["status"] = "total-source-byte-bound"
                            else:
                                raw = objects.read(
                                    row["sha256"], retained=True, expected_bytes=size
                                )
                                used += len(raw)
                                observed["current_sha256"] = hashlib.sha256(raw).hexdigest()
                                observed["current_bytes"] = len(raw)
                                observed["status"] = (
                                    "current"
                                    if row.get("sha256") == observed["current_sha256"]
                                    else "changed"
                                )
                                observed["custody"] = "exchange-object"
                        else:
                            observed["status"] = "missing"
                    elif info.st_size > MAX_PROFILE_BYTES:
                        observed["status"] = "per-source-byte-bound"
                    elif used + info.st_size > MAX_BASIS_BYTES:
                        observed["status"] = "total-source-byte-bound"
                    else:
                        with path.open("rb") as source:
                            raw = source.read(MAX_PROFILE_BYTES + 1)
                        if len(raw) > MAX_PROFILE_BYTES or used + len(raw) > MAX_BASIS_BYTES:
                            observed["status"] = "source-byte-bound"
                        else:
                            used += len(raw)
                            observed["current_sha256"] = hashlib.sha256(raw).hexdigest()
                            observed["current_bytes"] = len(raw)
                            observed["status"] = (
                                "current"
                                if row.get("sha256") == observed["current_sha256"]
                                else "changed"
                            )
                except (OSError, ValueError) as exc:
                    observed["error"] = str(exc)

                captured[name] = observed

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
        _integer(limit, "interaction group limit", 200)
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
        budget = _ViewBudget()
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

    @staticmethod
    def _collect_pairs(
        all_opportunities: list[dict[str, Any]],
        pair_limit: int,
        budget: _ViewBudget,
    ) -> tuple[list[dict[str, Any]], bool]:
        pairs: list[dict[str, Any]] = []
        reads_sets = [frozenset(o.get("reads", ())) for o in all_opportunities]
        writes_sets = [frozenset(o.get("writes", ())) for o in all_opportunities]
        conflicts_sets = [frozenset(o.get("conflicts", ())) for o in all_opportunities]
        byte_saturated = False

        for index, left in enumerate(all_opportunities):
            if len(pairs) >= pair_limit or byte_saturated:
                break

            l_id = left["id"]
            l_ref = left["ref"]
            l_reads, l_writes, l_conflicts = (
                reads_sets[index],
                writes_sets[index],
                conflicts_sets[index],
            )
            l_unknown = left["footprint"] == "unknown"

            for j in range(index + 1, len(all_opportunities)):
                if len(pairs) >= pair_limit:
                    break

                right = all_opportunities[j]
                reasons: list[dict[str, Any]] = []
                if right["id"] in l_conflicts or l_id in conflicts_sets[j]:
                    reasons.append({"kind": "explicit-conflict"})

                r_reads, r_writes = reads_sets[j], writes_sets[j]
                if l_writes and r_writes:
                    common = l_writes & r_writes
                    if common:
                        reasons.append({"kind": "write-write", "resources": sorted(common)})
                if l_writes and r_reads:
                    common = l_writes & r_reads
                    if common:
                        reasons.append(
                            {"kind": "left-write-right-read", "resources": sorted(common)}
                        )
                if l_reads and r_writes:
                    common = l_reads & r_writes
                    if common:
                        reasons.append(
                            {"kind": "left-read-right-write", "resources": sorted(common)}
                        )

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

    def inspect(
        self,
        record_id: str,
        focus: str | None = None,
        limit: int = 20,
        pair_limit: int = 200,
    ) -> dict[str, Any]:
        _integer(limit, "composition limit", 200)
        _integer(pair_limit, "composition pair_limit", 20000, minimum=0)
        if focus is not None:
            _text(focus, "composition focus")
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
            item = {"ref": ref, "declared_in": owner}
            if (ref, owner) not in unresolved_keys:
                unresolved_keys.add((ref, owner))
                unresolved.append(item)
            return ref

        if focus is not None and focus not in known:
            missing(focus, "requested-focus")
        premises = self._evaluate_premises(rows, basis)
        relations = self._evaluate_relations(rows, basis, known, missing)
        opportunities = self._evaluate_opportunities(rows, premises, known, missing)

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
            "elements": [
                dict(row, ref="element:" + ident) for ident, row in rows["elements"].items()
            ],
            "relations": list(relations.values()),
            "premises": list(premises.values()),
            "opportunities": all_opportunities,
        }
        coverage: dict[str, dict[str, Any]] = {}
        for collection, items in complete.items():
            if collection != "opportunities":
                # An explicitly requested row precedes related rows at the same
                # detail bound. Related rows alone must not crowd out that focus.
                collections[collection] = sorted(
                    items, key=lambda row: (row["ref"] != focus, row["ref"] not in related_refs)
                )[:limit]
            shown = {row["ref"] for row in collections[collection]}
            coverage[collection] = {
                "total": len(items),
                "shown": len(shown),
                "omitted_refs": [row["ref"] for row in items if row["ref"] not in shown],
            }

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
        budget = _ViewBudget()
        if not budget.include(result):
            raise ValueError(
                "composition base view exceeds the 1 MiB returned-view byte bound; "
                "reduce limit, narrow the nominated composition, or read its exact body with show"
            )
        collected, byte_saturated = self._collect_pairs(all_opportunities, pair_limit, budget)
        pairs.extend(collected)
        pair_coverage.update(examined=len(pairs), omitted=count - len(pairs))
        if byte_saturated:
            saturated.append("view-byte-bound")
        if len(pairs) == pair_limit and len(pairs) < count:
            saturated.append("pair-limit")
        return result
