#!/usr/bin/env python3
"""Standalone entry point for explicit continuation exchange."""
import os
from pathlib import Path
import sys

if __name__ == '__main__':
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from core.exchange.cli import main
    main()
