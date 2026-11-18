import json
import os
import tempfile
import unittest
from pathlib import Path

from turn import MAX_PACKET, run


class TurnTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / 'journal.sqlite'
        for name in ('partial', 'source', 'authority', 'evidence'):
            (self.root / name).write_bytes((name + '\r\n').encode())
        self.plan = dict(target='Choose the transfer time', gap='Unknown cutover convention',
                         prediction='Second reader advances', rival='Second reader holds',
                         query='receiving local use', partial='partial')
        self.use = dict(name='first', decision='advance or hold', authority='authority')
        self.opened = self.act('open', **self.plan)
        self.packet = dict(prospect=self.opened['id'], sources=[dict(role='basis', path='source')], uses=[self.use])

    def act(self, action, **spec):
        path = self.root / 'spec.json'
        path.write_text(json.dumps(spec))
        return run(self.state, action, path)

    def observation(self, issued, name, verdict='supports'):
        return self.act('observe', issue=issued['id'], use=name, evidence='evidence', result='Reader holds at cutoff', verdict=verdict)

    def test_open_freezes_partial_before_source_or_lookup(self):
        (self.root / 'partial').write_text('changed after request')
        issue = self.act('issue', **self.packet)
        self.assertEqual(issue['partial']['text'], 'partial\r\n')
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.act('issue', **{**self.packet, 'prospect': 'unseen'})
        with self.assertRaisesRegex(ValueError, 'expected observation'):
            self.act('issue', **self.packet, parent=issue['id'], relation='reuse', because='same')

    def test_different_receivers_do_not_inherit_outcomes(self):
        first = self.act('issue', **self.packet)
        observed = self.observation(first, 'first')
        second = dict(name='second', decision='deliver or retain', authority='authority')
        params = dict(parent=observed['id'], prospect=self.opened['id'], use=second,
                      warrant='Both receivers use the same cutoff', challenge='Delivery uses a different clock')
        (self.root / 'source').unlink()
        carried = self.act('transfer', **params)
        self.assertEqual(carried['sources'], first['sources'])
        self.assertNotIn('verdict', carried)
        self.assertEqual(carried['previous_result']['use'], 'first')
        with self.assertRaisesRegex(ValueError, 'not issued'):
            self.observation(carried, 'first')
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.act('transfer', **params, roles=['wrong'])
        self.assertEqual(self.observation(carried, 'second', 'contradicts')['verdict'], 'contradicts')
        (self.root / 'authority').write_text('new mandate')
        changed = self.act('transfer', **params)
        self.assertTrue(changed['authority_changed'])

        page = self.act('catchup', parent=observed['id'], scan_limit=1)
        self.assertFalse(page['complete'])
        # A new source result lies beyond the already pinned local cut.
        late = self.observation(first, 'first', 'contradicts')
        while not page['complete']:
            page = self.act('catchup', **page['next'])
            self.assertEqual(page['observations'], [])  # other original use/issue only
        fresh = self.act('catchup', parent=observed['id'])
        self.assertEqual([r['observation'] for r in fresh['observations']], [late['id']])
        self.assertEqual(fresh['observations'][0]['evidence_sha256'], late['evidence']['sha256'])

    def test_independent_branch_and_authority_change(self):
        first = self.act('issue', **self.packet)
        parent = self.observation(first, 'first')['id']
        spec = dict(**self.packet, parent=parent, because='new instruction')
        self.assertEqual(self.act('issue', **spec, relation='reuse')['relation'], 'reuse')
        (self.root / 'authority').write_text('new mandate')
        with self.assertRaisesRegex(ValueError, 'authority'):
            self.act('issue', **spec, relation='reuse')
        for relation in ('split', 'reopen'):
            self.assertEqual(self.act('issue', **spec, relation=relation)['parent'], parent)
        other = self.observation(first, 'first', 'unresolved')
        with self.assertRaisesRegex(ValueError, 'prior result'):
            self.act('issue', **{**spec, 'parent': other['id']}, relation='reuse')

    def test_partial_projection_keeps_full_owner_and_limits_context(self):
        data = b'Z' * 100000 + b'\r\n\xc3\xa9'
        (self.root / 'source').write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'oversized'):
            self.act('issue', **self.packet)
        def source(start, end):
            return [dict(role='cutoff', path=dict(path='source', start=start, end=end))]
        issued = self.act('issue', **{**self.packet, 'sources': source(100000, len(data))})
        selection = issued['sources'][0]
        self.assertEqual(selection['text'].encode(), data[100000:])
        (self.root / 'source').unlink()
        self.assertEqual(run(self.state, 'recover', selection['owner_sha256']), data)
        self.assertLess(len(json.dumps(issued)), 2000)
        (self.root / 'source').write_bytes(data)
        with self.assertRaises(UnicodeDecodeError):
            self.act('issue', **{**self.packet, 'sources': source(len(data)-1, len(data))})
        with self.assertRaisesRegex(ValueError, '1..3'):
            self.act('issue', **{**self.packet, 'sources': self.packet['sources'] * 4})
        with self.assertRaisesRegex(ValueError, 'packet byte budget'):
            self.act('issue', **{**self.packet, 'sources': source(0, MAX_PACKET)})

    def test_wider_field_not_echoed_through_partial_source_or_authority(self):
        first = b'Stop when either reader sees an empty slot.\n'
        owner = first + b'\n' + b'\n'.join(f'CUE-{i:03}-omitted'.encode() for i in range(110))
        (self.root / 'field').write_bytes(owner)
        view = dict(path='field', start=0, end=len(first))
        plan = self.act('open', **{**self.plan, 'partial': view})
        packet = self.act('issue', prospect=plan['id'], sources=[dict(role='local field', path=view)],
                          uses=[{**self.use, 'authority': view}])
        for record in (plan, packet):
            self.assertNotIn('omitted', json.dumps(record))
            self.assertIn(first.decode(), record['partial']['text'])
        self.assertEqual(run(self.state, 'recover', packet['partial']['owner_sha256']), owner)

    @unittest.skipUnless(os.environ.get('G60_READER'), 'set G60_READER to exercise a release reader')
    def test_real_knowledge_reader_and_pin(self):
        import subprocess
        import sys
        reader = os.environ['G60_READER']
        found = self.act('lookup', prospect=self.opened['id'], reader=reader)
        self.assertGreater(found['coverage']['examined_bodies'], len(found['bodies']))
        pin = found['bodies'][0]['pin']
        body = subprocess.check_output([sys.executable, '-B', reader, 'show', pin['id'], '--text'])
        (self.root / 'source').write_bytes(body)
        entries = [dict(role='nominated route', path=dict(path='source', start=0, end=16), identity=pin['id'])]
        spec = {**self.packet, 'sources': entries, 'nominations': found['id']}
        self.assertEqual(self.act('issue', **spec)['sources'][0]['pin'], pin)
        (self.root / 'source').write_bytes(body + b'changed')
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.act('issue', **spec)
        new = self.act('open', **{**self.plan, 'prediction': 'another decision'})
        with self.assertRaisesRegex(ValueError, 'different prospect'):
            self.act('issue', **{**spec, 'prospect': new['id']})


if __name__ == '__main__':
    unittest.main()
