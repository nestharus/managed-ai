"""Contract checks for the explicit exchange, not semantic efficacy trials."""
import errno
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3] / 'engine'
sys.path.insert(0, str(ROOT))
from core.exchange import Exchange, main


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.work = self.base / 'work'; self.work.mkdir()
        self.root = self.base / 'runtime'
        for name, body in {'brief.md': 'Continue this work.', 'method.md': 'Read what is actually present.',
                           'material.txt': 'an actual source', 'result.txt': 'old result'}.items():
            (self.work/name).write_text(body)
        Exchange.initialize(self.root, self.work)
        self.ex = Exchange(self.root)
        self.reply = self.base/'reply.md'; self.reply.write_text('A caller-supplied interpretation.')
        self.proposal = self.base/'proposed.txt'; self.proposal.write_text('new result')

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, packet='one', targets=None, methods=None, materials=None, returns=None):
        return self.ex.prepare(packet, 'reader', 'brief.md', methods or ['method.md'],
                               materials or ['material.txt'], ['result.txt'] if targets is None else targets,
                               returns or [])

    def received(self, packet='one', target='result.txt'):
        return self.ex.receive(packet, self.reply, 'test broker', [target+'='+str(self.proposal)])

    def test_publication_respects_package_inside_enclosing_workspace(self):
        engine=self.work/'engine'
        engine.mkdir()
        target=engine/'guide.md'
        target.write_text('Installed guide')
        self.prepare(targets=['engine/guide.md','result.txt'])
        self.ex.receive('one',self.reply,'test broker',[
            'engine/guide.md='+str(self.proposal),'result.txt='+str(self.proposal)])
        with patch('core.ownership.__file__',str(engine/'ownership.py')):
            with self.assertRaisesRegex(ValueError,'publication destination'):
                self.ex.publish('one','engine/guide.md','Try to publish inside the running package.')
            self.ex.publish('one','result.txt','Deliver the independent result.')
        self.assertEqual(target.read_text(),'Installed guide')
        self.assertEqual((self.work/'result.txt').read_text(),'new result')

    def test_fresh_process_reopens_external_context(self):
        self.prepare()
        p = subprocess.run([sys.executable, '-B', str(ROOT/'core/cli/exchange_packets.py'), '--root', str(self.root),
                            'inspect', 'one'], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)['consumer'], 'reader')
        self.assertTrue(all(x['unchanged'] for x in json.loads(p.stdout)['basis']))

    def test_module_entry_reopens_external_context(self):
        self.prepare()
        result = subprocess.run(
            [sys.executable, '-B', '-m', 'core.exchange',
             '--root', str(self.root), 'inspect', 'one'],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['consumer'], 'reader')

    def test_inspect_opens_existing_lock_read_only_after_append_denial(self):
        self.prepare()
        self.received()
        expected = self.ex.inspect('one')
        lock = self.root / 'owner.lock'
        original_open = Path.open
        modes = []

        def restricted_open(path, mode='r', *args, **kwargs):
            if path == lock:
                modes.append(mode)
                if mode == 'a+b':
                    raise PermissionError(errno.EACCES, 'Permission denied', str(path))
            return original_open(path, mode, *args, **kwargs)

        argv = ['exchange_packets.py', '--root', str(self.root), 'inspect', 'one']
        with patch.object(Path, 'open', restricted_open), patch.object(sys, 'argv', argv), \
                patch.object(sys, 'stdout', new_callable=io.StringIO) as output:
            main()
        self.assertEqual(modes, ['a+b', 'rb'])
        self.assertEqual(json.loads(output.getvalue()), expected)

    def test_method_and_material_are_frozen_not_live_links(self):
        self.prepare()
        (self.work/'method.md').write_text('changed')
        _, request = self.ex.load('one')
        self.assertEqual(self.ex.get(request['methods'][0]['sha256']), b'Read what is actually present.')
        self.assertFalse(self.ex.inspect('one')['basis'][1]['unchanged'])

    def test_receive_does_not_publish(self):
        self.prepare(); self.received()
        self.assertEqual((self.work/'result.txt').read_text(), 'old result')

    def test_explicit_publication_and_reentry(self):
        self.prepare(); self.received()
        self.ex.publish('one', 'result.txt', 'explicit caller choice')
        self.assertEqual((self.work/'result.txt').read_text(), 'new result')
        self.prepare('two', materials=['result.txt'], returns=['one'])
        _, r = self.ex.load('two')
        self.assertEqual(self.ex.get(r['materials'][0]['sha256']), b'new result')
        self.assertEqual(len(r['returns']), 1)

    def test_changed_basis_refuses_effect_but_keeps_return(self):
        self.prepare(); self.received()
        (self.work/'material.txt').write_text('later source')
        with self.assertRaisesRegex(ValueError, 'basis changed'):
            self.ex.publish('one', 'result.txt', 'attempt')
        self.assertEqual((self.work/'result.txt').read_text(), 'old result')
        self.assertIsNotNone(self.ex.inspect('one')['return'])

    def test_unselected_change_is_not_global_invalidation(self):
        self.prepare(); self.received()
        (self.work/'unselected.txt').write_text('another interest')
        self.ex.publish('one', 'result.txt', 'declared basis unchanged')
        self.assertEqual((self.work/'unselected.txt').read_text(), 'another interest')

    def test_shared_return_different_receivers(self):
        self.prepare(); self.ex.receive('one', self.reply, 'broker', [])
        self.ex.prepare('human', 'human', 'brief.md', ['method.md'], [], [], ['one'])
        self.ex.prepare('agent', 'agent', 'brief.md', [], ['material.txt'], [], ['one'])
        h = self.ex.load('human')[1]; a = self.ex.load('agent')[1]
        self.assertEqual(h['returns'][0]['sha256'], a['returns'][0]['sha256'])
        self.assertNotEqual(h['consumer'], a['consumer'])
        self.assertNotIn('transport', h['returns'][0]['origin'])
        self.assertIn('return_sha256', h['returns'][0]['origin'])
        self.assertIsNone(self.ex.inspect('human')['return'])

    def test_proposal_must_be_selected_target(self):
        self.prepare()
        with self.assertRaises(ValueError):
            self.ex.receive('one', self.reply, 'broker', ['method.md='+str(self.proposal)])
        self.assertFalse((self.ex.directory('one')/'return.json').exists())

    def test_duplicate_packet_return_and_publication_are_not_replayed(self):
        self.prepare()
        with self.assertRaises(ValueError): self.prepare()
        self.received()
        with self.assertRaises(ValueError): self.received()
        self.ex.publish('one', 'result.txt', 'first')
        with self.assertRaises(ValueError): self.ex.publish('one', 'result.txt', 'second')

    def test_context_and_object_corruption_are_visible(self):
        self.prepare()
        context = self.ex.directory('one')/'context.md'
        body = context.read_bytes(); context.write_bytes(body+b'changed')
        with self.assertRaises(ValueError): self.ex.load('one')
        context.write_bytes(body)
        request = self.ex.load('one')[1]
        key = request['materials'][0]['sha256']
        (self.root/'objects'/key).write_text('changed')
        with self.assertRaises(ValueError): self.ex.load('one')

    def test_symlink_and_traversal_rejected(self):
        (self.work/'linked').symlink_to(self.reply)
        for item in ['../reply.md', str(self.reply), 'linked']:
            with self.assertRaises(ValueError):
                self.prepare('p'+str(len(item)), materials=[item])

    def test_new_target_and_no_parent_creation(self):
        self.prepare(targets=['new.txt'])
        self.received(target='new.txt')
        self.ex.publish('one', 'new.txt', 'new artifact')
        self.assertEqual((self.work/'new.txt').read_text(), 'new result')
        self.prepare('two', targets=['absent/new.txt'])
        self.received('two', 'absent/new.txt')
        with self.assertRaisesRegex(ValueError, 'parent'):
            self.ex.publish('two', 'absent/new.txt', 'new artifact')

    def test_partial_multi_file_publication_is_explicit(self):
        self.prepare(targets=['result.txt', 'other.txt'])
        self.ex.receive('one', self.reply, 'broker', ['result.txt='+str(self.proposal), 'other.txt='+str(self.proposal)])
        self.ex.publish('one', 'result.txt', 'first file')
        self.ex.publish('one', 'other.txt', 'second file')
        self.assertEqual((self.work/'other.txt').read_text(), 'new result')

    def test_existing_bound_publication_reports_already_attempted(self):
        self.prepare()
        returned = self.received()
        published = self.ex.publish('one', 'result.txt', 'Use the reviewed proposal.')
        self.assertEqual(published['phase'], 'complete')
        self.assertEqual(published['return_sha256'], returned['return_sha256'])
        before = self.ex.inspect('one')['publications']
        with self.assertRaisesRegex(ValueError, '^publication already attempted; inspect its receipt, do not replay$'):
            Exchange(self.root).publish('one', 'result.txt', 'Repeat the same target.')
        self.assertEqual(self.ex.inspect('one')['publications'], before)
        self.assertEqual((self.work / 'result.txt').read_bytes(), self.proposal.read_bytes())

    def test_dispatch_really_delivers_context_and_collects_return(self):
        self.prepare()
        code = "import os,pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text()); os.write(2,b'diagnostic\\n')"
        self.ex.dispatch('one', [sys.executable, '-B', '-c', code, '{context}'])
        r = self.ex.receive('one', None, 'actual local process', [], wait=8)
        self.assertIn('return_sha256', r)
        self.assertIn('an actual source', self.ex.get(r['reply_sha256']).decode())
        self.assertEqual(r['transport']['carrier']['result']['returncode'], 0)
        self.assertEqual(self.ex.recover_transport('one')['streams']['stderr']['utf8_text'], 'diagnostic\n')

    def test_dispatch_cannot_be_replaced_by_import(self):
        self.prepare()
        self.ex.dispatch('one', [sys.executable, '-B', '-c', 'print("done")', '{context}'])
        with self.assertRaises(ValueError): self.ex.receive('one', self.reply, 'not the command', [])
        self.ex.receive('one', None, 'command', [], wait=8)

    def test_list_recovers_pending_and_returned_without_a_global_done(self):
        self.prepare(); self.received()
        self.prepare('pending')
        rows = {r['packet']: r for r in self.ex.list()}
        self.assertEqual(rows['one']['transport'], 'returned')
        self.assertEqual(rows['pending']['transport'], 'prepared')
        self.assertTrue(all(r['semantic_fulfillment'] == 'not-decided-by-exchange' for r in rows.values()))
        self.assertEqual(self.ex.list('someone-else'), [])

    def test_origin_workspace_unavailable_does_not_erase_return(self):
        import shutil
        self.prepare(); self.received()
        shutil.rmtree(self.work)
        restarted = Exchange(self.root)
        view = restarted.inspect('one')
        self.assertIsNotNone(view['return'])
        self.assertIsNone(view['basis'])
        self.assertTrue(view['basis_error'])
        self.assertEqual(restarted.get(view['return']['reply_sha256']), self.reply.read_bytes())
        self.assertEqual(restarted.list()[0]['transport'], 'returned')

    def test_packet_and_runtime_bounds(self):
        with self.assertRaises(ValueError): Exchange.initialize(self.work/'runtime', self.work)
        with self.assertRaises(ValueError): Exchange.initialize(ROOT/'state', self.work)
        (self.work/'large').write_bytes(b'x'*(2*1024*1024+1))
        with self.assertRaises(ValueError): self.prepare(materials=['large'])

    def test_binary_material_is_preserved_without_fabricated_text(self):
        (self.work/'binary').write_bytes(b'\x00\xff')
        self.prepare(materials=['binary'])
        r = self.ex.load('one')[1]
        self.assertEqual(self.ex.get(r['materials'][0]['sha256']), b'\x00\xff')
        self.assertIn('Binary material', (self.ex.directory('one')/'context.md').read_text())


if __name__ == '__main__':
    unittest.main()
