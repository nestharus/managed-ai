"""Portable entrance for the recoverable signal queue."""
from pathlib import Path
import runpy

_queue = runpy.run_path(str(Path(__file__).resolve().parent / 'signal_field/queue.py'), run_name=__name__)
globals().update({name: value for name, value in _queue.items() if not name.startswith('__')})
