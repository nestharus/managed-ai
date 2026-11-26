#!/usr/bin/env python3
"""Run the included carrier without installation; runtime roots must be external."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys


def _ensure_package(engine: Path) -> None:
    if engine.name == 'managed_ai':
        src = str(engine.parent)
        if src not in sys.path:
            sys.path.insert(0, src)
        return
    loaded = sys.modules.get('managed_ai')
    if loaded is not None and getattr(loaded, '__file__', None):
        if Path(loaded.__file__).resolve().parent == engine:
            return
    init = engine / '__init__.py'
    if init.is_file():
        spec = importlib.util.spec_from_file_location(
            'managed_ai', str(init), submodule_search_locations=[str(engine)],
        )
    else:
        from importlib.machinery import ModuleSpec
        spec = ModuleSpec('managed_ai', None, is_package=True)
        spec.submodule_search_locations = [str(engine)]
    if spec is None:
        raise ImportError(f'unable to load managed_ai package from {engine}')
    module = importlib.util.module_from_spec(spec)
    sys.modules['managed_ai'] = module
    if spec.loader is not None:
        spec.loader.exec_module(module)


def main() -> None:
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    _ensure_package(Path(__file__).resolve().parents[2])
    from managed_ai.core.exchange.transport.collector import main as run
    from managed_ai.core.ownership import external_root
    run(validate_root=external_root)


if __name__ == '__main__':
    main()
