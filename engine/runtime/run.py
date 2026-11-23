"""Portable entrance for callers of the earlier runtime layout."""
from pathlib import Path
import runpy

if __name__ == '__main__':
    runpy.run_path(str((Path(__file__).resolve().parent / 'collect_returns.py').resolve()), run_name='__main__')
