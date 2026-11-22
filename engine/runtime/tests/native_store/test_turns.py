"""Reopen a native context after actual process exits at its two local owners."""
from native_test_paths import APP, ENGINE, PRODUCT
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


# Only the injected process installs these cut points. Readers and subsequent commands use
# the ordinary CLI in fresh processes. Runs on the source tree and the extracted product.
CUT = """
import os,sys
sys.path.insert(0,sys.argv.pop(1))
point=sys.argv.pop(1)
import system,turn_preparation
from continuation_support import attention
if point=='private-packet':
    turn_preparation.TurnPreparation.seal=lambda *args: os._exit(61)
elif point=='sealed':
    turn_preparation.TurnPreparation.resume=lambda *args: os._exit(61)
elif point=='attention-installed':
    write=attention._atomic_bytes
    def cut(*args):
        write(*args)
        if args[0].parent.name == 'attention': os._exit(61)
    attention._atomic_bytes=cut
elif point=='published':
    turn_preparation.TurnPreparation.cleanup_committed=lambda *args: os._exit(61)
else: raise ValueError(point)
system.main()
"""


class TurnCommit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base/'owner'
        self.call('init','--goal-text','Schedule the loading bay.')
        self.source = self.root/'work/sources/notice'
        self.source.write_text('Only the south bay is clear.')
        self.request = self.base/'request.json'
        self.request.write_text(json.dumps({
            'task':'Route a cart.', 'sources':['sources/notice'], 'targets':['outputs/route'],
            'attention':{'items':[{'id':'south','use':'Assign a bay.','question':'Is the bay clear?'}]}
        }))

    def call(self,*args,cut=None,expected=0):
        program = [str(PRODUCT/'manage_store.py')] if not cut else [
            '-c',CUT,str(PRODUCT/'native'),cut]
        p=subprocess.run([sys.executable,'-B',*program,'--root',self.root,*map(str,args)],
                         capture_output=True,text=True)
        self.assertEqual(p.returncode,expected,p.stderr)
        return json.loads(p.stdout or p.stderr) if p.stdout or p.stderr else {}

    def interrupt(self,point):
        self.call('context','cart','--request',self.request,cut=point,expected=61)

    def index(self):
        return json.loads((self.root/'exchange/attention/index.json').read_bytes())['body']

    def public_packet(self):
        return self.root/'exchange/packets/cart'

    def other_work(self):
        self.call('context','west','--task','Plan the independent west entrance.')
        return self.index()['feedbacks']['turn-west']

    def test_private_and_public_crashes(self):
        self.interrupt('private-packet')
        self.assertEqual(self.call('status')['incomplete_turns']['cart']['phase'],'draft')
        self.assertFalse(self.public_packet().exists())
        self.assertNotIn('turn-cart',self.index()['feedbacks'])
        self.source.write_text('New east entrance is clear.')
        self.interrupt('published')  # Same identity, new selection because the first issue was private.
        self.assertTrue(self.public_packet().exists())
        self.assertIn('turn-cart',self.index()['feedbacks'])
        seen = self.call('recover','cart')
        self.assertIn(self.source.read_text(),seen['context'])
        self.assertNotIn('Only the south bay is clear.',seen['context'])
        self.assertEqual(self.call('resume-context','cart'),seen)

    def test_resume_preserves_exact_source_and_publication_guard(self):
        self.interrupt('sealed')
        before=self.call('status')['incomplete_turns']['cart']
        self.assertFalse(self.public_packet().exists())
        self.assertNotIn('turn-cart',self.index()['feedbacks'])
        self.assertIn('resume-context',self.call('recover','cart',expected=2)['error'])
        self.source.write_text('South bay is obstructed.')
        saved=self.call('resume-context','cart')
        self.assertEqual(saved['request_sha256'],before['request_sha256'])
        self.assertEqual(saved['context_sha256'],before['context_sha256'])
        self.assertIn('Only the south bay is clear.',saved['context'])
        self.assertNotIn('South bay is obstructed.',saved['context'])
        reply = self.base/'route.txt';reply.write_text('Send the cart south.')
        self.call('receive','cart','--reply',reply,'--producer','bay desk','--proposal','outputs/route='+str(reply))
        self.assertIn('basis changed',self.call('adopt','cart','--target','outputs/route','--reason','Use the route.',expected=2)['error'])
        self.assertFalse((self.root/'work/outputs/route').exists())
        self.assertTrue((self.public_packet()/'return.json').is_file())

    def test_sealed_before_independent_change(self):
        self.interrupt('sealed')
        sibling=self.other_work()
        # An independent use of Attention can even claim the same text. Only the
        # exact occurrence warrants resuming; a different one must allow withdrawal.
        result=subprocess.run([sys.executable,'-B',PRODUCT/'track_attention.py','--root',self.root/'exchange',
                               'feedback','turn-cart'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        competing=self.index()['feedbacks']['turn-cart']
        self.assertIn('changed',self.call('resume-context','cart',expected=2)['error'])
        self.assertFalse(self.public_packet().exists())
        self.assertEqual(self.index()['feedbacks']['turn-cart'],competing)
        self.assertEqual(self.index()['feedbacks']['turn-west'],sibling)
        self.call('abandon-context','cart','--account','The desk has already served another bay. Recheck both selections.')
        self.call('context','cart-next','--request',self.request)
        self.assertIn('turn-cart-next',self.index()['feedbacks'])

    def test_installed_before_independent_change(self):
        self.interrupt('attention-installed')
        original=self.index()['feedbacks']['turn-cart']
        self.assertFalse(self.public_packet().exists())
        sibling=self.other_work()
        self.assertIn('resume-context',self.call('abandon-context','cart','--account','Attempt a replacement.',expected=2)['error'])
        first=self.call('resume-context','cart')
        self.assertTrue(self.public_packet().exists())
        self.assertEqual(self.call('resume-context','cart'),first)
        self.assertEqual(self.index()['feedbacks']['turn-cart'],original)
        self.assertEqual(self.index()['feedbacks']['turn-west'],sibling)
        reply = self.base/'route.txt';reply.write_text('Send the cart south.')
        self.call('receive','cart','--reply',reply,'--producer','bay desk','--proposal','outputs/route='+str(reply))
        self.call('adopt','cart','--target','outputs/route','--reason','Take the desk route against the current bay.')
        self.assertEqual((self.root/'work/outputs/route').read_bytes(),reply.read_bytes())

    def test_corrupt_candidate_withholds_both(self):
        self.interrupt('sealed')
        path=self.root/'exchange/packets/.sealed-cart/context.md'
        path.write_bytes(path.read_bytes()+b'Substituted route.')
        self.assertIn('changed',self.call('resume-context','cart',expected=2)['error'])
        self.assertNotIn('turn-cart',self.index()['feedbacks'])
        self.assertFalse(self.public_packet().exists())

    def test_wrong_draft_withholds_both(self):
        self.interrupt('sealed')
        path=self.root/'work/.system/turns/cart/attention-draft/index.json'
        path.write_bytes(b'{}')
        self.assertIn('changed',self.call('resume-context','cart',expected=2)['error'])
        self.assertNotIn('turn-cart',self.index()['feedbacks'])
        self.assertFalse(self.public_packet().exists())


if __name__=='__main__':
    unittest.main()
