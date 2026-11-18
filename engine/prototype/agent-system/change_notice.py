"""Compare a Git worktree with a pinned commit and consult a saved composition."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import time

from composition import Composition, MAX_VIEW_BYTES, _integer

GIT_BYTES = 4 * 1024 * 1024
GIT_SECONDS = 15


def git(repo, *args):
    # Read incrementally. An oversized or incomplete Git response cannot certify an empty set.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    with subprocess.Popen(['git', '-C', str(repo), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=env) as child, selectors.DefaultSelector() as sel:
        sel.register(child.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + GIT_SECONDS
        data = bytearray()
        try:
            while sel.select(max(0, deadline - time.monotonic())):
                chunk = os.read(child.stdout.fileno(), 65536)
                if not chunk:
                    if child.wait(timeout=max(0, deadline - time.monotonic())):
                        raise ValueError(f'Git {args[0]} failed. Check the repository and base revision.')
                    return bytes(data)
                data.extend(chunk)
                if len(data) > GIT_BYTES:
                    raise ValueError(f'Git {args[0]} exceeded the 4 MiB input bound')
                if time.monotonic() >= deadline:
                    break
            raise ValueError(f'Git {args[0]} exceeded the time bound')
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f'Git {args[0]} exceeded the time bound') from exc
        finally:
            if child.poll() is None:
                child.kill()


def notice(system, ident, repo, base, limit):
    _integer(limit, 'notice limit', 200)
    record = system.show(ident)
    body, rows, _, _ = Composition(system).profile(record)
    owners = {}
    for key, node in rows['elements'].items():
        detail = node.get('content')
        paths = detail.get('paths', []) if isinstance(detail, dict) else []
        if not isinstance(paths, list) or any(not isinstance(p, str) or '\0' in p or
                any(part in ('', '.', '..') for part in p.split('/')) for p in paths):
            raise ValueError('element paths must be exact Git-root-relative file names')
        owners[key] = set(paths)
    root = Path(os.fsdecode(git(repo, 'rev-parse', '--show-toplevel').removesuffix(b'\n')))
    commit = git(root, 'rev-parse', '--verify', '--end-of-options', base + '^{commit}').decode().strip()
    raw = git(root, 'diff', '--no-ext-diff', '--no-renames', '--ignore-submodules=none', '--name-only', '-z', commit, '--')
    untracked = git(root, 'ls-files', '--others', '--exclude-standard', '-z')
    # ASCII escaped output retains arbitrary Git-path bytes via surrogateescape.
    changed = {os.fsdecode(p) for data in (raw, untracked) for p in data.split(b'\0') if p}
    touched = {i for i, names in owners.items() if names & changed}
    review = set().union(*(owners[i] & changed for i in touched))
    candidates = []
    for seam in rows['relations'].values():
        ends = set(seam.get('from', []) + seam.get('to', []))
        if ends - owners.keys():
            raise ValueError('unresolved endpoint in relation ' + seam['id'])
        if ends & touched:
            candidates.append({'id': seam['id'], 'prediction': seam['prediction']})
            review.update(*(owners[i] for i in ends))
    unmapped = changed - set().union(*owners.values())
    report = dict(format='g60-change-notice/1', record={k: record[k] for k in ('id', 'revision', 'sha256')},
                  base=commit, comparison='changes-present' if changed else 'no-visible-changes',
                  assessment='unassessed', coverage=body.get('coverage'), unknowns=body.get('unknowns', []),
                  scope='Exact nominated paths and one incident relation hop. Ignored files, external recipients, undeclared dependencies and nested submodule paths are outside this view. Changes nominate a receiving check. Predictions require observation.',
                  counts={}, truncated=[])
    for name, values in [('changed', sorted(changed)), ('touched_elements', sorted(touched)),
                         ('candidate_relations', candidates), ('suggested_sources', sorted(review)),
                         ('unmapped_changes', sorted(unmapped))]:
        report['counts'][name] = len(values)
        report[name] = values[:limit]
        if len(values) > limit:
            report['truncated'].append(name)
    if len(json.dumps(report, indent=2)) > MAX_VIEW_BYTES:
        raise ValueError('change notice exceeds the 1 MiB output bound; reduce detail or profile size')
    return report
