"""Compare a Git worktree with a pinned commit and consult a saved composition."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
import time

from managed_ai.core.formats import CHANGE_NOTICE_FORMAT
from managed_ai.store.composition.composition import Composition, MAX_VIEW_BYTES, _integer

GIT_BYTES = 4 * 1024 * 1024
GIT_SECONDS = 15


def git(repo, *args):
    """Run a bounded read-only Git command cleanly on both POSIX and Windows."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    child = subprocess.Popen(
        ['git', '-C', str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    data = bytearray()
    overflow = threading.Event()
    read_error: list[BaseException] = []

    def reader():
        assert child.stdout is not None
        try:
            while True:
                chunk = child.stdout.read(65536)
                if not chunk:
                    break
                if len(data) + len(chunk) > GIT_BYTES:
                    overflow.set()
                    try:
                        child.kill()
                    except OSError:
                        pass
                    break
                data.extend(chunk)
        except BaseException as exc:  # pragma: no cover - defensive pipe read guard
            read_error.append(exc)
        finally:
            try:
                child.stdout.close()
            except OSError:
                pass

    worker = threading.Thread(target=reader, daemon=True)
    worker.start()
    deadline = time.monotonic() + GIT_SECONDS

    try:
        remaining = max(0.0, deadline - time.monotonic())
        worker.join(timeout=remaining)

        if worker.is_alive():
            child.kill()
            worker.join(timeout=2.0)
            raise ValueError(f'Git {args[0]} exceeded the time bound')

        if overflow.is_set():
            child.wait(timeout=2.0)
            raise ValueError(f'Git {args[0]} exceeded the 4 MiB input bound')

        if read_error:
            raise ValueError(f'Git {args[0]} failed. Check the repository and base revision.') from read_error[0]

        try:
            returncode = child.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f'Git {args[0]} exceeded the time bound') from exc

        if returncode:
            raise ValueError(f'Git {args[0]} failed. Check the repository and base revision.')

        return bytes(data)
    finally:
        if child.poll() is None:
            child.kill()
            try:
                child.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass

        if child.stdout is not None and not child.stdout.closed:
            try:
                child.stdout.close()
            except OSError:
                pass


def notice(system, ident, repo, base, limit):
    _integer(limit, 'notice limit', 200)
    record = system.show(ident)
    body, rows, _, _ = Composition(system).profile(record)

    owners = {}
    for key, node in rows['elements'].items():
        detail = node.get('content')
        paths = detail.get('paths', []) if isinstance(detail, dict) else []
        if (
            not isinstance(paths, list)
            or any(
                not isinstance(p, str) or '\0' in p or any(part in ('', '.', '..') for part in p.split('/'))
                for p in paths
            )
        ):
            raise ValueError('element paths must be exact Git-root-relative file names')
        owners[key] = set(paths)

    top_raw = git(repo, 'rev-parse', '--show-toplevel').removesuffix(b'\n').removesuffix(b'\r')
    root = Path(os.fsdecode(top_raw))
    commit = git(root, 'rev-parse', '--verify', '--end-of-options', base + '^{commit}').decode().strip()

    raw = git(root, 'diff', '--no-ext-diff', '--no-renames', '--ignore-submodules=none', '--name-only', '-z', commit, '--')
    untracked = git(root, 'ls-files', '--others', '--exclude-standard', '-z')
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
    report = dict(
        format=CHANGE_NOTICE_FORMAT,
        record={k: record[k] for k in ('id', 'revision', 'sha256')},
        base=commit,
        comparison='changes-present' if changed else 'no-visible-changes',
        assessment='unassessed',
        coverage=body.get('coverage'),
        unknowns=body.get('unknowns', []),
        scope=(
            'Exact nominated paths and one incident relation hop. Ignored files, external recipients, '
            'undeclared dependencies and nested submodule paths are outside this view. '
            'Changes nominate a receiving check. Predictions require observation.'
        ),
        counts={},
        truncated=[],
    )

    for name, values in [
        ('changed', sorted(changed)),
        ('touched_elements', sorted(touched)),
        ('candidate_relations', candidates),
        ('suggested_sources', sorted(review)),
        ('unmapped_changes', sorted(unmapped)),
    ]:
        report['counts'][name] = len(values)
        report[name] = values[:limit]
        if len(values) > limit:
            report['truncated'].append(name)

    if len(json.dumps(report, indent=2)) > MAX_VIEW_BYTES:
        raise ValueError('change notice exceeds the 1 MiB output bound; reduce detail or profile size')
    return report
