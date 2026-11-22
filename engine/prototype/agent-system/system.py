"""Portable entrance for callers of the original source distribution."""
from pathlib import Path
import runpy

DEPLOYMENT_API = 'agent-system-deployment/2'

_entry = runpy.run_path(str((Path(__file__).resolve().parent / '../../runtime/native/system.py').resolve()), run_name=__name__)
globals().update({key: value for key, value in _entry.items() if not key.startswith('__')})
