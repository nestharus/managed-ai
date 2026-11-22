#!/usr/bin/env python3
"""Enter a prepared deployment, or install its stable entrance from this product."""
from pathlib import Path
import runpy
import sys

if __name__ == '__main__':
    sys.dont_write_bytecode = True
    here = Path(__file__).resolve().parent
    if len(sys.argv) > 1 and sys.argv[1].startswith('--file-origin='):
        bootstrap = runpy.run_path(str(here / 'manage_store.py'))
        bootstrap['enter_proposal_origin'](sys.argv.pop(1))
    controller = here / 'deployment' / 'controller.py'
    runpy.run_path(str(controller), run_name='__main__')
