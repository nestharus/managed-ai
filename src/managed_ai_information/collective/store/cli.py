"""Public command syntax for collective artifact work."""

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import PRODUCT, Collective
from .contract import CONTEXT_LIMIT, loads_strict
from .database import _require_ordinary_path


def _load_request_payload(path: Path) -> Any:
    """Validate an ordinary request file within CONTEXT_LIMIT and decode strict JSON."""
    _require_ordinary_path(path, "request file", must_exist=True)
    raw = path.read_bytes()
    if len(raw) > CONTEXT_LIMIT:
        raise ValueError("Request file exceeds 2 MiB")
    return loads_strict(raw)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--db", required=True, type=Path)
    sub = parser.add_subparsers(dest="operation", required=True)

    sub.add_parser("init", allow_abbrev=False)
    sub.add_parser("upgrade", allow_abbrev=False)

    review = sub.add_parser("next-review", allow_abbrev=False)
    review.add_argument("proposal")

    relation = sub.add_parser("use-source", allow_abbrev=False)
    relation.add_argument("target")
    relation.add_argument("source", nargs="?")

    call = sub.add_parser("call", allow_abbrev=False)
    call.add_argument("--request", required=True, type=Path)

    show = sub.add_parser("show", allow_abbrev=False)
    show.add_argument("id")
    show.add_argument("--revision", type=int)
    show.add_argument("--content", action="store_true")

    for name_ in ("context", "proposal"):
        command = sub.add_parser(name_, allow_abbrev=False)
        command.add_argument("id")

    for name_ in ("inbox", "changes"):
        command = sub.add_parser(name_, allow_abbrev=False)
        command.add_argument("--after", type=int, default=0)
        command.add_argument("--limit", type=int, default=20)
        if name_ == "inbox":
            command.add_argument("--actor", required=True)

    encounter = sub.add_parser("encounters", allow_abbrev=False)
    encounter.add_argument("--after", type=int, default=0)
    encounter.add_argument("--limit", type=int, default=20)
    encounter.add_argument("--scan-limit", type=int, default=256)

    receipt = sub.add_parser("receipt", allow_abbrev=False)
    receipt.add_argument("--actor", required=True)
    receipt.add_argument("--command", required=True)

    notice = sub.add_parser("notice", allow_abbrev=False)
    notice.add_argument("id", type=int)

    return parser


_LIFECYCLE_HANDLERS: dict[str, Callable[[Path, Path], Any]] = {
    "init": lambda db, prod: Collective.initialize(db, product=prod),
    "upgrade": lambda db, prod: Collective.upgrade(db, product=prod),
}

_STORE_HANDLERS: dict[str, Callable[[Collective, argparse.Namespace], Any]] = {
    "call": lambda store, args: store.call(_load_request_payload(args.request)),
    "show": lambda store, args: store.show(args.id, args.revision, content=args.content),
    "context": lambda store, args: store.retained("context", args.id),
    "proposal": lambda store, args: store.retained("proposal", args.id),
    "receipt": lambda store, args: store.receipt(args.actor, args.command),
    "notice": lambda store, args: store.notice(args.id),
    "next-review": lambda store, args: store.next_review(args.proposal),
    "use-source": lambda store, args: store.use_source(args.target, args.source),
    "encounters": lambda store, args: store.encounters(
        after=args.after,
        limit=args.limit,
        scan_limit=args.scan_limit,
    ),
    "inbox": lambda store, args: store.view(
        "inbox",
        actor=args.actor,
        after=args.after,
        limit=args.limit,
    ),
    "changes": lambda store, args: store.view(
        "changes",
        actor=None,
        after=args.after,
        limit=args.limit,
    ),
}


def main(argv: list[str] | None = None, *, product: Path = PRODUCT) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        lifecycle = _LIFECYCLE_HANDLERS.get(args.operation)
        if lifecycle is not None:
            result = lifecycle(args.db, product)
        else:
            handler = _STORE_HANDLERS.get(args.operation)
            if handler is None:
                raise ValueError("Unknown operation: " + str(args.operation))
            store = Collective(args.db, product=product)
            result = handler(store, args)

        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if not isinstance(result, dict) or result.get("status") != "refused" else 2
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "unresolved", "error": str(exc)}, allow_nan=False))
        return 3
