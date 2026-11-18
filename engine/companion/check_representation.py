#!/usr/bin/env python3
"""Narrow integration checks at the real native receiving/adoption boundary."""
import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest


arguments = argparse.ArgumentParser()
arguments.add_argument('--engine', required=True, type=Path, metavar='PROTOTYPE',
                       help='native prototype directory, for example engine/prototype')
args, rest = arguments.parse_known_args()
sys.path.insert(0, str(args.engine.resolve() / 'agent-system'))
from system import System
from representation import RepresentationOperations, OPERATION, encoded


class NativeRepresentationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'runtime'
        System.initialize(self.root, {'contribution': 'Exercise selected representation binding.'})
        self.system = System(self.root)
        self.adapter = RepresentationOperations(self.system)
        self.work = self.system.work
        self.source = self.work / 'sources/rule.txt'
        self.source.write_text('The nominated primary value can increase by one; the independent label is unaffected.\n')
        self.seed('seed-a', 'outputs/a.json', {'value': 1})
        self.seed('seed-b', 'outputs/b.json', {'label': 'old'})

    def tearDown(self):
        self.temp.cleanup()

    def candidate(self, name, value, json_body=True):
        path = self.base / name
        path.write_bytes(encoded(value) if json_body else value.encode())
        return path

    def seed(self, turn, target, value):
        self.system.context(turn, {'task': 'Accept the exact supplied test state.', 'targets': [target]})
        reply = self.candidate(turn + '.reply', 'Actual finite fixture acceptance.', False)
        candidate = self.candidate(turn + '.json', value)
        self.system.receive(turn, reply, 'actual integration fixture', [target + '=' + str(candidate)])
        self.system.adopt(turn, target, 'Accept this scoped state for the finite check.')

    def spec(self, turn, mode='transform'):
        return {'format': OPERATION, 'turn': turn, 'mode': mode,
                'task': 'Change only the nominated value, then bind the actual return.',
                'consumer': 'actual finite operation participant',
                'representations': [self.adapter.pin('outputs/a.json', 'seed-a')],
                'sources': [self.adapter.pin('sources/rule.txt')],
                'applications': [{'id': 'selected-use', 'sources': ['sources/rule.txt'],
                                  'representations': ['outputs/a.json'],
                                  'operation': 'Use the selected value as the starting operand.',
                                  'prediction': 'The candidate value is two; the separately held label remains unchanged.'}],
                'targets': ['outputs/result.txt' if mode == 'realize' else 'outputs/a.json'],
                'interactions': {'account': 'The finite fixture stipulates the separate label is not read or written.',
                                 'unknowns': ['No general semantic independence claim is made.']}}

    def witness(self, turn):
        witness = self.adapter.template(turn)
        target = 'outputs/result.txt' if 'realization' in witness else 'outputs/a.json'
        witness['applications'][0].update(account='The exact selected input supplied the operand.',
                                          outputs=[target])
        if 'realization' in witness:
            witness['realization']['account'] = 'The output displays the accepted value without altering it.'
        return witness

    def receive(self, turn, witness=None, realized=False):
        reply = self.candidate(turn + '.reply', 'Actual candidate returned for the fixture.', False)
        target = 'outputs/result.txt' if realized else 'outputs/a.json'
        candidate = self.candidate(turn + '.candidate', 'Value: 1\n' if realized else {'value': 2}, not realized)
        return self.adapter.receive(turn, reply, witness or self.witness(turn), 'actual integration fixture',
                                    [target + '=' + str(candidate)])

    def test_selected_accepted_state_and_application_are_actual_context_inputs(self):
        self.adapter.prepare(self.spec('actual-input'))
        _, request = self.system.ex.load('actual-input')
        paths = {r['path'] for r in request['materials']}
        self.assertIn('outputs/a.json', paths)
        self.assertIn('sources/rule.txt', paths)
        self.assertIn('representation-operations/actual-input/operation.json', paths)
        self.assertNotIn('outputs/b.json', paths)
        self.receive('actual-input')
        self.adapter.adopt('actual-input', 'outputs/a.json', 'Accept the actual scoped increment.')
        self.assertEqual(json.loads((self.work / 'outputs/a.json').read_text()), {'value': 2})

    def test_stale_state_return_is_retained_but_native_adoption_refuses(self):
        self.adapter.prepare(self.spec('stale-state'))
        self.seed('changed-a', 'outputs/a.json', {'value': 9})
        self.receive('stale-state')
        with self.assertRaisesRegex(ValueError, 'declared basis changed'):
            self.adapter.adopt('stale-state', 'outputs/a.json', 'This stale candidate must fail.')
        self.assertIsNotNone(self.system.ex.inspect('stale-state')['return'])
        self.assertEqual(json.loads((self.work / 'outputs/a.json').read_text()), {'value': 9})

    def test_unrelated_accepted_change_does_not_invalidate_selected_scope(self):
        self.adapter.prepare(self.spec('scoped'))
        self.seed('changed-b', 'outputs/b.json', {'label': 'new'})
        self.receive('scoped')
        self.adapter.adopt('scoped', 'outputs/a.json', 'The actual independent label change leaves this selected basis current.')
        self.assertEqual(json.loads((self.work / 'outputs/a.json').read_text()), {'value': 2})
        self.assertEqual(json.loads((self.work / 'outputs/b.json').read_text()), {'label': 'new'})

    def test_missing_application_or_inconsistent_basis_is_refused(self):
        bad = self.spec('bad-base'); bad['representations'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'input basis changed'):
            self.adapter.prepare(bad)
        self.adapter.prepare(self.spec('missing-app'))
        witness = self.witness('missing-app'); witness['applications'] = []
        with self.assertRaisesRegex(ValueError, 'source-application witness'):
            self.receive('missing-app', witness)
        inconsistent = self.witness('missing-app'); inconsistent['representations'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'representation basis is inconsistent'):
            self.receive('missing-app', inconsistent)
        self.assertIsNone(self.system.ex.inspect('missing-app')['return'])

    def test_missing_realization_binding_is_refused_and_complete_binding_is_retained(self):
        missing = self.spec('no-input', mode='realize'); missing['representations'] = []
        with self.assertRaisesRegex(ValueError, 'requires an accepted representation binding'):
            self.adapter.prepare(missing)
        self.adapter.prepare(self.spec('realize', mode='realize'))
        witness = self.witness('realize'); del witness['realization']
        with self.assertRaisesRegex(ValueError, 'realization lacks'):
            self.receive('realize', witness, realized=True)
        self.receive('realize', realized=True)
        self.adapter.adopt('realize', 'outputs/result.txt', 'Accept the displayed accepted state.')
        self.assertEqual((self.work / 'outputs/result.txt').read_text(), 'Value: 1\n')

    def test_plain_native_return_cannot_claim_adapter_binding(self):
        self.adapter.prepare(self.spec('unbound'))
        reply = self.candidate('plain.reply', 'A returned proposal alone has no representation application witness.', False)
        candidate = self.candidate('plain.json', {'value': 2})
        self.system.receive('unbound', reply, 'actual integration fixture', ['outputs/a.json=' + str(candidate)])
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            self.adapter.adopt('unbound', 'outputs/a.json', 'This unbound return must fail.')


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], *rest], verbosity=2)
