"""Import roots for direct and discovery runs of native store checks."""

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tests._paths import APP, ENGINE, PRODUCT, REPO, SRC  # noqa: E402

__all__ = ["APP", "ENGINE", "PRODUCT", "REPO", "SRC"]
