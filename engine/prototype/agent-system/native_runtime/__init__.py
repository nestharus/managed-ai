"""Portable import root for native embedding clients."""
from pathlib import Path

__path__[:] = [str(Path(__file__).resolve().parents[3] / 'runtime/native/native_runtime')]
