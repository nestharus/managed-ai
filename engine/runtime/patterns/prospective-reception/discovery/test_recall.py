import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from turn import encode, run


class RecallTest(unittest.TestCase):
    def test_fresh_process_retrieves_returns_and_requires_new_qualifying_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root/'state'
            spec = root/'spec'
            (root/'partial').write_text('provisional')
            (root/'prior').write_text('a versioned datum')
            (root/'evidence').write_text('The fracture is at the seat of the aperture.\n')
            (root/'owner').write_text('weekday')
            plan = dict(target='record wear',gap='lifetime',prediction='can remain in use',rival='must retire',
                        partial='partial',query='durability')
            def invoke(action, obj):
                spec.write_bytes(encode(obj))
                return run(db, action, spec)['id']
            def search(prospect, k=3):
                output = subprocess.check_output([sys.executable,'-B', Path(__file__).with_name('recall.py'),db,prospect,'--limit',str(k)])
                return json.loads(output)
            first=invoke('open',plan)
            primary=dict(name='item',decision='assess',authority='owner')
            secondary=dict(name='station',decision='inspect',authority='owner')
            issue=invoke('issue',dict(prospect=first,sources=[dict(role='mark',path='prior')],uses=[primary,secondary]))
            def obs(issue, use, verdict):
                return invoke('observe',dict(issue=issue,use=use,verdict=verdict,result='owner reading',evidence='evidence'))
            original=obs(issue,'item','supports')
            peer=obs(issue,'station','supports')
            changed=obs(issue,'item','contradicts')
            invoke('issue',dict(prospect=first,parent=original,relation='reopen',because='new condition',
                                sources=[dict(role='new condition',path='prior')],uses=[primary]))
            (root/'partial').write_text('new prospect in another department')
            fresh=invoke('open',{**plan,'query':'aperture','target':'estimate replacements'})
            candidates=search(fresh)
            self.assertEqual(candidates['observations'],3)
            prior=next(row for row in candidates['candidates'] if row['verdict']=='supports' and row['use']=='item')
            self.assertEqual(prior['observation'],original)
            self.assertFalse(prior['same_partial'])
            self.assertEqual(prior['recorded_branches'],['reopen'])
            self.assertEqual(prior['later_different_verdict'],{'observation':changed,'verdict':'contradicts'})
            self.assertEqual(prior['other_observed_uses_of_same_evidence'],[dict(observation=peer,use='station')])
            self.assertEqual(prior['evidence']['sha256'],run(db,'show',original)['evidence']['sha256'])
            self.assertIn('aperture',prior['lexical_match'])
            self.assertEqual(search(fresh,1)['matches_unissued'],2)
            self.assertEqual(search(fresh)['new_records_indexed'],0)
            transfer=invoke('transfer',dict(parent=prior['observation'],prospect=fresh,use=secondary,
                warrant='same failure site',challenge='different assembly changes replacement need'))
            handoff = run(db, 'show', transfer)
            self.assertEqual(handoff['later_different_verdicts'], [prior['later_different_verdict']])
            self.assertEqual(handoff['previous_result']['verdict'], 'supports')
            self.assertNotIn('verdict', handoff)
            self.assertEqual(search(fresh)['observations'],3)
            obs(transfer,'station','unresolved')
            self.assertEqual(search(fresh)['observations'],4)
            pending = obs(issue,'item','unresolved')
            another = invoke('transfer',dict(parent=original,prospect=fresh,use=secondary,
                warrant='same failure site',challenge='different assembly changes replacement need'))
            self.assertEqual(run(db,'show',another)['later_different_verdicts'],
                [dict(observation=pending,verdict='unresolved'), dict(observation=changed,verdict='contradicts')])
            self.assertEqual(run(db,'show',transfer),handoff)
            miss=invoke('open',{**plan,'query':'unmatchablexyz'})
            self.assertEqual(search(miss)['candidates'],[])
