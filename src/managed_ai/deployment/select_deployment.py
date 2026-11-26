#!/usr/bin/env python3
"""Enter a prepared deployment, or install its stable entrance from this product."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _ensure_package(engine: Path) -> None:
    if engine.name == "managed_ai":
        src = str(engine.parent)
        if src not in sys.path:
            sys.path.insert(0, src)
        return

    loaded = sys.modules.get("managed_ai")
    if loaded is not None and getattr(loaded, "__file__", None):
        if Path(loaded.__file__).resolve().parent == engine:
            return

    spec = importlib.util.spec_from_file_location(
        "managed_ai",
        str(engine / "__init__.py"),
        submodule_search_locations=[str(engine)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"unable to load managed_ai package from {engine}")

    module = importlib.util.module_from_spec(spec)
    sys.modules["managed_ai"] = module
    spec.loader.exec_module(module)


def main() -> None:
    sys.dont_write_bytecode = True
    engine = Path(__file__).resolve().parents[1]
    _ensure_package(engine)

    if len(sys.argv) > 1 and sys.argv[1].startswith("--file-origin="):
        from managed_ai.store.manage_store import enter_proposal_origin

        enter_proposal_origin(sys.argv.pop(1))

    from managed_ai.deployment.cli import main as deployment_main

    deployment_main()


if __name__ == "__main__":
    main()
