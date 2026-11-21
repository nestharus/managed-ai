"""Compatibility entry forwarding to the single canonical knowledge implementation."""
import importlib.util as _importlib_util
from pathlib import Path as _Path
import sys as _sys

_canonical = _Path(__file__).resolve().parent.parent / "knowledge-system" / "knowledge.py"
_spec = _importlib_util.spec_from_file_location("_g60_canonical_knowledge", _canonical)
_module = _importlib_util.module_from_spec(_spec)
_sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
globals().update({name: value for name, value in vars(_module).items() if not name.startswith("__")})

if __name__ == "__main__":
    if not any(arg == "--library" or arg.startswith("--library=") for arg in _sys.argv[1:]):
        import runpy as _runpy
        _runpy.run_path(str(_Path(__file__).resolve().parents[2] / 'knowledge.py'), run_name='__main__')
    else:
        _module.main()
