"""Native import root for installed domain tools that pin the source loaders here."""
from pathlib import Path

__path__.append(str(Path(__file__).resolve().parents[3] / 'runtime/native/native_runtime'))
