"""Portable entrance for callers of the earlier runtime layout."""
from pathlib import Path
import runpy

_entry = runpy.run_path(str((Path(__file__).resolve().parent / 'manage_store.py').resolve()), run_name=__name__)
globals().update({key: value for key, value in _entry.items() if not key.startswith('__')})
