"""Regression cases for original custody, history coverage and receiving scope."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from admission import Admission, EQUALITY, MAX_BYTES, observe, same_value


class AdmissionChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = Admission.initialize(self.root / 'source.sqlite', 'one', 'original')

    def opened(self, job='job', prior='empty'):
        self.source.open(job, 'one', prior)
        return job

    def judge(self, requirement='all-originals', job='job', expected=b'{"page":1}', use='current', epoch='one'):
        return self.source.judge(job, expected, 'receiver-' + requirement, requirement,
                                 EQUALITY, use, epoch)

    def test_decoded_duplicates_include_nested_escaped_and_identical_values(self):
        for raw in (b'{"a":1,"a":1}', b'{"a":1,"\\u0061":1}',
                    b'{"nested":[{"a":1,"a":2}]}'):
            result = observe(raw)
            self.assertTrue(result['valid_json'])
            self.assertFalse(result['unique_member_names'])

    def test_malformed_and_nonfinite_inputs_remain_recoverable(self):
        self.opened()
        for name, raw in (('broken', b'{'), ('nan', b'{"page":NaN}'), ('encoding', b'\xff')):
            result = self.source.admit('job', name, raw)
            self.assertFalse(result['observation']['valid_json'])
            self.assertEqual(self.source.original('job', name), raw)
        self.source.admit('job', 'selected', b'{"page":1}')
        self.source.close('job', 'selected')
        self.assertEqual(self.judge()['history'], 'fail')

    def test_each_receiver_keeps_its_own_requirement(self):
        self.opened()
        self.source.admit('job', 'earlier', b'{"page":0,"page":0}')
        self.source.admit('job', 'selected', b'{"page":1}')
        self.source.close('job', 'selected')
        self.assertTrue(self.judge('normalized')['eligible'])
        self.assertTrue(self.judge('selected-original')['eligible'])
        self.assertFalse(self.judge('all-originals')['eligible'])

    def test_selected_duplicate_can_satisfy_only_normalized_receiver(self):
        self.opened()
        self.source.admit('job', 'selected', b'{"page":0,"page":1}')
        self.source.close('job', 'selected')
        self.assertTrue(self.judge('normalized')['eligible'])
        self.assertFalse(self.judge('selected-original')['eligible'])

    def test_unselected_content_need_not_equal_selected_record(self):
        self.opened()
        self.source.admit('job', 'earlier', b'{"other":"different information"}')
        self.source.admit('job', 'selected', b'{"page":1}')
        self.source.close('job', 'selected')
        self.assertTrue(self.judge()['eligible'])

    def test_unknown_prefix_stays_unknown_and_bad_observation_establishes_failure(self):
        self.opened(prior='unknown')
        self.source.admit('job', 'selected', b'{"page":1}')
        self.source.close('job', 'selected')
        self.assertEqual(self.judge()['history'], 'unknown')
        self.assertFalse(self.judge()['eligible'])
        with self.assertRaisesRegex(ValueError, 'different terms'):
            self.source.open('job', 'one', 'empty')
        self.opened('bad', 'unknown')
        self.source.admit('bad', 'selected', b'{"page":1,"page":1}')
        self.source.close('bad', 'selected')
        self.assertEqual(self.judge(job='bad')['history'], 'fail')

    def test_open_window_cannot_qualify_a_final_receiver(self):
        self.opened()
        self.source.admit('job', 'candidate', b'{"page":1}')
        result = self.judge()
        self.assertFalse(result['eligible'])
        self.assertEqual(result['history'], 'unknown')
        self.assertIn('admission-window-open', result['reasons'])

    def test_cutoff_is_final_and_exact_repetition_is_idempotent(self):
        self.opened()
        self.source.admit('job', 'selected', b'{"page":1}')
        self.source.admit('job', 'other', b'{"page":2}')
        first = self.source.close('job', 'selected')
        self.assertEqual(first, self.source.close('job', 'selected'))
        self.source.admit('job', 'selected', b'{"page":1}')
        with self.assertRaisesRegex(ValueError, 'cutoff is final'):
            self.source.admit('job', 'late', b'{"page":1}')
        with self.assertRaisesRegex(ValueError, 'cannot be replaced'):
            self.source.close('job', 'other')
        with self.assertRaisesRegex(ValueError, 'different original bytes'):
            self.source.admit('job', 'selected', b'{"page":2}')
        self.assertTrue(self.judge()['eligible'])

    def test_source_change_refuses_current_use_and_preserves_historical_use(self):
        self.opened()
        self.source.admit('job', 'selected', b'{"page":1}')
        self.source.close('job', 'selected')
        self.source.advance('two', 'derived', 'one')
        self.assertFalse(self.judge()['eligible'])
        self.assertIn('source-edition-changed', self.judge()['reasons'])
        self.assertTrue(self.judge(use='historical')['eligible'])
        with self.assertRaisesRegex(ValueError, 'edition changed'):
            self.source.admit('job', 'late', b'{"page":1}')
        self.assertEqual(self.source.original('job', 'selected'), b'{"page":1}')

    def test_current_derived_input_does_not_establish_original_predicate(self):
        self.source.advance('two', 'derived', 'one')
        self.source.open('job', 'two', 'empty')
        self.source.admit('job', 'selected', b'{"page":1}')
        self.source.close('job', 'selected')
        self.assertTrue(self.judge('normalized', epoch='two')['eligible'])
        result = self.judge(epoch='two')
        self.assertFalse(result['eligible'])
        self.assertIn('original-input-authority-unavailable', result['reasons'])

    def test_explicit_equality_treats_numbers_by_value_and_distinguishes_booleans(self):
        self.opened()
        self.source.admit('job', 'selected', b'{"page":1.0}')
        self.source.close('job', 'selected')
        self.assertTrue(self.judge()['eligible'])
        self.assertFalse(self.judge(expected=b'{"page":true}')['eligible'])
        with self.assertRaisesRegex(ValueError, 'equality'):
            self.source.judge('job', b'{"page":1}', 'R', 'all-originals', 'unspecified', 'current', 'one')
        comparisons = (
            (b'{"n":1}', b'{"n":1e0}', True),
            (b'{"n":9007199254740992}', b'{"n":9007199254740993}', False),
            (b'{"n":9007199254740993}', b'{"n":9007199254740993.0}', True),
            (b'{"n":0}', b'{"n":false}', False),
        )
        for left, right, expected in comparisons:
            self.assertEqual(same_value(observe(left)['value'], observe(right)['value']), expected)
        for token in ('NaN', 'Infinity', '-Infinity'):
            self.assertFalse(observe(('{"n":' + token + '}').encode())['valid_json'])
        expanded = b'{"n":1' + b'0' * 4999 + b'}'
        exponent = b'{"n":1e4999}'
        self.assertTrue(observe(expanded)['valid_json'])
        self.assertTrue(same_value(observe(expanded)['value'], observe(exponent)['value']))

    def test_parser_resource_limit_retains_unknown_history(self):
        self.opened()
        raw = b'{"n":1e9999999999999999999999999}'
        self.assertIsNone(observe(raw)['valid_json'])
        self.source.admit('job', 'unavailable', raw)
        self.source.admit('job', 'selected', b'{"page":1}')
        self.source.close('job', 'selected')
        result = self.judge()
        self.assertFalse(result['eligible'])
        self.assertEqual(result['history'], 'unknown')
        self.assertEqual(result['bad_candidates'], [])
        self.assertEqual(result['unobserved_candidates'], ['unavailable'])
        self.assertEqual(self.source.original('job', 'unavailable'), raw)

    def test_witnessed_duplicate_survives_a_later_parser_limit(self):
        self.opened()
        raw = b'{"witness":{"a":0,"a":1},"n":1e99999999999999999999}'
        observation = observe(raw)
        self.assertIsNone(observation['valid_json'])
        self.assertFalse(observation['unique_member_names'])
        self.assertEqual(observation['duplicate_names'], ['a'])
        self.source.admit('job', 'witnessed', raw)
        self.source.admit('job', 'selected', b'{"page":1}')
        self.source.close('job', 'selected')
        result = self.judge()
        self.assertFalse(result['eligible'])
        self.assertEqual(result['history'], 'fail')
        self.assertEqual(result['bad_candidates'], ['witnessed'])

    def test_wrong_epoch_and_changed_expectation_do_not_inherit_acceptance(self):
        self.opened()
        self.source.admit('job', 'selected', b'{"page":1}')
        self.source.close('job', 'selected')
        self.assertFalse(self.judge(epoch='other')['eligible'])
        self.assertFalse(self.judge(expected=b'{"page":2}')['eligible'])
        with self.assertRaisesRegex(ValueError, 'unambiguous'):
            self.judge(expected=b'{"page":1,"page":1}')

    def test_unrelated_job_completes_after_neighbor_is_closed_or_unqualified(self):
        self.opened()
        self.source.admit('job', 'selected', b'{"page":1,"page":1}')
        self.source.close('job', 'selected')
        self.opened('neighbor')
        self.source.admit('neighbor', 'selected', b'{"page":1}')
        self.source.close('neighbor', 'selected')
        self.assertFalse(self.judge()['eligible'])
        self.assertTrue(self.judge(job='neighbor')['eligible'])

    def test_input_bounds_and_conflicting_source_changes_are_refused(self):
        with self.assertRaisesRegex(ValueError, '2 MiB'):
            observe(b' ' * (MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, 'epoch conflict'):
            self.source.advance('two', 'original', 'wrong')
        self.assertEqual(self.source.open('job', 'one', 'empty')['epoch'], 'one')


if __name__ == '__main__':
    unittest.main()
