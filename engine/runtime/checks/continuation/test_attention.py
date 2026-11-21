"""Exact record and delivery boundaries, not model attention or semantic efficacy."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from continuation_support.attention import Attention
from continuation_support.collector import encode, sha
from continuation_support.exchange import Exchange

class AttentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.work = self.base/'work'; self.work.mkdir()
        self.root = self.base/'runtime'
        Exchange.initialize(self.root, self.work)
        self.ex = Exchange(self.root); self.a = Attention(self.ex)
        self.write('source.txt','Source before revision.')
        self.write('field.md','Keep discovery, sharing and comparative use available.')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self,path,value):
        p=self.work/path; p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(value if isinstance(value,str) else json.dumps(value))
        return str(path)

    def item(self, ident='q1', **kw):
        return dict(id=ident,question='What contribution is available?', use='current design',
                    basis=['source.txt'],groups=['shared'],**kw)

    def track(self, items=None):
        self.write('spec.json',{'field':'field.md','items':items or [self.item()]})
        return self.a.track('spec.json')

    def record(self, packet='p1',ident='r1',items=('q1',),**kw):
        self.write('receipt.json',{'id':ident,'assessments':[
            dict(item=x,disposition='considered',account='A scoped public treatment.',**kw) for x in items]})
        return self.a.record(packet,'receipt.json','current test author')

    def test_empty_feedback_no_completion_claim(self):
        out=self.a.feedback('p1')
        self.assertEqual(out['active'],0)
        self.assertIn('No named-list completion',out['context'])

    def test_track_unaccounted(self):
        self.track(); out=self.a.feedback('p1')
        self.assertEqual(out['counts']['unaccounted'],1)

    def test_member_treatment_not_group_credit(self):
        self.track([self.item('q1'),self.item('q2')]);self.a.feedback('p1')
        self.record();out=self.a.feedback('p2')
        self.assertEqual(out['counts']['considered'],1)
        self.assertEqual(out['counts']['unaccounted'],1)

    def test_question_change_preserves_history_not_current_credit(self):
        self.track();self.a.feedback('p1');self.record()
        item=self.item();item['question']='A different current question?';self.track([item])
        out=self.a.feedback('p2')
        self.assertEqual(out['counts']['changed-basis'],1)
        self.assertIn('What contribution',self.a.inspect('p1')['context'])

    def test_use_change_requires_current_account(self):
        self.track();self.a.feedback('p1');self.record()
        item=self.item();item['use']='other receiver';self.track([item])
        self.assertEqual(self.a.feedback('p2')['counts']['changed-basis'],1)

    def test_regrouping_keeps_bound_treatment(self):
        self.track();self.a.feedback('p1');self.record()
        item=self.item();item['groups']=['other','shared'];self.track([item])
        self.assertEqual(self.a.feedback('p2',groups=['other'])['counts']['considered'],1)

    def test_new_item_does_not_inherit_record(self):
        self.track();self.a.feedback('p1');self.record()
        self.track([self.item('q2')])
        self.assertEqual(self.a.feedback('p2')['counts']['unaccounted'],1)

    def test_source_change_nomination_not_history_loss(self):
        self.track();self.a.feedback('p1');self.record()
        self.write('source.txt','New material.')
        self.assertEqual(self.a.feedback('p2')['counts']['changed-basis'],1)
        self.assertEqual(self.a.inspect('p1')['delivered'][0]['basis'][0]['bytes'],23)

    def test_source_missing_history_readable(self):
        self.track();self.a.feedback('p1');self.record();(self.work/'source.txt').unlink()
        self.assertEqual(self.a.feedback('p2')['counts']['changed-basis'],1)
        self.assertIsNotNone(self.a.inspect('p1')['delivered'][0]['basis'][0]['sha256'])

    def test_missing_workspace_does_not_block_historical_read(self):
        self.track();self.a.feedback('p1');shutil.rmtree(self.work)
        self.assertEqual(Attention(Exchange(self.root)).inspect('p1')['active'],1)

    def test_open_question_separate_from_treatment(self):
        self.track();self.a.feedback('p1');self.record(remaining=['Does another use require more?'])
        out=self.a.feedback('p2')
        self.assertEqual(out['counts']['considered-with-open-questions'],1)
        self.assertEqual(out['counts']['unaccounted'],0)

    def test_deferred_is_not_considered(self):
        self.track();self.a.feedback('p1')
        self.write('receipt.json',{'id':'r1','assessments':[{'item':'q1','disposition':'deferred','account':'Other work has higher present leverage.'}]})
        self.a.record('p1','receipt.json','test author')
        out=self.a.feedback('p2')
        self.assertEqual(out['counts']['deferred'],1)
        self.assertEqual(out['counts']['considered'],0)

    def test_product_change_is_not_source_change(self):
        self.track();self.a.feedback('p1');self.write('product.md','actual first body')
        self.record(products=['product.md']);self.write('product.md','actual second body')
        out=self.a.feedback('p2')
        self.assertEqual(out['counts']['considered'],1)
        self.assertFalse(out['delivered'][0]['products'][0]['current_bytes_match'])

    def test_product_binding_does_not_assert_uptake(self):
        self.track();self.a.feedback('p1');self.write('product.md','x');self.record(products=['product.md'])
        self.assertIn('does not establish',self.a.feedback('p2')['context'])

    def test_bounds_do_not_disappear_omissions(self):
        self.track([self.item('q1'),self.item('q2')]);out=self.a.feedback('p1',limit=1)
        self.assertEqual(out['omitted'],['q2'])
        self.assertIn('1 selected items omitted',out['context'])
        with self.assertRaises(ValueError):self.record(items=('q2',))

    def test_group_filter_retains_wider_field(self):
        one=self.item('q1');two=self.item('q2');two['groups']=['other'];self.track([one,two])
        out=self.a.feedback('p1',groups=['other'])
        self.assertEqual(out['outside_view'],['q1'])
        self.assertIn('comparative use',out['context'])

    def test_overlapping_groups_no_duplicate_credit_or_delivery(self):
        a=self.item();a['groups']=['one','two'];self.track([a])
        self.assertEqual(len(self.a.feedback('p1',groups=['one','two'])['delivered']),1)

    def test_field_not_silently_truncated(self):
        self.track();self.write('field.md','x'*1800)
        with self.assertRaisesRegex(ValueError,'wider field'):self.a.feedback('p1',max_bytes=1024)

    def test_inactive_item_not_deleted_from_history(self):
        self.track();self.a.feedback('p1')
        item=self.item();item['active']=False;self.track([item])
        self.assertEqual(self.a.feedback('p2')['active'],0)
        self.assertEqual(self.a.inspect('p1')['active'],1)

    def test_unknown_and_duplicate_ids_refused(self):
        with self.assertRaises(ValueError):self.track([self.item(),self.item()])
        self.track();self.a.feedback('p1')
        with self.assertRaises(ValueError):self.a.feedback('p1')
        with self.assertRaises(ValueError):self.a.feedback('p2',items=['unknown'])
        self.record()
        with self.assertRaises(ValueError):self.record()

    def test_group_only_assessment_refused(self):
        self.track();self.a.feedback('p1')
        self.write('receipt.json',{'id':'r1','assessments':[{'item':'shared','disposition':'considered','account':'Everything in group.'}]})
        with self.assertRaises(ValueError):self.a.record('p1','receipt.json','author')

    def test_changed_after_delivery_records_old_occurrence(self):
        self.track();self.a.feedback('p1');self.write('source.txt','changed');self.record()
        self.assertEqual(self.a.feedback('p2')['counts']['changed-basis'],1)

    def test_new_source_does_not_exist_until_observed(self):
        item=self.item();item['basis']=['future.txt'];self.track([item]);self.a.feedback('p1');self.record()
        self.write('future.txt','arrived')
        self.assertEqual(self.a.feedback('p2')['counts']['changed-basis'],1)

    def test_bad_record_atomic(self):
        self.track();self.a.feedback('p1');before=self.a.state_path.read_bytes()
        self.write('receipt.json',{'id':'r1','assessments':[{'item':'q1','disposition':'considered','account':'ok'}, {'item':'bad','disposition':'considered','account':'bad'}]})
        with self.assertRaises(ValueError):self.a.record('p1','receipt.json','author')
        self.assertEqual(before,self.a.state_path.read_bytes())

    def test_index_corruption_refused(self):
        self.track();p=self.a.state_path;obj=json.loads(p.read_text());obj['body']['field']='evil';p.write_text(json.dumps(obj))
        with self.assertRaisesRegex(ValueError,'integrity'):self.a.feedback('p1')

    def test_object_corruption_refused(self):
        self.track();out=self.a.feedback('p1');key=out['delivered'][0]['basis'][0]['sha256']
        (self.root/'objects'/key).write_bytes(b'bad')
        with self.assertRaises(ValueError):self.a.inspect('p1')

    def test_paths_and_symlinks_refused(self):
        for path in ['../out','/tmp/out','bad\\file']:
            a=self.item();a['basis']=[path]
            with self.assertRaises(ValueError):self.track([a])
        (self.work/'link').symlink_to(self.base)
        a=self.item();a['basis']=['link/file']
        with self.assertRaises(ValueError):self.track([a])

    def test_thousand_declared_records_share_source_capture(self):
        self.track([self.item('q'+str(i)) for i in range(1000)])
        with patch.object(self.ex,'snapshot',wraps=self.ex.snapshot) as calls:
            out=self.a.feedback('p1',limit=7,max_bytes=10000)
        self.assertEqual(out['active'],1000)
        self.assertEqual(len(out['delivered']),7)
        self.assertEqual(len(out['omitted']),993)
        self.assertEqual(sum(c.args[0]=='source.txt' for c in calls.call_args_list),1)
        self.assertLessEqual(out['context_bytes'],10000)


    def test_unselected_source_not_read_or_claimed_current(self):
        one=self.item('q1');two=self.item('q2');two['groups']=['other'];two['basis']=['private.txt']
        self.write('private.txt','a second use');self.track([one,two])
        with patch.object(self.ex,'snapshot',wraps=self.ex.snapshot) as calls:
            out=self.a.feedback('p1',groups=['shared'])
        self.assertEqual(out['active'],2)
        self.assertEqual(out['selected'],1)
        self.assertEqual(out['counts_scope'],'selected-view')
        self.assertEqual(sum(out['counts'].values()),1)
        self.assertNotIn('private.txt',[x.args[0] for x in calls.call_args_list])
        self.assertIn('not revalidated',out['context'])

    def test_empty_account_is_not_credit(self):
        self.track();self.a.feedback('p1')
        self.write('r.json',{'id':'r','assessments':[{'item':'q1','disposition':'considered','account':'  '}]})
        with self.assertRaises(ValueError):self.a.record('p1','r.json','author')
        self.assertEqual(self.a.feedback('p2')['counts']['unaccounted'],1)


    def test_field_shared_with_basis_is_captured_once(self):
        item=self.item();item['basis']=['field.md'];self.track([item])
        with patch.object(self.ex,'snapshot',wraps=self.ex.snapshot) as calls:
            self.a.feedback('p1')
        self.assertEqual(sum(x.args[0]=='field.md' for x in calls.call_args_list),1)

    def test_missing_field_preserves_historical_feedback(self):
        self.track();self.a.feedback('p1');(self.work/'field.md').unlink()
        with self.assertRaisesRegex(ValueError,'field is unavailable'):self.a.feedback('p2')
        self.assertEqual(self.a.inspect('p1')['active'],1)

    def edit_index(self, edit):
        """Model an older writer or damage confined to a derived view."""
        state = self.a._load()
        edit(state)
        self.a.state_path.write_bytes(encode({'body': state, 'sha256': sha(encode(state))}))

    def test_current_feedback_reads_each_item_once_and_only_selected_receipt(self):
        self.track([self.item('q1'), self.item('q2')]); self.a.feedback('p1')
        old = self.record(items=('q1', 'q2'))
        latest = self.record(ident='r2')
        item_keys = self.a._load()['items'].values()
        with patch.object(self.ex, 'get', wraps=self.ex.get) as calls:
            out = self.a.feedback('p2', items=['q1'])
        keys = [call.args[0] for call in calls.call_args_list]
        self.assertEqual(out['delivered'][0]['assessment']['receipt'], 'r2')
        self.assertEqual(out['outside_view'], ['q2'])
        self.assertEqual(keys.count(latest['object']), 1)
        self.assertNotIn(old['object'], keys)
        for key in item_keys:
            self.assertEqual(keys.count(key), 1)

    def test_selected_assessments_share_one_receipt_read(self):
        self.track([self.item('q1'), self.item('q2')]); self.a.feedback('p1')
        receipt = self.record(items=('q2', 'q1'))
        with patch.object(self.ex, 'get', wraps=self.ex.get) as calls:
            out = self.a.feedback('p2')
        self.assertEqual(out['counts']['considered'], 2)
        self.assertEqual(sum(c.args[0] == receipt['object'] for c in calls.call_args_list), 1)
        self.assertEqual([r['assessment']['item'] for r in out['delivered']], ['q1', 'q2'])

    def test_return_to_basis_uses_latest_account_for_that_basis(self):
        self.track(); self.a.feedback('p1'); self.record()
        self.record(ident='r2', remaining=['Latest question for basis A.'])
        self.write('source.txt', 'Basis B.'); self.a.feedback('p2')
        self.record(packet='p2', ident='r3')
        self.write('source.txt', 'Source before revision.')
        out = self.a.feedback('p3')
        self.assertEqual(out['delivered'][0]['assessment']['receipt'], 'r2')
        self.assertEqual(out['counts']['considered-with-open-questions'], 1)
        self.assertEqual(self.a.inspect('p2')['counts']['changed-basis'], 1)

    def test_reactivation_and_regrouping_preserve_latest_account(self):
        self.track(); self.a.feedback('p1'); self.record()
        item = self.item(); item.update(active=False, groups=['other']); self.track([item])
        self.assertEqual(self.a.feedback('p2')['selected'], 0)
        item['active'] = True; self.track([item])
        out = self.a.feedback('p3', groups=['other'])
        self.assertEqual(out['counts']['considered'], 1)
        self.assertEqual(out['delivered'][0]['assessment']['receipt'], 'r1')

    def test_legacy_view_reconstructs_once_without_extra_sequence(self):
        self.track(); self.a.feedback('p1')
        receipts = [self.record(ident='r' + str(i)) for i in range(3)]
        self.edit_index(lambda state: state.pop('current'))
        sequence = self.a._load()['sequence']
        with patch.object(self.ex, 'get', wraps=self.ex.get) as calls:
            out = self.a.feedback('p2')
        keys = [c.args[0] for c in calls.call_args_list]
        for receipt in receipts:
            self.assertIn(receipt['object'], keys)
        self.assertEqual(out['sequence'], sequence)
        self.assertEqual(self.a._load()['sequence'], sequence + 1)
        with patch.object(self.ex, 'get', wraps=self.ex.get) as calls:
            self.a.feedback('p3')
        keys = [c.args[0] for c in calls.call_args_list]
        self.assertNotIn(receipts[0]['object'], keys)
        self.assertEqual(keys.count(receipts[-1]['object']), 1)

    def test_damaged_or_stale_derived_view_reconstructs(self):
        self.track(); self.a.feedback('p1'); self.record()
        prior = self.a._load()['current']
        self.record(ident='r2')

        def malformed(state):
            body = state['current']['body']
            body['latest']['q1'] = []
            state['current']['sha256'] = sha(encode(body))

        def wrong_reference(state):
            body = state['current']['body']
            next(iter(body['latest']['q1'].values()))[1] = 100
            state['current']['sha256'] = sha(encode(body))

        changes = [lambda s: s.update(current=None),
                   lambda s: s['current'].update(sha256='bad'),
                   lambda s: s.update(current=prior), malformed, wrong_reference]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                self.edit_index(change)
                out = self.a.feedback('repair-' + str(index))
                self.assertEqual(out['delivered'][0]['assessment']['receipt'], 'r2')
                self.assertEqual(out['counts']['considered'], 1)

    def test_legacy_record_reconstructs_receipt_membership_and_updates_view(self):
        self.track(); self.a.feedback('p1'); first = self.record()
        self.edit_index(lambda state: state.pop('current'))
        with self.assertRaisesRegex(ValueError, 'receipt identity already used'):
            self.record()
        self.record(ident='r2')
        with patch.object(self.ex, 'get', wraps=self.ex.get) as calls:
            out = self.a.feedback('p2')
        self.assertNotIn(first['object'], [c.args[0] for c in calls.call_args_list])
        self.assertEqual(out['delivered'][0]['assessment']['receipt'], 'r2')

    def test_duplicate_id_check_does_not_expand_historical_receipts(self):
        self.track(); self.a.feedback('p1'); first = self.record()
        second = self.record(ident='r2')
        before = self.a.state_path.read_bytes()
        with patch.object(self.ex, 'get', wraps=self.ex.get) as calls:
            with self.assertRaisesRegex(ValueError, 'receipt identity already used'):
                self.record()
        keys = [c.args[0] for c in calls.call_args_list]
        self.assertNotIn(first['object'], keys); self.assertNotIn(second['object'], keys)
        self.assertEqual(before, self.a.state_path.read_bytes())

    def test_unselected_history_is_not_an_incidental_integrity_sweep(self):
        self.track([self.item('q1'), self.item('q2')]); self.a.feedback('p1')
        unrelated = self.record(items=('q2',)); self.record(ident='r2')
        (self.root / 'objects' / unrelated['object']).write_bytes(b'damaged unrelated receipt')
        self.assertEqual(self.a.feedback('p2', items=['q1'])['counts']['considered'], 1)
        with self.assertRaisesRegex(ValueError, 'content object changed'):
            self.a.feedback('p3', items=['q2'])
        self.edit_index(lambda state: state.pop('current'))
        with self.assertRaisesRegex(ValueError, 'content object changed'):
            self.a.feedback('p4', items=['q1'])

    def test_changed_basis_needs_history_membership_without_receipt_body(self):
        self.track(); self.a.feedback('p1'); receipt = self.record()
        self.write('source.txt', 'Changed current basis.')
        with patch.object(self.ex, 'get', wraps=self.ex.get) as calls:
            out = self.a.feedback('p2')
        self.assertEqual(out['counts']['changed-basis'], 1)
        self.assertNotIn(receipt['object'], [c.args[0] for c in calls.call_args_list])

    def test_fresh_cli_processes(self):
        self.write('spec.json',{'field':'field.md','items':[self.item()]})
        def call(*args):
            p=subprocess.run([sys.executable,'-B',str(ROOT/'attend.py'),'--root',str(self.root),*args],capture_output=True,text=True,timeout=10)
            self.assertEqual(p.returncode,0,p.stderr);return json.loads(p.stdout)
        call('track','--spec','spec.json');call('feedback','p1')
        self.write('r.json',{'id':'r1','assessments':[dict(item='q1',disposition='considered',account='actual public account')]})
        call('record','p1','--receipt','r.json','--producer','test process')
        self.assertEqual(call('feedback','p2')['counts']['considered'],1)

    def test_distributed_root_rejected(self):
        p=subprocess.run([sys.executable,'-B',str(ROOT/'attend.py'),'--root',str(ROOT/'runtime'),'feedback','p1'],capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,2)
        self.assertIn('outside',p.stderr)
        self.assertFalse((ROOT/'runtime').exists())

if __name__=='__main__':
    unittest.main(verbosity=2)
