#!/usr/bin/env python3
"""Exercise a temporary runtime through the release root and an activated engine."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


def call(*args: object) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-B", *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"{args}: {result.stderr or result.stdout}")
    loaded: dict[str, Any] = json.loads(result.stdout)
    return loaded


def exercise(engine: Path | str, work: Path) -> dict[str, Any]:
    engine_path = Path(engine).resolve()
    state = work / "state"
    payload = work / "input.json"
    app: list[object] = [engine_path / "store/manage_store.py", "--root", state]
    call(*app, "init", "--goal-text", "Prepare a pickup instruction the night shift can use.")

    def spec(data: dict[str, Any]) -> Path:
        payload.write_text(
            json.dumps(data, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        return payload

    def put(
        ident: str,
        kind: str,
        content: Any,
        expected: int = 0,
        basis: list[str] | None = None,
    ) -> dict[str, Any]:
        record = {"id": ident, "kind": kind, "content": content, "basis": basis or []}
        row = call(*app, "put", "--record", spec(record), "--expected", expected)["record"]
        return {k: row[k] for k in ("id", "revision", "sha256")}

    ident = "pickup-trial"
    candidate = put("instruction", "candidate", "Use the west desk between 18:00 and 22:00.")
    outside = put("desk-contact", "source", "Night-shift pickup from the west desk.")
    intent = put(
        "shift-requirement",
        "requirement",
        "A night worker must be able to collect after 19:00.",
    )
    opening = {
        "id": ident,
        "expected": 0,
        "scope": "Night shift, west desk",
        "candidate": candidate,
        "forecast": "A night worker collects at the west desk after 19:00.",
        "rival": "The desk has closed and the worker needs another pickup point.",
        "outside": outside,
        "references": [intent],
    }
    call(*app, "feedback", "open", "--spec", spec(opening))
    exercised = [
        {
            "reference": outside,
            "role": "outside",
            "observed": "The west desk shutter was closed at 19:05.",
        },
        {
            "reference": intent,
            "role": "intended",
            "observed": "The night worker could not collect the package.",
        },
    ]
    meter = state / "work/sources/walk.txt"
    meter.write_text("Fixture walk at 19:05: shutter closed; pickup failed.\n", encoding="utf-8")
    reading = put(
        "night-walk",
        "evidence",
        {
            "subject": candidate,
            "observed": "The pickup attempt failed.",
            "exercised": exercised,
        },
        basis=["sources/walk.txt"],
    )
    next_work = "Revise the instruction to name a pickup point open after 19:00."
    observed = {
        "id": ident,
        "expected": 1,
        "subject": candidate,
        "observation": reading,
        "interpretation": "The posted desk hours fail for this shift.",
        "route": "work",
        "next": next_work,
        "witnesses": [
            {
                "reference": outside["id"],
                "role": "outside",
                "account": "Saw the closed shutter.",
            },
            {
                "reference": intent["id"],
                "role": "intended",
                "account": "A night worker could not collect.",
            },
        ],
    }
    call(*app, "feedback", "observe", "--spec", spec(observed))
    packet = call(*app, "context", "repair", "--task", "Prepare the next handoff.")
    assert next_work in Path(packet["context"]).read_text(encoding="utf-8")
    dep = work / "deployment"
    deploy: list[object] = [engine_path / "deployment/select_deployment.py", "--deployment", dep]
    initial = call(*deploy, "init", "--runtime", state, "--engine", engine_path)
    staged = call(*deploy, "stage", "current", "--engine", engine_path)
    active = call(
        *deploy,
        "activate",
        "current",
        "--expected",
        initial["descriptor_sha256"],
        "--manifest-sha256",
        staged["candidate_sha256"],
        "--reason",
        "Use the reviewed release for the pickup trial.",
    )
    direct = call(*app, "feedback", "inspect", ident)
    selected = call(*deploy, "system", "feedback", "inspect", ident)
    assert selected == direct and next_work in json.dumps(selected)
    captured = call(
        *deploy,
        "system",
        "context",
        "selected",
        "--task",
        "Repair the pickup instructions.",
        "--feedback",
        ident,
    )
    assert next_work in Path(captured["context"]).read_text(encoding="utf-8")
    return {
        "record": ident,
        "observed": reading["id"],
        "ordinary_next_instruction": next_work,
        "selected_engine": active["engine"],
        "deployment_inspect_matches_direct": True,
        "selected_deployment_context_carries_next": True,
    }


def main(argv: list[str] | None = None) -> int:
    default_engine = Path(__file__).resolve().parents[1] / "src" / "managed_ai_information"
    parser = argparse.ArgumentParser(
        description=(
            "Run the standalone native feedback example against a temporary store and "
            "activated shared deployment."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--engine",
        type=Path,
        default=default_engine,
        help="Path to the managed_ai_information package directory (default: src/managed_ai_information).",
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=None,
        help="Optional working directory (default: a fresh temporary directory).",
    )
    args = parser.parse_args(argv)
    if args.work is not None:
        args.work.mkdir(parents=True, exist_ok=True)
        print(json.dumps(exercise(args.engine, args.work), indent=2))
        return 0
    with TemporaryDirectory() as td:
        print(json.dumps(exercise(args.engine, Path(td)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
