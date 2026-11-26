#!/usr/bin/env python3
"""Run the small agent system from this standalone distribution."""

import importlib.util
import json
import os
import sys
from pathlib import Path


def enter_proposal_origin(argument: str) -> None:
    """Restore the nominating cwd in the new CLI process before entering the receiver."""
    from managed_ai_information.platform import PLATFORM

    try:
        device, inode, directory = argument.removeprefix("--file-origin=").split(":", 2)
        path = bytes.fromhex(directory)
        PLATFORM.enter_proposal_origin(path)
        cwd = os.stat(".")
        if (cwd.st_dev, cwd.st_ino) != (int(device), int(inode)):
            raise ValueError("proposal origin directory changed")
    except (OSError, ValueError) as exc:
        print(
            json.dumps({"status": "unresolved", "operation": "receive", "error": str(exc)}),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


def _ensure_package(engine: Path) -> None:
    if engine.name == "managed_ai_information":
        src = str(engine.parent)
        if src not in sys.path:
            sys.path.insert(0, src)
        return

    loaded = sys.modules.get("managed_ai_information")
    loaded_file = getattr(loaded, "__file__", None) if loaded is not None else None
    if isinstance(loaded_file, str) and Path(loaded_file).resolve().parent == engine:
        return

    spec = importlib.util.spec_from_file_location(
        "managed_ai_information",
        str(engine / "__init__.py"),
        submodule_search_locations=[str(engine)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"unable to load managed_ai_information package from {engine}")

    module = importlib.util.module_from_spec(spec)
    sys.modules["managed_ai_information"] = module
    spec.loader.exec_module(module)


def main() -> None:
    sys.dont_write_bytecode = True
    engine = Path(__file__).resolve().parents[1]
    _ensure_package(engine)

    if len(sys.argv) > 1 and sys.argv[1].startswith("--file-origin="):
        enter_proposal_origin(sys.argv.pop(1))

    # Parse CLI arguments via the lightweight parser before importing System/Exchange.
    from managed_ai_information.store.cli.command_parser import parser

    args = parser().parse_args()

    from managed_ai_information.store.system import main as system_main

    system_main(args)


if __name__ == "__main__":
    main()
