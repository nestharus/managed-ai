#!/usr/bin/env python3
"""Narrow integration checks at the real native receiving/adoption boundary."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest

if __package__ in (None, ""):
    _src_root = str(Path(__file__).resolve().parents[3])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai.companions.representation.representation import (
    OPERATION,
    RepresentationOperations,
    encoded,
    loads_strict,
    mount_engine,
)

_SELECTED_SYSTEM_CLS: Any = None


def _system_cls() -> Any:
    if _SELECTED_SYSTEM_CLS is not None:
        return _SELECTED_SYSTEM_CLS
    from managed_ai.store.system import System

    return System


class NativeRepresentationChecks(unittest.TestCase):
    """Integration checks verifying bound representation prepare/receive/adopt invariants."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "runtime"
        system_cls = _system_cls()
        system_cls.initialize(self.root, {"contribution": "Exercise selected representation binding."})
        self.system = system_cls(self.root)
        self.adapter = RepresentationOperations(self.system)
        self.work = self.system.work
        self.source = self.work / "sources/rule.txt"
        self.source.write_text(
            "The nominated primary value can increase by one; the independent label is unaffected.\n",
            encoding="utf-8",
        )
        self.seed("seed-a", "outputs/a.json", {"value": 1})
        self.seed("seed-b", "outputs/b.json", {"label": "old"})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def candidate(self, name: str, value: Any, json_body: bool = True) -> Path:
        path = self.base / name
        path.write_bytes(encoded(value) if json_body else value.encode("utf-8"))
        return path

    def seed(self, turn: str, target: str, value: Any) -> None:
        self.system.context(turn, {"task": "Accept the exact supplied test state.", "targets": [target]})
        reply = self.candidate(turn + ".reply", "Actual finite fixture acceptance.", False)
        candidate = self.candidate(turn + ".json", value)
        self.system.receive(
            turn,
            reply,
            "actual integration fixture",
            [target + "=" + str(candidate)],
        )
        self.system.adopt(turn, target, "Accept this scoped state for the finite check.")

    def spec(self, turn: str, mode: str = "transform") -> dict[str, Any]:
        return {
            "format": OPERATION,
            "turn": turn,
            "mode": mode,
            "task": "Change only the nominated value, then bind the actual return.",
            "consumer": "actual finite operation participant",
            "representations": [self.adapter.pin("outputs/a.json", "seed-a")],
            "sources": [self.adapter.pin("sources/rule.txt")],
            "applications": [
                {
                    "id": "selected-use",
                    "sources": ["sources/rule.txt"],
                    "representations": ["outputs/a.json"],
                    "operation": "Use the selected value as the starting operand.",
                    "prediction": "The candidate value is two; the separately held label remains unchanged.",
                },
            ],
            "targets": ["outputs/result.txt" if mode == "realize" else "outputs/a.json"],
            "interactions": {
                "account": "The finite fixture stipulates the separate label is not read or written.",
                "unknowns": ["No general semantic independence claim is made."],
            },
        }

    def witness(self, turn: str) -> dict[str, Any]:
        witness = self.adapter.template(turn)
        target = "outputs/result.txt" if "realization" in witness else "outputs/a.json"
        witness["applications"][0].update(
            account="The exact selected input supplied the operand.",
            outputs=[target],
        )
        if "realization" in witness:
            witness["realization"]["account"] = "The output displays the accepted value without altering it."
        return witness

    def receive(self, turn: str, witness: dict[str, Any] | None = None, realized: bool = False) -> dict[str, Any]:
        reply = self.candidate(turn + ".reply", "Actual candidate returned for the fixture.", False)
        target = "outputs/result.txt" if realized else "outputs/a.json"
        candidate = self.candidate(
            turn + ".candidate",
            "Value: 1\n" if realized else {"value": 2},
            not realized,
        )
        return self.adapter.receive(
            turn,
            reply,
            witness or self.witness(turn),
            "actual integration fixture",
            [target + "=" + str(candidate)],
        )

    def test_selected_accepted_state_and_application_are_actual_context_inputs(self) -> None:
        self.adapter.prepare(self.spec("actual-input"))
        _, request = self.system.ex.load("actual-input")
        paths = {row["path"] for row in request["materials"]}
        self.assertIn("outputs/a.json", paths)
        self.assertIn("sources/rule.txt", paths)
        self.assertIn("representation-operations/actual-input/operation.json", paths)
        self.assertNotIn("outputs/b.json", paths)
        self.receive("actual-input")
        self.adapter.adopt("actual-input", "outputs/a.json", "Accept the actual scoped increment.")
        self.assertEqual(loads_strict((self.work / "outputs/a.json").read_bytes()), {"value": 2})

    def test_stale_state_return_is_retained_but_native_adoption_refuses(self) -> None:
        self.adapter.prepare(self.spec("stale-state"))
        self.seed("changed-a", "outputs/a.json", {"value": 9})
        self.receive("stale-state")
        with self.assertRaisesRegex(ValueError, "declared basis changed"):
            self.adapter.adopt("stale-state", "outputs/a.json", "This stale candidate must fail.")
        self.assertIsNotNone(self.system.ex.inspect("stale-state")["return"])
        self.assertEqual(loads_strict((self.work / "outputs/a.json").read_bytes()), {"value": 9})

    def test_unrelated_accepted_change_does_not_invalidate_selected_scope(self) -> None:
        self.adapter.prepare(self.spec("scoped"))
        self.seed("changed-b", "outputs/b.json", {"label": "new"})
        self.receive("scoped")
        self.adapter.adopt(
            "scoped",
            "outputs/a.json",
            "The actual independent label change leaves this selected basis current.",
        )
        self.assertEqual(loads_strict((self.work / "outputs/a.json").read_bytes()), {"value": 2})
        self.assertEqual(loads_strict((self.work / "outputs/b.json").read_bytes()), {"label": "new"})

    def test_missing_application_or_inconsistent_basis_is_refused(self) -> None:
        bad = self.spec("bad-base")
        bad["representations"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "input basis changed"):
            self.adapter.prepare(bad)

        self.adapter.prepare(self.spec("missing-app"))
        witness = self.witness("missing-app")
        witness["applications"] = []
        with self.assertRaisesRegex(ValueError, "source-application witness"):
            self.receive("missing-app", witness)

        inconsistent = self.witness("missing-app")
        inconsistent["representations"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "representation basis is inconsistent"):
            self.receive("missing-app", inconsistent)
        self.assertIsNone(self.system.ex.inspect("missing-app")["return"])

    def test_missing_realization_binding_is_refused_and_complete_binding_is_retained(self) -> None:
        missing = self.spec("no-input", mode="realize")
        missing["representations"] = []
        with self.assertRaisesRegex(ValueError, "requires an accepted representation binding"):
            self.adapter.prepare(missing)

        self.adapter.prepare(self.spec("realize", mode="realize"))
        witness = self.witness("realize")
        del witness["realization"]
        with self.assertRaisesRegex(ValueError, "realization lacks"):
            self.receive("realize", witness, realized=True)

        self.receive("realize", realized=True)
        self.adapter.adopt("realize", "outputs/result.txt", "Accept the displayed accepted state.")
        self.assertEqual((self.work / "outputs/result.txt").read_text(encoding="utf-8"), "Value: 1\n")

    def test_plain_native_return_cannot_claim_adapter_binding(self) -> None:
        self.adapter.prepare(self.spec("unbound"))
        reply = self.candidate(
            "plain.reply",
            "A returned proposal alone has no representation application witness.",
            False,
        )
        candidate = self.candidate("plain.json", {"value": 2})
        self.system.receive(
            "unbound",
            reply,
            "actual integration fixture",
            ["outputs/a.json=" + str(candidate)],
        )
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            self.adapter.adopt("unbound", "outputs/a.json", "This unbound return must fail.")


def main(argv: list[str] | None = None) -> int:
    """Parse --engine safely at CLI execution time (never on module import) and run checks."""
    global _SELECTED_SYSTEM_CLS
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--engine",
        required=True,
        type=Path,
        metavar="ENGINE",
        help="canonical src/managed_ai package directory containing store/ and core/",
    )
    args, rest = parser.parse_known_args(argv)
    current_module = sys.modules[__name__]

    try:
        with mount_engine(args.engine) as (system_cls, _, _, _, _):
            _SELECTED_SYSTEM_CLS = system_cls
            try:
                program = unittest.main(
                    module=current_module,
                    argv=[sys.argv[0], *rest],
                    verbosity=2,
                    exit=False,
                )
            finally:
                _SELECTED_SYSTEM_CLS = None
    except (OSError, ValueError) as exc:
        print(f"check_representation: {exc}", file=sys.stderr)
        return 2

    return 0 if program.result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
