"""Owner-authored prospective uses and returned observations on native records.

The enclosing system holds its ordinary owner lock. Each mutation is one record
replacement. The exchange retains superseded revisions and the bodies of pins.
"""

from collections.abc import Callable
from typing import Any, ClassVar

from managed_ai_information.core.ownership import packet_id
from managed_ai_information.core.storage import encode, now, sha
from managed_ai_information.store.protocol.identities import (
    exact_fields,
    identity,
    nonempty,
    record_identity,
    require,
    strict_json,
)

KIND = "feedback-use"
PLAN = {"candidate", "forecast", "rival", "outside", "references"}


class FeedbackConflict(ValueError):
    details: ClassVar[dict[str, str]] = {"code": "feedback_revision_conflict"}


class Feedback:
    def __init__(self, system: Any) -> None:
        self.records = system.record_store

    def check_source(self, record: dict[str, Any]) -> None:
        require(record["kind"] != KIND, "a feedback account cannot witness its own process")
        require(
            not self.records.basis_changes(record) and all(p["sha256"] for p in record["basis"]),
            "source record basis is missing or changed: " + record["id"],
        )

    def pin(self, wanted: Any) -> dict[str, Any]:
        record = self.records.head(wanted)
        self.check_source(record)
        return self.records.pin(record)

    @staticmethod
    def roles(content: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        roles = {("intended", r["id"]): r for r in content["references"]}
        if content["outside"]:
            roles["outside", content["outside"]["id"]] = content["outside"]
        return roles

    @staticmethod
    def bindings(content: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            content["candidate"],
            *Feedback.roles(content).values(),
            *(r["observation"] for r in content["returns"]),
        ]

    def changes(
        self,
        content: dict[str, Any],
        *,
        lookup: Callable[[str], dict[str, Any]] | None = None,
    ) -> list[str]:
        lookup_fn = self.records.show if lookup is None else lookup
        changed: list[str] = []

        for ident, pin in {p["id"]: p for p in self.bindings(content)}.items():
            try:
                record = lookup_fn(ident)
                if record_identity(record) == record_identity(
                    pin
                ) and not self.records.basis_changes(record):
                    continue
            except OSError, ValueError:
                pass
            changed.append(ident)

        return changed

    @staticmethod
    def outstanding(content: dict[str, Any]) -> dict[int, dict[str, Any]]:
        pending = {r["return_number"]: r for r in content.get("carried_returns", [])}
        first = content.get("return_offset", 0) + 1

        for number, row in enumerate(content["returns"], first):
            if row["route"] in ("work", "hold"):
                pending[number] = {
                    "return_number": number,
                    "route": row["route"],
                    "observation": row["observation"],
                    "interpretation": row["interpretation"],
                    "next": row["next"],
                }
            for resolution in row.get("resolutions", []):
                pending.pop(resolution["return_number"], None)

        return pending

    def route(
        self,
        record: dict[str, Any],
        *,
        lookup: Callable[[str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        content = record["content"]
        stale = self.changes(content, lookup=lookup)
        state = "stale" if stale else content["state"]
        instruction = (
            "Revisit changed bindings (" + ", ".join(stale) + ") and revise the forecast."
            if stale
            else content["next"]
        )
        pending = self.outstanding(content)
        return {
            "id": record["id"],
            "revision": record["revision"],
            "scope": content["scope"],
            "state": state,
            "next": instruction,
            "stale": stale,
            "unresolved_returns": sorted(pending),
            "has_unresolved_hold": bool(pending) and self.progress_state(record, pending) == "hold",
            "channel": "outside-bound" if content["outside"] else "rehearsal",
        }

    def previous(self, record: dict[str, Any]) -> dict[str, Any]:
        require(
            record.get("previous_object"),
            "feedback revision is outside retained history; "
            "current-use storage keeps selected evidence, not every prior account",
        )
        previous = strict_json(self.records.objects.read(record["previous_object"]))
        require(
            previous["id"] == record["id"]
            and previous["revision"] == record["revision"] - 1
            and previous["sha256"]
            == sha(encode({k: v for k, v in previous.items() if k != "sha256"})),
            "broken feedback revision history",
        )
        return previous

    def resolved_carried_routes(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """Recover only missing legacy duty dispositions before ending ancestry."""
        carried = [dict(row) for row in record["content"].get("carried_returns", [])]
        missing = {row["return_number"] for row in carried if "route" not in row}
        routes: dict[int, str] = {}
        current = record

        while missing:
            current = self.previous(current)
            content = current["content"]
            for number, row in enumerate(content["returns"], content.get("return_offset", 0) + 1):
                if number in missing:
                    require(
                        row["route"] in {"work", "hold"}, "unexpected legacy carried disposition"
                    )
                    routes[number] = row["route"]
                    missing.remove(number)

        for row in carried:
            if "route" not in row:
                row["route"] = routes[row["return_number"]]
        return carried

    def inspect(self, ident: str, revision: int | None = None) -> dict[str, Any]:
        head = self.records.show(ident)
        require(head["kind"] == KIND, "record does not own a feedback use")
        record = head

        if revision is not None:
            require(1 <= revision <= head["revision"], "unknown feedback revision")
            while record["revision"] > revision:
                record = self.previous(record)

        return {"record": record, "route": self.route(head)}

    @staticmethod
    def opening_id(ident: str) -> None:
        require(ident not in {"goal", "field", "frontier"}, "choose a distinct feedback identity")

    @staticmethod
    def predictions(spec: dict[str, Any]) -> tuple[str, str]:
        forecast = nonempty(spec["forecast"], "prospective forecast")
        rival = nonempty(spec["rival"], "rival expected return")
        require(
            forecast.strip().casefold() != rival.strip().casefold(),
            "rival must differ from forecast",
        )
        return forecast, rival

    def plan(self, spec: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
        forecast, rival = self.predictions(spec)
        require(
            isinstance(spec["references"], list), "references must be an array of intended sources"
        )

        candidate = self.pin(spec["candidate"])
        outside = self.pin(spec["outside"]) if spec["outside"] is not None else None
        refs = [self.pin(row) for row in spec["references"]]
        roles = self.roles({"outside": outside, "references": refs})

        require(
            len({r["id"] for r in refs}) == len(refs)
            and all(ident != candidate["id"] for _, ident in roles),
            "duplicate intended source or candidate used as its own reference",
        )

        if previous:
            removed = self.roles(previous).keys() - roles.keys()
            releases = spec.get("releases", [])
            require(isinstance(releases, list), "releases must be an array")
            supplied: set[tuple[str, str]] = set()
            for release in releases:
                exact_fields(
                    release, {"role", "reference", "owner", "account"}, set(), "reference release"
                )
                key = (release["role"], release["reference"])
                require(key in removed - supplied, "unknown or duplicate reference release")
                nonempty(release["owner"], "releasing owner")
                nonempty(release["account"], "reason for releasing this reference")
                supplied.add(key)
            require(
                supplied == removed, "retain separately owed references or explicitly release them"
            )

        return {
            "scope": previous["scope"] if previous else nonempty(spec["scope"], "use scope"),
            "candidate": candidate,
            "forecast": forecast,
            "rival": rival,
            "outside": outside,
            "references": refs,
            "forecast_at": now(),
            "returns": [],
            "state": "open",
            "next": (
                "Exercise the candidate and record its actual return against "
                "the forecast and rival."
            ),
        }

    def progress_state(
        self,
        record: dict[str, Any],
        pending: dict[int, dict[str, Any]],
    ) -> str:
        if any(row.get("route") == "hold" for row in pending.values()):
            return "hold"

        unknown = {number for number, row in pending.items() if "route" not in row}
        while unknown:
            require(
                record.get("previous_object"), "missing original disposition for a carried return"
            )
            record = self.previous(record)
            content = record["content"]
            for number, row in enumerate(content["returns"], content.get("return_offset", 0) + 1):
                if number in unknown:
                    require(row["route"] in ("work", "hold"), "unexpected original disposition")
                    if row["route"] == "hold":
                        return "hold"
                    unknown.remove(number)

        return "work"

    def observe(self, record: dict[str, Any], spec: dict[str, Any]) -> None:
        content = record["content"]
        require(
            identity(spec["subject"]) == record_identity(content["candidate"]),
            "wrong observation subject",
        )

        observation = self.pin(spec["observation"])
        require(
            observation["id"]
            not in {content["candidate"]["id"], *(p["id"] for p in self.roles(content).values())},
            "the observation must identify a separate actual return",
        )

        source = self.records.show(observation["id"])
        body = source["content"]
        require(
            isinstance(body, dict) and body.get("subject") == spec["subject"],
            "actual return does not bind this candidate revision",
        )

        observed = body.get("observed")
        require(
            isinstance(observed, str) and bool(observed.strip()),
            "feedback evidence content.observed must be nonempty text describing the returned "
            "result. Keep structured output in content.details. Revise the evidence record and "
            "retry with its new pin. See docs/engine/store/README.md.",
        )
        require(
            source["updated_at"] >= content["forecast_at"],
            "observation predates the prospective forecast",
        )

        route = spec["route"]
        require(route in ("work", "hold", "progress", "review"), "unknown feedback route")
        interpretation = nonempty(spec["interpretation"], "interpretation of the actual return")
        instruction = nonempty(spec["next"], "next receiving action or condition")
        roles = self.roles(content)

        exercised = body.get("exercised", [])
        require(isinstance(exercised, list), "exercised references must be an array")
        exercised_roles: set[tuple[str, str]] = set()
        for row in exercised:
            exact_fields(row, {"role", "reference", "observed"}, set(), "exercised reference")
            pin = identity(row["reference"])
            key = (row["role"], pin["id"])
            require(
                key in roles and record_identity(roles[key]) == pin,
                "wrong exercised reference revision or role",
            )
            nonempty(row["observed"], "observed effect for this reference")
            exercised_roles.add(key)

        require(isinstance(spec["witnesses"], list), "witnesses must be an array")
        witnessed: set[tuple[str, str]] = set()
        accounts: list[dict[str, Any]] = []
        for row in spec["witnesses"]:
            exact_fields(row, {"reference", "account"}, {"role"}, "witness")
            ident = nonempty(row["reference"], "witness reference")
            possible = [
                key
                for key in roles
                if key[1] == ident and ("role" not in row or row["role"] == key[0])
            ]
            require(
                len(possible) == 1, "unknown witness or ambiguous role; name its role explicitly"
            )
            key = possible[0]
            require(
                key in exercised_roles - witnessed,
                "reference was not exercised in this return or is duplicated",
            )
            nonempty(row["account"], "reference witness account")
            witnessed.add(key)
            accounts.append(dict(row, role=key[0]))

        pending = self.outstanding(content)
        resolutions = spec.get("resolutions", [])
        require(isinstance(resolutions, list), "resolutions must be an array")
        for resolution in resolutions:
            exact_fields(resolution, {"return_number", "account"}, set(), "return resolution")
            number = resolution["return_number"]
            require(
                type(number) is int and number in pending, "unknown or duplicate adverse return"
            )
            nonempty(resolution["account"], "account for this adverse return")
            del pending[number]

        require(
            route != "review" or not pending,
            "unresolved adverse returns: " + ", ".join(map(str, sorted(pending))),
        )

        state = route
        if route == "progress":
            require(
                bool(resolutions) and bool(pending),
                "progress must address at least one return and leave another open",
            )
            state = self.progress_state(record, pending)

        content["returns"].append(
            {
                "resolutions": resolutions,
                "observation": observation,
                "interpretation": interpretation,
                "route": route,
                "next": instruction,
                "witnesses": accounts,
            }
        )
        content.update(state=state, next=instruction)

    def apply(self, action: str, spec: dict[str, Any]) -> dict[str, Any]:
        specific = {
            "open": PLAN | {"scope"},
            "revise": PLAN | {"reason"},
            "observe": {"subject", "observation", "interpretation", "route", "next", "witnesses"},
            "accept": {"owner", "account"},
        }
        optional = {"revise": {"releases"}, "observe": {"resolutions"}}
        exact_fields(
            spec,
            specific[action] | {"id", "expected"},
            optional.get(action, set()),
            "feedback " + action,
        )

        ident = packet_id(nonempty(spec["id"], "feedback record ID"))
        expected = spec["expected"]
        require(
            type(expected) is int and expected >= 0,
            "expected revision must be a nonnegative integer",
        )
        self.opening_id(ident)

        old = self.records.show(ident) if self.records.path(ident).exists() else None
        if expected != (old["revision"] if old else 0):
            raise FeedbackConflict("feedback revision conflict")

        require(
            (old is None) == (action == "open") and (not old or old["kind"] == KIND),
            "feedback action requires the corresponding existing or new feedback record",
        )

        if old and any("route" not in row for row in old["content"].get("carried_returns", [])):
            old = dict(
                old,
                content=dict(old["content"], carried_returns=self.resolved_carried_routes(old)),
            )

        content: dict[str, Any] | None = old["content"] if old else None
        if action in ("open", "revise"):
            content = self.plan(spec, content)
            if action == "revise":
                if old is None:
                    raise ValueError(
                        "feedback action requires the corresponding existing or new feedback record"
                    )
                previous: dict[str, Any] = old["content"]
                return_offset = previous.get("return_offset", 0)
                if type(return_offset) is not int:
                    raise ValueError("return_offset must be an integer")
                content["return_offset"] = return_offset + len(previous["returns"])
                content["carried_returns"] = list(self.outstanding(previous).values())
                content["revision_reason"] = nonempty(spec["reason"], "revision account")
                content["releases"] = spec.get("releases", [])
        else:
            if old is None or content is None:
                raise ValueError(
                    "feedback action requires the corresponding existing or new feedback record"
                )
            require(
                not self.changes(content), "stale bindings; inspect and revise before further use"
            )
            require(content["state"] != "accepted", "revise an accepted use before further work")
            if action == "observe":
                self.observe(old, spec)
            else:
                require(
                    content["state"] == "review",
                    "owner acceptance requires a returned review route",
                )
                require(not self.outstanding(content), "resolve adverse returns before acceptance")
                require(
                    content["outside"] is not None, "rehearsal cannot establish outside acceptance"
                )
                witnessed = {
                    (w["role"], w["reference"]) for r in content["returns"] for w in r["witnesses"]
                }
                missing = self.roles(content).keys() - witnessed
                require(
                    not missing,
                    "missing separate witness: " + ", ".join("/".join(k) for k in sorted(missing)),
                )
                content.update(
                    state="accepted",
                    next="Scoped use accepted.",
                    acceptance={
                        "owner": nonempty(spec["owner"], "accepting owner"),
                        "account": nonempty(spec["account"], "acceptance account"),
                    },
                )

        receipt = self.records.put_feedback(
            {"id": ident, "kind": KIND, "content": content}, expected
        )
        return {"record": receipt, "route": self.route(self.records.show(ident))}
