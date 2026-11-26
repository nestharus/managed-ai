#!/usr/bin/env python3
"""Enter a prepared deployment, or install its stable entrance from this product."""
from pathlib import Path
import runpy
import sys

if __name__ == '__main__':
    sys.dont_write_bytecode = True
    engine = Path(__file__).resolve().parents[1]
    if str(engine) not in sys.path:
        sys.path.insert(0, str(engine))
    if len(sys.argv) > 1 and sys.argv[1].startswith('--file-origin='):
        bootstrap = runpy.run_path(str(engine / 'store' / 'manage_store.py'))
        bootstrap['enter_proposal_origin'](sys.argv.pop(1))
    from deployment.controller import main
    main()
