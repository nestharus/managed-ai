"""Duties and the proposal they qualify have different lifetimes."""
import tempfile
import sys
import unittest
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from managed_ai.collective import Collective, pin


class StagedReceiving(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'station.sqlite'
        Collective.initialize(self.path)
        self.db = Collective(self.path, clock=lambda: 1.0)
        self.seq = 0

    def call(self, actor, op, **args):
        self.seq += 1
        return self.db.call(dict(actor=actor, command=str(self.seq), op=op, **args))

    def accepted(self, actor, op, **args):
        result = self.call(actor, op, **args)
        self.assertEqual('accepted', result['status'], result)
        return result

    def target(self, ident):
        return self.accepted('owner', 'create', id=ident, body={}, reconsider_from=['watcher'])['artifact']

    def propose(self, target, content, sources=()):
        ctx = self.accepted('owner', 'capture', target=target, id=f'c{self.seq}', staged=True,
                            sources=list(sources))['context']
        return self.accepted('owner', 'propose', id=f'p{self.seq}', context=ctx['id'], body=content)['proposal']['id']

    def offer(self, target, basis=()):
        return self.accepted('watcher', 'offer', target=target, basis=list(basis), body='Review this fitting.',
                             account='The source inspection found a clearance question.')['notice']

    def review(self, proposal, notice, basis, **changes):
        request = dict(actor='owner', op='review', command=str(self.seq+1), proposal=proposal, notice=notice,
                       basis=basis, disposition='incorporated', account='Width changed in this drawing.')
        self.seq += 1
        return self.db.call(dict(request, **changes))

    def test_129_then_a_new_arrival_no_partial_publication(self):
        initial = self.target('drawing')
        others = [self.offer('drawing') for _ in range(129)]
        chosen = self.propose('drawing', {'width': 12})
        rival = self.propose('drawing', {'width': 16})
        for no in others:
            self.assertEqual(self.db.next_review(chosen)['notice'], no)
            self.assertEqual(self.review(chosen, no, [])['status'], 'accepted')
        self.assertEqual(self.db.next_review(rival)['notice'], others[0])
        self.assertIsNone(self.db.next_review(chosen)['notice'])
        late = self.offer('drawing')
        self.assertEqual('reconsideration-required',
                         self.call('owner', 'publish', proposal=chosen, account='First try')['code'])
        self.assertEqual(initial, pin(self.db.show('drawing')))
        self.assertTrue(all(self.db.notice(no)['state']=='pending' for no in others+[late]))
        self.review(chosen, late, [])
        sent = self.accepted('owner', 'publish', proposal=chosen, account='Receiver validated this issue.')
        self.assertEqual(sent['staged_received'], 130)
        receipts = [self.db.notice(no)['receipt'] for no in others+[late]]
        self.assertTrue(all(r['artifact']==sent['artifact'] and r['proposal']==chosen for r in receipts))
        self.assertEqual([r['notice'] for r in receipts], others+[late])
        repeat = self.db.call(dict(actor='owner', command=sent['command'], op='publish', proposal=chosen,
                                   account='Receiver validated this issue.'))
        self.assertEqual(sent, repeat)
        self.assertEqual('target-changed', self.call('owner','publish',proposal=rival,account='Stale sibling')['code'])

    def test_original_parts_and_current_pins_must_both_be_addressable(self):
        self.target('drawing')
        pins = [self.accepted('lab', 'create', id=f'scan-{i}', body={'data':'×'*130995,'zone':i})['artifact']
                for i in range(8)]
        no = self.offer('drawing', pins)
        self.assertEqual('context-bound', self.call('owner','capture',id='whole',target='drawing')['code'])
        proposal = self.propose('drawing', {'safe': False})
        self.assertEqual('incomplete-basis', self.review(proposal,no,pins[:-1])['code'])
        self.assertEqual('wrong-owner', self.review(proposal,no,pins,actor='lab')['code'])
        self.target('sibling')
        wrong = self.propose('sibling', {})
        self.assertEqual('wrong-receiving-target',self.review(wrong,no,pins)['code'])
        for i, p in enumerate(pins):
            observed = self.db.show(p['id'], p['revision'], content=True)
            self.assertEqual(p, pin(observed))
            self.assertEqual(observed['body']['zone'], i)
        before = self.review(proposal, no, pins)
        self.assertEqual('accepted', before['status'])
        request = dict(actor='owner', command=before['command'], op='review', proposal=proposal, notice=no,
                       basis=pins,disposition='incorporated',account='Width changed in this drawing.')
        reopened = Collective(self.path)
        self.assertEqual(reopened.call(request), before)
        self.assertEqual('pending', reopened.notice(no)['state'])
        self.assertEqual('accepted', self.call('owner','publish',proposal=proposal,account='Cut after checking.')['status'])

    def test_stale_review_during_publish_and_at_point_of_use(self):
        self.target('station')
        self.target('jig')
        self.target('calibration')
        self.accepted('shop', 'create', id='drawing', body={'width':16})
        no = self.offer('station', [pin(self.db.show('calibration'))])
        approval = dict(format='g60-receiver-use/1',disposition='ready',candidate='drawing',observation='jig')
        proposal = self.propose('station', approval, ['drawing','jig'])
        self.review(proposal,no,[pin(self.db.show('calibration'))])
        def calibrate():
            fresh = self.propose('calibration', {'loaded':True})
            self.accepted('owner','publish',proposal=fresh,account='Measured again.')
        calibrate()
        self.assertEqual({'notice':no,'reason':'changed-source'}, self.db.next_review(proposal))
        self.assertEqual('basis-changed', self.call('owner','publish',proposal=proposal,account='Old gauge')['code'])
        self.review(proposal,no,[pin(self.db.show('calibration'))])
        issued = self.accepted('owner','publish',proposal=proposal,account='Reviewed revision.')
        calibrate()
        self.assertEqual('basis-changed',self.call('owner','take',approval=issued['artifact'])['code'])
        self.assertEqual('ready',self.db.show('station')['body']['disposition'])

    def test_positive_and_negative_scalar_source_bodies_round_trip(self):
        for ident, value in [('off',False),('on',True),('empty',None),('note','ready'),('weight',0)]:
            self.accepted('lab','create',id=ident,body=value)
            seen = self.db.show(ident,1,content=True)
            self.assertIs(type(seen['body']),type(value))
            self.assertEqual(seen['body'],value)

    def test_carried_source_survives_hold_and_explicit_release_requires_new_issue(self):
        self.target('slot')
        self.target('gate')
        self.target('reading')
        self.accepted('maker','create',id='plan',body={'bay':'west'})
        ready=dict(format='g60-receiver-use/1', disposition='ready', candidate='plan', observation='reading')
        def issue(content, sources=('plan','reading')):
            prop=self.propose('slot',content,sources)
            return self.call('owner','publish',proposal=prop,account='Reviewed the nominated current evidence.')
        npin=self.offer('slot',[pin(self.db.show('gate'))])
        p=self.propose('slot',ready,['plan','reading'])
        self.review(p,npin,[pin(self.db.show('gate'))])
        self.accepted('owner','publish',proposal=p,account='Uses the measured west gate.')
        omitted=issue(ready)['artifact']
        # This revision depends on gate through its ongoing use, although absent from its direct proposal.
        gate=self.propose('gate',{'closed':True})
        self.accepted('owner','publish',proposal=gate,account='The gate was closed.')
        self.assertEqual('basis-changed',self.call('owner','take',approval=omitted)['code'])
        self.assertEqual('basis-changed',issue(dict(ready,title='new words'))['code'])
        self.assertEqual('accepted',issue({'disposition':'hold'},[])['status'])
        self.assertEqual('basis-changed',issue(ready)['code'])
        relation=self.db.use_source('slot','gate')
        self.accepted('owner','qualify',target=pin(self.db.show('slot')),prior=relation['source'],
                      current=relation['current'],disposition='renew',account='Desk remeasured clearance for this use.')
        fresh=issue(ready)['artifact']
        relation=self.db.use_source('slot','gate')
        self.accepted('owner','qualify',target=fresh,prior=relation['source'],current=relation['current'],
                      disposition='release',account='New loading arrangement no longer uses the gate.')
        self.assertEqual('basis-changed',self.call('owner','take',approval=fresh)['code'])
        final=issue(ready)['artifact']
        self.accepted('owner','take',approval=final)
        self.assertIsNone(self.db.use_source('slot','reading'))

    def test_staged_and_direct_citation_of_own_predecessor_remains_historical(self):
        self.target('reading')
        self.accepted('maker','create',id='plan',body={'bay':'east'})
        ready=dict(format='g60-receiver-use/1',disposition='ready',candidate='plan',observation='reading')
        for target,staged in [('left',True),('right',False)]:
            initial=self.target(target)
            notice=self.offer(target,[initial])
            ctx=self.accepted('owner','capture',target=target,id=f'ctx-{target}',sources=['plan','reading'],
                              staged=staged)['context']
            proposal=self.accepted('owner','propose',context=ctx['id'],id=f'design-{target}',body=ready)['proposal']['id']
            if staged:
                self.review(proposal,notice,[initial])
                treatments=[]
            else:
                treatments=[dict(notice=notice,disposition='incorporated',account='Compared the original order.')]
            issued=self.accepted('owner','publish',proposal=proposal,account='Current order fits.',treatments=treatments)
            self.assertIsNone(self.db.use_source(target,target))
            self.accepted('owner','take',approval=issued['artifact'])
