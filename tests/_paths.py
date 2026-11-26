"""Canonical repository, source, and engine paths for the Managed AI test suite."""
from __future__ import annotations

import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / 'src'
_DEFAULT_ENGINE = SRC / 'managed_ai'
_RAW_ENGINE = os.environ.get('G60_TEST_ENGINE') or os.environ.get('G60_TEST_PRODUCT')
ENGINE = Path(_RAW_ENGINE).resolve() if _RAW_ENGINE else _DEFAULT_ENGINE.resolve()
PRODUCT = ENGINE
APP = ENGINE / 'store'

_ACTIVE_SRC = ENGINE.parent if ENGINE.name == 'managed_ai' and (ENGINE / '__init__.py').is_file() else SRC
for _path in (_ACTIVE_SRC, SRC):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)
