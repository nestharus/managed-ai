"""Exercise continuing-work recovery with real native packets and disposable host roots."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / 'src'
ENGINE_ROOT = SRC_ROOT / 'managed_ai'
HOST_DIR = ENGINE_ROOT / 'host/continuing_work'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from managed_ai.host.continuing_work.continuing_work import Host

HERE = HOST_DIR
ENGINE = (
    ENGINE_ROOT / 'store/manage_store.py'
    if (ENGINE_ROOT / 'store/manage_store.py').is_file()
    else REPO_ROOT / 'engine/runtime/manage_store.py'
)
COLLECT_RETURNS = (
    ENGINE_ROOT / 'core/exchange/transport/collector.py'
    if (ENGINE_ROOT / 'core/exchange/transport/collector.py').is_file()
    else ENGINE.with_name('collect_returns.py')
)
# Drop a successful native reply before the host can log it or update its scope.
# Recovery runs the ordinary CLI in a fresh process.
CUT = """
import os, sys
sys.path.insert(0, sys.argv.pop(1))
from managed_ai.host.continuing_work import continuing_work as host
point = sys.argv.pop(1)
call = host.Host.call
event = host.Host.event
def before(self, action, *args, **kwargs):
    if action == 'receive':
        os._exit(73)
    return call(self, action, *args, **kwargs)
def cut(self, action, value):
    if action == 'native-call' and value['argv'][5] == point and value['exit'] == 0:
        os._exit(73)
    return event(self, action, value)
if point == 'before-receive':
    host.Host.call = before
else:
    host.Host.event = cut
host.main()
"""

# Stop after the target replacement, just before the native completion receipt.
PUBLICATION_CUT = """
import os, sys
sys.path.insert(0, sys.argv.pop(1))
try:
    from managed_ai.store import system
    from managed_ai.core.exchange import publication
except ImportError:
    try:
        from store import system
        from core.exchange import publication
    except ImportError:
        import system
        from continuation_support.exchange import publication
write = publication.write_json
def cut(path, value):
    if value.get('phase') == 'complete':
        os._exit(74)
    write(path, value)
