import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class Coverage(unittest.TestCase):
    def test_unit_and_supersession(self):
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            area = work / 'coverage'
            shutil.copytree(Path(__file__).parent / 'coverage', area)
            product = work / 'product'
            product.write_text('Load only readable tags.')
            observation = work / 'observation.json'

            def run(action, *args, refused=False):
                result = subprocess.run([sys.executable, '-B', area/'scope.py','--ledger',work/'ledger',
                                         action,product,*args],text=True,capture_output=True)
                self.assertEqual(result.returncode, 2 if refused else 0, result.stderr)
                return None if refused else json.loads(result.stdout)

            init = run('init')
            issued = run('issue','dispatch','--ids','tag')
            row = issued['items'][0]
            evidence = dict(id='tag', unit='entire load',contrast=row['contrast'],sample_target='parcel with unreadable tag',
                            observed_decision='held it',artifact_quote='readable tags',artifact_offset=10,
                            owner_quote='Hold unreadable tags.',reviewer='operator test',result='pass',reason='The held parcel exercises the exclusion.')
            observation.write_text(json.dumps(evidence))
            run('receive',issued['ticket'],observation,refused=True)
            evidence['unit'] = row['unit']
            observation.write_text(json.dumps(evidence))
            run('receive',issued['ticket'],observation)
            self.assertEqual(run('status')['counts'],{'open':1,'pass':1})
            old_ticket = run('issue','dispatch')['ticket']
            registry = json.loads((area/'registry.json').read_text())
            registry['tag']['interpretation'] = 'Check the physical tag even when the register certifies the group.'
            (area/'registry.json').write_text(json.dumps(registry))
            run('status',refused=True)
            run('supersede',init['basis'],'A register-only sample omitted the unreadable physical tag.')
            self.assertEqual(run('status')['counts'],{'open':2})
            run('receive',old_ticket,observation,refused=True)
            ticket = run('issue','eligibility')['ticket']
            product.write_text('Hold the unreadable tag.')
            run('reconcile')
            run('receive',ticket,observation,refused=True)
