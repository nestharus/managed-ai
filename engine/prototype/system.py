"""Compatibility entrance for earlier operating commands."""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "runtime/system.py"), run_name="__main__")
