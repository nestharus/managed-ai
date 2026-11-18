#!/usr/bin/env python3
"""Deployment acceptance reuses the current shared-instance behavioral cases."""
import unittest
from check_shared_instances import SharedInstances

if __name__ == '__main__':
    suite = unittest.TestSuite(SharedInstances(name) for name in (
        'test_01_shared_code_and_separate_actual_turns',
        'test_04_incompatible_selection_refuses_before_mutation',
        'test_06_portable_archive_uses_relocated_shared_engine'))
    raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