publication.write_json = cut
system.main()
"""


class HandoffRecovery(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        commission = self.base / 'commission.md'
        commission.write_text('Determine which gauge needs attention.')
        self.host = Host(self.base / 'host')
        self.host.init({'commission': str(commission), 'engine': str(ENGINE),
                        'duties': [{'id': 'gauge', 'text': 'Report the gauge reading.'}]})
        self.material = self.base / 'reading'
        self.material.write_text('Gauge reads 12.')
        self.resume_spec = {'account': 'Check the retained gauge observation.'}

    def prepare(self, sid, outputs=('reading.md',)):
        self.host.prepare({'id': sid, 'task': 'Read the gauge.', 'prediction': 'It reads 12.',
                           'duty_ids': ['gauge'], 'outputs': list(outputs), 'sources': []})

    def cli(self, action, sid, spec, *, cut=None, code=0):
        entry = ['-c', CUT, str(SRC_ROOT), cut] if cut else [str(HERE / 'continuing_work.py')]
        spec_path = self.base / 'command.json'
        spec_path.write_text(json.dumps(spec))
        result = subprocess.run([sys.executable, '-B', *entry, '--root', str(self.host.root),
                                 action, sid, '--spec', str(spec_path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, code, result.stderr)
        return json.loads(result.stdout) if code == 0 else result.stderr

    def receive(self, sid, outputs=('reading.md',), **options):
        return self.cli('receive', sid, dict(producer='meter reader', reply=str(self.material),
                        proposals={target: str(self.material) for target in outputs},
                        account='Retain the reading.'), **options)

    def scope(self, sid):
        return json.loads((self.host.root / 'scopes' / (sid + '.json')).read_bytes())

    def test_pending_carrier_keeps_its_original_directory_and_attempt(self):
        # A real child has finished. Its collector is waiting for the nominated
        # file, and loses the reply before it can tell the host what is missing.
        self.prepare('old')
        self.prepare('next')
        current = self.base / 'station'
        current.mkdir()
        argv = self.base / 'worker.json'
        argv.write_text(json.dumps([sys.executable, '-c', 'print("Gauge reads 12.")', '{context}']))
        def dispatch(sid, stdout=subprocess.PIPE):
            return subprocess.run([sys.executable, '-B', str(ENGINE), '--root', str(self.host.native),
                                   'run', sid, '--argv-json', str(argv), '--wait', '10',
                                   '--producer', 'meter reader', '--proposal',
                                   f'outputs/{sid}/reading.md=reading'], cwd=current, stdout=stdout,
                                  stderr=subprocess.PIPE)
        old = dispatch('old', subprocess.DEVNULL)
        self.assertEqual(old.returncode, 0, old.stderr)
        self.assertIsNone(old.stdout)
        pending = self.host.call('receive', '--run-nominations', '--', 'old')['return']
        self.assertFalse(pending['received'])
        self.assertEqual(pending['carrier']['result']['returncode'], 0)
        self.assertEqual(pending['proposal_error']['file'], 'reading')
        original_attempt = pending['carrier']['result']['attempt_id']
        (current / 'reading').write_bytes(self.material.read_bytes())
        current.rename(self.base / 'retained-old-station')
        current.mkdir()
        (current / 'reading').write_bytes(self.material.read_bytes())
        following = dispatch('next')
        self.assertEqual(following.returncode, 0, following.stderr)
        returned = json.loads(following.stdout)['return']
        self.assertEqual(returned['packet'], 'next')
        self.assertNotEqual(returned['transport']['carrier']['result']['attempt_id'], original_attempt)
        with self.assertRaisesRegex(ValueError, 'proposal origin directory changed'):
            self.host.call('receive', '--run-nominations', '--', 'old')
        self.assertIsNone(self.host.inspect_native('old')['return'])
        self.assertFalse((self.host.work / 'outputs/old/reading.md').exists())
        carrier = self.host.native / 'exchange/carrier'
        inventory = subprocess.check_output([sys.executable, '-B', str(COLLECT_RETURNS),
                                             '--root', str(carrier), 'list'])
        jobs = {row['job_id']: row['result'] for row in json.loads(inventory)}
        self.assertEqual(set(jobs), {'exchange-old', 'exchange-next'})
        self.assertEqual(jobs['exchange-old']['attempt_id'], original_attempt)
        self.assertEqual(jobs['exchange-old']['streams'], jobs['exchange-next']['streams'])
        self.cli('resume', 'next', self.resume_spec)
        self.assertEqual((self.host.work / 'outputs/next/reading.md').read_bytes(), self.material.read_bytes())
        self.assertEqual(self.host.goal()['content']['obligations'][0]['disposition'], 'open')

    def test_identical_no_output_reply_from_another_source_keeps_the_handoff_open(self):
        self.prepare('old')
        self.prepare('next')
        spec = {'producer': 'meter reader', 'successor': 'next', 'account': 'Use the replacement station.'}
        self.cli('supersede', 'old', spec, cut='before-receive', code=73)
        self.assertIn('no return confirms', self.cli('resume', 'old', self.resume_spec, code=2))
        original = self.host.root / 'requests/old-supersession.md'
        other = self.base / 'independent-note.md'
        other.write_bytes(original.read_bytes())
        self.host.call('receive', 'old', '--reply', other, '--producer', spec['producer'])
        self.assertIn('does not confirm the recorded handoff to next',
                      self.cli('resume', 'old', self.resume_spec, code=2))
        self.receive('next')
        self.assertNotIn('superseded', self.scope('old'))
        self.assertEqual(self.host.inspect_native('old')['return']['transport']['source_path'], str(other))
        row, = [r for r in self.host.status()['scopes'] if r['id'] == 'old']
        self.assertEqual((row['host_state'], row['successor']), ('supersession-needs-reconciliation', 'next'))

    def test_receipt_without_handoff_binding_remains_owned(self):
        self.prepare('old')
        self.host.call('receive', 'old', '--reply', self.material, '--producer', 'meter reader')
        self.assertIn('no recorded successor binding', self.cli('resume', 'old', self.resume_spec, code=2))
        self.assertNotIn('superseded', self.scope('old'))
        self.assertEqual(self.host.inspect_native('old')['publications'], [])

    def test_pending_handoff_does_not_adopt_a_competing_caller_return(self):
        self.prepare('old')
        self.prepare('next')
        spec = {'producer': 'meter reader', 'successor': 'next', 'account': 'Use the replacement station.'}
        self.cli('supersede', 'old', spec, cut='before-receive', code=73)
        self.assertIn('handoff to next', self.receive('old', code=2))
        target = 'outputs/old/reading.md'
        self.host.call('receive', 'old', '--reply', self.material, '--producer', spec['producer'],
                       '--proposal', target + '=' + str(self.material))
        self.assertIn('handoff to next', self.cli('resume', 'old', self.resume_spec, code=2))
        self.assertFalse((self.host.work / target).exists())
        self.receive('next')
        self.assertEqual(self.host.inspect_native('old')['publications'], [])

    def test_interrupted_request_can_finish_only_its_selected_handoff(self):
        self.prepare('old')
        self.prepare('next')
        self.prepare('alternate')
        spec = {'producer': 'meter reader', 'successor': 'next', 'account': 'Use the replacement station.'}
        self.cli('supersede', 'old', spec, cut='before-receive', code=73)
        original = self.scope('old')['supersession_request']
        error = self.cli('supersede', 'old', dict(spec, successor='alternate'), code=2)
        self.assertIn('outstanding handoff to next differs', error)
        self.assertEqual(self.scope('old')['supersession_request'], original)
        self.assertIsNone(self.host.inspect_native('old')['return'])
        completed = self.cli('supersede', 'old', spec)
        self.assertEqual(completed['successor'], 'next')
        self.assertEqual(completed['return_sha256'], self.host.inspect_native('old')['return']['return_sha256'])
        self.assertNotIn('result', self.scope('alternate'))

    def test_already_dispatched_carrier_keeps_its_existing_receiving_route(self):
        self.prepare('bound')
        self.prepare('next')
        argv = self.base / 'worker.json'
        argv.write_text(json.dumps([sys.executable, '-c', 'print("Gauge reads 12.")', '{context}']))
        self.host.call('run', 'bound', '--argv-json', argv)
        spec = {'producer': 'meter reader', 'successor': 'next', 'account': 'Use the replacement station.'}
        self.assertIn('must receive its actual carrier return', self.cli('supersede', 'bound', spec, code=2))
        target = 'outputs/bound/reading.md'
        bound = self.host.call('receive', 'bound', '--producer', 'meter reader', '--wait', 10,
                               '--proposal', target + '=' + str(self.material))['return']
        self.cli('resume', 'bound', self.resume_spec)
        self.assertEqual(self.scope('bound')['result']['return'], bound)
        self.assertNotIn('superseded', self.scope('bound'))
        self.assertEqual((self.host.work / target).read_bytes(), self.material.read_bytes())

    def test_uncertain_target_leaves_sibling_free_to_finish(self):
        self.prepare('old')
        self.receive('old', cut='receive', code=73)
        before = self.host.inspect_native('old')['return']
        target = 'outputs/old/reading.md'
        if ENGINE.parent.name == 'store' and ENGINE.parents[1].name == 'managed_ai':
            native_root = SRC_ROOT
        elif ENGINE.parent.name == 'store':
            native_root = ENGINE.parent.parent
        else:
            native_root = ENGINE.parent / 'native'
        child = subprocess.run([sys.executable, '-B', '-c', PUBLICATION_CUT, str(native_root),
                                '--root', str(self.host.native), 'adopt', 'old', '--target', target,
                                '--reason', 'Publish the selected observation.'], capture_output=True)
        self.assertEqual(child.returncode, 74, child.stderr)
        observed = self.host.inspect_native('old')
        self.assertEqual(observed['return'], before)
        self.assertTrue(observed['publications'][0]['current_target']['matches_proposal'])
        self.assertEqual(observed['publications'][0]['record']['phase'], 'intent')
        error = self.cli('resume', 'old', self.resume_spec, code=2)
        self.assertIn(target, error)
        self.assertIn('unresolved outcome', error)
        self.prepare('sibling')
        self.receive('sibling')
        self.assertEqual(self.host.inspect_native('old')['publications'], observed['publications'])
        self.assertNotIn('result', self.scope('old'))
        self.assertEqual(len(self.scope('sibling')['result']['outputs']), 1)

    def test_reopen_lost_no_output_handoff_after_successor_finishes(self):
        self.prepare('old')
        self.prepare('next')
        self.cli('supersede', 'old', {'producer': 'meter reader', 'successor': 'next',
                 'account': 'The replacement station takes the reading.'}, cut='receive', code=73)
        receipt = self.host.inspect_native('old')['return']
        self.assertEqual((receipt['packet'], receipt['proposals']), ('old', []))
        self.receive('next')
        self.assertNotEqual(self.scope('next')['result']['return']['return_sha256'], receipt['return_sha256'])
        self.assertNotIn('superseded', self.scope('old'))
        recovered = self.cli('resume', 'old', self.resume_spec)
        self.assertEqual((recovered['state'], recovered['successor']), ('superseded-without-output', 'next'))
        self.assertEqual(self.scope('old')['superseded']['return'], receipt)
        self.assertEqual(self.cli('resume', 'old', self.resume_spec)['successor'], 'next')
        self.assertEqual(self.host.inspect_native('old')['publications'], [])
        self.assertEqual(self.host.goal()['content']['obligations'][0]['disposition'], 'open')

    def test_resume_only_the_outstanding_publications(self):
        for point, complete in [('receive', 0), ('adopt', 1)]:
            with self.subTest(point=point):
                sid = 'after-' + point
                outputs = ('reading.md', 'notes.md')
                self.prepare(sid, outputs)
                self.receive(sid, outputs, cut=point, code=73)
                before = self.host.inspect_native(sid)
                self.assertEqual(len(before['publications']), complete)
                self.assertNotIn('result', self.scope(sid))
                sibling = sid + '-sibling'
                self.prepare(sibling)
                self.receive(sibling)
                self.cli('resume', sid, self.resume_spec)
                after = self.host.inspect_native(sid)
                self.assertEqual(after['return'], before['return'])
                self.assertEqual(len(after['publications']), 2)
                published = {p['record']['path']: p['record'] for p in after['publications']}
                self.assertEqual(set(published), {f'outputs/{sid}/{name}' for name in outputs})
                self.assertTrue(all(p['current_target']['matches_proposal'] for p in after['publications']))
                for row in before['publications']:
                    self.assertEqual(published[row['record']['path']], row['record'])
                self.assertTrue(all(p['phase'] == 'complete' for p in published.values()))
                self.assertTrue(self.cli('resume', sid, self.resume_spec)['already_received'])
                result = self.scope(sid)['result']
                self.assertEqual(result['return'], before['return'])
                self.assertEqual(len(result['outputs']), 2)
                calls = [json.loads(line) for line in (self.host.root / 'EVENTS.jsonl').read_text().splitlines()]
                commands = [r['argv'][5:7] for r in calls if r['action'] == 'native-call']
                # Exactly one successful native reply was lost at the selected cut.
                self.assertEqual(commands.count(['receive', sid]), 1 - (point == 'receive'))
                self.assertEqual(commands.count(['adopt', sid]), 2 - (point == 'adopt'))
                self.assertEqual(self.host.goal()['content']['obligations'][0]['disposition'], 'open')
