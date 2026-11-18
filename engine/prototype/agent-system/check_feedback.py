#!/usr/bin/env python3
"""Exercise native feedback, measured evidence, receiving work and historical custody."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

from system import System
from continuation_support.collector import Collector
from feedback import Feedback
from information import record_identity


class FeedbackChecks(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'state'
        System.initialize(self.root, {'contribution': 'Construct a useful reading for the operator.'}, record_retention='append-history')
        self.system = System(self.root)
        self.feedback = Feedback(self.system)
        self.candidate = self.put('candidate', 'Use the upper scale.')
        self.outside = self.put('instrument', 'Bench instrument')
        self.intent = self.put('operator', 'Operator needs a reading within tolerance.')
        self.plan = dict(candidate=self.candidate, outside=self.outside, references=[self.intent],
                         forecast='Operator reads within tolerance.', rival='A second scale induces error.')

    def put(self, name, content, revision=0, basis=None):
        row = self.system.put(dict(id=name, kind='source', content=content, basis=basis or []), revision)
        return record_identity(row)

    def apply(self, action, revision, **data):
        return self.feedback.apply(action, dict(id='trial', expected=revision, **data))

    def open(self, **changes):
        return self.apply('open', 0, scope='Upper scale at bench one.', **(self.plan | changes))

    def observation(self, name='reading', roles=('outside', 'intended'), subject=None, basis=None):
        subject = self.candidate if subject is None else subject
        sources = self.feedback.roles(self.system.show('trial')['content'])
        exercised = [dict(role=role, reference=record_identity(pin), observed='Recorded ' + role + ' effect.')
                     for (role, _), pin in sources.items() if role in roles]
        evidence = self.put(name, dict(subject=subject, observed='Reading at bench one was 103.',
                                       exercised=exercised), basis=basis)
        witnesses = [dict(role=r['role'], reference=r['reference']['id'], account='Linked the measured effect.')
                     for r in exercised]
        return dict(subject=subject, observation=evidence, interpretation='Scale orientation explains the difference.',
                    route='review', next='Compare the revised instruction with the scale.', witnesses=witnesses)

    def test_resuming_one_native_turn_cannot_launch_another_pending_worker(self):
        argv=[sys.executable,'-c','print("received")','{context}']
        self.system.context('old-job', {})
        with patch.object(Collector, 'resume', side_effect=RuntimeError('interrupted before launch')):
            with self.assertRaisesRegex(RuntimeError, 'before launch'):
                self.system.run('old-job',argv)
        self.open()
        carrier=Collector(self.system.ex.root/'carrier')
        with self.assertRaisesRegex(ValueError, 'native runtime'):
            carrier.resume()
        with self.assertRaisesRegex(ValueError,'routed feedback changed'):
            self.system.run('old-job',argv)
        self.system.context('current-job', {})
        dispatch=self.system.run('current-job',argv)
        self.assertEqual([row['job_id'] for row in dispatch['dispatches']],['exchange-current-job'])
        self.system.receive('current-job',None,'test worker',[],wait=5)
        self.assertEqual(carrier.views(job_id='exchange-current-job')[0]['status'],'completed')
        self.assertEqual(carrier.views(job_id='exchange-old-job')[0]['status'],'queued')

    def test_rehearsal_changes_receiving_work_then_obtains_outside(self):
        self.open(outside=None, references=[])
        returned = self.observation(roles=())
        self.apply('observe', 1, **returned)
        with self.assertRaisesRegex(ValueError, 'rehearsal'):
            self.apply('accept', 2, owner='operator', account='Review complete.')
        parsed = self.system.context('repair', {'task': 'Reconsider the instruction.'})
        self.assertIn(returned['next'], Path(parsed['context']).read_text())
        self.assertEqual('rehearsal', self.system.view('repair')['feedback_uses'][0]['channel'])
        self.apply('revise', 2, reason='Instrument is available now.', **self.plan)
        self.assertEqual('outside-bound', self.feedback.inspect('trial')['route']['channel'])
        self.assertEqual('reading', self.feedback.inspect('trial', 2)['record']['content']['returns'][0]['observation']['id'])

    def test_subject_forecast_comparison_and_conflict(self):
        with self.assertRaisesRegex(ValueError, 'differ'):
            self.open(rival=self.plan['forecast'])
        self.open()
        returned = self.observation(subject=self.intent)
        with self.assertRaisesRegex(ValueError, 'wrong observation subject'):
            self.apply('observe', 1, **returned)
        with self.assertRaisesRegex(ValueError, 'does not bind'):
            self.apply('observe', 1, **(returned | {'subject': self.candidate}))
        with self.assertRaisesRegex(ValueError, 'revision conflict'):
            self.apply('observe', 0, **returned)

    def test_owner_and_separate_witnesses_including_shared_exemplar(self):
        self.open(references=[self.outside])
        partial = self.observation(roles=['outside'])
        fake = dict(reference=self.outside['id'], role='intended', account='Unexercised.')
        with self.assertRaisesRegex(ValueError, 'not exercised'):
            self.apply('observe', 1, **(partial | {'witnesses': [fake]}))
        self.apply('observe', 1, **partial)
        with self.assertRaisesRegex(ValueError, 'missing separate witness'):
            self.apply('accept', 2, owner='operator', account='Missing desired-use exercise.')
        third = self.observation('second', roles=['intended'])
        self.apply('observe', 2, **(third | {'interpretation': 'A third explanation is parallax.'}))
        with self.assertRaisesRegex(ValueError, 'accepting owner'):
            self.apply('accept', 3, owner='', account='Unattributed.')
        self.apply('accept', 3, owner='bench operator', account='Scale and reading meet this shift’s tolerance.')
        self.assertEqual('accepted', Feedback(System(self.root)).inspect('trial')['route']['state'])

    def test_measured_file_changes_block_stale_dispatch_and_adoption(self):
        self.open()
        path = self.system.work / 'sources/meter.txt'
        path.write_text('103\n')
        evidence = self.observation(basis=['sources/meter.txt'])
        self.apply('observe', 1, **evidence)
        self.apply('accept', 2, owner='operator', account='Observed on the meter.')
        self.system.context('selected', {'feedback_uses': ['trial'], 'targets': ['outputs/result.txt']})
        frozen = self.system.recover('selected')['context']
        self.assertIn('103', frozen)
        reply = self.root.parent / 'reply.txt'
        reply.write_text('Read 103 on the upper scale.\n')
        self.system.receive('selected', reply, 'fixture operator', ['outputs/result.txt=' + str(reply)])
        self.system.context('dispatch', {'feedback_uses': ['trial']})
        path.write_text('107\n')
        self.assertEqual(['reading'], self.feedback.inspect('trial')['route']['stale'])
        with self.assertRaisesRegex(ValueError, 'routed feedback changed'):
            self.system.adopt('selected', 'outputs/result.txt', 'No longer current.')
        with self.assertRaisesRegex(ValueError, 'routed feedback changed'):
            self.system.run('dispatch', ['unused', '{context}'])
        self.system.context('revisit', {})
        self.assertIn('Revisit changed bindings (reading)', self.system.recover('revisit')['context'])
        self.assertEqual(frozen, self.system.recover('selected')['context'])
        self.assertFalse((self.system.work / 'outputs/result.txt').exists())

    def test_candidate_replacement_retains_independent_duties_and_history(self):
        self.open()
        self.apply('observe', 1, **self.observation())
        changed = self.put('candidate', 'Use the lower scale.', 1)
        new_plan = self.plan | {'candidate': changed}
        self.assertEqual('stale', self.feedback.inspect('trial')['route']['state'])
        with self.assertRaisesRegex(ValueError, 'retain separately owed'):
            self.apply('revise', 2, reason='Replace the candidate.', **(new_plan | {'references': []}))
        self.apply('revise', 2, reason='Replace the candidate.', **new_plan)
        with self.assertRaisesRegex(ValueError, 'review route'):
            self.apply('accept', 3, owner='operator', account='Old witness cannot accept new candidate.')
        self.assertEqual([self.intent['id']], [r['id'] for r in self.system.show('trial')['content']['references']])
        self.assertEqual('reading', self.feedback.inspect('trial', 2)['record']['content']['returns'][0]['observation']['id'])
        release = dict(role='intended', reference=self.intent['id'], owner='operator', account='Instrument no longer serves this shift.')
        self.apply('revise', 3, reason='Shift changed.', releases=[release], **(new_plan | {'references': []}))
        self.assertEqual([release], self.system.show('trial')['content']['releases'])
        with self.assertRaisesRegex(ValueError, 'through feedback'):
            self.system.put(dict(id='trial', kind='note', content='accept'), 4)

    def test_multiple_adverse_returns_need_individual_reconciliation(self):
        self.open()
        first = self.observation(roles=['outside'])
        self.apply('observe', 1, **(first | {'route': 'work'}))
        self.apply('observe', 2, **(first | {'route': 'hold', 'interpretation': 'Calibration is also unresolved.'}))
        second = self.observation('second', roles=['intended'])
        self.assertEqual([1, 2], self.feedback.inspect('trial')['route']['unresolved_returns'])
        with self.assertRaisesRegex(ValueError, 'unresolved adverse returns: 1, 2'):
            self.apply('observe', 3, **second)
        resolutions = [{'return_number': 1, 'account': 'The owner verified the orientation marking.'},
                       {'return_number': 2, 'account': 'The operator verified that calibration is in date.'}]
        with self.assertRaisesRegex(ValueError, 'unresolved adverse returns: 2'):
            self.apply('observe', 3, **second, resolutions=resolutions[:1])
        self.apply('observe', 3, **second, resolutions=resolutions)
        self.apply('accept', 4, owner='operator', account='Both issues resolved, reading and intended use checked.')
        current = self.feedback.inspect('trial')
        self.assertEqual(resolutions, current['record']['content']['returns'][-1]['resolutions'])
        self.assertEqual([], current['route']['unresolved_returns'])

    def test_adverse_return_survives_multiple_revisions_without_reusing_old_witnesses(self):
        self.open()
        failure = self.observation('failed')
        self.apply('observe', 1, **(failure | {'route': 'hold'}))
        self.apply('revise', 2, reason='Rerun the original plan.', **self.plan)
        new_candidate = self.put('candidate', 'Different scale.', 1)
        revised = self.plan | {'candidate': new_candidate}
        self.apply('revise', 3, reason='Use a corrected candidate.', **revised)
        head = self.feedback.inspect('trial')
        self.assertEqual(head['route']['unresolved_returns'], [1])
        self.assertEqual(head['record']['content']['carried_returns'][0]['observation']['id'], 'failed')
        # An old observation may change after revision. Its originally pinned
        # adverse account survives without becoming evidence for the new plan.
        self.put('failed', 'A subsequent measurement.', 1)
        self.assertEqual(self.feedback.inspect('trial')['route']['stale'], [])
        partial = self.observation('later', roles=['outside'], subject=new_candidate)
        with self.assertRaisesRegex(ValueError, 'unresolved adverse returns: 1'):
            self.apply('observe', 4, **partial)
        self.apply('observe', 4, **(partial | {'route': 'work'}))
        self.assertEqual(self.feedback.inspect('trial')['route']['unresolved_returns'], [1, 2])
        self.apply('revise', 5, reason='Improve the measurement.', **revised)
        partial = self.observation('fresh', roles=['outside'], subject=new_candidate)
        self.apply('observe', 6, **partial, resolutions=[
            {'return_number': 1, 'account': 'Corrected and tried the first scale.'},
            {'return_number': 2, 'account': 'Checked the measurement again.'}])
        with self.assertRaisesRegex(ValueError, 'missing separate witness'):
            self.apply('accept', 7, owner='operator', account='Only the outside role has fresh coverage.')
        self.apply('observe', 7, **self.observation('end-user', roles=['intended'], subject=new_candidate))
        self.apply('accept', 8, owner='operator', account='Received here on the new scale.')
        self.assertEqual(self.feedback.inspect('trial')['route']['unresolved_returns'], [])
        self.apply('revise', 9, reason='Next plan after accounted resolution.', **revised)
        self.assertEqual(self.feedback.inspect('trial')['record']['content']['carried_returns'], [])

    def test_current_repair_can_resolve_one_carried_return_while_adding_another(self):
        self.open()
        self.apply('observe', 1, **(self.observation('old-meter') | {'route': 'hold'}))
        self.apply('observe', 2, **(self.observation('old-label') | {'route': 'work'}))
        changed = self.put('candidate', 'Move the label.', 1)
        self.apply('revise', 3, reason='Change the label on this instrument.', **(self.plan | {'candidate': changed}))
        repaired = self.observation('new-label', roles=['outside'], subject=changed)
        one = dict(return_number=2, account='The label is readable on this revision.')
        with self.assertRaisesRegex(ValueError, 'unknown or duplicate'):
            self.apply('observe', 4, **(repaired | {'route': 'work'}), resolutions=[one, one])
        self.assertEqual([1, 2], self.feedback.inspect('trial')['route']['unresolved_returns'])
        new = self.apply('observe', 4, **(repaired | {'route': 'work',
                         'interpretation': 'Label is legible; zero adjustment slips.'}), resolutions=[one])
        self.assertEqual(new['route']['unresolved_returns'], [1, 3])
        self.assertEqual('work', new['route']['state'])
        self.assertTrue(new['route']['has_unresolved_hold'])
        self.assertEqual('new-label', self.system.show('trial')['content']['returns'][0]['observation']['id'])
        with self.assertRaisesRegex(ValueError, 'review route'):
            self.apply('accept', 5, owner='operator', account='Only the label was checked.')
        self.apply('revise', 5, reason='Forecast another trial.', **(self.plan | {'candidate': changed}))
        self.assertEqual([1, 3], Feedback(System(self.root)).inspect('trial')['route']['unresolved_returns'])
        revised_route = self.feedback.inspect('trial')['route']
        self.assertEqual(('open', True), (revised_route['state'], revised_route['has_unresolved_hold']))
        historical = self.feedback.inspect('trial', 3)['record']['content']
        self.assertEqual([1, 2], sorted(self.feedback.outstanding(historical)))

    def test_progress_only_keeps_the_hold_without_creating_a_replacement_duty(self):
        self.open()
        self.apply('observe', 1, **(self.observation('scale') | {'route': 'work'}))
        self.apply('observe', 2, **(self.observation('door') | {'route': 'hold'}))
        result = self.observation('scale-retest', roles=['outside']) | {'route': 'progress'}
        one = dict(return_number=1, account='Scale is back in tolerance.')
        two = dict(return_number=2, account='An operator gained access.')
        for unsupported in ([], [one, two]):
            with self.assertRaisesRegex(ValueError, 'progress must'):
                self.apply('observe', 3, **result, resolutions=unsupported)
        route = self.apply('observe', 3, **result, resolutions=[one])['route']
        self.assertEqual((route['state'], route['unresolved_returns']), ('hold', [2]))
        with self.assertRaisesRegex(ValueError, 'unresolved adverse returns: 2'):
            self.apply('observe', 4, **self.observation('unresolved'))
        self.apply('observe', 4, **self.observation('open-door'), resolutions=[two])
        self.apply('accept', 5, owner='operator', account='Gained access and took a valid reading.')
        self.assertEqual('accepted', self.feedback.inspect('trial')['route']['state'])

    def test_progress_follows_the_surviving_disposition_across_a_revision(self):
        self.open()
        for revision, (name, route) in enumerate([('need-scale', 'work'), ('locked-door', 'hold'),
                                                 ('need-label', 'work')], 1):
            self.apply('observe', revision, **(self.observation(name) | {'route': route}))
        self.apply('revise', 4, reason='Make another attempt.', **self.plan)
        reading = self.observation('scale-fixed') | {'route': 'progress'}
        held = self.apply('observe', 5, **reading, resolutions=[dict(return_number=1, account='Scale works.')])
        self.assertEqual((held['route']['state'], held['route']['unresolved_returns']), ('hold', [2, 3]))
        reading = self.observation('granted-entry') | {'route': 'progress'}
        work = self.apply('observe', 6, **reading, resolutions=[dict(return_number=2, account='Entry restored.')])
        self.assertEqual((work['route']['state'], work['route']['unresolved_returns']), ('work', [3]))
        self.assertFalse(work['route']['has_unresolved_hold'])

    def test_general_scoped_and_independent_production_guards(self):
        self.open()
        self.apply('observe', 1, **self.observation())
        self.apply('accept', 2, owner='operator', account='Checked on the instrument.')
        self.feedback.apply('open', dict(id='other', expected=0, scope='Independent reading.', **self.plan))
        reply = self.root.parent / 'memo.txt'
        reply.write_text('A new operator memo.')
        for turn, selectors in [('general', {}), ('chosen', {'feedback_uses': ['other']}),
                                ('independent', {'feedback_uses': []})]:
            target = f'outputs/{turn}.txt'
            self.system.context(turn, dict(targets=[target], **selectors))
            self.system.receive(turn, reply, 'fixture operator', [target + '=' + str(reply)])
        self.assertNotIn('trial', [r['id'] for r in self.system.view('general').get('feedback_uses', [])])
        self.assertIn('trial', self.system.view('general')['feedback_guard']['uses'])
        self.assertEqual({}, self.system.view('independent')['feedback_guard']['uses'])
        self.put('reading', {'observed': 'Calibration changed.'}, 1)
        with self.assertRaisesRegex(ValueError, 'routed feedback changed'):
            self.system.adopt('general', 'outputs/general.txt', 'Would use the superseded reading.')
        for turn in ('chosen', 'independent'):
            target = f'outputs/{turn}.txt'
            self.system.adopt(turn, target, 'Authorized an independent memo.')
            self.assertEqual(reply.read_text(), (self.system.work / target).read_text())
        self.system.context('broad', {})
        self.feedback.apply('open', dict(id='new-use', expected=0, scope='New scope.', **self.plan))
        with self.assertRaisesRegex(ValueError, 'routed feedback changed'):
            self.system.run('broad', ['unused', '{context}'])


if __name__ == '__main__':
    unittest.main()
