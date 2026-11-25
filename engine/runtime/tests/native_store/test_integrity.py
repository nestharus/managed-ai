"""End-to-end regressions from the external system-integrity review."""
if __package__:
    from .native_test_paths import APP, ENGINE, PRODUCT
else:
    from native_test_paths import APP, ENGINE, PRODUCT
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

if os.getenv('G60_TEST_PRODUCT'):
    sys.path.insert(0, str(Path(os.environ['G60_TEST_PRODUCT']).resolve() / 'native'))
import system
from native_runtime.composition import MAX_PROFILE_BYTES
from continuation_support.storage import MAX_BYTES

PRODUCT = Path(system.__file__).resolve().parents[1]


class Integrity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.owner = self.base / 'owner'
        system.System.initialize(self.owner / 'runtime', {'contribution': 'Machine the keyed base.'}, record_retention='append-history')
        self.s = system.System(self.owner / 'runtime')

    def test_f02_encoded_record_and_next_use(self):
        # Subtract the exact native envelope, including previous-revision identity.
        for ident, kind in [('note','note'), ('goal','goal'), ('field','note'),
                            ('frontier','note'), ('outstanding','obligation')]:
            for delta in (-1, 0, 1):
                with self.subTest(ident=ident, delta=delta), patch('native_runtime.records.now', return_value='2026-11-06T00:00:00.123456+00:00'):
                    rec = {'id':ident, 'kind':kind, 'content':{'contribution':'Turn the panel.', 'detail':''}}
                    prev = self.s.show(ident)['revision'] if self.s.record_path(ident).exists() else 0
                    # A small native revision supplies the exact next-revision envelope.
                    self.s.put(rec,prev)
                    self.s.put(rec,prev+1)
                    raw = self.s.record_path(ident).read_bytes()
                    n = MAX_BYTES + delta - len(raw)
                    rec['content']['detail'] = 'é'*(n//2) + 'x'*(n%2)
                    if delta == 1:
                        with self.assertRaises(ValueError):
                            self.s.put(rec,prev+2)
                        self.assertEqual(self.s.record_path(ident).read_bytes(),raw)
                    else:
                        self.s.put(rec,prev+2)
                        raw = self.s.record_path(ident).read_bytes()
                        self.assertEqual(len(raw),MAX_BYTES+delta)
                        self.s.context(f'{ident}-{delta+1}',{'task':'Read panel requirement.', 'records':[ident]})
                        rec['content']['detail']=''
                        seen=self.s.put(rec,prev+3)
                        self.assertEqual(self.s.ex.get(seen['previous_object']),raw)

    def test_f02_legacy_oversized_head_keeps_exact_history(self):
        record={'id':'load','kind':'obligation','content':{'text':'Maintain clearance.'}}
        self.s.put(record,0)
        path=self.s.record_path('load')
        old=self.s.show('load');old['content']['legacy']='é'*(MAX_BYTES//2)
        del old['sha256'];old['sha256']=system.sha(system.encode(old));raw=system.encode(old);path.write_bytes(raw)
        with self.assertRaises(ValueError):
            self.s.context('stuck',{'task':'Check clearance.'})
        receipt=self.s.put(record,1)
        self.assertEqual(receipt['previous_object'],system.sha(raw))
        self.assertEqual((self.s.ex.root/'objects'/receipt['previous_object']).read_bytes(),raw)
        history = list(self.s.record_store.history_pages('load', limit=1))
        self.assertEqual(history[-1]['records'], [old])
        self.assertTrue(history[-1]['complete'])
        self.assertEqual(self.s.show('load')['revision'],2)
        self.s.context('after-repair',{'task':'Proceed.'})

    def test_f03_both_signs_and_alternatives(self):
        path = self.s.work/'sources/clearance'; path.write_text('Measured')
        basis=['sources/clearance']
        premises=[dict(id=k,disposition=v,basis=basis) for k,v in [('yes','available'),('no','unavailable')]]
        premises += [dict(id='always',disposition='available'),dict(id='never',disposition='unavailable')]
        for p in premises:p['account']='fixture disposition '+p['id']
        groups = [[['no']], [['no'],['always']], [['yes','never']]]
        opportunities = [dict(id=key,requires=g,contribution=key) for key,g in zip(['neg','alt','blocked'],groups)]
        self.s.put({'id':'jig','kind':'design','basis':basis,'content':dict(format='g60-composition/1',purpose='Assess jig',premises=premises,opportunities=opportunities)},0)
        def view():
            v=self.s.compose('jig',None,20,20)
            return ({p['id']:p for p in v['premises']},{o['id']:o['availability'] for o in v['opportunities']})
        self.assertEqual(view()[0]['no']['availability'],'unavailable')
        for state in ('changed','missing','per-source-byte-bound','unbound'):
            if state=='changed':path.write_text('Revised')
            if state=='missing':path.unlink()
            if state=='per-source-byte-bound':path.write_bytes(b'x'*(MAX_PROFILE_BYTES+1))
            if state=='unbound':
                for p in premises[:2]:p['basis']=['sources/else']
                self.s.put({'id':'jig','kind':'design','content':dict(format='g60-composition/1',purpose='Assess jig',premises=premises,opportunities=opportunities)},1)
            p,o=view()
            self.assertEqual([p[k]['support']['sources'][0]['status'] for k in ['yes','no']],[state]*2)
            self.assertEqual([p[k]['availability'] for k in ['yes','no']],['unassessed']*2)
            self.assertEqual(p['no']['disposition'],'unavailable')
            self.assertEqual(o,{'neg':'needs-qualification','alt':'available-under-declared-premises','blocked':'unavailable-under-declared-premises'})

    def test_f05_native_census_capture_verify_recover(self):
        adapters = 'adapters' if (PRODUCT/'adapters').is_dir() else 'patterns'
        adapter=PRODUCT/adapters/'continuing-state/support.py'
        spec=importlib.util.spec_from_file_location('capture_native',adapter)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for ident,kind in [('_note','note'),('-ob','obligation'),('-ob-extra','obligation'),('_'+'a'*119,'note')]:
            self.s.put({'id':ident,'kind':kind,'content':{'purpose':'Check clearance.'}},0)
        account=self.base/'account';account.write_text('Continue. A fixture report is due.')
        self.s.interpret(1,'continue',account,[])
        pin=next((self.s.work/'interpretations').glob('*.json'))
        def binding(path):
            b=path.read_bytes();return {'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}
        snap=system.encode(dict(format='g60-deployment-snapshot/1',original_root=str(self.owner),files={},directories={}))
        archive=self.base/'base.zip'
        with ZipFile(archive,'w') as z:z.writestr('snapshot.json',snap)
        selection=self.base/'capture.json'
        selection.write_bytes(system.encode(dict(format='g60-continuation-capture/1',work_root='runtime/work',object_root='runtime/exchange/objects',owner_lock='runtime/exchange/owner.lock',interpretation=str(pin.relative_to(self.owner)),expected_interpretation=binding(pin),expected_records=[],base=dict(archive=str(archive),**binding(archive),snapshot_sha256=system.sha(snap)))))
        zpath=self.base/'projection.zip';module.capture(self.owner,selection,zpath)
        sha=binding(zpath)['sha256'];module.verify(zpath,sha)
        dest=self.base/'projection';module.recover(zpath,sha,dest)
        self.assertEqual((dest/pin.relative_to(self.owner)).read_bytes(),pin.read_bytes())
        for ident in ('-ob','-ob-extra'):
            rel=self.s.record_path(ident).relative_to(self.owner)
            self.assertEqual((dest/rel).read_bytes(),(self.owner/rel).read_bytes())

    def test_f06_preflight_refusal_and_late_retained_effect(self):
        with self.assertRaises(ValueError):
            self.s.context('east',{'task':'Cut','techniques':['no-such-technique']})
        self.assertFalse((self.s.work/'.system/turns/east').exists())
        self.s.context('east',{'task':'Corrected'})
        req={'task':'Cut','attention':{'items':[{'id':'jig','use':'Check the cut','question':'Which fixture is installed?'}]}}
        with patch.object(self.s.ex,'prepare',side_effect=OSError('injected cutoff')), self.assertRaises((OSError,ValueError)):
            self.s.context('west',req)
        self.assertNotIn('turn-west',self.s.attention._load()['feedbacks'])
        self.assertNotIn('jig',self.s.attention._load()['items'])
        self.assertFalse((self.s.work/'.system/turns/west').exists())
        # Historical v28 can have a real Attention occurrence with no sealed packet.
        # Preserve its account route when opening the old layout.
        old=self.s.work/'.system/turns/west';old.mkdir()
        source=system.write_input(self.s.work, old,'attention-spec.json',system.encode(req['attention']))
        self.s.attention.track(source)
        self.s.attention.feedback('turn-west',items=['jig'])
        system.write_json(old/'preparation.json',{'phase':'attention-started','feedback':'turn-west'})
        self.s=system.System(self.owner/'runtime')
        pending=self.s.status()['incomplete_turns']
        self.assertEqual(pending['west']['phase'],'attention-started')
        self.assertTrue(self.s.attention._load()['feedbacks']['turn-west'])
        with self.assertRaisesRegex(ValueError,'abandon'):
            self.s.recover('west')
        self.s.abandon_context('west','Use a new projection with the retained jig evidence.')
        self.assertEqual(self.s.status()['incomplete_turns']['west']['phase'],'abandoned')
        self.s.context('west-revised',{'task':'Read current fixture','attention_items':['jig']})
        self.assertIn('jig',self.s.view('west-revised')['attention_delivered'])
        with self.assertRaises(ValueError):self.s.abandon_context('east','Already installed.')

    def test_f07_placement_checks_effective_argv(self):
        product=self.base/'product';product.mkdir()
        shutil.copy2(PRODUCT/'collect_returns.py',product/'collect_returns.py')
        shutil.copytree(PRODUCT/'continuation',product/'continuation')
        inside=product/'illegal';outside=self.base/'allowed'
        for args in [['--root',outside,'--root',inside],[f'--root={outside}',f'--root={inside}'],['--roo',inside]]:
            p=subprocess.run([sys.executable,'-B',str(product/'collect_returns.py'),*map(str,args),'list'],capture_output=True)
            self.assertNotEqual(p.returncode,0)
            self.assertFalse(inside.exists())
        p=subprocess.run([sys.executable,'-B',str(product/'collect_returns.py'),'--root',str(inside),f'--root={outside}','list'],capture_output=True)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertTrue((outside/'jobs').is_dir())
