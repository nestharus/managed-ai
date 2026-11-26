#!/usr/bin/env python3
"""Commission an open inquiry and return a UTF-8 observation through an installed G60 system."""

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    _src_root = str(Path(__file__).resolve().parents[3])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai.platform import strict_json_loads


def need(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def fields(value: Any, required: Iterable[str], optional: Iterable[str] = ()) -> None:
    req_set = set(required)
    need(
        isinstance(value, dict) and req_set <= value.keys() <= req_set | set(optional),
        "Missing or unknown fields; consult the command examples.",
    )


def texts(value: dict[str, Any], keys: Iterable[str]) -> None:
    for key in keys:
        need(isinstance(value[key], str) and value[key].strip(), f"{key} requires text")


def pin(record: dict[str, Any]) -> dict[str, Any]:
    return {k: record[k] for k in ("id", "revision", "sha256")}


class NativeError(ValueError):
    pass


def _resolve_store_entry(product: Path) -> Path:
    for candidate in (
        product / "store" / "manage_store.py",
        product / "manage_store.py",
        product / "system.py",
    ):
        if candidate.is_file():
            return candidate
    return product / "store" / "manage_store.py"


def _strict_json_loads(raw: bytes | str, label: str = "JSON") -> Any:
    return strict_json_loads(raw, label)


def _load_json_object(path: Path) -> dict[str, Any]:
    return strict_json_loads(path.read_bytes(), str(path), require_object=True)


def _error_code(exc: NativeError) -> str | None:
    try:
        parsed = _strict_json_loads(str(exc), "native error")
    except ValueError, TypeError:
        return None
    return parsed.get("code") if isinstance(parsed, dict) else None


class Contact:
    def __init__(self, product: Path | str, state: Path | str, scratch: Path | str) -> None:
        self.system = _resolve_store_entry(Path(product))
        self.state = state
        self.spec = Path(scratch) / "input.json"

    def run(self, *args: Any, payload: Any = None) -> Any:
        if payload is not None:
            self.spec.write_text(
                json.dumps(payload, ensure_ascii=False, allow_nan=False),
                encoding="utf-8",
            )
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-B", str(self.system), "--root", str(self.state), *map(str, args)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if proc.returncode:
            raise NativeError(proc.stderr.strip())
        return _strict_json_loads(proc.stdout, "native stdout")

    def open(self, order: dict[str, Any]) -> Any:
        return self.run("inquiry", "open", "--spec", self.spec, payload=order)

    def current(self, ticket: dict[str, Any]) -> dict[str, Any]:
        fields(ticket, ("id", "revision", "sha256"))
        need(type(ticket["revision"]) is int and ticket["revision"] == 1, "Expected opening ticket")
        view: dict[str, Any] = self.run("feedback", "inspect", ticket["id"], "--revision", "1")
        need(pin(view["record"]) == ticket, "Ticket belongs to a different opening")
        need(
            view["route"]["state"] != "stale",
            "Bindings changed; inspect and reopen for the new use",
        )
        route = view["route"]
        need(
            route["revision"] == 1
            or (route["revision"] == 2 and route["state"] in ("work", "hold")),
            "Scope advanced beyond its original return opportunity; inspect the current scope",
        )
        return view

    def attach(
        self,
        ticket: dict[str, Any],
        response: dict[str, Any],
        raw: str,
    ) -> dict[str, Any]:
        keys = ("provenance", "observed", "uncertainty", "interpretation", "next", "route")
        fields(response, keys, ("basis", "initial_source_return", "intended_return"))
        texts(response, keys)
        need(
            response["route"] in ("work", "hold"), "Choose work or hold for the receiving decision"
        )
        need(raw.strip(), "An actual observer return is required")
        view = self.current(ticket)
        plan = view["record"]["content"]
        witnesses: list[dict[str, Any]] = []
        for field, role, ref in [
            ("initial_source_return", "outside", plan["outside"]),
            ("intended_return", "intended", plan["references"][0]),
        ]:
            if field in response:
                texts(response, (field,))
                need(ref is not None, "No initial outside source was bound at opening")
                witnesses.append({"role": role, "reference": pin(ref), "observed": response[field]})
        subject = pin(plan["candidate"])
        content = {
            "subject": subject,
            "observed": response["observed"],
            "exercised": witnesses,
            "ticket": ticket,
            "raw_return": raw,
            "receiving_account": {k: v for k, v in response.items() if k != "basis"},
        }
        basis = response.get("basis", [])
        need(
            isinstance(basis, list) and all(isinstance(x, str) for x in basis),
            "basis requires paths",
        )
        ident = ticket["id"]
        evidence = self.run(
            "inquiry",
            "evidence",
            "--spec",
            self.spec,
            payload={"id": ident + "-returned", "content": content, "basis": basis},
        )
        claim = {
            "id": ident,
            "expected": 1,
            "subject": subject,
            "observation": evidence,
            "witnesses": [
                {"role": w["role"], "reference": w["reference"]["id"], "account": w["observed"]}
                for w in witnesses
            ],
            **{k: response[k] for k in ("interpretation", "next", "route")},
        }
        try:
            result = self.run("feedback", "observe", "--spec", self.spec, payload=claim)
        except NativeError as exc:
            if _error_code(exc) != "feedback_revision_conflict":
                raise
            result = self.run("feedback", "inspect", ident)
            c = result["record"]["content"]
            need(
                result["record"]["revision"] == 2
                and result["route"]["state"] != "stale"
                and len(c["returns"]) == 1
                and pin(c["returns"][0]["observation"]) == evidence
                and all(
                    c["returns"][0][k] == claim[k]
                    for k in ("interpretation", "next", "route", "witnesses")
                ),
                "Scope has moved; preserve this late return separately and "
                "inspect the current scope",
            )
        return {"ticket": ticket, "evidence": evidence, "route": result["route"]}

    def context(self, ident: str, turn: str) -> Any:
        view = self.run("feedback", "inspect", ident)
        route = view["route"]
        need(route["state"] != "stale", "Bindings changed; inspect before requesting a new context")
        record = view["record"]
        if record["revision"] == 1:
            candidate = self.run("show", record["content"]["candidate"]["id"])
            task = candidate["content"]["question"]
        else:
            task = route["next"]
        packet = self.run("context", turn, "--task", task, "--feedback", ident)
        captured = self.run("view", turn).get("feedback_uses", [])
        need(
            any(row == route for row in captured),
            "Scope changed during context preparation; use a new turn identity",
        )
        return packet


_COMMAND_HANDLERS: dict[str, Callable[[Contact, argparse.Namespace], Any]] = {
    "init": lambda contact, args: contact.run("init", "--goal-text", args.goal),
    "open": lambda contact, args: contact.open(_load_json_object(args.order)),
    "return": lambda contact, args: contact.attach(
        _load_json_object(args.ticket)["ticket"],
        _load_json_object(args.account),
        args.raw.read_bytes().decode("utf-8"),
    ),
    "context": lambda contact, args: contact.context(args.scope, args.turn),
    "stages": lambda contact, args: contact.run("inquiry", "stages", args.scope),
    "status": lambda contact, args: contact.run("feedback", "inspect", args.scope),
}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("product", type=Path, help="extracted G60-system-use directory")
    p.add_argument("state", type=Path, help="existing native state, or empty destination for init")
    cmds = p.add_subparsers(dest="command", required=True)
    cmds.add_parser("init", allow_abbrev=False).add_argument("goal")
    cmds.add_parser("open", allow_abbrev=False).add_argument("order", type=Path)
    att = cmds.add_parser("return", allow_abbrev=False)
    att.add_argument("ticket", type=Path, help="JSON stdout of open")
    att.add_argument("account", type=Path)
    att.add_argument("raw", type=Path)
    ctx = cmds.add_parser("context", allow_abbrev=False)
    ctx.add_argument("scope")
    ctx.add_argument("turn")
    cmds.add_parser("status", allow_abbrev=False).add_argument("scope")
    cmds.add_parser("stages", allow_abbrev=False).add_argument("scope")
    a = p.parse_args()

    handler = _COMMAND_HANDLERS.get(a.command)
    if handler is None:
        raise ValueError(f"Unsupported command: {a.command}")
    with TemporaryDirectory() as scratch:
        result = handler(Contact(a.product, a.state, scratch), a)
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        sys.exit(str(exc))
