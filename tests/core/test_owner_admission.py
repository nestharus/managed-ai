"""The admission retry ends when the guarded operation starts."""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from managed_ai_information.core import storage  # noqa: E402
from managed_ai_information.core.storage import (  # noqa: E402
    AdmissionContended,
    OwnerBusy,
    exclusive,
    owner_admission,
    shared,
)


class OwnerAdmission(unittest.TestCase):
    def test_backoff_releases_partial_guards_and_outlasts_ten_minutes(self):
        elapsed = 0
        windows = []
        with TemporaryDirectory() as temp:
            outer, inner = Path(temp) / "deployment.lock", Path(temp) / "owner.lock"

            def acquire(guards):
                guards.enter_context(exclusive(outer))
                if elapsed < 601:
                    raise OwnerBusy(inner)
                return inner

            def jitter(low, high):
                windows.append((low, high))
                return high

            def sleep(delay):
                nonlocal elapsed
                with exclusive(outer):
                    elapsed += delay

            with (
                patch.object(storage.random, "uniform", side_effect=jitter),
                patch.object(storage.time, "monotonic", side_effect=lambda: elapsed),
                patch.object(storage.time, "sleep", side_effect=sleep),
                owner_admission(acquire) as selected,
            ):
                self.assertEqual(selected, inner)
            self.assertEqual(windows[:6], [(n / 2, n) for n in (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)])
            self.assertTrue(all(window == (1, 2) for window in windows[6:]))
            self.assertGreater(elapsed, 600)

    def test_prior_cutoff_limits_the_last_sleep_and_prevents_late_entry(self):
        path = Path("owner.lock")
        with (
            patch.object(storage.time, "monotonic", side_effect=[12, 12.04]),
            patch.object(storage.random, "uniform", return_value=0.05),
            patch.object(storage.time, "sleep") as sleep,
        ):

            def acquire(guards):
                raise OwnerBusy(path)

            with (
                self.assertRaises(AdmissionContended) as busy,
                owner_admission(acquire, deadline=12.03),
            ):
                self.fail("entered after cutoff")
        self.assertEqual(busy.exception.path, path)
        self.assertAlmostEqual(sleep.call_args.args[0], 0.03)

    def test_only_kernel_contention_is_retryable(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "owner.lock"
            with exclusive(path):
                for guard in (shared, exclusive):
                    with self.subTest(guard=guard.__name__):
                        with (
                            self.assertRaises(OwnerBusy) as direct,
                            guard(path),
                        ):
                            self.fail("entered held owner")
                        self.assertIs(type(direct.exception), OwnerBusy)
                        with (
                            self.assertRaises(AdmissionContended) as timed,
                            owner_admission(
                                lambda stack, guard=guard: stack.enter_context(guard(path)),
                                timeout=0,
                            ),
                        ):
                            self.fail("entered held owner")
                        self.assertEqual(timed.exception.path, path)
            with (
                self.assertRaises(FileNotFoundError),
                owner_admission(
                    lambda stack: stack.enter_context(shared(Path(temp) / "missing" / "owner"))
                ),
            ):
                self.fail("entered missing owner")

    def test_error_after_entry_does_not_repeat_an_effect(self):
        with TemporaryDirectory() as temp:
            outer, inner = (Path(temp) / name for name in ("outer", "inner"))
            entered = []
            with (
                exclusive(inner),
                self.assertRaises(OwnerBusy) as failed,
                owner_admission(lambda stack: stack.enter_context(exclusive(outer))),
            ):
                entered.append("effect")
                with shared(inner):
                    self.fail("entered held owner")
            self.assertIs(type(failed.exception), OwnerBusy)
            self.assertEqual(entered, ["effect"])
            with exclusive(outer):
                self.assertEqual(failed.exception.path, inner)
