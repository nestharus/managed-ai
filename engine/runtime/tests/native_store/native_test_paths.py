"""Import roots for direct and discovery runs of native store checks."""
import os
from pathlib import Path
import sys

PRODUCT = Path(os.environ.get('G60_TEST_PRODUCT', Path(__file__).resolve().parents[2])).resolve()
APP = PRODUCT / 'native'
ENGINE = PRODUCT.parent
sys.path[:0] = [str(APP), str(PRODUCT / 'source_library'),
                str(PRODUCT / 'deployment'), str(PRODUCT / 'continuation')]
