"""Resolve the installed native package for callers using the portable import root."""
from pathlib import Path
import runpy

__path__ = [str((Path(__file__).resolve().parent / "../../../runtime/continuation/continuation_support").resolve())]
__version__ = runpy.run_path(str(Path(__path__[0]) / '__init__.py'))['__version__']
