"""Exercise evidence capture across an outside owner's publication and its byte return."""
import base64
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'collective'))
import record_evidence as evidence


class EvidenceUseTests(unittest.TestCase):
    def test_complete_read_binds_its_extent_to_the_opened_edition(self):
        with TemporaryDirectory() as temp:
            root=Path(temp); db=root/'store'; source=root/'note'; newer=root/'replacement'
            evidence.native(db,'init')
            original_open=Path.open
            def publish_then_open(path,*args,**kw):
                if path==source and newer.exists():
                    os.replace(newer,source)
                return original_open(path,*args,**kw)
            account=dict(source_locator='evening duty board',use='recover the whole notice',
                         observation='Check the receiving desk before leaving.')
            for size in (61,evidence.LIMIT,evidence.LIMIT+1):
                source.write_bytes(b'closed')
                data=b'A'*(size-1)+b'\xff'
                newer.write_bytes(data)
                ident='edition-'+str(size)
                with patch.object(Path,'open',publish_then_open):
                    if size>evidence.LIMIT:
                        with self.assertRaisesRegex(ValueError,'interval'):
                            evidence.record(db,source,account,'office',ident,ident)
                        continue
                    evidence.record(db,source,account,'office',ident,ident)
                saved=evidence.native(db,'show',ident,'--revision',1)
                self.assertEqual(evidence.selection(saved)[1],data)

    def test_selected_bytes_require_matching_recorded_extent_and_digest(self):
        with TemporaryDirectory() as temp:
            root=Path(temp); db=root/'store'; file=root/'binary'
            file.write_bytes(b'prefix\x00\xfftail')
            evidence.native(db,'init')
            evidence.record(db,file,dict(source_locator='counter',use='recover interval',
                observation='Two-byte sample',selection=[6,8]),'owner','sample','read')
            returned=evidence.native(db,'show','sample','--revision',1)
            self.assertEqual(evidence.selection(returned)[1],b'\x00\xff')
            returned['body']['witness']['base64']=base64.b64encode(b'zz').decode()
            with self.assertRaisesRegex(ValueError,'differ'):
                evidence.selection(returned)
