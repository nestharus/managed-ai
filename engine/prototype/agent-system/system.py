"""Native API entrance used by earlier independently selected domain tools."""
from pathlib import Path
import runpy

_entry = Path(__file__).resolve().parents[2] / "runtime/store/system.py"
if __name__ == "__main__":
    runpy.run_path(str(_entry), run_name="__main__")
else:
    globals().update({k: v for k, v in runpy.run_path(str(_entry)).items() if not k.startswith("__")})
