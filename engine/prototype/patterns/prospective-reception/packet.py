"""Materialize a fresh participant packet from an operator-frozen allowlist."""
import argparse
import json
from hashlib import sha256
from pathlib import Path


def deliver(source, pins, destination):
    if not pins or destination.exists():
        raise ValueError('use a fresh destination and a nonempty allowlist')
    contents = {}
    for name, pin in pins.items():
        path = source / name
        if Path(name).name != name or path.is_symlink() or not path.is_file():
            raise ValueError('invalid packet member')
        body = path.read_bytes()
        if sha256(body).hexdigest() != pin:
            raise ValueError('packet bytes changed')
        contents[name] = body
    destination.mkdir(parents=True)
    for name, body in contents.items():
        (destination / name).write_bytes(body)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for arg in ('source','pins','destination'):
        p.add_argument(arg, type=Path)
    args = p.parse_args()
    deliver(args.source, json.loads(args.pins.read_text()), args.destination)
