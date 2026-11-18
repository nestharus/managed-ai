#!/usr/bin/env python3
"""Run the collective CLI or load its public API from this exact installation."""
from hashlib import sha256
from importlib import import_module, util
from pathlib import Path
import sys

if __name__ == '__main__':
    sys.dont_write_bytecode = True

PRODUCT = Path(__file__).resolve().parent


def _installed_api():
    # Two explicitly selected editions can coexist in a receiving process.
    directory = PRODUCT / 'collective_store'
    key = '_g60_collective_' + sha256(str(directory).encode()).hexdigest()
    if key not in sys.modules:
        spec = util.spec_from_file_location(key, directory / '__init__.py',
                                            submodule_search_locations=[str(directory)])
        module = util.module_from_spec(spec)
        sys.modules[key] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[key]
            raise
    return sys.modules[key], import_module(key + '.cli').main


_api, main = _installed_api()
Collective, Refused, CONTEXT_LIMIT, pin = _api.Collective, _api.Refused, _api.CONTEXT_LIMIT, _api.pin

if __name__ == '__main__':
    raise SystemExit(main())
