#!/usr/bin/env python3
"""Standalone entry point for explicit continuation exchange."""
import os
from pathlib import Path
import sys

if __name__ == '__main__':
    if os.name != 'posix':
        raise SystemExit('Continuation exchange requires POSIX.')
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'continuation'))
    from continuation_support.exchange import main
    main()
