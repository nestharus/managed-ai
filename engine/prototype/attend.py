#!/usr/bin/env python3
"""Run the local attention interface from a standalone source distribution."""
from pathlib import Path
import sys

if __name__ == '__main__':
    sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from continuation_support.attention import main

if __name__ == '__main__':
    main()
