#!/usr/bin/env python3
"""Run the included carrier without installation; runtime roots must be external."""
from pathlib import Path
import os
import sys


def main() -> None:
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from core.exchange.transport.collector import main as run
    from core.ownership import external_root
    run(validate_root=external_root)


if __name__ == '__main__':
    main()
