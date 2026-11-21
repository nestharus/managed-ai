"""Compatibility entry forwarding to the single canonical knowledge implementation."""
import importlib.util as _importlib_util
from pathlib import Path as _Path
import sys as _sys

_canonical = _Path(__file__).resolve().parent.parent / "knowledge-system" / "source_producer.py"
_spec = _importlib_util.spec_from_file_location("_g60_canonical_source_producer", _canonical)
_module = _importlib_util.module_from_spec(_spec)
_sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
globals().update({name: value for name, value in vars(_module).items() if not name.startswith("__")})
