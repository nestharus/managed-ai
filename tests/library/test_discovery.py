"""Receiving failures: incidental matches, useful ranking, and stale preparation."""
from pathlib import Path
from contextlib import closing
import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from tests._paths import ENGINE, SRC
from managed_ai.library.reader import knowledge


def has_fts5():
    if knowledge.sqlite3 is None:
        return False
    try:
        with closing(knowledge.sqlite3.connect(':memory:')) as connection:
            connection.execute('CREATE VIRTUAL TABLE availability USING fts5(body)')
        return True
    except knowledge.SQLITE_ERRORS:
        return False


FTS5_AVAILABLE = has_fts5()
NO_SQLITE_CLI = '''
import importlib.abc
import runpy
import sys
class NoSQLiteBinding(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == '_sqlite3':
            raise ModuleNotFoundError('simulated Python without _sqlite3')
sys.meta_path.insert(0, NoSQLiteBinding())
source, library, backend = sys.argv[1:]
sys.argv = [source, '--library', library, 'discover', '--query', 'Complete', '--backend', backend]
runpy.run_path(source, run_name='__main__')
'''


class DiscoveryTest(unittest.TestCase):
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

    @staticmethod
    def ids(result):
        return [r['pin']['id'] for r in result['bodies']]

    def test_whole_tokens_keep_meaningful_short_words(self):
        lib = self.make_library('tokens', {'one.md': b'Explain rain and living things.',
                                        'two.md': b'AI inference is useful.'})
        self.assertEqual(self.ids(lib.discover('AI')), ['two.md'])
        self.assertEqual(lib.discover('a in the')['coverage']['matched_bodies'], 0)
        self.assertEqual(lib.discover('a in the')['coverage']['examined_bodies'], 0)
        self.assertTrue(lib.discover('a in the')['query']['empty_after_filtering'])
        self.assertEqual(lib.discover('AI', backend='literal')['coverage']['matched_bodies'], 2)

    def test_rare_term_can_beat_incidental_common_vocabulary(self):
        bodies = {f'doc/{i}.md': b'shared state change ' * 30 for i in range(8)}
        bodies['target.md'] = b'quasar calibration'
        lib = self.make_library('rare', bodies)
        self.assertEqual(self.ids(lib.discover('shared state change quasar'))[0], 'target.md')

    def test_ordered_phrase_does_not_cross_fields_or_match_substrings(self):
        lib = self.make_library('phrase', {'first.md': b'source changed meaning',
            'second.md': b'meaning changed source', 'third.md': b'sources changed meaningfully'})
        self.assertEqual(self.ids(lib.discover('"source changed"')), ['first.md'])
        self.assertEqual(self.ids(lib.discover('"body source"')), [])
        with self.assertRaisesRegex(ValueError, 'unclosed'): lib.discover('"source')

    def test_known_identity_is_direct_but_does_not_erase_omitted_scope(self):
        lib = self.make_library('identity', {'a': b'One', 'later/body.md': b'Two'})
        self.assertEqual(self.ids(lib.discover('a')), ['a'])
        self.assertEqual(self.ids(lib.discover('later/body.md'))[0], 'later/body.md')
        bounded = lib.discover('later/body.md', scan_limit=1)
        self.assertNotIn('later/body.md', self.ids(bounded))
        self.assertEqual(bounded['coverage']['omitted_body_ids'], ['later/body.md'])
        self.assertEqual(lib.show('later/body.md')['text'], 'Two')

    def assert_ordinary_search_rechecks_source(self, backend):
        lib = self.make_library('fresh-' + backend, {'a.md': b'Original useful source.'})
        first = lib.discover('useful', backend=backend)
        self.assertFalse(first['preparation']['reused'])
        warmed = lib.discover('useful', backend=backend)
        self.assertTrue(warmed['preparation']['reused'])
        self.assertEqual(warmed['coverage']['verified_bodies'], 1)
        (lib.directory/lib.bodies['a.md']['object']).write_bytes(b'Changed source bytes.')
        with self.assertRaisesRegex(ValueError, 'differs from declared source'):
            lib.discover('useful', backend=backend)
        lib.close()

    def test_reused_index_still_rejects_changed_source_bytes(self):
        self.assert_ordinary_search_rechecks_source('bm25')

    @unittest.skipUnless(FTS5_AVAILABLE, 'optional SQLite FTS5 unavailable')
    def test_fts_reused_index_still_rejects_changed_source_bytes(self):
        self.assert_ordinary_search_rechecks_source('fts5')

    def test_changed_scope_replaces_preparation(self):
        self.library.discover('Complete', scan_limit=1)
        full = self.library.discover('Complete')
        self.assertEqual(full['coverage']['examined_bodies'], 2)
        self.assertFalse(full['preparation']['reused'])
        self.assertTrue(self.library.discover('Complete')['preparation']['reused'])
        self.library.close()

    @unittest.skipUnless(FTS5_AVAILABLE, 'optional SQLite FTS5 unavailable')
    def test_fts_backend_replaces_preparation(self):
        self.library.discover('Complete')
        self.assertFalse(self.library.discover('Complete', backend='fts5')['preparation']['reused'])
        self.library.close()

    def test_new_catalogue_metadata_changes_its_index_binding(self):
        self.library.discover('needle')
        manifest = copy.deepcopy(self.library.catalogue)
        manifest['bodies'][1]['title'] = 'needle'
        (self.library.directory/'catalogue.json').write_bytes(knowledge.encoded(manifest))
        current = knowledge.Library(self.library.directory)
        result = current.discover('needle')
        self.assertNotEqual(current.sha256, self.library.sha256)
        self.assertEqual(self.ids(result), ['meaning/b.md'])
        self.assertFalse(result['preparation']['reused'])

    @unittest.skipUnless(FTS5_AVAILABLE, 'optional SQLite FTS5 unavailable')
    def test_fts_stems_and_treats_query_operators_as_data(self):
        lib = self.make_library('fts', {'one.md': b'Corrected results from semantic junctions.',
                                      'two.md': b'Unrelated revisions.'})
        self.assertEqual(self.ids(lib.discover('correcting', backend='fts5')), ['one.md'])
        self.assertEqual(self.ids(lib.discover('"semantic junction"', backend='fts5')), ['one.md'])
        result = lib.discover('correcting OR nonexistent', backend='fts5')
        self.assertEqual(self.ids(result), ['one.md'])
        self.assertEqual(result['query']['terms'], ['correcting', 'nonexistent'])
        self.assertEqual(result['bodies'][0]['matched_terms'], ['correcting'])
        lib.close()

    def test_missing_fts_is_explicit_and_default_stays_available(self):
        class MissingExtensionError(Exception): pass
        class MissingExtensionConnection:
            closed = False
            def execute(self, statement): raise MissingExtensionError('no such module: fts5')
            def close(self): self.closed = True
        connection = MissingExtensionConnection()
        class MissingExtensionSQLite:
            @staticmethod
            def connect(path): return connection
        with patch.object(knowledge, 'sqlite3', MissingExtensionSQLite), \
             patch.object(knowledge, 'SQLITE_ERRORS', (MissingExtensionError,)):
            with self.assertRaisesRegex(ValueError, 'FTS5 preparation unavailable'):
                self.library.discover('Complete', backend='fts5')
        self.assertTrue(connection.closed)
        self.assertEqual(self.library.discover('Complete')['coverage']['matched_bodies'], 2)
        with patch.object(knowledge, 'sqlite3', None):
            with self.assertRaisesRegex(ValueError, 'SQLite support unavailable'):
                self.library.discover('Complete', backend='fts5')
            self.assertEqual(self.library.discover('Complete')['coverage']['matched_bodies'], 2)

    def test_cli_without_sqlite_binding_keeps_default_and_refuses_fts(self):
        source = str(Path(knowledge.__file__).resolve())
        for backend, expected_exit in [('bm25', 0), ('fts5', 2)]:
            result = subprocess.run([sys.executable, '-B', '-c', NO_SQLITE_CLI, source,
                                     str(self.library.directory), backend], capture_output=True, text=True)
            self.assertEqual(result.returncode, expected_exit, result.stderr)
            if backend == 'bm25':
                returned = json.loads(result.stdout)['libraries'][0]
                self.assertEqual(returned['coverage']['matched_bodies'], 2)
                self.assertEqual(returned['query']['backend'], 'bm25')
            else:
                refused = json.loads(result.stderr)
                self.assertEqual(refused['status'], 'refused')
                self.assertIn('SQLite support unavailable', refused['error'])

    @unittest.skipUnless(FTS5_AVAILABLE, 'optional SQLite FTS5 unavailable')
    def test_prepared_contentless_index_preserves_rank_phrases_and_module_results(self):
        lib = self.make_library('prepared', {'one.md': b'Corrected results from semantic junctions.',
            'two.md': b'semantic evidence and useful sources', 'three.md': b'AI explanation'})
        output = self.root / 'prepared.sqlite'
        receipt = lib.prepare_index(output)
        for query in ['correcting', '"semantic junction"', 'semantic evidence', 'AI', 'one.md', 'base', 'a in the']:
            expected = lib.discover(query, backend='fts5')
            actual = lib.discover(query, backend='fts5', index=output, index_sha256=receipt['index']['sha256'])
            self.assertEqual(actual['bodies'], expected['bodies'])
            self.assertEqual(actual['modules'], expected['modules'])
            self.assertEqual(actual['coverage']['omitted_body_ids'], expected['coverage']['omitted_body_ids'])
            self.assertEqual(actual['coverage']['verified_bodies'], 0)
            self.assertEqual(actual['coverage']['examined_body_bytes'], 0)
        with closing(knowledge.sqlite3.connect(output)) as db:
            self.assertEqual(db.execute('SELECT body FROM body_docs').fetchall(), [(None,)] * 3)
            self.assertEqual(db.execute('PRAGMA freelist_count').fetchone()[0], 0)
        self.assertEqual(knowledge.digest(output.read_bytes()), receipt['index']['sha256'])

    @unittest.skipUnless(FTS5_AVAILABLE, 'optional SQLite FTS5 unavailable')
    def test_prepared_nomination_does_not_claim_source_verification(self):
        output = self.root / 'source-change.sqlite'; receipt = self.library.prepare_index(output)
        row = self.library.bodies['meaning/a.md']
        (self.library.directory / row['object']).write_bytes(b'Changed! A\n')
        with patch.object(self.library, 'body', side_effect=AssertionError('nomination read canonical body')):
            result = self.library.discover('Complete', backend='fts5', index=output,
                                           index_sha256=receipt['index']['sha256'])
        self.assertEqual(result['coverage']['verified_bodies'], 0)
        self.assertIn('meaning/a.md', self.ids(result))
        for operation in [lambda: self.library.show('meaning/a.md'), self.library.verify,
                          lambda: self.library.discover('Complete', backend='fts5')]:
            with self.assertRaisesRegex(ValueError, 'differs from declared source'): operation()

    @unittest.skipUnless(FTS5_AVAILABLE, 'optional SQLite FTS5 unavailable')
    def test_prepared_index_requires_expected_bytes_and_exact_basis(self):
        output = self.root / 'basis.sqlite'; receipt = self.library.prepare_index(output, scan_limit=1)
        args = dict(backend='fts5', index=output, index_sha256=receipt['index']['sha256'])
        self.assertEqual(self.library.discover('Complete', scan_limit=1, **args)['coverage']['omitted_body_ids'], ['meaning/b.md'])
        with self.assertRaisesRegex(ValueError, 'basis differs'): self.library.discover('Complete', **args)
        with patch.object(knowledge, 'SEARCH_POLICY', 'changed-policy'):
            with self.assertRaisesRegex(ValueError, 'basis differs'): self.library.discover('Complete', scan_limit=1, **args)
        manifest = copy.deepcopy(self.library.catalogue); manifest['bodies'][0]['title'] = 'Changed metadata'
        (self.library.directory / 'catalogue.json').write_bytes(knowledge.encoded(manifest))
        current = knowledge.Library(self.library.directory)
        with self.assertRaisesRegex(ValueError, 'basis differs'): current.discover('Complete', scan_limit=1, **args)
        data = bytearray(output.read_bytes()); data[-1] ^= 1; output.write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'expected digest'): self.library.discover('Complete', scan_limit=1, **args)

    @unittest.skipUnless(FTS5_AVAILABLE, 'optional SQLite FTS5 unavailable')
    def test_prepared_build_is_fresh_outside_library_and_failure_has_no_output(self):
        with self.assertRaisesRegex(ValueError, 'outside the library'):
            self.library.prepare_index(self.library.directory / 'index.sqlite')
        sibling = self.root / 'sibling'; sibling.mkdir()
        alias = sibling / '..' / self.library.directory.name
        with self.assertRaisesRegex(ValueError, 'outside the library'):
            self.library.prepare_index(alias / 'index.sqlite')
        with self.assertRaisesRegex(ValueError, 'outside the library'):
            knowledge.Library(alias).prepare_index(self.library.directory / 'index.sqlite')
        self.assertFalse((self.library.directory / 'index.sqlite').exists())
        output = self.root / 'fresh.sqlite'; self.library.prepare_index(output)
        before = output.read_bytes()
        with self.assertRaisesRegex(ValueError, 'already exists'): self.library.prepare_index(output)
        self.assertEqual(output.read_bytes(), before)
        row = self.library.bodies['meaning/a.md']; (self.library.directory / row['object']).write_bytes(b'broken')
        with self.assertRaisesRegex(ValueError, 'differs from declared source'):
            self.library.prepare_index(self.root / 'failed.sqlite')
        self.assertFalse((self.root / 'failed.sqlite').exists())
        self.assertFalse(list(self.root.glob('.knowledge-index-*')))

    @unittest.skipUnless(FTS5_AVAILABLE, 'optional SQLite FTS5 unavailable')
    def test_prepared_index_private_copy_fallback(self):
        output = self.root / 'fallback.sqlite'; receipt = self.library.prepare_index(output)
        connect = knowledge.sqlite3.connect
        class NoDeserialize:
            def __init__(self, *args, **kwargs): self.connection = connect(*args, **kwargs)
            def __getattr__(self, name):
                if name == 'deserialize': raise AttributeError(name)
                return getattr(self.connection, name)
        expected = self.library.discover('Complete', backend='fts5')
        with patch.object(knowledge.sqlite3, 'connect', NoDeserialize):
            result = self.library.discover('Complete', backend='fts5', index=output,
                                          index_sha256=receipt['index']['sha256'])
        self.assertEqual(result['bodies'], expected['bodies'])
        self.assertEqual(knowledge.digest(output.read_bytes()), receipt['index']['sha256'])

    def test_prepared_index_options_are_explicit_and_sqlite_remains_optional(self):
        for options in [dict(index='not-read'), dict(index_sha256='0' * 64),
                        dict(index='not-read', index_sha256='0' * 64)]:
            with self.assertRaises(ValueError): self.library.discover('Complete', **options)
        with patch.object(knowledge, 'sqlite3', None):
            with self.assertRaisesRegex(ValueError, 'SQLite support unavailable'):
                self.library.prepare_index(self.root / 'missing.sqlite')
            with self.assertRaisesRegex(ValueError, 'SQLite support unavailable'):
                self.library.discover('Complete', backend='fts5', index='not-read', index_sha256='0' * 64)
            self.assertEqual(self.library.discover('Complete')['coverage']['verified_bodies'], 2)

    def test_empty_query_does_not_assert_an_unread_index_was_verified(self):
        result = self.library.discover('a in the', backend='fts5', index=self.root / 'absent.sqlite',
                                       index_sha256='0' * 64)
        self.assertFalse(result['preparation']['index_verified'])
        self.assertFalse(result['preparation']['reused'])
        self.assertEqual(result['preparation']['index_read_bytes'], 0)
        self.assertEqual(result['coverage']['examined_bodies'], 0)

    def test_bounded_read_consumes_short_reads_and_rejects_growth(self):
        file = self.root / 'read.bin'; file.write_bytes(b'x')
        class ShortReads(io.BytesIO):
            def read(self, size=-1): return super().read(min(size, 17))
        payload = b'retained bytes' * 100
        with patch.object(Path, 'open', return_value=ShortReads(payload)):
            self.assertEqual(knowledge.read_bytes(file, len(payload)), payload)
        # The on-disk stat still reports one byte: simulate growth after stat.
        with patch.object(Path, 'open', return_value=ShortReads(payload)):
            with self.assertRaisesRegex(ValueError, 'grew beyond byte bound'):
                knowledge.read_bytes(file, len(payload) - 1)


