"""End-to-end prepared-shard checks against the supplied unmodified G60 reader."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from turn import digest, encode, run


@unittest.skipUnless(os.environ.get('G60_READER'), 'set G60_READER to exercise a release reader')
class Federation(unittest.TestCase):
    def test_scope_exact_recovery_disagreement_and_index_tamper(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / 'journal.sqlite'
            reader = Path(os.environ['G60_READER']).resolve()
            shards = []
            for n in range(3):
                library = root / f'lib-{n}'
                (library / 'objects').mkdir(parents=True)
                rows = []
                for i in range(600):
                    wording = 'ordinary entry'
                    ident = f'item-{n}-{i}'
                    if i == 599 and n in (0, 2):
                        ident = 'distant-witness' if n == 2 else 'early-witness'
                        wording = 'beacon open' if n == 2 else 'beacon closed'
                    data = wording.encode()
                    sha = digest(data)
                    obj = f'objects/{sha}.txt'
                    (library / obj).write_bytes(data)
                    rows.append(dict(id=ident, title=ident, summary=wording, sha256=sha, bytes=len(data),
                                     object=obj, source=dict(path=ident, sha256=sha), link_base=ident, references=[]))
                cat = dict(format='knowledge-library/1', edition=f'shard-{n}', modules=[], provenance={}, bodies=rows)
                (library / 'catalogue.json').write_bytes(encode(cat))
                index = root / f'index-{n}.sqlite'
                receipt = json.loads(subprocess.check_output([sys.executable, '-B', reader, '--library', library,
                    'prepare-index', '--output', index, '--scan-limit', '600']))
                shards.append(dict(name=f'bin-{n}', library=library.name, index=index.name,
                                   index_sha256=receipt['index']['sha256'], scan_limit=600))
            manifest = root / 'scope.json'
            manifest.write_bytes(encode(dict(shards=shards)))
            (root / 'partial').write_text('the decision is open')

            def act(action, spec):
                path = root / 'request.json'
                path.write_bytes(encode(spec))
                return run(db, action, path)

            def query(q, **extras):
                opened = act('open', dict(target='use the owner', prediction='allow', rival='hold',
                                          gap='await the owner', partial='partial', query=q))
                spec = dict(prospect=opened['id'], reader=str(reader), manifest=manifest.name,
                            manifest_sha256=digest(manifest.read_bytes()), limit=2)
                return act('lookup', {**spec, **extras})

            hit = query('distant-witness', limit=1)
            self.assertEqual([r['pin']['id'] for r in hit['bodies']], ['distant-witness'])
            self.assertEqual(hit['bodies'][0]['shard'], 'bin-2')
            self.assertEqual(hit['coverage']['examined_bodies'], 1800)
            self.assertEqual(hit['coverage']['shards_consulted'], 3)
            log = json.loads(run(db, 'recover', hit['audit_sha256']))
            self.assertEqual(sum(s['index_read_bytes'] for s in log), hit['cost']['index_read_bytes'])
            self.assertNotIn('ordinary entry', json.dumps(hit))
            self.assertEqual({r['pin']['id'] for r in query('beacon')['bodies']}, {'distant-witness', 'early-witness'})
            self.assertEqual(query('axolotl')['coverage']['matched_bodies'], 0)
            broad = query('ordinary')
            self.assertEqual(len(broad['bodies']), 2)
            self.assertGreater(broad['coverage']['matches_unissued'], 1700)
            with self.assertRaisesRegex(ValueError, 'manifest'):
                query('beacon', manifest_sha256='0'*64)
            with (root / shards[-1]['index']).open('ab') as stream:
                stream.write(b'CORRUPTED')
            with self.assertRaisesRegex(ValueError, 'digest'):
                query('beacon')
