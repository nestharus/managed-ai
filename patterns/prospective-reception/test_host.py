import copy
import tempfile
import unittest
from pathlib import Path
from host import Host, digest


class Encounter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        self.h = Host(self.d / 'journal')
        self.owner = (Path(__file__).parent / 'fixture/early/owner.txt').read_bytes()
        for name, body in {'owner': self.owner, 'cue': b'Use a tag.', 'partial': b'Load blue?', 'other': b'Other.', 'question': b'What would you load?', 'answer': b'I loaded both.', 'next': b'Inspect tag and use east vehicle.', 'report': b'I held the unreadable one.'}.items():
            (self.d / name).write_bytes(body)
        self.specs = [dict(owner='owner', owner_sha256=digest(self.owner), writer='a', receiver='b', required_human=False),
                      dict(product='partial', owner='owner', passage='Hold unverified bags at the desk.', purpose='informed-source-comparison', decision='Load by register.', alternative='Inspect each tag.', obligations=['eligibility','destination']),
                      dict(product='partial', actor='b', response='answer', observations={'eligibility':'I loaded both.'}),
                      dict(product='partial', response='answer', revision='next', uptake='Changed load rule after receiver loaded both.'),
                      dict(revision='next', actor='b', report='report', outcomes={'eligibility':'I held the unreadable one.'}, human_receipt=None, owner_sha256=digest(self.owner))]
        self.ops = ['init','project','return','revise','review']

    def run_stage(self, i, **kw):
        s = copy.deepcopy(self.specs[i])
        if i >= 2:
            history = self.h.history()
            s['event'] = history[-1]['digest'] if history else 'absent'
        event = self.h.apply(self.ops[i], dict(s, **kw), self.d)
        if i == 1:
            self.h.apply('issue', dict(event=event['digest'], question='question', purpose=s['purpose'], actor='b'), self.d)
        if i == 3:
            setup = self.h.history()[0]['data']
            self.h.apply('issue-review',dict(event=event['digest'],question='question',purpose=event['data']['purpose'],actor=setup.get('reviewer',setup['receiver'])), self.d)
        return event

    def stages(self, n):
        for i in range(n):
            self.run_stage(i)

    def test_complete_with_gate_and_open_coverage(self):
        self.stages(5)
        final = self.h.history()[-1]['data']
        self.assertEqual(final['authority'], 'scoped model encounter')
        self.assertEqual(set(final['outcomes']), {'eligibility'})

    def test_issue_requires_frozen_partial_and_current_receiver(self):
        self.run_stage(0)
        with self.assertRaises(ValueError):
            self.h.apply('issue', dict(event='unregistered',question='question',purpose='informed-source-comparison',actor='b'), self.d)
        project = self.h.apply('project',self.specs[1],self.d)
        request = dict(event=project['digest'], question='question',purpose='informed-source-comparison',actor='b')
        with self.assertRaises(ValueError):
            self.run_stage(2)
        for overrides in ({'actor':'a'}, {'event':'stale'}, {'question':'../question'}, {'question':'link'}):
            if overrides.get('question') == 'link':
                (self.d/'link').symlink_to(self.d/'question')
            with self.assertRaises(ValueError):
                self.h.apply('issue',dict(request,**overrides),self.d)
        issued = self.h.apply('issue',request,self.d)
        packet = self.h.root/'outbox'/project['digest']
        self.assertEqual((packet/'product.txt').read_bytes(),(self.d/'partial').read_bytes())
        self.assertEqual((packet/'owner.txt').read_bytes(),self.owner)
        self.assertEqual(self.h.apply('issue',request,self.d), issued)
        (packet/'product.txt').write_bytes(b'changed partial')
        with self.assertRaises(ValueError):
            self.run_stage(2)

    def test_appointed_reviewer_and_changed_owner(self):
        self.run_stage(0,reviewer='c')
        self.run_stage(1)
        self.run_stage(2)
        (self.d/'new-owner').write_bytes(self.owner+b' Updated order.')
        new_pin = digest((self.d/'new-owner').read_bytes())
        with self.assertRaises(ValueError):
            self.run_stage(3,owner='new-owner',owner_sha256=digest(self.owner))
        rev = self.run_stage(3,owner='new-owner',owner_sha256=new_pin)
        self.assertEqual(rev['data']['prior_owner_basis'], digest(self.owner))
        with self.assertRaises(ValueError):
            self.run_stage(4,actor='c')
        with self.assertRaises(ValueError):
            self.run_stage(4,owner_sha256=new_pin)
        self.run_stage(4,actor='c',owner_sha256=new_pin)
        with self.assertRaises(ValueError):
            self.run_stage(1,product='next')
        self.run_stage(1,owner='new-owner',product='next')

    def test_human_gate_stops_dependent_action(self):
        self.run_stage(0,required_human=True)
        with self.assertRaisesRegex(ValueError,'human approval pending'):
            self.run_stage(1)

    def test_unaided_custody_and_mode_confusion(self):
        self.run_stage(0)
        self.specs[1]['purpose']='unaided-product-use'
        projection = self.h.apply('project',self.specs[1],self.d)
        request = dict(event=projection['digest'],question='question',actor='b',purpose='unaided-product-use')
        with self.assertRaises(ValueError):
            self.h.apply('issue',dict(request,purpose='informed-source-comparison'),self.d)
        with self.assertRaises(ValueError):
            self.h.apply('issue',dict(request,question='owner'),self.d)
        issued = self.h.apply('issue',request,self.d)
        packet = self.h.root/'outbox'/projection['digest']
        self.assertEqual({p.name for p in packet.iterdir()}, {'product.txt','question.txt'})
        self.assertEqual((self.h.objects/digest(self.owner)).read_bytes(),self.owner)
        (packet/'owner.txt').write_bytes(self.owner)
        with self.assertRaises(ValueError):
            self.run_stage(2)
        (packet/'owner.txt').unlink()
        self.assertEqual(self.run_stage(2)['data']['purpose'], 'unaided-product-use')
        self.run_stage(3)
        reviewer_packet = self.h.history()[-1]['data']['packet']
        self.assertEqual(set(reviewer_packet), {'product.txt','question.txt'})
        outcome = self.run_stage(4)['data']
        self.assertEqual(outcome['purpose'], 'unaided-product-use')
        self.assertNotIn('improvement',outcome)

    def test_public_product_may_reproduce_posted_instruction(self):
        owner = b'Use the south door.'
        card = b'LOCKER W\n' + owner
        (self.d/'owner').write_bytes(owner)
        (self.d/'partial').write_bytes(card)
        self.run_stage(0, owner_sha256=digest(owner))
        projection = self.h.apply('project',dict(self.specs[1], passage=owner.decode(),
                                  purpose='unaided-product-use'),self.d)
        request = dict(event=projection['digest'],question='question',actor='b',purpose='unaided-product-use')
        (self.d/'question').write_bytes(b'Hint: ' + owner)
        with self.assertRaises(ValueError):
            self.h.apply('issue',request,self.d)
        (self.d/'question').write_bytes(b'Which door would you try?')
        issued = self.h.apply('issue',request,self.d)
        packet = self.h.root/'outbox'/projection['digest']
        self.assertEqual(set(issued['data']['packet']), {'product.txt','question.txt'})
        self.assertEqual((packet/'product.txt').read_bytes(),card)
        self.assertEqual(issued['data']['product'],digest(card))
        (packet/'sidecar.txt').write_bytes(owner)
        with self.assertRaises(ValueError):
            self.run_stage(2)

    def test_missing_or_unrecognized_purpose(self):
        self.run_stage(0)
        without = self.specs[1].copy()
        del without['purpose']
        for spec in [without,dict(without,purpose='all')]:
            with self.assertRaises(ValueError):
                self.h.apply('project',spec,self.d)

    def test_attacks(self):
        for i, kw in [(2,{}), (1,{'owner':'cue'}), (2,{'actor':'a'}), (2,{'product':'other'}),
                      (2,{'event':'yesterday'}), (2,{'timestamp':'pretend earlier'}),
                      (2,{'observations':{'eligibility':'invented'}}),
                      (3,{'response':'other'}), (4,{'revision':'other'}),
                      (4,{'human_receipt':'agent says yes'}),
                      (4,{'outcomes':{'destination':'I held the unreadable one.'}})]:
            with self.subTest(stage=i, mutation=kw):
                temp = tempfile.TemporaryDirectory(dir=self.d)
                self.addCleanup(temp.cleanup)
                self.h = Host(Path(temp.name))
                self.stages(i if kw else 1)
                with self.assertRaises(ValueError):
                    self.run_stage(i, **kw)

    def test_reopening_changed_product(self):
        self.stages(5)
        with self.assertRaises(ValueError):
            self.run_stage(1)
        self.run_stage(1, product='next')


if __name__ == '__main__':
    unittest.main()
