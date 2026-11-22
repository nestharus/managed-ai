"""Admission and reader agreement for selected schemas, catalogues and workspace editions."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

if __package__:
    from .native_test_paths import ENGINE
else:
    from native_test_paths import ENGINE

from continuation_support.exchange import Exchange
from continuation_support.storage import read_json, sha, write_json
from controller import runtime_binding
from native_runtime.loading import Loading
from native_runtime.selection.domain_selection import validate_payload
from native_runtime.selection.installed_catalogue import InstalledCatalogue
from system import System


class SelectionContracts(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.store = self.base / 'store'
        self.goal = {'contribution': 'Qualify selected owner contracts.'}

    def manifest(self, name, schema):
        path = self.base / (name + '.json')
        write_json(path, {'format': 'managed-ai-domain/1', 'engine_api': 'managed-ai-runtime/1',
                          'id': name, 'schemas': {'reading': schema}})
        return path

    def test_joint_schema_requires_the_same_binding_in_either_order(self):
        left = self.manifest('quantity', {'const': {'arr': [1]}})
        right = self.manifest('condition', {'const': {'arr': [True]}})
        for first, second in ((left, right), (right, left)):
            with self.subTest(first=first.stem):
                with self.assertRaisesRegex(ValueError, f'reading: {first.stem} and {second.stem}'):
                    System.initialize(self.store, self.goal, domains=[first, second])
                self.assertFalse(self.store.exists())
        identical = self.manifest('copy', {'const': {'arr': [1]}})
        System.initialize(self.store, self.goal, domains=[left, identical])
        system = System(self.store)
        system.put({'id': 'sample', 'kind': 'reading', 'content': {'arr': [1]}}, 0)
        self.assertEqual(system.domain_selection.domains, ['quantity', 'copy'])

    def test_literals_keep_scalar_distinctions_at_every_depth(self):
        for literal, other in [(True, 1), (1, True), (1, 1.0), (False, 0)]:
            for key in ('enum', 'const'):
                with self.subTest(key=key, value=repr(literal)):
                    nested = [{'value': literal}]
                    schema = {key: [nested] if key == 'enum' else nested}
                    validate_payload(nested, schema)
                    with self.assertRaises(ValueError):
                        validate_payload([{'value': other}], schema)
        validate_payload([0.0], {'const': [-0.0]})
        validate_payload(1, {'type': ['null', 'number']})

    def test_unused_type_refuses_init_and_configure_before_publication(self):
        bad = self.manifest('measures', {'properties': {'nested': {'type': ['number', 'decimal']}}})
        with self.assertRaisesRegex(ValueError, 'measures.*reading.*decimal'):
            System.initialize(self.store, self.goal, domains=[bad])
        self.assertFalse(self.store.exists())
        System.initialize(self.store, self.goal)
        system = System(self.store)
        descriptor = self.store / 'system.json'
        before, head = descriptor.read_bytes(), system.record_path('goal').read_bytes()
        with self.assertRaisesRegex(ValueError, 'measures.*reading.*decimal'):
            Loading(system).configure({'expected_sha256': sha(before), 'domains': [bad],
                                       'account': 'Qualify the unused declaration.'})
        self.assertEqual(descriptor.read_bytes(), before)
        self.assertEqual(system.record_path('goal').read_bytes(), head)

    def test_workspace_readers_accept_their_issued_editions(self):
        workspace = self.base / 'standalone-work'
        workspace.mkdir()
        exchange = self.base / 'standalone-exchange'
        issued = Exchange.initialize(exchange, workspace)
        self.assertEqual(issued['version'], 1)
        self.assertEqual(Exchange(exchange).workspace, workspace)
        write_json(exchange / 'workspace.json', dict(issued, version=True))
        with self.assertRaisesRegex(ValueError, 'another workspace'):
            Exchange.initialize(exchange, workspace)

        System.initialize(self.store, self.goal, instance_id='fixture')
        descriptor = self.store / 'exchange/workspace.json'
        portable = read_json(descriptor)
        self.assertEqual(portable['version'], 2)
        self.assertEqual(runtime_binding(self.store)['instance_id'], 'fixture')
        head = System(self.store).record_path('goal').read_bytes()
        absolute = dict(issued, workspace=str(self.store / 'work'))
        write_json(descriptor, absolute)
        self.assertEqual(System(self.store).show('goal')['revision'], 1)
        with self.assertRaisesRegex(ValueError, 'portable store binding'):
            runtime_binding(self.store)
        for bad in [dict(workspace=absolute['workspace']), dict(absolute, version=True),
                    dict(absolute, version=99), dict(portable, version=2.0),
                    dict(portable, instance_id='another')]:
            with self.subTest(binding=bad):
                write_json(descriptor, bad)
                prior = descriptor.read_bytes()
                with self.assertRaises(ValueError):
                    System(self.store)
                with self.assertRaises(ValueError):
                    runtime_binding(self.store)
                self.assertEqual(descriptor.read_bytes(), prior)
                self.assertEqual((self.store / 'work/records/goal.json').read_bytes(), head)

    def test_installed_catalogue_refuses_ambiguous_names_and_unknown_formats(self):
        app = self.base / 'catalogue'
        app.mkdir()
        entries = []
        for name in ('left', 'right'):
            data = name.encode()
            (app / name).write_bytes(data)
            entries.append({'id': name, 'body_path': name, 'sha256': sha(data)})
        alias = {'id': 'compat', 'target': 'left'}
        catalogue = InstalledCatalogue(app)
        path = app / 'catalogue.json'
        def capture():
            bodies = []
            def receive(directory, name, data):
                bodies.append(data)
                return name
            catalogue.capture(self.base, ['compat'], receive)
            return bodies
        for tag in ({}, {'format': 'agent-system-catalogue/1'}):
            write_json(path, dict(tag, entries=entries, aliases=[alias]))
            self.assertEqual(catalogue.search('compat')['body'], 'left')
            self.assertEqual(capture(), [b'left'])
        for tag, aliases, error in [
                ({'format': 'agent-system-catalogue/99'}, [alias], 'unsupported installed'),
                ({}, [alias, dict(alias, target='right')], 'ambiguous catalogue alias compat')]:
            write_json(path, dict(tag, entries=entries, aliases=aliases))
            for read in (catalogue.search, lambda: catalogue.search('compat'), capture):
                with self.subTest(error=error, reader=read):
                    with self.assertRaisesRegex(ValueError, error):
                        read()

    def checked_out_domains(self):
        paths = [ENGINE.parent / 'domains' / name / 'domain.json'
                 for name in ('research', 'writing', 'ai-system-building')]
        if not all(path.is_file() for path in paths):
            self.skipTest('integration requires the three pinned domain checkouts')
        return paths

    def test_owed_basis_change_under_either_or_both_domain_owners(self):
        research, writing, building = self.checked_out_domains()
        for domains in ([research], [writing], [research, writing], [research, writing, building]):
            names = [p.parent.name for p in domains]
            with self.subTest(selected=names):
                root = self.base / '-'.join(names)
                System.initialize(root, self.goal, domains=domains)
                system = System(root)
                self.assertEqual(system.domain_selection.domains, names)
                content = {'text': 'Read the evidence.', 'disposition': 'open', 'source_scope': 'fixture'}
                (system.work / 'sources/old').write_bytes(b'first')
                (system.work / 'sources/new').write_bytes(b'second')
                row = {'id': 'duty', 'kind': 'obligation', 'basis': ['sources/old'], 'content': content}
                system.put(row, 0)
                original = system.record_path('duty').read_bytes()
                replacement = dict(row, basis=['sources/new'])
                for account in [None, {'from_revision': 2, 'account': 'Stale.'},
                                {'from_revision': True, 'account': 'Wrong type.'},
                                {'from_revision': 1}, {'from_revision': 1, 'account': ' '}]:
                    update = content if account is None else dict(content, basis_change=account)
                    with self.assertRaises(ValueError):
                        system.put(dict(replacement, content=update), 1)
                    self.assertEqual(system.record_path('duty').read_bytes(), original)
                updated = dict(content, basis_change={'from_revision': 1, 'account': 'Use the second reading.'})
                system.put(dict(replacement, content=updated), 1)
                head = system.show('duty')
                self.assertEqual(head['revision'], 2)
                self.assertEqual(head['basis'][0]['sha256'], sha(b'second'))
                self.assertIn('duty', system.status()['owed_record_obligations'])

    def test_shipped_alias_follows_selection_and_primary_precedence(self):
        research, _, _ = self.checked_out_domains()
        System.initialize(self.store, self.goal)
        system = System(self.store)
        with self.assertRaisesRegex(ValueError, 'unknown technique'):
            system.catalogue('method:01')
        with self.assertRaisesRegex(ValueError, 'unknown technique'):
            system.context('unselected', {'techniques': ['method:01']})
        body = self.base / 'local.txt'
        body.write_bytes(b'Local primary fixture.')
        local = self.base / 'references.json'
        write_json(local, {'format': 'managed-ai-source-catalogue/1', 'sources': [
            {'id': 'method:01', 'path': body.name, 'bytes': body.stat().st_size,
             'sha256': sha(body.read_bytes())}]})
        for name, cats in [('alias', []), ('primary', [local])]:
            Loading(system).configure({'expected_sha256': sha((self.store / 'system.json').read_bytes()),
                                       'account': 'Qualify selected names.', 'domains': [research], 'catalogues': cats})
            system = System(self.store)
            selected = system.catalogue('method:01')
            if name == 'alias':
                self.assertEqual(selected['alias_of'], 'research/current/method.md')
                self.assertEqual(selected['body'], system.catalogue(selected['alias_of'])['body'])
            else:
                self.assertNotIn('alias_of', selected)
                self.assertEqual(selected['body'], body.read_text())
            system.context(name, {'techniques': ['method:01']})
            self.assertIn(selected['body'], system.recover(name)['context'])
