"""Import roots for direct and discovery runs of native store checks."""
import os
from pathlib import Path
import sys

ENGINE = Path(os.environ.get('G60_TEST_ENGINE', Path(__file__).resolve().parents[3] / 'engine')).resolve()
PRODUCT = ENGINE
APP = ENGINE / 'store'
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))
