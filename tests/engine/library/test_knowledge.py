from pathlib import Path
import copy
import json
import sys
import tempfile
import unittest
import zipfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'engine'))
from library.reader import knowledge
from library.bundles import publish


class KnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = self.make_library('first', {'meaning/a.md': b'Complete A\n', 'meaning/b.md': b'Complete B\n'})

    def tearDown(self):
        self.temporary.cleanup()

    def make_library(self, name, bodies):
        directory = self.root / name; directory.mkdir(); (directory / 'objects').mkdir()
        rows = []
        for ident, body in bodies.items():
            h = knowledge.digest(body); obj = 'objects/' + h + '.txt'
            (directory / obj).write_bytes(body)
            rows.append(dict(id=ident, title=ident, summary='Complete supplied body', object=obj,
                sha256=h, bytes=len(body), source=dict(path=ident, sha256=h), link_base=ident,
                references=[dict(target='outside/history.md', anchor='', kind='source')]))
        modules = [dict(id='base', title='Base', purpose='Supply first body', members=[next(iter(bodies))],
            requires=[], parents=[dict(id='outside-parent', role='Historical relation, not required participation')],
            external_participation=['Capable receiver interprets the complete source and supplies authority.'])]
        if len(bodies) > 1:
            modules.append(dict(id='joined', title='Joined use', purpose='Supply both declared contributions',
                members=[list(bodies)[1]], requires=['base'], parents=[], external_participation=['Actual receiving host.']))
        manifest = dict(format=knowledge.FORMAT, edition=name, bodies=rows, modules=modules, provenance={})
        (directory / 'catalogue.json').write_bytes(knowledge.encoded(manifest))
        return knowledge.Library(directory)

    def selection(self, library=None, module='joined'):
        library = library or self.library
        return dict(format=knowledge.SELECTION, purpose='Actual complete-body receiving test', bodies=[],
            modules=[library.pin(library.modules[module], 'module')])

    def test_required_closure_and_full_data_roundtrip(self):
        selection = self.selection(); bundle = self.root / 'out.zip'
        context = knowledge.context([self.library], selection)
        self.assertEqual({x['text'] for x in context['bodies']}, {'Complete A\n', 'Complete B\n'})
        result = knowledge.export([self.library], selection, 'joined-product', bundle)
        self.assertEqual((result['bodies'], result['modules']), (2, 2))
        self.assertTrue(any(x['target'] == 'outside/history.md' for x in result['provenance']['outside_reference_routes']))
        store = self.root / 'store'; store.mkdir()
        first = knowledge.import_bundle(bundle, store); second = knowledge.import_bundle(bundle, store)
        self.assertEqual(first['status'], 'imported-data'); self.assertEqual(second['status'], 'already-present-verified')
        received = knowledge.Library(first['library']); received.verify()
        self.assertEqual(received.body('meaning/a.md'), b'Complete A\n')
        self.assertNotIn('outside-parent', received.modules)
        self.assertIn('No source owner', first['authority'])

    def test_stale_pin_and_complete_body_budget_refuse(self):
        stale = self.selection(); stale['modules'][0]['sha256'] = '0' * 64
        with self.assertRaises(ValueError): knowledge.context([self.library], stale)
        with self.assertRaises(ValueError): knowledge.context([self.library], self.selection(), max_bytes=1)
        self.assertEqual(knowledge.context([self.library], self.selection())['body_bytes'], 22)

    def test_colliding_logical_id_needs_explicit_version_selection(self):
        other = self.make_library('second', {'meaning/a.md': b'Different source A\n'})
        selection = dict(format=knowledge.SELECTION, purpose='Compare both versions', modules=[], bodies=[
            self.library.pin(self.library.bodies['meaning/a.md']), other.pin(other.bodies['meaning/a.md'])])
        self.assertEqual(len(knowledge.context([self.library, other], selection)['bodies']), 2)
        with self.assertRaisesRegex(ValueError, 'collision'): knowledge.product([self.library, other], selection, 'ambiguous')

    def test_missing_required_member_and_unsupported_schema_refuse(self):
        malformed = copy.deepcopy(self.library.catalogue)
        malformed['modules'][0]['members'].append('missing.md')
        with self.assertRaises(ValueError): knowledge.check_manifest(malformed)
        malformed = copy.deepcopy(self.library.catalogue); malformed['format'] = 'knowledge-library/99'
        with self.assertRaises(ValueError): knowledge.check_manifest(malformed)
        malformed = copy.deepcopy(self.library.catalogue); malformed['modules'][0]['members'] = [{}]
        with self.assertRaises(ValueError): knowledge.check_manifest(malformed)
        with self.assertRaises(ValueError): knowledge.decoded(b'{"same":1,"same":2}')
        with self.assertRaises(ValueError): knowledge.decoded(b'{"numeric":NaN}')

    def test_import_rejects_unselected_members_before_store_mutation(self):
        good = self.root / 'good.zip'; knowledge.export([self.library], self.selection(), 'good', good)
        bad = self.root / 'bad.zip'
        with zipfile.ZipFile(good) as source, zipfile.ZipFile(bad, 'w') as target:
            for info in source.infolist(): target.writestr(info, source.read(info.filename))
            target.writestr('../escape.txt', b'Never extracted')
        store = self.root / 'store'; store.mkdir()
        with self.assertRaises(ValueError): knowledge.import_bundle(bad, store)
        self.assertEqual(list(store.iterdir()), [])
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_import_rejects_changed_object_and_symlink_input(self):
        good = self.root / 'good.zip'; knowledge.export([self.library], self.selection(), 'good', good)
        bad = self.root / 'bad.zip'
        with zipfile.ZipFile(good) as source, zipfile.ZipFile(bad, 'w') as target:
            for info in source.infolist():
                body = source.read(info.filename)
                target.writestr(info, b'X' * len(body) if info.filename.startswith('objects/') else body)
        store = self.root / 'store'; store.mkdir()
        with self.assertRaises(ValueError): knowledge.import_bundle(bad, store)
        self.assertEqual(list(store.iterdir()), [])
        link = self.root / 'linked.zip'; link.symlink_to(good)
        with self.assertRaises(ValueError): knowledge.inspect_bundle(link)

    def test_discovery_reports_unexamined_bodies(self):
        result = self.library.discover('complete', scan_limit=1, max_bytes=1024)
        self.assertEqual(result['coverage']['examined_bodies'], 1)
        self.assertEqual(result['coverage']['omitted_body_ids'], ['meaning/b.md'])
        self.assertEqual(self.library.show('meaning/b.md')['text'], 'Complete B\n')

    def test_current_owner_override_and_new_source_inventory_are_checked(self):
        project = self.root / 'project'; (project / 'current').mkdir(parents=True); (project / 'owners').mkdir()
        (project / 'current/a.md').write_text('# Compatibility\n\nThis is only a stub.\n')
        (project / 'owners/a.txt').write_text('# Complete source\n\nFull operating source.\n')
        spec = dict(format='knowledge-source-map/1', families=['current'], additional=[], modules=[],
            overrides={'current/a.md': dict(owner='owners/a.txt', link_base='current/a.md')})
        output = self.root / 'published'
        first = publish.publish(project, None, spec, 'current', output)
        again = publish.publish(project, None, spec, 'current', output)
        self.assertEqual(first['status'], 'published-data'); self.assertEqual(again['status'], 'already-present-verified')
        library = knowledge.Library(output)
        self.assertIn(b'Full operating source', library.body('current/a.md'))
        self.assertEqual(publish.audit(project, None, library, spec)['status'], 'same')
        (project / 'current/new.md').write_text('# New understanding\n\nA newly necessary contribution.\n')
        audit = publish.audit(project, None, library, spec)
        self.assertEqual(audit['status'], 'different')
        self.assertEqual(audit['current_inventory']['missing_current_bodies'], ['current/new.md'])
        with self.assertRaises(ValueError): publish.publish(project, None, spec, 'current', output)


if __name__ == '__main__':
    unittest.main()
