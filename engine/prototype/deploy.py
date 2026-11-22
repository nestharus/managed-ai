"""Portable entrance for callers of the original source distribution."""
from pathlib import Path
import runpy

if __name__ == '__main__':
    runpy.run_path(str((Path(__file__).resolve().parent / '../runtime/select_deployment.py').resolve()), run_name='__main__')
