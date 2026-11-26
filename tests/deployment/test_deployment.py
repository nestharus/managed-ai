#!/usr/bin/env python3
"""Deployment acceptance reuses the current shared-instance behavioral cases."""
from pathlib import Path
import sys
import unittest

_STORE_TESTS = str(Path(__file__).resolve().parents[1] / 'store')
if _STORE_TESTS not in sys.path:
    sys.path.insert(0, _STORE_TESTS)

from test_shared_instances import SharedInstances


class DeploymentAcceptance(unittest.TestCase):
    def test_shared_deployment_cases(self):
        for name in (
            'test_01_shared_code_and_separate_actual_turns',
            'test_04_incompatible_selection_refuses_before_mutation',
            'test_06_portable_archive_uses_relocated_shared_engine',
        ):
            with self.subTest(case=name):
                case = SharedInstances(name)
                case.setUp()
                try:
                    getattr(case, name)()
                finally:
                    case.doCleanups()


if __name__ == '__main__':
    unittest.main(verbosity=2)
