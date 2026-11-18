import json
import tempfile
import unittest
from pathlib import Path
from packet import deliver


class Packet(unittest.TestCase):
    def test_future_and_ambient_material_excluded(self):
        fixture = Path(__file__).parent / 'fixture'
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / 'participant'
            pins = json.loads((fixture/'early-pins.json').read_text())
            deliver(fixture/'early', pins, folder)
            self.assertEqual(set(x.name for x in folder.iterdir()), {'commission.txt','owner.txt'})
            self.assertFalse(any(b'east vehicle' in x.read_bytes() for x in folder.iterdir()))
            for bad in [dict(pins, **{'../later/update.txt':'unused'}), {'owner.txt':'wrong-version'}]:
                with self.assertRaises(ValueError):
                    deliver(fixture/'early', bad, Path(tmp)/'bad')
                self.assertFalse((Path(tmp)/'bad').exists())
