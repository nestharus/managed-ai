#!/usr/bin/env python3
"""Run the included carrier without installation; runtime roots must be external."""
from pathlib import Path
import os
import sys

def main() -> None:
    if os.name != "posix":
        raise SystemExit("This local return carrier requires POSIX (fcntl and detached workers).")
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    from continuation_support.collector import main as run
    from continuation_support.ownership import external_root
    run(validate_root=external_root)

if __name__ == "__main__":
    main()
