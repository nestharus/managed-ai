#!/usr/bin/env python3
"""Operate the collective component with the enclosing distribution's storage boundary."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

PRODUCT = Path(__file__).resolve().parent


def main(argv=None):
    nested = PRODUCT / 'collective' / 'collective.py'
    entry = nested if nested.is_file() else PRODUCT / 'collective.py'
    spec = spec_from_file_location('g60_collective_component', entry)
    component = module_from_spec(spec)
    spec.loader.exec_module(component)
    return component.main(argv, product=PRODUCT)


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    raise SystemExit(main())
