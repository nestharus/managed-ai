"""Verified access to the installed knowledge catalogue and operation archives."""
from __future__ import annotations

from pathlib import Path
import shutil
import uuid
import zipfile

from continuation_support.ownership import packet_id, relative_path
from continuation_support.storage import sha
from native_runtime.inputs import bounded_source_bytes, integer, read


class InstalledCatalogue:
    def __init__(self, root: Path, selection=None):
        self.root = root.resolve()
        self.selection = selection

    def index(self) -> dict:
        path = self.root / 'catalogue.json'
        if not path.is_file():
            return {'format': 'pending-catalogue', 'entries': []}
        result = read(path)
        result['entries'] = [dict(e, source_root=str((self.root / e['source_root_relative']).resolve()))
                             if 'source_root_relative' in e else e for e in result.get('entries', [])]
        if self.selection is not None:
            entries = result.get('entries', []) + self.selection.entries
            ids = [e['id'] for e in entries]
            if len(ids) != len(set(ids)):
                raise ValueError('core/domain/store source identity collision')
            result = dict(result, entries=entries)
        by_id = {e['id']: e for e in result.get('entries', [])}
        for alias in result.get('aliases', []):
            if alias['target'] in by_id and alias['id'] not in by_id:
                result['entries'].append(dict(by_id[alias['target']], id=alias['id'], alias_of=alias['target']))
        return result

    def search(self, ident: str | None = None, query: str = '', kind: str | None = None,
                  limit: int = 25, offset: int = 0, content: bool = False) -> dict:
        catalogue = self.index()
        entries = catalogue.get('entries', [])
        if not isinstance(entries, list):
            raise ValueError('catalogue entries must be a list')
        if ident is None:
            integer(limit, 'catalogue limit', 1); integer(offset, 'catalogue offset')
            if limit > 200:
                raise ValueError('catalogue limit must be <= 200; use offset to continue')
            terms = query.casefold().split()
            body_cache, matches = {}, []
            for entry in entries:
                if kind is not None and entry.get('kind') != kind:
                    continue
                searchable = ' '.join(str(entry.get(k, '')) for k in
                                      ('id', 'title', 'kind', 'family', 'source_path', 'purpose')).casefold()
                if content and terms and entry.get('availability') != 'sealed':
                    key = entry['sha256']
                    if key not in body_cache:
                        body_cache[key] = self.body(entry).decode('utf-8').casefold()
                    searchable += '\n' + body_cache[key]
                if all(term in searchable for term in terms):
                    matches.append(entry)
            page = matches[offset:offset + limit]
            return {'format': catalogue.get('format'), 'total_entries': len(entries),
                    'query': query, 'kind': kind, 'content_search': content, 'matches': len(matches),
                    'offset': offset, 'limit': limit, 'shown': len(page),
                    'next_offset': offset + len(page) if offset + len(page) < len(matches) else None,
                    'entries': [{k: e.get(k) for k in ('id', 'title', 'kind', 'family', 'availability', 'body_role')}
                                for e in page], 'scope': 'Case-insensitive whitespace-separated terms, all present anywhere in metadata or, with --content, its complete body. Lexical discovery only; a listing is not exposure to or use of a technique.'}
        entry = next((e for e in entries if e['id'] == ident), None)
        if entry is None:
            raise ValueError('unknown technique: ' + ident)
        source = self.body(entry)
        return dict(entry, body=source.decode('utf-8'))

    def body(self, entry: dict, *, byte_limit: int | None = None) -> bytes:
        if entry.get('body_role') == 'complete-current-source-reference':
            from native_runtime.domains import DomainSelection
            return DomainSelection.body(None, entry, byte_limit=byte_limit)
        source = self.root / relative_path(entry['body_path'])
        if source.is_symlink() or not source.resolve().is_relative_to(self.root):
            raise ValueError('technique body path escapes the installed app')
        data = source.read_bytes() if byte_limit is None else bounded_source_bytes(source, byte_limit)
        if sha(data) != entry['sha256']:
            raise ValueError('technique body binding changed: ' + entry['id'])
        return data

    def capture(self, directory: Path, identities: list[str], write_input) -> dict:
        entries = self.index().get('entries', [])
        by_id = {e['id']: e for e in entries}
        if len(by_id) != len(entries):
            raise ValueError('duplicate catalogue identity')
        selected, paths = [], []
        for ident in identities:
            if ident not in by_id:
                raise ValueError('unknown technique: ' + ident)
            entry = by_id[ident]
            data = self.body(entry)
            paths.append(write_input(directory, 'knowledge/' + sha(ident.encode()) + '.txt', data))
            selected.append(entry)
        return {'paths': paths, 'selected': selected, 'outside': [e['id'] for e in entries if e['id'] not in identities]}

    def materialize(self, operation: str, exchange) -> dict:
        operation = operation if operation.startswith('operation:') else 'operation:' + operation
        catalogue = self.index()
        asset = next((a for a in catalogue.get('assets', []) if a.get('kind') == 'operation-archive'
                      and a.get('operation_id') == operation), None)
        if asset is None:
            raise ValueError('no bundled operation archive for ' + operation)
        slug = packet_id(operation.split(':', 1)[1])
        archive = self.root / relative_path(asset['path'])
        if archive.is_symlink() or not archive.resolve().is_relative_to(self.root):
            raise ValueError('operation archive path escapes the app')
        data = archive.read_bytes()
        if sha(data) != asset['sha256'] or len(data) != asset['bytes']:
            raise ValueError('operation archive binding changed')
        destination = exchange.path('operations/' + slug)
        if destination.exists():
            raise ValueError('materialized destination already exists; retain it or choose another runtime')
        members = asset.get('members')
        if not isinstance(members, list):
            raise ValueError('exact operation member table is required')
        expected = {m['path']: m for m in members}
        if len(expected) != len(members):
            raise ValueError('duplicate operation member')
        bodies = []
        with zipfile.ZipFile(archive) as zipped:
            names = zipped.namelist()
            if len(names) != len(set(names)) or set(names) != set(expected):
                raise ValueError('operation archive membership differs')
            for info in zipped.infolist():
                relative = relative_path(info.filename)
                if not relative.startswith(slug + '/') or info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('operation member must be an ordinary file under its declared root')
                body = zipped.read(info)
                binding = expected[relative]
                if len(body) != binding['bytes'] or sha(body) != binding['sha256']:
                    raise ValueError('operation member binding changed')
                bodies.append((relative[len(slug) + 1:], body, binding.get('mode', 0o644) & 0o777))
        staging = exchange.workspace / '.system' / ('materialize-' + uuid.uuid4().hex)
        staging.mkdir()
        try:
            for relative, body, mode in bodies:
                path = staging / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                path.chmod(mode)
            destination.parent.mkdir(exist_ok=True)
            staging.rename(destination)
        finally:
            if staging.exists(): shutil.rmtree(staging)
        return {'operation': operation, 'path': str(destination), 'files': len(bodies),
                'prerequisites': asset.get('prerequisites', []),
                'scope': 'Verified source materialized; nothing executed or authorized by acquisition.'}
