#!/usr/bin/env python3
"""Run the small agent system from this standalone distribution."""
import json
import os
from pathlib import Path
import runpy
import sys


def enter_proposal_origin(argument):
    """Restore the nominating cwd in the new CLI process before entering the receiver."""
    try:
        device, inode, directory = argument.removeprefix('--file-origin=').split(':', 2)
        path = bytes.fromhex(directory)
        if not os.path.isabs(path) or b'\0' in path:
            raise ValueError('absolute proposal origin required')
        # Traverse using search permission. No OS lookup receives the joined pathname.
        anchor = b'//' if path.startswith(b'//') and not path.startswith(b'///') else b'/'
        os.chdir(anchor)
        for component in path.split(b'/'):
            if component:
                os.chdir(component)
        cwd = os.stat('.')
        if (cwd.st_dev, cwd.st_ino) != (int(device), int(inode)):
            raise ValueError('proposal origin directory changed')
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'unresolved', 'operation': 'receive', 'error': str(exc)}), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    entry = Path(__file__).resolve().parent / 'native' / 'system.py'
    if len(sys.argv) > 1 and sys.argv[1].startswith('--file-origin='):
        enter_proposal_origin(sys.argv.pop(1))
    runpy.run_path(str(entry), run_name='__main__')
