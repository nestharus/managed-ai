"""Verified access to the installed knowledge catalogue and operation archives."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid

from core.ownership import packet_id, relative_path
from core.storage import is_ordinary_file_stat, sha
from core.formats import INSTALLED_CATALOGUE_FORMAT
from store.protocol.inputs import bounded_source_bytes, integer, read


class InstalledCatalogue:
    def __init__(self, root: Path, selection=None):
        self.root = root.resolve()
        self.selection = selection

    def index(self) -> dict:
        path = self.root / 'catalogue.json'
        if not path.is_file():
            return {'format': 'pending-catalogue', 'entries': []}
        result = read(path)
        if 'format' in result and result['format'] != INSTALLED_CATALOGUE_FORMAT:
            raise ValueError('unsupported installed catalogue format')
        # Bind each declared root once for this read of the installed catalogue.
        # Keep the bindings local so the next index observes a relocated source.
        roots = {}
        entries = result.get('entries', [])
        if not isinstance(entries, list):
            raise ValueError('catalogue entries must be a list')
        for entry in entries:
            if 'source_root_relative' in entry:
                relative = entry['source_root_relative']
                if relative not in roots:
                    roots[relative] = str((self.root / relative).resolve())
                entry['source_root'] = roots[relative]
        if self.selection is not None:
            entries = entries + self.selection.entries
        result['entries'] = self._resolve_names(entries, result.get('aliases', []))
        return result

    @staticmethod
    def _resolve_names(entries, aliases):
        """Primary IDs win. A compatibility name can expose one selected primary."""
        primary = {}
        collisions = []
        for entry in entries:
            ident = entry['id']
            if ident in primary and ident not in collisions:
                collisions.append(ident)
            primary[ident] = entry
        if collisions:
            sample = ', '.join(sorted(collisions)[:5])
            suffix = f' (+{len(collisions) - 5} more)' if len(collisions) > 5 else ''
            raise ValueError(f'core/domain/store source identity collision: {sample}{suffix}')
        targets = {}
        for alias in aliases:
            name, target = alias['id'], alias['target']
            if name in targets and targets[name] != target:
                raise ValueError(f'ambiguous catalogue alias {name}: {targets[name]} and {target}')
            targets[name] = target
        resolved = list(entries)
        for name, target in targets.items():
            if name not in primary and target in primary:
                resolved.append(dict(primary[target], id=name, alias_of=target))
        return resolved

    def search(self, ident: str | None = None, query: str = '', kind: str | None = None,
                  limit: int = 25, offset: int = 0, content: bool = False) -> dict:
        catalogue = self.index()
        entries = catalogue['entries']
        if ident is None:
            integer(limit, 'catalogue limit', 1); integer(offset, 'catalogue offset')
            if limit > 200:
                raise ValueError('catalogue limit must be <= 200; use offset to continue')
            terms = query.casefold().split()
            meta_hits: list[tuple[int, dict, str, bool]] = []
            need_body: dict[str, dict] = {}
            for idx, entry in enumerate(entries):
                if kind is not None and entry.get('kind') != kind:
                    continue
                searchable = ' '.join(str(entry.get(k, '')) for k in
                                      ('id', 'title', 'kind', 'family', 'source_path', 'purpose')).casefold()
                matched_meta = all(term in searchable for term in terms)
                meta_hits.append((idx, entry, searchable, matched_meta))
                if not matched_meta and content and terms and entry.get('availability') != 'sealed':
                    key = entry['sha256']
                    if key not in need_body:
                        need_body[key] = entry

            body_cache: dict[str, str] = {}
            if need_body:
                from store.selection.domain_selection import _verify_ordinary_dir
                validated_roots: set[str] = set()
                if self.selection is not None:
                    validated_roots.update(getattr(self.selection, '_validated_roots', ()))
                for entry in need_body.values():
                    if entry.get('body_role') == 'complete-current-source-reference':
                        sr = entry.get('source_root')
                        if sr and sr not in validated_roots:
                            _verify_ordinary_dir(Path(sr))
                            validated_roots.add(sr)

                items = list(need_body.items())

                def fetch_chunk(chunk):
                    return [(k, self.body(e, validated_root=True).decode('utf-8').casefold()) for k, e in chunk]

                if len(items) >= 8:
                    workers = min(8, len(items))
                    step = (len(items) + workers - 1) // workers
                    chunks = [items[i:i + step] for i in range(0, len(items), step)]
                    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
                        for sub in pool.map(fetch_chunk, chunks):
                            for k, text_cf in sub:
                                body_cache[k] = text_cf
                else:
                    for k, text_cf in fetch_chunk(items):
                        body_cache[k] = text_cf

            matches = []
            for _, entry, searchable, matched_meta in meta_hits:
                if matched_meta:
                    matches.append(entry)
                elif content and terms and entry.get('availability') != 'sealed':
                    body_cf = body_cache.get(entry['sha256'])
                    if body_cf is not None and all(term in searchable or term in body_cf for term in terms):
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

    def body(self, entry: dict, *, byte_limit: int | None = None, validated_root: bool = False) -> bytes:
        if entry.get('body_role') == 'complete-current-source-reference':
            from store.selection.domain_selection import read_selected_body
            if not validated_root and self.selection is not None:
                validated_root = entry.get('source_root') in getattr(self.selection, '_validated_roots', ())
            return read_selected_body(entry, byte_limit=byte_limit, validated_root=validated_root)
        source = self.root / relative_path(entry['body_path'])
        try:
            info = source.lstat()
        except OSError:
            raise ValueError('technique body path escapes the installed app') from None
        if not is_ordinary_file_stat(info) or not source.resolve().is_relative_to(self.root):
            raise ValueError('technique body path escapes the installed app')
        data = source.read_bytes() if byte_limit is None else bounded_source_bytes(source, byte_limit)
        if sha(data) != entry['sha256']:
            raise ValueError('technique body binding changed: ' + entry['id'])
        return data

    def capture(self, directory: Path, identities: list[str], write_input) -> dict:
        entries = self.index().get('entries', [])
        by_id = {e['id']: e for e in entries}
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
        import shutil
        import zipfile

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
                member_rel = relative_path(relative[len(slug) + 1:])
                body = zipped.read(info)
                binding = expected[relative]
                if len(body) != binding['bytes'] or sha(body) != binding['sha256']:
                    raise ValueError('operation member binding changed')
                bodies.append((member_rel, body, binding.get('mode', 0o644) & 0o777))
        staging = exchange.workspace / '.system' / ('materialize-' + uuid.uuid4().hex)
        staging.mkdir()
        staging_resolved = staging.resolve()
        try:
            for relative, body, mode in bodies:
                path = staging / relative
                if not path.resolve().is_relative_to(staging_resolved):
                    raise ValueError('operation member path escapes staging directory')
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
