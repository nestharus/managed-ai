#!/usr/bin/env python3
"""Refresh source identities from canonical maintained files, without body copies."""
from contextlib import ExitStack
from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory
import argparse
import hashlib
import json
import os
import re

ROOT = Path(__file__).resolve().parents[1]


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def anchor(ident):
    return 'source-' + digest(ident.encode())[:20]


def source_identity_line(row):
    return (f"[{row['source']}]({row['source']}) — {row['bytes']} bytes; "
            f"SHA-256 `{row['sha256']}`.")


def _read_strict_object(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('ordinary JSON file required: ' + str(path))

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError('nonfinite JSON: ' + value)

    data = json.loads(
        path.read_bytes().decode('utf-8'),
        object_pairs_hook=unique_keys,
        parse_constant=reject_constant,
    )
    if not isinstance(data, dict):
        raise ValueError('JSON object required: ' + str(path))
    return data


def render(root):
    """Read canonical UTF-8 inputs and prepare each projection without writing."""
    selection_name = 'release/current_sources.json'
    catalogue_name = 'runtime/native/catalogue.json'
    selected = _read_strict_object(root / selection_name)
    catalogue = _read_strict_object(root / catalogue_name)
    prior = {e['id']: e for e in catalogue['entries']}
    entries = []
    for row in selected['bodies']:
        spelling = row['source']
        if not isinstance(spelling, str) or not spelling or Path(spelling).anchor:
            raise ValueError('current source reference must be relative to the engine: ' + row['id'])
        relative = Path(spelling)
        depth = 0
        for part in relative.parts:
            depth += -1 if part == '..' else 1
            if depth < 0:
                break
        if depth < 0:
            raise ValueError('current source reference must be relative to the engine: ' + spelling)
        path = root / spelling
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not path.is_file() or not resolved.is_relative_to(root):
            raise ValueError('ordinary canonical source required: ' + row['source'])
        raw = path.read_bytes(); text = raw.decode()
        row.update(sha256=digest(raw), bytes=len(raw), presentation=row['source'])
        title = next((line.lstrip('#').strip() for line in text.splitlines() if line.startswith('# ')), row['id'])
        entry = dict(prior[row['id']])
        body_path = resolved.relative_to(root).as_posix()
        if '\\' in body_path:
            raise ValueError('current source reference has an unsupported filename: ' + spelling)
        entry.update(title=title, body_path=body_path, source_root_relative='../..',
                     body_role='complete-current-source-reference', availability='available',
                     source_root=selected['edition'], source_sha256=row['sha256'], sha256=row['sha256'],
                     bytes=row['bytes'], body_id='sha256:' + row['sha256'])
        entries.append(entry)
    catalogue['entries'] = entries + [e for e in catalogue['entries'] if e['id'] not in {r['id'] for r in selected['bodies']}]
    catalogue['source_root'] = selected['edition']
    selection = json_bytes(selected)
    selection_sha = digest(selection)
    catalogue['snapshot'] = {'edition': selected['edition'], 'selection_sha256': selection_sha,
                             'relationship': 'References to one canonical source body; domain sources are selected per store.'}
    nav = _read_strict_object(root / 'release/navigation.json')
    lines = ['# Current source owners', '',
             'The native catalogue verifies the same canonical files linked here. Domain sources are selected by each store. '
             'No second library of source copies or predecessor runtime is required.', '']
    selected_ids = set()
    for row in selected['bodies']:
        selected_ids.add(row['id'])
        lines += ['<a id="' + anchor(row['id']) + '"></a>', '## ' + row['id'], '',
                  source_identity_line(row), '']
    for ident, route in sorted(nav.get('reference_routes', {}).items()):
        if ident in selected_ids:
            continue
        lines += ['<a id="' + anchor(ident) + '"></a>', '## ' + ident, '',
                  '[' + route['label'] + '](' + route['path'] + ').', '', route['scope'], '']
    for ident in nav.get('unavailable_references', []):
        lines += ['<a id="' + anchor(ident) + '"></a>', '## ' + ident, '',
                  'The former source is not installed at this logical path. Select a maintained account or explicitly acquire the exact original when its identity matters.', '']
    marker = {'format': 'managed-ai-shared-engine/1',
         'edition': selected['edition'], 'engine_api': 'managed-ai-runtime/1',
         'deployment_api': 'agent-system-deployment/2', 'store_schema': 'managed-ai-store/1',
         'selection_sha256': selection_sha, 'source_count': len(entries),
         'source_ownership': 'Canonical maintained source files; reference catalogues do not duplicate their bodies.',
         'scope': 'Same executable engine for independent schema-governed stores; domains and selected knowledge are data.'}
    projections = {
        selection_name: selection,
        catalogue_name: json_bytes(catalogue),
        'SOURCES.md': '\n'.join(lines).encode('utf-8'),
        'CURRENT-RELEASE.json': json_bytes(marker),
    }
    summary = {'status': 'refreshed', 'canonical_sources': len(entries), 'selection_sha256': selection_sha}
    return projections, summary


def refresh(root):
    """Stage complete files, then replace targets in order with the marker last.

    The release owner serializes refreshes. A replacement failure can leave a
    prefix published. This sequence has no power-loss durability guarantee.
    """
    projections, summary = render(root)
    with ExitStack() as cleanup:
        staged_paths = []
        for name, raw in projections.items():
            target = (root / name).resolve()
            try:
                fd = os.open(target, os.O_WRONLY)
            except FileNotFoundError:
                mode = None
            else:
                try:
                    mode = S_IMODE(os.fstat(fd).st_mode)
                finally:
                    os.close(fd)
            scratch = cleanup.enter_context(TemporaryDirectory(dir=target.parent, prefix=f'.{target.name}.'))
            staged = Path(scratch) / target.name
            with staged.open('xb') as stream:
                if stream.write(raw) != len(raw):
                    raise OSError(f'incomplete write while staging {name}')
            if mode is not None:
                staged.chmod(mode)
            staged_paths.append((staged, target))
        for staged, target in staged_paths:
            staged.replace(target)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        print(json.dumps(refresh(args.root.resolve(strict=True))))
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
