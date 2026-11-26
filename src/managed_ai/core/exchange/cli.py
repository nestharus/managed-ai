"""Explicit context, return, and single-file publication across local invocations.

The caller supplies meaning and authority. This module supplies byte-bound
transport and optional publication, not a cognitive schema or semantic selector.
Trusted local use only; it is not a filesystem or process sandbox.
"""

import argparse
import json
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    _src_root = str(Path(__file__).resolve().parents[3])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai.core.exchange.api import Exchange
from managed_ai.core.ownership import external_root
from managed_ai.core.storage import (
    AdmissionContended,
    admission_deadline,
    exclusive,
    owner_admission,
    shared,
)

_READONLY_ACTIONS = frozenset({"list", "inspect", "context", "recover_transport"})


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--admission-deadline", type=float, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="action", required=True)

    init = sub.add_parser("init", allow_abbrev=False)
    init.add_argument("--workspace", type=Path, required=True)

    prep = sub.add_parser("prepare", allow_abbrev=False)
    prep.add_argument("packet")
    prep.add_argument("--consumer", required=True)
    prep.add_argument("--brief", required=True)
    for flag in ("method", "material", "target", "return"):
        prep.add_argument("--" + flag, dest=flag + "s", action="append", default=[])
    prep.add_argument(
        "--artifact",
        dest="artifacts",
        action="append",
        default=[],
        metavar="PACKET:TARGET",
        help="include a recorded proposal as material, without publication",
    )

    dis = sub.add_parser("dispatch", allow_abbrev=False)
    dis.add_argument("packet")
    dis.add_argument("argv", nargs=argparse.REMAINDER)

    rec = sub.add_parser("receive", allow_abbrev=False)
    rec.add_argument("packet")
    rec.add_argument("--reply", type=Path)
    rec.add_argument("--producer", required=True)
    rec.add_argument("--proposal", dest="proposals", action="append", default=[])
    rec.add_argument("--wait", type=float, default=0)

    listing = sub.add_parser("list", allow_abbrev=False)
    listing.add_argument("--consumer")

    ins = sub.add_parser("inspect", allow_abbrev=False)
    ins.add_argument("packet")

    context = sub.add_parser(
        "context", allow_abbrev=False, help="read the validated originating context"
    )
    context.add_argument("packet")
    context.add_argument("--text", action="store_true")

    recovery = sub.add_parser(
        "recover-transport",
        allow_abbrev=False,
        help="recover the captured carrier view from retained evidence",
    )
    recovery.add_argument("packet")
    recovery.set_defaults(action="recover_transport")

    closure = sub.add_parser(
        "close-transport",
        allow_abbrev=False,
        help="record an owner-accounted terminal failure without a contribution",
    )
    closure.add_argument("packet")
    closure.add_argument("--account", required=True)
    closure.set_defaults(action="close_transport")

    reconciliation = sub.add_parser(
        "reconcile-publication",
        allow_abbrev=False,
        help="acknowledge observed target bytes without replaying publication",
    )
    reconciliation.add_argument("packet")
    reconciliation.add_argument("--target", required=True)
    reconciliation.add_argument("--expected-sha256", required=True, metavar="SHA256_OR_absent")
    reconciliation.add_argument("--account", required=True)
    reconciliation.set_defaults(action="reconcile_publication")

    pub = sub.add_parser("publish", allow_abbrev=False)
    pub.add_argument("packet")
    pub.add_argument("--target", required=True)
    pub.add_argument("--reason", required=True)

    return p


def main() -> None:
    args = vars(_build_parser().parse_args())
    root = args.pop("root")
    action = args.pop("action")
    text = args.pop("text", False)
    deadline = admission_deadline(args.pop("admission_deadline", None))

    try:
        if action == "init":
            result = Exchange.initialize(root, **args)
        else:
            resolved = external_root(root)
            if not (resolved / "workspace.json").is_file():
                raise ValueError("initialize the external exchange first")

            read_only = action in _READONLY_ACTIONS
            guard = shared if read_only else exclusive
            lock_path = resolved / "owner.lock"

            with owner_admission(
                lambda guards: guards.enter_context(
                    guard(lock_path, allow_readonly_open=read_only)
                ),
                deadline=deadline,
            ):
                ex = Exchange(resolved)
                if action == "dispatch" and args["argv"][:1] == ["--"]:
                    args["argv"] = args["argv"][1:]
                result = getattr(ex, action)(**args)

        if text:
            if hasattr(sys.stdout, "buffer"):
                sys.stdout.buffer.write(result["context"].encode("utf-8"))
            else:
                sys.stdout.write(result["context"])
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except AdmissionContended as exc:
        print(json.dumps(exc.result(action)), file=sys.stderr)
        raise SystemExit(75) from None
    except (OSError, ValueError, KeyError, RuntimeError, StopIteration) as exc:
        print(json.dumps({"error": str(exc), "operation": action}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
