"""Crash a real host process and reopen it. Includes a packet-only receiver control."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
HOST = Path(os.environ.get('G60_TEST_HOST', HERE / 'host.py')).resolve()
spec = importlib.util.spec_from_file_location('tested_prospective_host', HOST)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Host = module.Host
PURPOSE = 'unaided-product-use'
RECEIVER = """import pathlib, sys
packet = pathlib.Path(sys.argv[1])
assert {p.name for p in packet.iterdir()} == {'question.txt','product.txt'}
assert (packet/'question.txt').read_text() == 'Which bin?'
label, bin_number = (packet/'product.txt').read_text().strip().split('=')
assert label == 'bin'
print('I placed the item in bin ' + bin_number + '.')"""


class Trial:
    def __init__(self, directory, route):
        self.d = directory
        self.root = self.d/'journal'
        self.route = route
        for name, body in dict(owner='Retain custody.', product='bin=B', revision='bin=Q', question='Which bin?').items():
            (self.d/name).write_text(body)
        self.pin = module.digest((self.d/'owner').read_bytes())
        self.h = Host(self.root)
        self.h.apply('init', dict(owner='owner', owner_sha256=self.pin, writer='writer', receiver='receiver', reviewer='reviewer', required_human=False), self.d)
        preceding = self.h.apply('project', dict(product='product', owner='owner', passage='Retain custody.', decision='Arrange by bin.', alternative='Use shelf.', obligations=['placement'], purpose=PURPOSE), self.d)
        self.request = dict(event=preceding['digest'], actor='receiver', purpose=PURPOSE, question='question')
        if route == 'issue-review':
            issuance = self.h.apply('issue', self.request, self.d)
            response = self.receive(issuance, 'return')
            preceding = self.h.apply('revise', dict(event=response['digest'], product='product', response='response', revision='revision', uptake='Use the corrected bin Q.'), self.d)
            self.request = dict(self.request, event=preceding['digest'], actor='reviewer')
        self.before = preceding
        self.final = self.root/'outbox'/preceding['digest']
        (self.d/'request.json').write_text(json.dumps(self.request))

    def crash(self, point):
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), 'crash', str(self.d), self.route, point], env=dict(os.environ,G60_TEST_HOST=str(HOST)), capture_output=True)
        assert result.returncode == 37, (result.returncode, result.stderr)
        self.h = Host(self.root)

    def receive(self, issuance, action):
        packet = self.root/'outbox'/issuance['data']['event']
        observed = subprocess.check_output([sys.executable,'-I','-c',RECEIVER,str(packet)], cwd=self.d, text=True).strip()
        (self.d/'response').write_text(observed)
        if action == 'return':
            return self.h.apply(action, dict(event=issuance['digest'], product='product', actor='receiver', response='response', observations={'placement':observed}), self.d)
        return self.h.apply(action, dict(event=issuance['digest'], revision='revision', actor='reviewer', report='response', outcomes={'placement':observed}, human_receipt=None, owner_sha256=self.pin), self.d)


def crash(directory, route, point):
    h = Host(directory/'journal')
    request = json.loads((directory/'request.json').read_text())
    if point in ('before-commit','after-commit'):
        record = h.record
        def interrupted(kind, data):
            if point == 'before-commit':
                os._exit(37)
            record(kind, data)
            os._exit(37)
        h.record = interrupted
    elif point in ('partial-build', 'partial-journal'):
        write = module.write_synced
        def interrupted(path, body):
            if (point == 'partial-build' and path.name == 'product.txt') or (point == 'partial-journal' and path.name == '.events.jsonl.tmp'):
                write(path, body[:len(body)//2])
                os._exit(37)
            write(path, body)
        module.write_synced = interrupted
    elif point == 'after-publish':
        rename = Path.rename
        def interrupted(path, target):
            result = rename(path, target)
            if target.parent.name == 'outbox':
                os._exit(37)
            return result
        Path.rename = interrupted
    h.apply(route, request, directory)
    raise AssertionError('failpoint did not fire')


class Commit(unittest.TestCase):
    def test_crash_boundaries_and_actual_return(self):
        for route in ('issue','issue-review'):
            for point in ('partial-build','partial-journal','before-commit','after-commit','after-publish'):
                with self.subTest(route=route,point=point), tempfile.TemporaryDirectory() as tmp:
                    t = Trial(Path(tmp),route)
                    t.crash(point)
                    latest = t.h.history()[-1]
                    visible_at_restart = t.final.exists()
                    committed = point in ('after-commit','after-publish')
                    self.assertEqual(latest['kind'], route if committed else t.before['kind'])
                    self.assertEqual(visible_at_restart, point=='after-publish')
                    (t.d/'other').write_text('Ship all bins.')
                    # Before a complete private intent exists, another request may still replace it.
                    if point != 'partial-build':
                        with self.assertRaises(ValueError):
                            t.h.apply(route, dict(t.request,question='other'), t.d)
                    if point == 'after-commit':
                        follow = dict(event=latest['digest'], product='product', actor='receiver', response='response', observations={}) if route == 'issue' else dict(event=latest['digest'], revision='revision', actor='reviewer', report='response', outcomes={}, owner_sha256=t.pin, human_receipt=None)
                        with self.assertRaisesRegex(ValueError,'no public packet'):
                            t.h.apply('return' if route == 'issue' else 'review',follow,t.d)
                    issued = t.h.apply(route,t.request,t.d)
                    first_stat = t.final.stat().st_ino
                    self.assertEqual(t.h.apply(route,t.request,t.d), issued)
                    self.assertEqual(t.final.stat().st_ino,first_stat)
                    self.assertEqual(len([e for e in t.h.history() if e['kind']==route]),1)
                    result = t.receive(issued, 'return' if route=='issue' else 'review')
                    observations = result['data'].get('observations', result['data'].get('outcomes'))
                    self.assertEqual(observations['placement'], 'I placed the item in bin '+('B' if route=='issue' else 'Q')+'.')
                    if observations_path := os.environ.get('G60_TEST_COMMIT_OBSERVATIONS'):
                        with Path(observations_path).open('a') as stream:
                            print(json.dumps(dict(route=route, stop=point, last_on_restart=latest['kind'], public_on_restart=visible_at_restart, actual_return=observations['placement'])), file=stream)

    def test_corrupt_tail_preserved_and_broken_packet_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Trial(Path(tmp),'issue')
            with t.h.events.open('ab') as stream:
                stream.write(b'{"sequence":')
            before = t.h.events.read_bytes()
            with self.assertRaisesRegex(ValueError, 'uncertain journal tail'):
                Host(t.root).apply('issue',t.request,t.d)
            self.assertEqual(t.h.events.read_bytes(),before)
            self.assertFalse(t.final.exists())
        with tempfile.TemporaryDirectory() as tmp:
            t = Trial(Path(tmp),'issue')
            t.h.apply(t.route,t.request,t.d)
            (t.final/'product.txt').write_text('bin=Z')
            with self.assertRaisesRegex(ValueError,'changed issued product'):
                t.h.apply(t.route,t.request,t.d)
            self.assertEqual((t.final/'product.txt').read_text(), 'bin=Z')

    def test_legacy_visible_orphan_and_missing_committed_packet(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Trial(Path(tmp),'issue')
            t.final.mkdir(parents=True)
            (t.final/'product.txt').write_text('bin=B')
            (t.final/'question.txt').write_text('Which bin?')
            with self.assertRaisesRegex(ValueError,'uncertain'):
                t.h.apply(t.route,t.request,t.d)
            self.assertEqual(t.h.history()[-1], t.before)
        with tempfile.TemporaryDirectory() as tmp:
            t = Trial(Path(tmp),'issue')
            t.h.apply(t.route,t.request,t.d)
            t.final.rename(t.d/'removed')
            with self.assertRaisesRegex(ValueError,'uncertain'):
                t.h.apply(t.route,t.request,t.d)
            self.assertFalse(t.final.exists())


if __name__ == '__main__':
    if sys.argv[1:2] == ['crash']:
        crash(Path(sys.argv[2]),*sys.argv[3:])
    else:
        unittest.main()
