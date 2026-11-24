"""The admission retry ends when the guarded operation starts."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'continuation'))
from continuation_support.storage import AdmissionContended, OwnerBusy, exclusive, owner_admission, shared


class OwnerAdmission(unittest.TestCase):
    def test_only_kernel_contention_is_retryable(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'owner.lock'
            with exclusive(path):
                for guard in (shared, exclusive):
                    with self.subTest(guard=guard.__name__):
                        with self.assertRaises(OwnerBusy) as direct:
                            with guard(path):
                                self.fail('entered held owner')
                        self.assertIs(type(direct.exception), OwnerBusy)
                        with self.assertRaises(AdmissionContended) as timed:
                            with owner_admission(lambda stack: stack.enter_context(guard(path)), timeout=0):
                                self.fail('entered held owner')
                        self.assertEqual(timed.exception.path, path)
            with self.assertRaises(FileNotFoundError):
                with owner_admission(lambda stack: stack.enter_context(shared(Path(temp) / 'missing' / 'owner'))):
                    self.fail('entered missing owner')

    def test_error_after_entry_does_not_repeat_an_effect(self):
        with TemporaryDirectory() as temp:
            outer, inner = (Path(temp) / name for name in ('outer', 'inner'))
            entered = []
            with exclusive(inner), self.assertRaises(OwnerBusy) as failed:
                with owner_admission(lambda stack: stack.enter_context(exclusive(outer))):
                    entered.append('effect')
                    with shared(inner):
                        self.fail('entered held owner')
            self.assertIs(type(failed.exception), OwnerBusy)
            self.assertEqual(entered, ['effect'])
            with exclusive(outer):
                self.assertEqual(failed.exception.path, inner)
