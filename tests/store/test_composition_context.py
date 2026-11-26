#!/usr/bin/env python3
"""Exercise composition exposure against actual custody/publication boundaries."""
if __package__:
    from .native_test_paths import APP, ENGINE, PRODUCT
else:
    from native_test_paths import APP, ENGINE, PRODUCT
from pathlib import Path
import json
import tempfile
import unittest

from managed_ai.store.system import System, exclusive


class CompositionContextChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='composition-context-')
        self.base = Path(self.temp.name)
        self.root = self.base / 'runtime'
        System.initialize(self.root, {'contribution': 'Construct useful sibling artifacts.'})
        self.system = System(self.root)
        self.lock = exclusive(self.system.ex.root / 'owner.lock')
        self.lock.__enter__()
        (self.system.work / 'sources/claim.md').write_text('A source for the conditional claim.\n')
        self.system.put({'id': 'field', 'kind': 'field', 'content': {'selection': 'Compare siblings.'}}, 0)
        self.profile = {'format': 'g60-composition/1', 'purpose': 'Keep a useful sibling visible.',
                        'elements': [{'id': 'explanation', 'account': 'A receiving explanation.'}],
                        'premises': [{'id': 'source', 'account': 'Actual source available.',
                                      'disposition': 'available', 'basis': ['sources/claim.md']}],
                        'opportunities': [{'id': 'explain', 'at': ['element:explanation'],
                                           'contribution': 'Write a qualified explanation.',
                                           'requires': [['source']]}]}
        self.system.put({'id': 'composition', 'kind': 'composition', 'content': self.profile,
                         'basis': ['sources/claim.md']}, 0)

    def tearDown(self):
        self.lock.__exit__(None, None, None)
        self.temp.cleanup()

    def test_view_and_implicit_publication_basis_are_captured_together(self):
        self.system.context('inspect', {'compositions': [{'record': 'composition', 'pair_limit': 0}],
                                        'targets': ['outputs/explanation.md']})
        view = self.system.view('inspect')
        self.assertIn('composition', view['selected_records'])
        self.assertEqual(view['compositions'][0]['record']['sha256'], self.system.show('composition')['sha256'])
        footprint = view['publication_dependencies']
        self.assertIn('field', [r['id'] for r in footprint['governing_records']])
        self.assertIn('records/composition.json', footprint['material_paths'])
        self.assertIn('sources/claim.md', footprint['material_paths'])
        self.assertEqual(footprint['target_paths'], ['outputs/explanation.md'])
        original = self.system.recover('inspect')['context']
        (self.system.work / 'sources/claim.md').write_text('Changed source interpretation.\n')
        current = self.system.compose('composition', pair_limit=0)
        self.assertEqual(current['opportunities'][0]['availability'], 'needs-qualification')
        self.assertEqual(self.system.recover('inspect')['context'], original)

    def test_omitted_composition_is_refused_before_turn_preparation(self):
        self.system.put({'id': 'other', 'kind': 'note', 'content': 'Another selected record.'}, 0)
        with self.assertRaisesRegex(ValueError, 'omitted by record_limit'):
            self.system.context('too-narrow', {'records': ['other'], 'record_limit': 1,
                                               'compositions': [{'record': 'composition'}]})
        self.assertFalse((self.system.work / '.system/turns/too-narrow').exists())

    def test_field_change_keeps_guard_and_exact_current_reentry(self):
        request = {'compositions': [{'record': 'composition'}], 'targets': ['outputs/explanation.md']}
        self.system.context('original', request)
        proposal = self.base / 'proposal.md'
        proposal.write_text('Fixture proposal whose current use is explicitly reassessed.\n')
        self.system.receive('original', proposal, 'synthetic fixture', ['outputs/explanation.md=' + str(proposal)])
        self.system.put({'id': 'field', 'kind': 'field', 'content': {'selection': 'Another useful branch is available.'}}, 1)
        with self.assertRaisesRegex(ValueError, 'basis changed'):
            self.system.adopt('original', 'outputs/explanation.md', 'Expected current-basis refusal.')
        self.assertFalse((self.system.work / 'outputs/explanation.md').exists())
        self.system.context('current', dict(request, artifacts=['original:outputs/explanation.md'],
                                            task='Fixture owner explicitly reassesses the retained proposal.'))
        self.system.receive('current', proposal, 'synthetic current-use fixture', ['outputs/explanation.md=' + str(proposal)])
        self.system.adopt('current', 'outputs/explanation.md', 'Fixture owner judges the exact retained body adequate under the current field.')
        self.assertEqual((self.system.work / 'outputs/explanation.md').read_bytes(), proposal.read_bytes())

    def test_combined_view_budget_refuses_before_capture(self):
        # Two individually bounded valid views must not produce an oversized
        # combined context object. The repeated detail is actual profile text.
        large = dict(self.profile, content='a' * 530000)
        large['elements'] = [{'id': 'explanation', 'account': 'a' * 530000}]
        large.pop('content')
        self.system.put({'id': 'large-a', 'kind': 'composition', 'content': large}, 0)
        self.system.put({'id': 'large-b', 'kind': 'composition', 'content': large}, 0)
        with self.assertRaisesRegex(ValueError, 'combined composition views'):
            self.system.context('over-budget', {'compositions': [{'record': 'large-a', 'pair_limit': 0},
                                                                 {'record': 'large-b', 'pair_limit': 0}]})
        self.assertFalse((self.system.work / '.system/turns/over-budget').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
