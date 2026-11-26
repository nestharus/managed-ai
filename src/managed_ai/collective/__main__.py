#!/usr/bin/env python3
"""Run the collective artifact store CLI."""
from __future__ import annotations

from pathlib import Path
import sys

if __name__ == "__main__" and not __package__:
    _pkg_dir = Path(__file__).resolve().parent
    if _pkg_dir.parent.name == "managed_ai" and (_pkg_dir.parent / "__init__.py").is_file():
        _sys_root = str(_pkg_dir.parents[1])
        __package__ = "managed_ai.collective"
    else:
        _sys_root = str(_pkg_dir.parent)
        __package__ = _pkg_dir.name
    if _sys_root not in sys.path:
        sys.path.insert(0, _sys_root)
    __import__(__package__)

from .store.cli import main

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
