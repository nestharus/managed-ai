#!/usr/bin/env python3
"""Exercise current duty, historical exposure and selected Attention boundaries.

These are authored isolated fixtures, not an empirical completeness evaluation.
"""
from pathlib import Path
import json
import runpy
import tempfile
import unittest

API=runpy.run_path(str(Path(__file__).with_name('system.py')))
System, exclusive = API['System'], API['exclusive']

class ScopeChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='agent-context-scope-')
        self.base=Path(self.temp.name)
        self.root=self.base/'runtime'
        System.initialize(self.root,{'contribution':'Preserve owed work while selecting useful evidence.'})
        self.system=System(self.root)
        self.lock=exclusive(self.system.ex.root/'owner.lock');self.lock.__enter__()

    def tearDown(self):
        self.lock.__exit__(None,None,None);self.temp.cleanup()

    def file(self,name,body):
        path=self.system.work/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(body);return path

    def test_damaged_outside_head_blocks_capture_and_publication(self):
        target = 'outputs/report.txt'
        self.system.put({'id': 'outside', 'kind': 'note', 'content': 'original'}, 0)
        self.system.context('ready', {'targets': [target]})
        self.assertIn('outside', self.system.view('ready')['outside_view_records'])
        proposal = self.base / 'proposal.txt'
        proposal.write_text('Finished inspection.')
        self.system.receive('ready', proposal, 'operator', [target + '=' + str(proposal)])
        head = self.system.record_path('outside')
        head.write_bytes(head.read_bytes().replace(b'original', b'modified'))
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.system.context('broken', {})
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.system.adopt('ready', target, 'Authorize report.')
        self.assertFalse((self.system.work / target).exists())

    def test_large_head_receipt_cannot_hide_a_current_duty(self):
        from native_runtime.head_receipts import ATTRIBUTE
        import os
        store = self.system.record_store
        basis = self.file('sources/stock.txt', 'six')
        note = {'id': 'outside', 'kind': 'note', 'content': 'x' * (128 * 1024), 'basis': ['sources/stock.txt']}
        store.put(note, 0)
        path = store.path('outside')
        indexed = store.index_scope('outside')['indexed']
        old_receipt = os.getxattr(path, ATTRIBUTE) if indexed else None
        basis.write_text('seven')
        self.assertIn('outside', self.system.status()['gaps']['changed_record_basis'])
        store.put(dict(note, kind='obligation'), 1)
        if indexed:
            good = os.getxattr(path, ATTRIBUTE)
            # Keep the old valid owner tag and all other fields, but claim
            # that the governing head is an ordinary note.
            os.setxattr(path, ATTRIBUTE, good[:32] + good[32:].replace(b'"obligation"', b'"note"'))
        self.system.context('changed-label', {})
        self.assertIn('outside', self.system.view('changed-label')['governing_records'])
        if indexed:
            # An authentic prior receipt belongs to a different raw head.
            os.setxattr(path, ATTRIBUTE, old_receipt)
        self.system.context('changed-bytes', {})
        self.assertIn('outside', self.system.view('changed-bytes')['governing_records'])
        if indexed:
            os.removexattr(path, ATTRIBUTE)
        self.system.context('legacy-head', {})
        self.assertIn('outside', self.system.view('legacy-head')['governing_records'])
        if indexed:
            os.setxattr(path, ATTRIBUTE, old_receipt)
            store.receipts.key_path.unlink()
            self.system.context('missing-owner-key', {})
            self.assertIn('outside', self.system.view('missing-owner-key')['governing_records'])
            self.assertFalse(store.receipts.key_path.exists())

    def obligations(self):
        self.file('sources/closed.txt','EXACT CLOSED OCCURRENCE, not needed for this next duty.\n')
        self.file('sources/owed.txt','THE STILL OWED EFFECT needs this current contribution.\n')
        self.system.put({'id':'closed','kind':'obligation','content':{'text':'Old completed contribution.','disposition':'fulfilled','account':'Declared fixture closure.'},'basis':['sources/closed.txt']},0)
        self.system.put({'id':'owed','kind':'obligation','content':{'text':'Retain this current effect.','disposition':'open'},'basis':['sources/owed.txt']},0)

    def test_closed_evidence_is_recoverable_while_owed_effect_stays_exposed(self):
        self.obligations()
        before=self.system.show('closed')['basis'][0]
        self.system.context('bounded',{})
        body=self.system.recover('bounded')['context'];view=self.system.view('bounded')
        self.assertIn('THE STILL OWED EFFECT',body)
        self.assertNotIn('EXACT CLOSED OCCURRENCE',body)
        self.assertTrue({'owed','closed'}<=set(view['governing_records']))
        self.assertTrue({'owed','closed'}<=set(view['selected_records']))
        outside=view['outside_record_basis'][0]
        self.assertEqual(outside['sha256'],before['sha256'])
        self.assertIn(b'EXACT CLOSED OCCURRENCE',self.system.ex.get(outside['sha256']))
        self.file('sources/closed.txt','A changed current file, not the original closed observation.')
        self.assertEqual(self.system.recover('bounded')['context'],body)
        self.assertIn(b'EXACT CLOSED OCCURRENCE',self.system.ex.get(outside['sha256']))

    def test_separate_closure_requires_account_and_legacy_evidence_remains_owed(self):
        path = 'sources/shift.txt'
        self.file(path, 'The substitute still needs the actual labels.')
        duty = {'id': 'shift', 'kind': 'obligation',
                'content': {'text': 'Provide legible labels.'}, 'basis': [path]}
        original = self.system.put(duty, 0)
        for state in ('fulfilled', 'released'):
            with self.subTest(disposition=state):
                with self.assertRaisesRegex(ValueError, 'closed obligation account'):
                    self.system.put(dict(duty, content=dict(duty['content'], disposition=state, account='  ')), 1)
        self.assertEqual(self.system.show('shift')['sha256'], original['sha256'])
        # Construct an existing v26 head, which supported accountless closure.
        raw = self.system.show('shift')
        raw['content']['disposition'] = 'fulfilled'
        raw['sha256'] = API['sha'](API['encode']({k: v for k, v in raw.items() if k != 'sha256'}))
        self.system.record_path('shift').write_bytes(API['encode'](raw))
        self.assertIn('shift', self.system.status()['owed_record_obligations'])
        self.system.context('old-closure', {})
        self.assertIn('The substitute still needs the actual labels.', self.system.recover('old-closure')['context'])
        self.system.put(dict(duty, content=dict(duty['content'], disposition='released',
                            account='The shift was cancelled by its owner.')), 1)
        self.assertNotIn('shift', self.system.status()['owed_record_obligations'])
        self.system.context('new-closure', {})
        self.assertNotIn('The substitute still needs the actual labels.', self.system.recover('new-closure')['context'])

    def test_open_duty_navigation_and_provenance_changes(self):
        path = 'sources/current.txt'
        file = self.file(path, 'Original inspection')
        duty = {'id': 'labels', 'kind': 'obligation', 'content': {'text': 'Deliver the labels.'}}
        self.system.put(dict(duty, basis=[path]), 0)
        prior_pin = self.system.show('labels')['basis'][0]
        self.system.put(dict(duty, active=False), 1)
        self.assertIn('labels', self.system.status()['owed_record_obligations'])
        self.system.context('inactive-duty', {})
        self.assertIn('Original inspection', self.system.recover('inactive-duty')['context'])
        file.write_text('Independent later inspection')
        self.system.put(dict(duty, content={'text': 'Give the substitute the labels.'}), 2)
        self.assertEqual(self.system.show('labels')['basis'], [prior_pin])
        for basis in ([], [path]):
            with self.subTest(basis=basis):
                with self.assertRaisesRegex(ValueError, 'changed obligation basis'):
                    self.system.put(dict(duty, basis=basis), 3)
        self.system.put(dict(duty, basis=[path], content={'text': 'Deliver labels.',
                        'basis_change': {'from_revision': 3, 'account': 'The morning inspection now governs this duty.'}}), 3)
        self.assertNotEqual(self.system.show('labels')['basis'][0], prior_pin)
        self.assertEqual(self.system.ex.get(prior_pin['sha256']), b'Original inspection')
        self.assertIn('labels', self.system.status()['owed_record_obligations'])
        self.system.put({'id': 'memo', 'kind': 'note', 'content': 'Optional notes.', 'active': False}, 0)
        self.system.context('ordinary', {})
        view = self.system.view('ordinary')
        self.assertIn('labels', view['selected_records'])
        self.assertNotIn('memo', view['selected_records'])

    def test_explicit_record_or_shared_source_exposes_historical_evidence(self):
        self.obligations()
        self.system.context('explicit',{'records':['closed']})
        self.assertIn('EXACT CLOSED OCCURRENCE',self.system.recover('explicit')['context'])
        self.assertEqual(self.system.view('explicit')['outside_record_basis'],[])
        self.system.context('source',{'sources':['sources/closed.txt']})
        self.assertEqual(self.system.view('source')['outside_record_basis'],[])
        self.system.put({'id':'owed','kind':'obligation','content':{'text':'Now both sources matter.'},'basis':['sources/owed.txt','sources/closed.txt']},1)
        self.system.context('shared',{})
        self.assertIn('EXACT CLOSED OCCURRENCE',self.system.recover('shared')['context'])
        self.assertEqual(self.system.view('shared')['outside_record_basis'],[])

    def test_selected_attention_does_not_observe_an_outside_source(self):
        self.file('sources/selected.txt','Selected consideration basis.')
        self.file('sources/outside.txt','Outside consideration basis; must not be observed here.')
        spec={'items':[{'id':'chosen-a','question':'Does the chosen arrangement serve the use?','use':'Current maintenance','basis':['sources/selected.txt'],'groups':['current']},{'id':'chosen-b','question':'Is its old interpretation still applicable?','use':'Current maintenance','basis':['sources/selected.txt'],'groups':['current']},{'id':'elsewhere','question':'A separate possible future contribution?','use':'Other workload','basis':['sources/outside.txt'],'groups':['other']}]}
        p=self.base/'attention.json';p.write_text(json.dumps(spec));self.system.track(p)
        original=self.system.ex.snapshot
        def snapshot(path,*args,**kwargs):
            if path=='sources/outside.txt':raise AssertionError('Outside Attention source was read')
            return original(path,*args,**kwargs)
        self.system.ex.snapshot=snapshot
        self.system.context('selected',{'attention_groups':['current'],'attention_limit':1})
        view=self.system.view('selected')
        self.assertEqual(view['attention_outside_view'],['elsewhere'])
        self.assertEqual(len(view['attention_delivered']),1)
        self.assertEqual(len(view['attention_omitted']),1)
        with self.assertRaisesRegex(ValueError,'unknown active item or group'):
            self.system.context('unknown',{'attention_groups':['absent']})

    def test_native_exchange_cannot_publish_through_standalone_entrance(self):
        target = 'outputs/card.md'
        self.system.context('card', {'targets': [target]})
        reply = self.base/'card.txt'
        reply.write_text('Actual proposed direction.')
        self.system.receive('card', reply, 'crew', [target+'='+str(reply)])
        raw = API['Exchange'](self.system.ex.root)
        self.assertEqual(raw.inspect('card')['reply_text'], reply.read_text())
        self.system.put({'id':'admit','kind':'obligation','content':'Check the new entrance.'}, 0)
        with self.assertRaisesRegex(ValueError, 'native runtime'):
            raw.publish('card', target, 'Attempt using a lower-level publisher.')
        with self.assertRaisesRegex(ValueError, 'membership changed'):
            self.system.ex.publish('card', target, 'Attempt through the native exchange directly.')
        self.assertFalse((self.system.work/target).exists())
        self.system.context('fresh-card', {'targets': [target]})
        self.system.receive('fresh-card', reply, 'crew', [target+'='+str(reply)])
        self.system.adopt('fresh-card', target, 'Qualified after reconsidering the new entrance.')
        self.assertEqual((self.system.work/target).read_text(), reply.read_text())

    def test_new_duty_still_refuses_an_old_proposed_write(self):
        self.obligations()
        self.system.context('proposal',{'targets':['outputs/result.md']})
        reply=self.base/'reply.md';reply.write_text('Actual fixture proposal.')
        self.system.receive('proposal',reply,'declared fixture', ['outputs/result.md='+str(reply)])
        self.system.put({'id':'new-duty','kind':'obligation','content':{'text':'A new actual effect must be retained.'}},0)
        with self.assertRaisesRegex(ValueError,'membership changed'):
            self.system.adopt('proposal','outputs/result.md','Must refuse stale governing duties.')
        self.assertFalse((self.system.work/'outputs/result.md').exists())

if __name__=='__main__':unittest.main(verbosity=2)
