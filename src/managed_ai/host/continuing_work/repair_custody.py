"""Retain a failure, qualify its generation inputs, and accept its received repair."""

from pathlib import Path
from typing import Any

from managed_ai.host.continuing_work.occurrence_files import ident, nonempty, now, read, write


class RepairCustody:
    """Composed collaborator owning failure custody, repair-generation checks, and acceptance."""

    def __init__(self, host: Any) -> None:
        self._host = host

    def generation_repairs(
        self,
        repair_ids: list[str],
        sources: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Verify repair generation selects a changed producing means and original failure
        inputs.
        """
        producer = [row for row in sources if row["role"] == "producer"]
        repairs: list[dict[str, Any]] = []

        for rid in repair_ids:
            repair = read(self._host.root / "repairs" / f"{ident(rid)}.json")
            old = {row["id"]: row for row in repair["producer_before"]}
            changed_means = [
                row
                for row in producer
                if row["id"] in old and row["sha256"] != old[row["id"]]["sha256"]
            ]
            if not changed_means:
                raise ValueError(
                    "Repair generation requires a selected changed producing means, "
                    "not only a changed output",
                )

            original_inputs = {row["sha256"] for row in repair["inputs"]}
            selected_inputs = {row["sha256"] for row in sources}
            if not original_inputs.issubset(selected_inputs):
                raise ValueError(
                    "Original-case repair generation must select the retained original inputs; "
                    "run transfers as separate scopes",
                )

            repairs.append(
                {
                    "id": rid,
                    "changed_producers": changed_means,
                    "original_inputs": [row["sha256"] for row in repair["inputs"]],
                },
            )

        return repairs

    def repair_open(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Retain an immutable failure occurrence with pinned inputs, failures, and producing
        means.
        """
        rid = ident(spec["id"])
        path = self._host.root / "repairs" / f"{rid}.json"
        if path.exists():
            raise ValueError("Preserve the original failure occurrence")

        row = {
            "id": rid,
            "opened_at_utc": now(),
            "failure_account": nonempty(spec["failure_account"], "failure account"),
            "causal_hypothesis": nonempty(spec["causal_hypothesis"], "causal hypothesis"),
            "inputs": [self._host.pin(item) for item in spec["inputs"]],
            "failures": [self._host.pin(item) for item in spec["failures"]],
            "producer_before": [
                dict(self._host.pin(entry["path"]), id=ident(entry["id"]))
                for entry in spec["producer_before"]
            ],
        }
        if not row["inputs"] or not row["failures"] or not row["producer_before"]:
            raise ValueError(
                "A repair needs retained inputs, observed failure and responsible producing means",
            )

        write(path, row)
        self._host.event("repair-opened", {"id": rid, "retained": self._host.pin(path)})
        return row

    def _validate_repair_lineage(
        self,
        rid: str,
        repair: dict[str, Any],
        gen: dict[str, Any],
        rec: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], set[str]]:
        if gen["id"] == rec["id"] or "result" not in gen or "result" not in rec:
            raise ValueError("Require distinct actually returned generation and receiving scopes")

        self._host.current(self._host.config["commission"], "Current commission")
        if self._host.goal()["sha256"] != rec["parent_goal"]["sha256"]:
            raise ValueError(
                "Parent duty changed after receiving; requalify this repair under current work"
            )

        self._host.received_outputs(gen)
        receiving_outputs = self._host.received_outputs(rec)
        if gen["result"]["producer"] == rec["result"]["producer"]:
            raise ValueError(
                "This acceptance route requires a separately attributed actual receiver"
            )
        if rid not in [row["id"] for row in gen["repairs"]]:
            raise ValueError("Generation did not select this changed producer")
        if rec["prepared_at_utc"] <= gen["result"]["at_utc"]:
            raise ValueError("Receiver must encounter the actual returned product")

        generated = {pin["sha256"] for pin in gen["result"]["outputs"]}
        seen = {pin["sha256"] for pin in rec["sources"]}
        if not generated.issubset(seen):
            raise ValueError("Receiver did not select all actual generated outputs")

        failed = {pin["sha256"] for pin in repair["failures"]}
        if not failed.issubset(seen):
            raise ValueError("Receiver must encounter retained failure outputs")

        causal_sources = {pin["sha256"] for pin in [*repair["inputs"], *repair["producer_before"]]}
        changed = next(row for row in gen["repairs"] if row["id"] == rid)["changed_producers"]
        causal_sources.update(pin["sha256"] for pin in changed)
        if not causal_sources.issubset(seen):
            raise ValueError(
                "Receiver must encounter original inputs and actual old/new producing means"
            )

        return receiving_outputs, generated

    @staticmethod
    def _validate_receiver_assessment(
        assessment: dict[str, Any],
        gen: dict[str, Any],
        generated: set[str],
    ) -> None:
        if assessment.get("finding") != "fixed-within-stated-scope":
            raise ValueError("Receiver has not accepted this repair")

        for key in ("causal_account", "comparison", "transfer_or_rival"):
            nonempty(assessment.get(key), key)

        limits = assessment.get("limits")
        if isinstance(limits, list):
            if not limits:
                raise ValueError("limits must contain at least one substantive limit")
            for item in limits:
                nonempty(item, "limit")
        else:
            nonempty(limits, "limits")

        named = assessment.get("generated_sha256")
        if isinstance(named, dict):
            expected = {Path(pin["path"]).name: pin["sha256"] for pin in gen["result"]["outputs"]}
            if named != expected:
                raise ValueError("Assessment names different output names or bytes")
        elif (
            not isinstance(named, list)
            or len(named) != len(generated)
            or any(not isinstance(item, str) for item in named)
            or set(named) != generated
        ):
            raise ValueError("Assessment names different output bytes")

    def repair_accept(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Verify lineage and separately attributed receiver assessment before accepting a
        repair.
        """
        rid = ident(spec["id"])
        repair_path = self._host.root / "repairs" / f"{rid}.json"
        repair = read(repair_path)
        if "acceptance" in repair:
            raise ValueError(
                "Repair acceptance already recorded; new evidence requires a new occurrence"
            )

        gen = read(self._host.root / "scopes" / f"{ident(spec['generation'])}.json")
        rec = read(self._host.root / "scopes" / f"{ident(spec['receiving'])}.json")
        receiving_outputs, generated = self._validate_repair_lineage(rid, repair, gen, rec)

        assessment_target = "outputs/" + rec["id"] + "/" + ident(spec["assessment"])
        if assessment_target not in rec["targets"]:
            raise ValueError("Assessment must be an actual returned receiving target")

        expected_path = str((self._host.work / assessment_target).resolve())
        received_pin = next(
            (pin for pin in receiving_outputs if pin["path"] == expected_path), None
        )
        if received_pin is None:
            raise ValueError("Assessment is not one of the received output occurrences")

        assessment = read(self._host.root / received_pin["object"])
        self._validate_receiver_assessment(assessment, gen, generated)

        acceptance = {
            "at_utc": now(),
            "generation": gen["id"],
            "receiving": rec["id"],
            "assessment": received_pin,
            "account": assessment,
            "scope": (
                "Mechanical lineage plus attributed semantic receiving; not proof of universal "
                "efficacy."
            ),
        }
        repair["acceptance"] = acceptance
        write(repair_path, repair)
        self._host.event(
            "repair-accepted", {"id": rid, "generation": gen["id"], "receiving": rec["id"]}
        )
        return acceptance
