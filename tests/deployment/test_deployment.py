#!/usr/bin/env python3
"""Deployment acceptance reuses the current shared-instance behavioral cases."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for _p in (_REPO / "src", _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from managed_ai_information.deployment.controller import Deployment  # noqa: E402
from tests._paths import ENGINE  # noqa: E402
from tests.store.test_shared_instances import SharedInstances  # noqa: E402


class DeploymentAcceptance(unittest.TestCase):
    def test_shared_deployment_cases(self):
        self.assertTrue(ENGINE.is_dir())
        with self.assertRaises(ValueError):
            Deployment(ENGINE / "nonexistent-deployment-root")
        for name in (
            "test_01_shared_code_and_separate_actual_turns",
            "test_04_incompatible_selection_refuses_before_mutation",
            "test_06_portable_archive_uses_relocated_shared_engine",
        ):
            with self.subTest(case=name):
                case = SharedInstances(name)
                case.setUp()
                try:
                    getattr(case, name)()
                finally:
                    case.doCleanups()


if __name__ == "__main__":
    unittest.main(verbosity=2)
