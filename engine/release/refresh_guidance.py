#!/usr/bin/env python3
"""Refresh source identities from canonical maintained files, without body copies."""
from contextlib import ExitStack
from pathlib import Path
from stat import S_IMODE, S_ISDIR, S_ISLNK, S_ISREG
from tempfile import TemporaryDirectory
import argparse
import hashlib
import json
import os
import stat

ROOT = Path(__file__).resolve().parents[1]
REPARSE_FLAG = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def anchor(ident):
    return 'source-' + digest(ident.encode())[:20]


def source_identity_line(row):
    return (f"[{row['source']}]({row['source']}) — {row['bytes']} bytes; "
            f"SHA-256 `{row['sha256']}`.")


def _is_reparse_or_symlink(info):
    return bool(
        S_ISLNK(info.st_mode)
        or getattr(info, 'st_reparse_tag', 0)
        or (getattr(info, 'st_file_attributes', 0) & REPARSE_FLAG)
    )


def _ordinary_entry(path, kind, label):
    try:
        info = path.lstat()
    except OSError:
        raise ValueError(f'{label}: {path}') from None
    if _is_reparse_or_symlink(info) or not kind(info.st_mode):
        raise ValueError(f'{label}: {path}')
    return info


def _ordinary_within(path, boundary, kind, label):
    """Verify every segment from boundary down to path is ordinary (no symlinks or NTFS reparse points)."""
    boundary = boundary.resolve(strict=True)
    _ordinary_entry(boundary, S_ISDIR, label)
    normalized = Path(os.path.abspath(path))
    try:
        rel = normalized.relative_to(boundary)
    except ValueError:
        raise ValueError(f'{label}: {path}') from None
    cur = boundary
    for part in rel.parts[:-1]:
        cur = cur / part
        _ordinary_entry(cur, S_ISDIR, label)
    if rel.parts:
        cur = cur / rel.parts[-1]
        _ordinary_entry(cur, kind, label)
    resolved = cur.resolve(strict=True)
    if not resolved.is_relative_to(boundary):
        raise ValueError(f'{label}: {path}')
    return resolved


def _read_strict_object(path, boundary=None):
    if boundary is not None:
        path = _ordinary_within(path, boundary, S_ISREG, 'ordinary JSON file required')
    else:
        _ordinary_entry(path, S_ISREG, 'ordinary JSON file required')

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
    root = Path(root).resolve(strict=True)
    _ordinary_entry(root, S_ISDIR, 'ordinary engine directory required')
    bundle_mode = root.name == 'engine'
    bundle_root = root.parent.resolve(strict=True) if bundle_mode else root
    if bundle_mode:
        _ordinary_entry(bundle_root, S_ISDIR, 'ordinary bundle root required')
    source_root_rel = '../..' if bundle_mode else '..'

    selection_name = 'release/current_sources.json'
    catalogue_name = 'store/catalogue.json'
    selected = _read_strict_object(root / selection_name, root)
    catalogue = _read_strict_object(root / catalogue_name, root)
    prior = {e['id']: e for e in catalogue['entries']}
    entries = []
    for row in selected['bodies']:
        spelling = row['source']
        if not isinstance(spelling, str) or not spelling or '\\' in spelling or Path(spelling).anchor:
            raise ValueError('current source reference must be relative to the engine: ' + row['id'])
        relative = Path(spelling)
        min_depth = -1 if bundle_mode else 0
        depth = 0
        escaped = False
        for part in relative.parts:
            depth += -1 if part == '..' else 1
            if depth < min_depth:
                escaped = True
                break
        if escaped:
            raise ValueError('current source reference escapes allowed package root: ' + spelling)
        # Validate no symlink/reparse traversal along the declared relative steps.
        cursor = root
        for idx, part in enumerate(relative.parts):
            cursor = cursor.parent if part == '..' else cursor / part
            expected_kind = S_ISREG if idx == len(relative.parts) - 1 else S_ISDIR
            _ordinary_entry(cursor, expected_kind, 'ordinary canonical source required')
        allowed_root = bundle_root if '..' in relative.parts else root
        resolved = _ordinary_within(root / spelling, allowed_root, S_ISREG, 'ordinary canonical source required')
        raw = resolved.read_bytes()
        text = raw.decode('utf-8')
        row.update(sha256=digest(raw), bytes=len(raw), presentation=row['source'])
        title = next((line.lstrip('#').strip() for line in text.splitlines() if line.startswith('# ')), row['id'])
        entry = dict(prior[row['id']])
        body_path = resolved.relative_to(bundle_root).as_posix()
        if '\\' in body_path:
            raise ValueError('current source reference has an unsupported filename: ' + spelling)
        entry.update(
            title=title,
            body_path=body_path,
            source_root_relative=source_root_rel,
            body_role='complete-current-source-reference',
            availability='available',
            source_root=selected['edition'],
            source_sha256=row['sha256'],
            sha256=row['sha256'],
            bytes=row['bytes'],
            body_id='sha256:' + row['sha256'],
        )
        entries.append(entry)
    catalogue['entries'] = entries + [e for e in catalogue['entries'] if e['id'] not in {r['id'] for r in selected['bodies']}]
    catalogue['source_root'] = selected['edition']
    selection = json_bytes(selected)
    selection_sha = digest(selection)
    catalogue['snapshot'] = {
        'edition': selected['edition'],
        'selection_sha256': selection_sha,
        'relationship': 'References to one canonical source body; domain sources are selected per store.',
    }
    nav = _read_strict_object(root / 'release/navigation.json', root)
    lines = [
        '# Current source owners',
        '',
        'The native catalogue verifies the same canonical files linked here. Domain sources are selected by each store. '
        'No second library of source copies or predecessor runtime is required.',
        '',
    ]
    selected_ids = set()
    for row in selected['bodies']:
        selected_ids.add(row['id'])
        lines += [
            '<a id="' + anchor(row['id']) + '"></a>',
            '## ' + row['id'],
            '',
            source_identity_line(row),
            '',
        ]
    for ident, route in sorted(nav.get('reference_routes', {}).items()):
        if ident in selected_ids:
            continue
        lines += [
            '<a id="' + anchor(ident) + '"></a>',
            '## ' + ident,
            '',
            '[' + route['label'] + '](' + route['path'] + ').',
            '',
            route['scope'],
            '',
        ]
    for ident in nav.get('unavailable_references', []):
        lines += [
            '<a id="' + anchor(ident) + '"></a>',
            '## ' + ident,
            '',
            'The former source is not installed at this logical path. Select a maintained account or explicitly acquire the exact original when its identity matters.',
            '',
        ]
    marker = {
        'format': 'managed-ai-shared-engine/1',
        'edition': selected['edition'],
        'engine_api': 'managed-ai-runtime/1',
        'deployment_api': 'agent-system-deployment/2',
        'store_schema': 'managed-ai-store/1',
        'selection_sha256': selection_sha,
        'source_count': len(entries),
        'source_ownership': 'Canonical maintained source files; reference catalogues do not duplicate their bodies.',
        'scope': 'Same executable engine for independent schema-governed stores; domains and selected knowledge are data.',
    }
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
    root = Path(root).resolve(strict=True)
    projections, summary = render(root)
    with ExitStack() as cleanup:
        staged_paths = []
        for name, raw in projections.items():
            target = (root / name).resolve()
            try:
                info = target.lstat()
            except FileNotFoundError:
                mode = None
            else:
                if _is_reparse_or_symlink(info) or not S_ISREG(info.st_mode):
                    raise ValueError('ordinary projection target required: ' + str(target))
                mode = S_IMODE(info.st_mode)
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
