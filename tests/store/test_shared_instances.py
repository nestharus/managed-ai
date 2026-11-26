#!/usr/bin/env python3
"""Bounded behavioral acceptance cases for one engine and independent stores."""
if __package__:
    from .native_test_paths import APP, ENGINE, PRODUCT
else:
    from native_test_paths import APP, ENGINE, PRODUCT
from pathlib import Path
from tempfile import TemporaryDirectory
import base64
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from managed_ai.store.system import System
from managed_ai.deployment.controller import Deployment, code_identity, encode, exclusive, sha
from managed_ai.store.governance.feedback import Feedback
from managed_ai.store.query.information import Information, record_identity
from managed_ai.store.records import record_import, record_transfer
from managed_ai.store.selection.store_configuration import configure



def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')
    return path


def hashes(root):
    return {p.relative_to(root).as_posix(): sha(p.read_bytes()) for p in root.rglob('*') if p.is_file()}


class SharedInstances(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name) / 'bundle'
        self.bundle.mkdir()
        self.domain_a = self.domain('sample-a', 'alpha-source', 'Alpha complete source.\n')
        self.domain_b = self.domain('sample-b', 'beta-source', 'Beta complete source.\n')
        self.a, self.b = self.bundle / 'stores/a', self.bundle / 'stores/b'
        System.initialize(self.a, {'contribution': 'First independent receiving work.'},
                          instance_id='instance-a', domains=[self.domain_a])
        System.initialize(self.b, {'contribution': 'Second independent receiving work.'},
                          instance_id='instance-b', domains=[self.domain_b])
        self.sa, self.sb = System(self.a), System(self.b)

    def domain(self, name, source_id, text, *, sealed=False, schema=None):
        root = self.bundle / 'domains' / name
        root.mkdir(parents=True)
        source = root / 'source.md'; source.write_text(text, encoding='utf-8', newline='\n')
        catalogue = write(root / 'catalogue.json', {'format': 'managed-ai-source-catalogue/1', 'sources': [
            {'id': source_id, 'path': 'source.md', 'sha256': sha(source.read_bytes()), 'bytes': source.stat().st_size,
             'title': source_id, 'kind': 'guide', 'exposure': 'sealed' if sealed else 'available'}]})
        schema = schema or {'type': 'object', 'required': ['answer'], 'properties': {'answer': {'type': 'string'}},
                            'additionalProperties': False}
        return write(root / 'domain.json', {'format': 'managed-ai-domain/1', 'id': name, 'version': '1',
            'engine_api': 'managed-ai-runtime/1', 'source_root': '.',
            'catalogue': {'path': 'catalogue.json', 'sha256': sha(catalogue.read_bytes())},
            'schemas': {name + '-judgment': schema}, 'entry_sources': [] if sealed else [source_id]})

    def test_01_shared_code_and_separate_actual_turns(self):
        da, db = self.bundle / 'deployments/a', self.bundle / 'deployments/b'
        pa = Deployment.initialize(da, self.a, ENGINE)
        pb = Deployment.initialize(db, self.b, ENGINE)
        self.assertEqual((pa['engine'], pa['engine_code_sha256']), (pb['engine'], pb['engine_code_sha256']))
        self.assertNotEqual(pa['instance_id'], pb['instance_id'])
        before = hashes(self.b)
        record = write(self.bundle / 'input.json', {'id': 'result', 'kind': 'sample-a-judgment', 'content': {'answer': 'A only'}})
        Deployment(da).system(['put', '--record', str(record), '--expected', '0'])
        result, _, _ = Deployment(da).system(['context', 'owned-return', '--task', 'Inspect the first contribution.'])
        self.assertIn('Alpha complete source.', Path(result['context']).read_text())
        reply = self.bundle / 'reply.md'; reply.write_text('Actual bounded fixture return.\n')
        Deployment(da).system(['receive', 'owned-return', '--reply', str(reply), '--producer', 'fixture participant'])
        self.assertEqual(hashes(self.b), before)
        self.assertFalse((self.b / 'work/records/result.json').exists())
        self.assertFalse((da / 'editions').exists())
        self.assertEqual(list(da.glob('*.py')), [])

    def test_retained_deployment_reopens_status_and_refuses_stale_dispatch_or_staging(self):
        active = self.bundle / 'deployments/active'
        producer = self.bundle / 'deployments/producer'
        first = Deployment.initialize(active, self.a, ENGINE)
        retained = Deployment(active)

        # A sibling deployment prepares an explicitly reviewed selection of store B.
        Deployment.initialize(producer, self.b, ENGINE)
        staged = Deployment(producer).stage('second', ENGINE)
        shutil.copyfile(producer / 'candidate.json', active / 'candidate.json')
        published = retained.activate('second', first['descriptor_sha256'], staged['candidate_sha256'],
                                      'Select the second fixture instance.')

        self.assertEqual(published['instance_id'], 'instance-b')
        self.assertEqual(published['descriptor_sha256'], sha((active / 'deployment.json').read_bytes()))
        self.assertEqual(retained.status(), published)
        with self.assertRaisesRegex(ValueError, 'selection revision conflict'):
            retained.system(['show', 'goal'])
        with self.assertRaisesRegex(ValueError, 'selection revision conflict'):
            retained.stage('stale', ENGINE)
        self.assertFalse((active / 'candidate.json').exists())
        goal, _, _ = Deployment(active).system(['show', 'goal'])
        self.assertEqual(goal, self.sb.show('goal'))

    def test_deployed_put_uses_policy_current_at_owner_admission(self):
        deployment = self.bundle / 'deployments/a'
        Deployment.initialize(deployment, self.a, ENGINE)
        value = {'id': 'decision', 'kind': 'sample-a-judgment', 'content': {'answer': 'First'}}
        self.sa.put(value, 0)
        prior = self.sa.record_store.path('decision').read_bytes()
        update = write(self.bundle / 'update.json', dict(value, content={'answer': 'Second'}))
        spec = write(self.bundle / 'policy.json', {
            'expected_sha256': sha((self.a / 'system.json').read_bytes()),
            'record_retention': 'append-history', 'account': 'Retain the predecessor.'})

        @contextmanager
        def configure_then_lock(path):
            self.assertEqual(path, self.a / 'exchange/owner.lock')
            subprocess.run([sys.executable, '-B', ENGINE / 'store/manage_store.py', '--root', self.a,
                            'configure', '--spec', spec], check=True, capture_output=True)
            with exclusive(path):
                yield

        with patch('managed_ai.deployment.controller.exclusive', configure_then_lock):
            result, _, _ = Deployment(deployment).system(['put', '--record', str(update), '--expected', '1'])
        self.assertEqual(result['record']['previous_object'], sha(prior))
        page = next(System(self.a).record_store.history_pages('decision'))
        self.assertEqual([row['revision'] for row in page['records']], [2, 1])
        self.assertTrue(page['complete'])

    def test_02_schema_enforcement_without_generic_ontology(self):
        good = {'id': 'typed', 'kind': 'sample-a-judgment', 'content': {'answer': 'Qualified.'}}
        self.sa.put(good, 0)
        before = hashes(self.a)
        with self.assertRaisesRegex(ValueError, 'missing required'):
            self.sa.put(dict(good, content={}), 1)
        with self.assertRaisesRegex(ValueError, 'reclassification'):
            self.sa.put(dict(good, kind='note'), 1)
        self.assertEqual(hashes(self.a), before)
        self.sa.put({'id': 'open-content', 'kind': 'unanticipated-interpretation',
                     'content': {'new_distinction': [None, True, {'meaning': 'participant supplied'}]}}, 0)
        contract = self.sa.put({'id': 'contract', 'kind': 'content-contract', 'content': 'Keep the participant supplied interpretation open.'}, 0)
        source = self.sa.show('open-content')
        Information(self.sa).wrap({'source': record_identity(source), 'contract': record_identity(contract), 'account': 'Explicit interpretation binding.'})
        self.assertTrue(Information(self.sa).inspect('open-content')['contracted'])
        Information(self.sa).wrap({'source': record_identity(self.sa.show('typed')), 'contract': record_identity(contract),
                                   'account': 'Explicitly bind the typed payload interpretation.'})
        self.assertEqual(Information(self.sa).inspect('typed')['record']['content']['payload'], {'answer': 'Qualified.'})

    def test_03_unanticipated_domain_is_data_and_import_replays(self):
        domain = self.domain('future-domain', 'future-source', 'Future domain source.\n')
        store = self.bundle / 'stores/future'
        System.initialize(store, {'contribution': 'Use a domain not named in engine code.'}, domains=[domain])
        system = System(store)
        spec = {'format': 'managed-ai-import/1', 'id': 'current-selection', 'records': [
            {'id': 'new-judgment', 'kind': 'future-domain-judgment', 'content': {'answer': 'Owned by this store.'}}]}
        first = record_import.import_records(system, spec)
        second = record_import.import_records(system, spec)
        self.assertEqual(first, second)
        self.assertEqual(system.show('new-judgment')['revision'], 1)
        changed = copy.deepcopy(spec); changed['records'][0]['content']['answer'] = 'Different input'
        with self.assertRaisesRegex(ValueError, 'different current input'):
            record_import.import_records(system, changed)
        domain_data = json.loads(domain.read_text()); domain_data['version'] = '2'
        write(domain, domain_data)
        with self.assertRaisesRegex(ValueError, 'manifest changed'):
            System(store)
        config = write(self.bundle / 'configuration.json', {
            'expected_sha256': sha((store / 'system.json').read_bytes()),
            'domains': [str(domain)], 'account': 'Requalify the changed domain manifest with the same payload schema.'})
        command = subprocess.run([sys.executable, '-B', str(ENGINE / 'store/manage_store.py'), '--root', str(store),
                                  'configure', '--spec', str(config)], capture_output=True, text=True)
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(System(store).show('new-judgment')['revision'], 1)

    def test_04_incompatible_selection_refuses_before_mutation(self):
        bad = self.domain('bad-schema', 'bad-source', 'Body\n', schema={'type': 'object', 'imaginaryKeyword': True})
        destination = self.bundle / 'stores/bad'
        with self.assertRaisesRegex(ValueError, 'unsupported structural'):
            System.initialize(destination, {'contribution': 'Must refuse.'}, domains=[bad])
        self.assertFalse(destination.exists())
        raw = json.loads(bad.read_text()); raw['schemas'] = {}; raw['engine_api'] = 'future-api'
        write(bad, raw)
        with self.assertRaisesRegex(ValueError, 'incompatible domain'):
            System.initialize(destination, {'contribution': 'Must refuse.'}, domains=[bad])
        self.assertFalse(destination.exists())
        dep = self.bundle / 'deployments/a'
        Deployment.initialize(dep, self.a, ENGINE)
        original = (dep / 'deployment.json').read_bytes()
        deployment = Deployment(dep)
        staged = deployment.stage('candidate', ENGINE)
        candidate = json.loads((dep / 'candidate.json').read_text())
        candidate['store']['instance_id'] = 'another-instance'
        write(dep / 'candidate.json', candidate)
        with self.assertRaisesRegex(ValueError, 'different store'):
            deployment.activate('candidate', sha(original), sha(encode(candidate)), 'Fixture incompatible candidate.')
        self.assertEqual((dep / 'deployment.json').read_bytes(), original)
        stub = self.bundle / 'incompatible-engine'
        write(stub / 'placeholder.json', {})
        p = stub / 'store/selection/domain_selection.py'; p.parent.mkdir(parents=True)
        p.write_text("ENGINE_API = 'future-api'\nSTORE_SCHEMA = 'managed-ai-store/1'\n")
        (stub / 'store/system.py').write_text("DEPLOYMENT_API = 'agent-system-deployment/2'\n")
        with self.assertRaisesRegex(ValueError, 'incompatible'):
            deployment.stage('bad-engine', stub)
        self.assertEqual((dep / 'deployment.json').read_bytes(), original)

    def test_05_scoped_catalogue_exact_bytes_and_sealed_exposure(self):
        self.assertEqual(self.sa.catalogue('alpha-source')['body'], 'Alpha complete source.\n')
        with self.assertRaisesRegex(ValueError, 'unknown technique'):
            self.sa.catalogue('beta-source')
        sealed = self.domain('sealed-fixture', 'sealed-fixture-source', 'Synthetic sealed body.\n', sealed=True)
        store = self.bundle / 'stores/sealed'
        System.initialize(store, {'contribution': 'Respect explicit source exposure.'}, domains=[sealed])
        system = System(store); protected = sealed.parent / 'source.md'
        real_read = Path.read_bytes
        def guard(path):
            if path.resolve() == protected.resolve():
                raise AssertionError('Protected fixture body was read')
            return real_read(path)
        with patch.object(Path, 'read_bytes', guard):
            self.assertEqual(system.catalogue(query='sealed-fixture-source')['matches'], 1)
            system.catalogue(query='sealed-fixture-source', content=True)
            with self.assertRaisesRegex(ValueError, 'sealed source'):
                system.catalogue('sealed-fixture-source')
            with self.assertRaisesRegex(ValueError, 'sealed source'):
                system.context('sealed-request', {'techniques': ['sealed-fixture-source']})
        source = self.domain_a.parent / 'source.md'; source.write_text('Changed without selection.\n')
        with self.assertRaisesRegex(ValueError, 'source reference changed'):
            self.sa.catalogue('alpha-source')

    def test_installed_catalogue_rebinds_roots_on_each_read(self):
        from managed_ai.store.selection.installed_catalogue import InstalledCatalogue

        app = self.bundle / 'sample-app'
        app.mkdir()
        first, second = (self.bundle / name for name in ('first', 'second'))
        first.mkdir()
        second.mkdir()
        selected = app / 'selected'
        selected.symlink_to(first, target_is_directory=True)
        entries = [{'id': ident, 'source_root_relative': 'selected'} for ident in ('a', 'b')]
        entries.append({'id': 'c', 'source_root_relative': '../second'})
        write(app / 'catalogue.json', {'entries': entries, 'aliases': [{'id': 'alias', 'target': 'a'}]})
        catalogue = InstalledCatalogue(app)

        def roots():
            return {e['id']: e['source_root'] for e in catalogue.index()['entries']}

        self.assertEqual(roots(), {'a': str(first), 'b': str(first), 'c': str(second), 'alias': str(first)})
        selected.unlink()
        selected.symlink_to(second, target_is_directory=True)
        self.assertEqual(roots(), dict.fromkeys(('a', 'b', 'c', 'alias'), str(second)))

    def test_06_portable_archive_uses_relocated_shared_engine(self):
        # An isolated release fixture, not a deployment-produced engine copy.
        engine = self.bundle / 'src' / 'managed_ai'
        shutil.copytree(ENGINE, engine, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        dep = self.bundle / 'deployments/a'
        Deployment.initialize(dep, self.a, engine)
        moved = self.bundle.with_name('relocated')
        self.bundle.rename(moved)
        command = [sys.executable, '-B', str(moved / 'src/managed_ai/deployment/select_deployment.py'),
                   '--deployment', str(moved / 'deployments/a'), 'system']
        result = subprocess.run([*command, 'status'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status['instance_id'], 'instance-a')
        self.assertEqual(status['engine_entry'], str(moved / 'src/managed_ai/store/system.py'))
        result = subprocess.run([*command, 'context', 'after-move', '--task', 'Current use after relocation.'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Alpha complete source.', Path(json.loads(result.stdout)['context']).read_text())

    def test_07_foreign_observation_and_native_feedback_keep_ownership(self):
        self.sb.put({'id': 'finding', 'kind': 'note', 'content': {'finding': 'A returned contribution changed the receiver question.'}}, 0)
        before = hashes(self.b)
        bundle = self.bundle / 'finding-transfer.json'
        record_transfer.export(self.sb, 'finding', bundle)
        received = record_transfer.receive(self.sa, bundle, 'observed-finding', 'Inspect a foreign finding without adopting it.', 0)
        self.assertTrue(record_transfer.receive(self.sa, bundle, 'observed-finding', 'Replay exact arrival.', 0)['replayed'])
        self.assertEqual(record_transfer.inspect(self.sa, 'observed-finding')['source_record'], self.sb.show('finding'))
        with self.assertRaisesRegex(ValueError, 'transfer operations'):
            self.sa.put({'id': 'observed-finding', 'kind': 'source-observation', 'content': {}}, 1)
        candidate = self.sa.put({'id': 'proposal', 'kind': 'note', 'content': 'A receiver-specific explanation.'}, 0)
        outside = self.sa.show('observed-finding')
        feedback = Feedback(self.sa)
        feedback.apply('open', {'id': 'receiving-test', 'expected': 0, 'scope': 'Bounded local fixture.',
            'candidate': record_identity(candidate), 'outside': record_identity(outside), 'references': [],
            'forecast': 'The imported finding is separately inspected.', 'rival': 'Arrival is mistaken for acceptance.'})
        evidence = self.sa.put({'id': 'observed-use', 'kind': 'note', 'content': {
            'subject': record_identity(candidate), 'observed': 'The imported source and the receiver proposal were compared.',
            'exercised': [{'role': 'outside', 'reference': record_identity(outside), 'observed': 'Complete imported source inspected.'}]}}, 0)
        feedback.apply('observe', {'id': 'receiving-test', 'expected': 1, 'subject': record_identity(candidate),
            'observation': record_identity(evidence), 'interpretation': 'This source supports the bounded receiving proposal.',
            'route': 'review', 'next': 'Receiving owner judges the scoped use.',
            'witnesses': [{'role': 'outside', 'reference': outside['id'], 'account': 'Source and receiver roles kept distinct.'}]})
        feedback.apply('accept', {'id': 'receiving-test', 'expected': 2, 'owner': 'fixture receiving owner', 'account': 'Accepted this bounded use.'})
        self.assertEqual(feedback.inspect('receiving-test')['route']['state'], 'accepted')
        self.assertIsNone(self.sa.show('receiving-test')['previous_object'])
        self.assertEqual(hashes(self.b), before)
        self.assertEqual(received['source']['instance_id'], 'instance-b')

    def test_08_current_use_retention_pins_and_attached_host(self):
        self.sa.put({'id': 'changing', 'kind': 'note', 'content': {'value': 0}}, 0)
        pinned = self.sa.record_store.pin(self.sa.show('changing'))
        objects = set(self.sa.ex.objects.directory.iterdir())
        for revision in range(1, 81):
            self.sa.put({'id': 'changing', 'kind': 'note', 'content': {'value': revision}}, revision)
        self.assertEqual(set(self.sa.ex.objects.directory.iterdir()), objects)
        self.assertEqual(json.loads(self.sa.ex.objects.read(pinned['object']))['content'], {'value': 0})
        history = next(self.sa.record_store.history_pages('changing'))
        self.assertEqual([r['revision'] for r in history['records']], [81])
        self.assertFalse(history['complete']); self.assertIsNone(history['cursor'])
        self.assertEqual(history['retention_boundary']['retained_from_revision'], 81)
        archive = self.bundle / 'stores/explicit-history'
        System.initialize(archive, {'contribution': 'Explicit retained revision history.'}, record_retention='append-history')
        system = System(archive)
        for revision in range(3): system.put({'id': 'item', 'kind': 'note', 'content': revision}, revision)
        self.assertEqual([r['revision'] for r in next(system.record_store.history_pages('item'))['records']], [3, 2, 1])
        candidate = system.put({'id': 'legacy-candidate', 'kind': 'note', 'content': 'Legacy candidate.'}, 0)
        plan = {'candidate': record_identity(candidate), 'outside': None, 'references': [],
                'forecast': 'A bounded return resolves the use.', 'rival': 'The unresolved hold remains.'}
        feedback = Feedback(system)
        feedback.apply('open', {'id': 'legacy-feedback', 'expected': 0, 'scope': 'Legacy carried-route fixture.', **plan})
        observation = system.put({'id': 'legacy-observation', 'kind': 'note', 'content': {
            'subject': record_identity(candidate), 'observed': 'A hold was observed.'}}, 0)
        feedback.apply('observe', {'id': 'legacy-feedback', 'expected': 1, 'subject': record_identity(candidate),
            'observation': record_identity(observation), 'interpretation': 'The hold still needs work.',
            'route': 'hold', 'next': 'Repair the condition.', 'witnesses': []})
        feedback.apply('revise', {'id': 'legacy-feedback', 'expected': 2, 'reason': 'Carry the outstanding hold.', **plan})
        legacy = system.show('legacy-feedback')
        legacy['content']['carried_returns'][0].pop('route')
        legacy['sha256'] = sha(encode({k: v for k, v in legacy.items() if k != 'sha256'}))
        system.record_store.path('legacy-feedback').write_bytes(encode(legacy))
        configure(system, {'expected_sha256': sha((archive / 'system.json').read_bytes()),
                           'record_retention': 'current-use', 'account': 'Use current heads and selected evidence.'})
        system = System(archive); feedback = Feedback(system)
        feedback.apply('revise', {'id': 'legacy-feedback', 'expected': 3, 'reason': 'Resolve the legacy route before changing retention.', **plan})
        self.assertIsNone(system.show('legacy-feedback')['previous_object'])
        self.assertEqual(system.show('legacy-feedback')['content']['carried_returns'][0]['route'], 'hold')
        self.assertTrue(feedback.inspect('legacy-feedback')['route']['has_unresolved_hold'])
        self.sa.put({'id': 'duty', 'kind': 'obligation', 'content': {'text': 'An existing owned duty.', 'disposition': 'open'}}, 0)
        spec = importlib.util.spec_from_file_location('shared_host_fixture', ENGINE / 'host/continuing_work/continuing_work.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        host_root = self.a / 'host'
        commission = self.bundle / 'commission.md'; commission.write_text('Observe the existing native commission.\n')
        goal_before = self.sa.show('goal')
        host = module.Host(host_root)
        result = host.init({'engine': str(ENGINE / 'store/manage_store.py'), 'native_root': str(self.a),
                           'commission': str(commission), 'duties': [{'id': 'duty', 'text': 'An existing owned duty.'}]})
        self.assertTrue(result['attached']); self.assertEqual(result['instance_id'], 'instance-a')
        self.assertFalse((host_root / 'native').exists())
        self.assertEqual(self.sa.show('goal'), goal_before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
