#!/usr/bin/env python3
"""Focused checks of optional information bindings and their actual next context.

All meanings and migration examples are authored fixtures. Temporary runtimes
exercise exact owner boundaries; no shared research runtime is touched.
"""
from __future__ import annotations

if __package__:
    from .native_test_paths import APP, ENGINE, PRODUCT
else:
    from native_test_paths import APP, ENGINE, PRODUCT
import copy
from pathlib import Path
import runpy
import sys
import tempfile
import unittest

sys.path.insert(0, str(APP))
API = runpy.run_path(str(APP / 'system.py'))
System, exclusive = API['System'], API['exclusive']
from information import Information, FORMAT, record_identity, pointer


class InformationChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='agent-information-check-')
        self.root = Path(self.temporary.name) / 'runtime'
        System.initialize(self.root, {'contribution': 'Inspect an evolving information interpretation without inventing an implementation or receiving use.'})
        self.system = System(self.root)
        self.lock = exclusive(self.system.ex.root / 'owner.lock')
        self.lock.__enter__()
        self.info = Information(self.system)
        self.system.put({'id': 'inspection-meaning', 'kind': 'content-contract',
                         'content': {'version': '1', 'meaning': 'complete means source inspection finished, not repair or uptake.'}}, 0)
        self.system.put({'id': 'observation', 'kind': 'inspection',
                         'content': {'status': 'complete', 'count': None, 'note': 'Source-inspected fixture.'}}, 0)
        self.original = self.system.show('observation')
        self.definition = self.system.show('inspection-meaning')

    def tearDown(self):
        self.lock.__exit__(None, None, None)
        self.temporary.cleanup()

    def wrap(self):
        return self.info.wrap({'source': record_identity(self.system.show('observation')),
                               'contract': record_identity(self.definition),
                               'account': 'Bind the authored observation to its exact first convention.'})

    def successor(self):
        self.system.put({'id': 'inspection-meaning', 'kind': 'content-contract',
                         'content': {'version': '2', 'meaning': 'Inspection, implementation and receiving use are separate observations; unassessed supplies no finding about existence.'}}, 1)
        return self.system.show('inspection-meaning')

    def migration(self):
        return {'source': record_identity(self.system.show('observation')),
                'contract': record_identity(self.system.show('inspection-meaning')),
                'payload': {'inspection': {'status': 'complete'}, 'count': None,
                            'implementation': {'status': 'unassessed'},
                            'receiving_use': {'status': 'unassessed'}},
                'preserve': [{'from': '/status', 'to': '/inspection/status'},
                             {'from': '/count', 'to': '/count'}],
                'change': 'Expose separate observations for the next maintenance consumer.',
                'unknowns': 'No implementation or receiving-use observation was supplied.'}

    def test_pinned_definition_survives_head_revision_and_reaches_real_context(self):
        self.wrap()
        wrapped = self.system.show('observation')
        old_pin = wrapped['content']['contract']
        self.successor()
        current = self.info.inspect('observation')
        self.assertEqual(current['definition'], self.definition)
        self.assertEqual(current['record']['content']['payload'], self.original['content'])
        self.system.context('old-meaning', {'records': ['observation']})
        original_context = self.system.recover('old-meaning')['context']
        self.assertIn('complete means source inspection finished', original_context)
        self.assertNotIn('Inspection, implementation and receiving use are separate', original_context)
        self.assertIn(old_pin['object'], original_context)
        self.info.migrate(self.migration())
        self.system.context('new-meaning', {'records': ['observation']})
        fresh = self.system.recover('new-meaning')['context']
        self.assertIn('Inspection, implementation and receiving use are separate', fresh)
        self.assertIn('unassessed', fresh)
        self.assertEqual(self.system.recover('old-meaning')['context'], original_context)
        self.assertEqual(self.info._object_record(self.system.show('observation')['content']['derivation']['source'])[0], wrapped)

    def test_source_and_target_changes_refuse_reviewed_migration(self):
        self.wrap()
        self.successor()
        proposal = self.migration()
        record = self.system.show('observation')
        replacement = {k: record[k] for k in ('id', 'kind', 'content', 'relations', 'active')}
        replacement['content'] = copy.deepcopy(record['content'])
        replacement['content']['payload']['note'] = 'Changed after proposal.'
        self.system.put(replacement, record['revision'])
        unchanged = self.system.record_path('observation').read_bytes()
        with self.assertRaisesRegex(ValueError, 'revision conflict'):
            self.info.migrate(proposal)
        self.assertEqual(self.system.record_path('observation').read_bytes(), unchanged)
        revised = self.migration()
        self.system.put({'id': 'inspection-meaning', 'kind': 'content-contract',
                         'content': 'A changed third convention, not the reviewed second one.'}, 2)
        with self.assertRaisesRegex(ValueError, 'revision conflict'):
            self.info.migrate(revised)
        self.assertEqual(self.system.record_path('observation').read_bytes(), unchanged)

    def test_preservation_does_not_invent_measurements_or_fulfillment(self):
        self.wrap()
        self.successor()
        valid = self.migration()
        unchanged = self.system.record_path('observation').read_bytes()
        zero = copy.deepcopy(valid)
        zero['payload']['count'] = 0
        with self.assertRaisesRegex(ValueError, 'preservation failed'):
            self.info.migrate(zero)
        fulfilled = copy.deepcopy(valid)
        fulfilled['payload']['inspection']['status'] = 'fulfilled'
        with self.assertRaisesRegex(ValueError, 'preservation failed'):
            self.info.migrate(fulfilled)
        missing = copy.deepcopy(valid)
        missing['preserve'] = [{'from': '/absent', 'to': '/count'}]
        with self.assertRaisesRegex(ValueError, 'pointer is absent'):
            self.info.migrate(missing)
        self.assertEqual(self.system.record_path('observation').read_bytes(), unchanged)
        self.info.migrate(valid)
        self.assertIsNone(self.system.show('observation')['content']['payload']['count'])
        self.assertEqual(pointer({'a/b': {'~key': [None]}}, '/a~1b/~0key/0'), None)

    def test_unknown_version_contract_tampering_and_silent_switch_are_refused(self):
        self.wrap()
        current = self.system.show('observation')
        base = {k: current[k] for k in ('id', 'kind', 'content', 'relations', 'active')}
        unknown = copy.deepcopy(base)
        unknown['content']['format'] = 'g60-information/99'
        with self.assertRaisesRegex(ValueError, 'unsupported information envelope'):
            self.system.put(unknown, current['revision'])
        removed = dict(base, content={'untyped': 'silently discarding the interpretation'})
        with self.assertRaisesRegex(ValueError, 'contract removal'):
            self.system.put(removed, current['revision'])
        self.successor()
        replacement = copy.deepcopy(base)
        replacement['content']['contract'] = self.info._contract_pin(record_identity(self.system.show('inspection-meaning')))
        replacement['content'].pop('derivation')
        with self.assertRaisesRegex(ValueError, 'explicit migrate'):
            self.system.put(replacement, current['revision'])
        key = current['content']['contract']['object']
        (self.system.ex.root / 'objects' / key).write_bytes(b'Altered definition fixture')
        with self.assertRaisesRegex(ValueError, 'content object changed'):
            self.info.inspect('observation')

    def test_builtin_records_and_unreviewed_basis_refresh_are_guarded(self):
        with self.assertRaisesRegex(ValueError, 'no information-envelope adapter'):
            self.info.wrap({'source': record_identity(self.system.show('goal')),
                            'contract': record_identity(self.definition), 'account': 'Unsupported goal wrapping.'})
        path = self.system.work / 'sources/evidence.txt'
        path.write_text('Source basis before the observation.')
        self.system.put({'id': 'observation', 'kind': 'inspection', 'content': self.original['content'],
                         'basis': ['sources/evidence.txt']}, 1)
        path.write_text('Source basis changed after the observation.')
        spec = {'source': record_identity(self.system.show('observation')),
                'contract': record_identity(self.definition), 'account': 'Interpret the retained observation.'}
        with self.assertRaisesRegex(ValueError, 'source basis changed'):
            self.info.wrap(spec)
        spec['basis'] = []
        spec['account'] = 'Explicitly preserve the old observation by its retained source occurrence; no new evidence claim.'
        self.info.wrap(spec)
        source, _ = self.info._object_record(self.system.show('observation')['content']['derivation']['source'])
        self.assertEqual(source['basis'][0]['sha256'], self.system.ex.put(b'Source basis before the observation.'))
        self.assertEqual(self.system.show('observation')['basis'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
