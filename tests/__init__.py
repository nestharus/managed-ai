"""Test package bootstrap ensuring REPO/src is importable without manual PYTHONPATH."""
from pathlib import Path
import sys

_SRC = str(Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
