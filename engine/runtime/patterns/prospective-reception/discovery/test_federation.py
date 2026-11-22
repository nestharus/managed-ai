"""Federated lookup against three real prepared shards and a supplied release reader."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from turn import digest, encode, run


def prepare_shard(root, reader, number, witness=None):
    library = root / f'lib-{number}'
    (library / 'objects').mkdir(parents=True)
    rows = []
    for ordinal in range(600):
        ident, wording = f'item-{number}-{ordinal}', 'ordinary entry'
        if ordinal == 599 and witness is not None:
            ident, wording = witness
        data = wording.encode()
        sha = digest(data)
        obj = f'objects/{sha}.txt'
        (library / obj).write_bytes(data)
        rows.append(dict(id=ident, title=ident, summary=wording, sha256=sha, bytes=len(data),
                         object=obj, source=dict(path=ident, sha256=sha), link_base=ident, references=[]))
    catalogue = dict(format='knowledge-library/1', edition=f'shard-{number}',
                     modules=[], provenance={}, bodies=rows)
    (library / 'catalogue.json').write_bytes(encode(catalogue))
    index = root / f'index-{number}.sqlite'
    receipt = json.loads(subprocess.check_output([
        sys.executable, '-B', reader, '--library', library, 'prepare-index',
        '--output', index, '--scan-limit', '600']))
    return dict(name=f'bin-{number}', library=str(library), index=str(index),
                index_sha256=receipt['index']['sha256'], scan_limit=600)


@unittest.skipUnless(os.environ.get('G60_READER'), 'set G60_READER to exercise a release reader')
class Federation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        root = Path(directory.name)
        cls.reader = Path(os.environ['G60_READER']).resolve()
        cls.shards = [
            prepare_shard(root, cls.reader, 0, witness=('early-witness', 'beacon closed')),
            prepare_shard(root, cls.reader, 1),
            prepare_shard(root, cls.reader, 2, witness=('distant-witness', 'beacon open')),
        ]

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / 'journal.sqlite'
        self.manifest = self.root / 'scope.json'
        self.manifest.write_bytes(encode(dict(shards=self.shards)))
        (self.root / 'partial').write_text('the decision is open')

    def act(self, action, spec):
        path = self.root / 'request.json'
        path.write_bytes(encode(spec))
        return run(self.db, action, path)

    def query(self, query, **bounds):
        prospect = self.act('open', dict(target='use the owner', prediction='allow', rival='hold',
                                         gap='await the owner', partial='partial', query=query))
        request = dict(prospect=prospect['id'], reader=str(self.reader), manifest=self.manifest.name,
                       manifest_sha256=digest(self.manifest.read_bytes()), limit=2)
        return self.act('lookup', {**request, **bounds})

    def test_exact_last_shard_witness_and_recoverable_scan_cost(self):
        hit = self.query('distant-witness', limit=1)
        body, = hit['bodies']
        self.assertEqual((body['shard'], body['pin']['id']), ('bin-2', 'distant-witness'))
        self.assertEqual(hit['coverage']['examined_bodies'], 1800)
        self.assertEqual(hit['coverage']['shards_consulted'], 3)
        audit = json.loads(run(self.db, 'recover', hit['audit_sha256']))
        self.assertEqual(sum(shard['index_read_bytes'] for shard in audit), hit['cost']['index_read_bytes'])
        self.assertNotIn('ordinary entry', json.dumps(hit))

    def test_competing_beacon_witnesses_survive_the_global_limit(self):
        bodies = self.query('beacon')['bodies']
        self.assertEqual({r['pin']['id'] for r in bodies}, {'distant-witness', 'early-witness'})

    def test_absent_vocabulary_has_no_matches(self):
        self.assertEqual(self.query('axolotl')['coverage']['matched_bodies'], 0)

    def test_broad_query_reports_global_omissions(self):
        broad = self.query('ordinary')
        self.assertEqual(len(broad['bodies']), 2)
        self.assertGreater(broad['coverage']['matches_unissued'], 1700)

    def test_manifest_digest_mismatch_refuses_lookup(self):
        with self.assertRaisesRegex(ValueError, 'manifest size or digest mismatch'):
            self.query('beacon', manifest_sha256='0' * 64)

    def test_changed_index_refuses_lookup_under_its_original_pin(self):
        damaged = self.root / 'changed-index.sqlite'
        shutil.copyfile(self.shards[-1]['index'], damaged)
        with damaged.open('ab') as stream:
            stream.write(b'CORRUPTED')
        shards = [*self.shards[:-1], dict(self.shards[-1], index=damaged.name)]
        self.manifest.write_bytes(encode(dict(shards=shards)))
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.query('beacon')
