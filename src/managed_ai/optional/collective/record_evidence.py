"""Record a scoped interpretation and source selection in the existing G60 collective."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any

HERE = Path(__file__).resolve().parent
LIMIT = 64 * 1024


def observe(path: Path, start: int, end: int | None) -> tuple[int, str, bytes]:
    """Hash and select a byte interval from one single file stream."""
    hasher = hashlib.sha256()
    fragment = bytearray()
    length = 0

    with path.open("rb") as stream:
        if end is None:
            data = stream.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise ValueError("Select the relevant byte interval [start,end] for this large source.")
            if not data:
                raise ValueError("Choose a nonempty source or selection.")
            return len(data), hashlib.sha256(data).hexdigest(), data

        while block := stream.read(1024 * 1024):
            lo = max(start, length)
            hi = min(end, length + len(block))
            if hi > lo:
                fragment.extend(block[lo - length : hi - length])
            hasher.update(block)
            length += len(block)

    return length, hasher.hexdigest(), bytes(fragment)


def _loads_strict(raw: bytes | bytearray | str) -> Any:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("Duplicate JSON key: " + key)
            out[key] = value
        return out

    def reject(val: str) -> Any:
        raise ValueError("Nonfinite JSON constant: " + val)

    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=reject)


def native(db: Path | str, *args: Any) -> Any:
    result = subprocess.run(
        [sys.executable, "-B", str(HERE / "manage_collective.py"), "--db", str(db), *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        raise ValueError((result.stdout or result.stderr).strip())
    return _loads_strict(result.stdout)


def selection(saved: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    body = saved["body"]
    if not isinstance(body, dict) or body.get("format") != "g60-owned-observation/1":
        raise ValueError("Choose a retained G60 evidence observation.")

    witness = body.get("witness")
    if not isinstance(witness, dict) or not isinstance(witness.get("base64"), str):
        raise ValueError("The observation has no encoded selection.")

    start, end, size = (witness.get(key) for key in ("start", "end", "source_bytes"))
    if not all(type(n) is int for n in (start, end, size)) or not 0 <= start < end <= size or end - start > LIMIT:
        raise ValueError("The retained selection has invalid bounds.")

    data = base64.b64decode(witness["base64"], validate=True)
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != end - start or digest != witness.get("selected_sha256"):
        raise ValueError("The retained bytes differ from the recorded selection.")
    if start == 0 and end == size and digest != witness.get("source_sha256"):
        raise ValueError("The complete selection differs from the recorded source edition.")

    return witness, data


def record(
    db: Path | str,
    source: Path,
    spec: Any,
    actor: str,
    ident: str,
    command: str,
) -> Any:
    text_fields = {"source_locator", "use", "observation"}
    optional = {"prediction", "limits", "reopen", "related", "selection"}
    if not isinstance(spec, dict) or not text_fields <= set(spec) or set(spec) - text_fields - optional:
        raise ValueError(
            "Supply source_locator, use and observation. Optional fields are prediction, "
            "limits, reopen, related and selection.",
        )

    if any(not isinstance(spec[k], str) or not spec[k].strip() for k in set(spec) - {"selection", "related"}):
        raise ValueError("Evidence accounts need nonempty text")

    if "related" in spec and (
        not isinstance(spec["related"], list)
        or not all(isinstance(r, str) and r.strip() for r in spec["related"])
    ):
        raise ValueError("related must be a list of possible relationships in the writer’s own words")

    bounds = spec.get("selection")
    if bounds is None:
        start, end = 0, None
    else:
        if not isinstance(bounds, list) or len(bounds) != 2 or any(type(n) is not int for n in bounds):
            raise ValueError("selection must be a pair of byte offsets")
        start, end = bounds
        if not 0 <= start < end or end - start > LIMIT:
            raise ValueError("Choose a nonempty interval of at most 64 KiB")

    length, edition, part = observe(source, start, end)
    if end is None:
        end = length
    elif len(part) != end - start:
        raise ValueError("Selection exceeds the observed source")

    account = {k: v for k, v in spec.items() if k != "selection"}
    witness = {
        "source_bytes": length,
        "source_sha256": edition,
        "start": start,
        "end": end,
        "selected_sha256": hashlib.sha256(part).hexdigest(),
        "base64": base64.b64encode(part).decode("ascii"),
    }
    with TemporaryDirectory() as temp:
        request_path = Path(temp) / "request.json"
        payload = {
            "actor": actor,
            "id": ident,
            "command": command,
            "op": "create",
            "body": {"format": "g60-owned-observation/1", "account": account, "witness": witness},
        }
        request_path.write_text(
            json.dumps(payload, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        return native(db, "call", "--request", request_path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--db", type=Path, required=True)
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("init", allow_abbrev=False)

    rec = sub.add_parser("record", allow_abbrev=False)
    rec.add_argument("id")
    rec.add_argument("--actor", required=True)
    rec.add_argument("--command", required=True)
    rec.add_argument("--source", type=Path, required=True)
    rec.add_argument("--spec", type=Path, required=True)

    for op in ("show", "check"):
        cmd = sub.add_parser(op, allow_abbrev=False)
        cmd.add_argument("id")
        cmd.add_argument(
            "--revision",
            type=int,
            help="Read this retained revision. Defaults to the current head.",
        )
        if op == "show":
            cmd.add_argument(
                "--selection",
                action="store_true",
                help="Write the exact retained selected bytes to stdout.",
            )
        if op == "check":
            cmd.add_argument("--source", type=Path, required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.verb == "record":
            result = record(
                args.db,
                args.source,
                _loads_strict(args.spec.read_bytes()),
                args.actor,
                args.id,
                args.command,
            )
        elif args.verb == "init":
            result = native(args.db, "init")
        else:
            revision = ["--revision", args.revision] if args.revision is not None else []
            saved = native(args.db, "show", args.id, *revision)
            if args.verb == "show":
                if args.selection:
                    sys.stdout.buffer.write(selection(saved)[1])
                    return 0
                result = saved
            else:
                witness, _ = selection(saved)
                length, edition, part = observe(args.source, witness["start"], witness["end"])
                result = {
                    "source_edition_matches": edition == witness["source_sha256"],
                    "selection_at_old_position_matches": (
                        hashlib.sha256(part).hexdigest() == witness["selected_sha256"]
                    ),
                    "current_bytes": length,
                    "scope": (
                        "Physical checks at the old byte interval. Revisit the receiving "
                        "interpretation if its evidence, surroundings or purpose have changed."
                    ),
                }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