class DiscoveryBatchTest(unittest.TestCase):
    make_library = DiscoveryTest.make_library

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = self.make_library('batch', {
            'one.md': b'Corrected results from semantic junctions.',
            'two.md': 'A café has useful sources and evidence.'.encode(),
            'three.md': b'AI explanation and independent evidence.'})

    def tearDown(self):
        self.library.close()
        self.temporary.cleanup()

    def prepare(self, **scope):
        output = self.root / 'prepared.sqlite'
        receipt = self.library.prepare_index(output, **scope)
        return dict(index=output, index_sha256=receipt['index']['sha256'], **scope)

    @unittest.skipUnless(FTS5_AVAILABLE, 'optional SQLite FTS5 unavailable')
    def test_matches_separate_questions_and_acquires_one_image(self):
        args = self.prepare(scan_limit=2)
        queries = ['correcting', '"semantic junction"', 'café', 'one.md', 'base', 'a in the', 'correcting']
        expected = [self.library.discover(q, backend='fts5', **args) for q in queries]
        actual_read = knowledge.read_bytes
        with patch.object(self.library, 'body', side_effect=AssertionError('read canonical body')), \
             patch.object(knowledge, 'read_bytes', wraps=actual_read) as observed:
            batch = self.library.discover_batch(queries, **args)
        self.assertEqual(observed.call_count, 1)
        self.assertEqual(observed.call_args.args, (args['index'], knowledge.MAX_INDEX))
        self.assertEqual(batch['preparation']['index_read_bytes'], args['index'].stat().st_size)
        self.assertEqual(batch['preparation']['index_opens'], 1)
        self.assertEqual(batch['preparation']['canonical_body_read_bytes'], 0)
        for separate, result in zip(expected, batch['results']):
            self.assertEqual({k:v for k,v in result.items() if k != 'preparation'},
                             {k:v for k,v in separate.items() if k != 'preparation'})
            self.assertEqual(result['preparation']['index_read_bytes'], 0)
            self.assertEqual(result['preparation']['index_verified'], separate['preparation']['index_verified'])
        self.assertEqual(batch['results'][0]['coverage']['omitted_body_ids'], ['three.md'])
        self.assertEqual(batch['results'][5]['coverage']['omitted_body_ids'], list(self.library.bodies))

    def test_filtered_questions_do_not_open_or_verify_an_image(self):
        with patch.object(knowledge, 'PreparedIndex', side_effect=AssertionError('opened index')), \
             patch.object(knowledge, 'sqlite3', None):
            result = self.library.discover_batch(['a in the', '""'], index=self.root/'absent', index_sha256='0'*64)
        self.assertFalse(result['preparation']['index_verified'])
        self.assertEqual(result['preparation']['index_opens'], 0)
        self.assertEqual(result['preparation']['index_read_bytes'], 0)
        self.assertTrue(all(not x['preparation']['index_verified'] for x in result['results']))
        self.assertTrue(all(x['coverage']['examined_bodies'] == 0 for x in result['results']))

    def test_invalid_batch_is_rejected_before_index_acquisition(self):
        invalid = [[], 'question', {}, [None], [''], ['  '], ['"unfinished'],
                   ['x'] * (knowledge.MAX_BATCH_QUERIES + 1), ['😀' * 600],
                   ['x\x00' * 1024] * knowledge.MAX_BATCH_QUERIES]
        with patch.object(knowledge, 'PreparedIndex', side_effect=AssertionError('opened index')):
            for queries in invalid:
                with self.subTest(queries=str(queries)[:50]), self.assertRaises(ValueError):
                    self.library.discover_batch(queries, index=self.root/'absent', index_sha256='0'*64)
            for options in [dict(limit=0), dict(scan_limit=0), dict(max_bytes=0),
                            dict(max_result_bytes=0), dict(max_result_bytes=knowledge.MAX_BATCH_RESULT+1)]:
                with self.assertRaises(ValueError):
                    self.library.discover_batch(['useful'], index=self.root/'absent', index_sha256='0'*64, **options)

    @unittest.skipUnless(FTS5_AVAILABLE, 'optional SQLite FTS5 unavailable')
    def test_digest_and_basis_remain_required(self):
        args = self.prepare(scan_limit=1)
        with self.assertRaisesRegex(ValueError, 'basis differs'):
            self.library.discover_batch(['useful'], **{**args, 'scan_limit':2})
        with patch.object(knowledge, 'SEARCH_POLICY', 'changed-policy'):
            with self.assertRaisesRegex(ValueError, 'basis differs'):
                self.library.discover_batch(['useful'], **args)
        manifest = copy.deepcopy(self.library.catalogue)
        manifest['bodies'][0]['title'] = 'Changed metadata'
        (self.library.directory/'catalogue.json').write_bytes(knowledge.encoded(manifest))
        current = knowledge.Library(self.library.directory)
        try:
            with self.assertRaisesRegex(ValueError, 'basis differs'):
                current.discover_batch(['useful'], **args)
        finally: current.close()
        with self.assertRaisesRegex(ValueError, 'expected digest'):
            self.library.discover_batch(['useful'], **{**args, 'index_sha256':'0'*64})

    @unittest.skipUnless(FTS5_AVAILABLE, 'optional SQLite FTS5 unavailable')
    def test_image_is_a_snapshot_and_next_batch_reacquires(self):
        args = self.prepare()
        before = args['index'].read_bytes()
        expected = self.library.discover_batch(['correcting', 'evidence'], **args)
        original_search = knowledge.PreparedIndex.search
        changed = False
        def change_after_read(prepared, table, parsed):
            nonlocal changed
            if not changed:
                corrupted = bytearray(before); corrupted[-1] ^= 1
                args['index'].write_bytes(corrupted); changed = True
            return original_search(prepared, table, parsed)
        with patch.object(knowledge.PreparedIndex, 'search', change_after_read):
            actual = self.library.discover_batch(['correcting', 'evidence'], **args)
        self.assertEqual(actual, expected)
        with self.assertRaisesRegex(ValueError, 'expected digest'):
            self.library.discover_batch(['correcting'], **args)

    @unittest.skipUnless(FTS5_AVAILABLE, 'optional SQLite FTS5 unavailable')
    def test_exact_result_envelope_bound_includes_omissions_and_closes_image(self):
        args = self.prepare(scan_limit=1)
        queries = ['correcting', 'a in the', 'café']
        result = self.library.discover_batch(queries, **args)
        bound = len(knowledge.encoded(result))
        # The numeric bound is itself in the envelope. Reach its exact fixed
        # point, then verify that removing one byte refuses the whole result.
        for _ in range(5):
            result = self.library.discover_batch(queries, max_result_bytes=bound, **args)
            size = len(knowledge.encoded(result))
            if size == bound: break
            bound = size
        self.assertEqual(size, bound)
        opened = []
        class TrackedIndex(knowledge.PreparedIndex):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw); opened.append(self)
        with patch.object(knowledge, 'PreparedIndex', TrackedIndex):
            with self.assertRaisesRegex(ValueError, 'JSON byte bound'):
                self.library.discover_batch(queries, max_result_bytes=bound-1, **args)
        self.assertEqual(len(opened), 1)
        self.assertIsNone(opened[0].db)
        self.assertIsNone(opened[0].temporary)
        self.assertIn('three.md', result['results'][0]['coverage']['omitted_body_ids'])

    @unittest.skipUnless(FTS5_AVAILABLE, 'optional SQLite FTS5 unavailable')
    def test_query_failure_closes_private_copy_without_partial_return(self):
        args = self.prepare()
        opened, paths = [], []
        original_connect = knowledge.sqlite3.connect
        class NoDeserialize:
            def __init__(self, *a, **kw): self.connection = original_connect(*a, **kw)
            def __getattr__(self, name):
                if name == 'deserialize': raise AttributeError(name)
                return getattr(self.connection, name)
        class FailingIndex(knowledge.PreparedIndex):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw); opened.append(self)
                paths.append(Path(self.temporary.name))
                self.calls = 0
            def search(self, table, parsed):
                self.calls += 1
                if self.calls == 3: raise RuntimeError('simulated query interruption')
                return super().search(table, parsed)
        with patch.object(knowledge.sqlite3, 'connect', NoDeserialize), \
             patch.object(knowledge, 'PreparedIndex', FailingIndex):
            with self.assertRaisesRegex(RuntimeError, 'simulated query interruption'):
                self.library.discover_batch(['correcting', 'evidence'], **args)
        self.assertEqual(len(opened), 1)
        self.assertIsNone(opened[0].db)
        self.assertIsNone(opened[0].temporary)
        self.assertFalse(paths[0].exists())

    @unittest.skipUnless(FTS5_AVAILABLE, 'optional SQLite FTS5 unavailable')
    def test_cli_batch_returns_ordered_results_and_refuses_oversized_input(self):
        args = self.prepare()
        query_file = self.root/'questions.json'
        queries = ['"semantic junction"', 'café', 'a in the']
        query_file.write_bytes(knowledge.encoded(queries))
        command = [sys.executable, '-B', str(Path(knowledge.__file__).resolve()), '--library',
                   str(self.library.directory), 'discover-batch', '--queries', str(query_file),
                   '--index', str(args['index']), '--index-sha256', args['index_sha256']]
        result = subprocess.run(command, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.library.discover_batch(queries, **args))
        self.assertLessEqual(len(result.stdout), knowledge.MAX_BATCH_RESULT)
        query_file.write_bytes(b' ' * knowledge.MAX_BATCH_INPUT + b'[]')
        refused = subprocess.run(command, capture_output=True)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stdout, b'')
        self.assertEqual(json.loads(refused.stderr)['status'], 'refused')



if __name__ == '__main__': unittest.main()
