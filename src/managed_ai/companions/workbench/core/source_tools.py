"""Complete Markdown source access with disposable search/cache and durable use receipts.

One cooperative owner serializes calls and source changes. Known-path reads can
observe complete bodies without preparing search. Discovery/inventory requests
prepare an explicit complete view; refresh() supplies a new current observation.
Text is returned as evidence, never executed. No semantic acceptance is inferred.
"""
from __future__ import annotations

import builtins
from collections import OrderedDict
from copy import deepcopy
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import sqlite3
import stat
import sys
import time
from types import ModuleType
import uuid

try:
    from .io_utils import (
        canonical_bytes as encoded,
        canonical_digest as identity,
        is_reparse_or_symlink,
        loads_strict,
        sha256_bytes as digest,
        sync_dir,
    )
except ImportError:
    _src_root = Path(__file__).resolve().parents[4]
    if str(_src_root) not in sys.path:
        sys.path.insert(0, str(_src_root))
    from managed_ai.companions.workbench.core.io_utils import (
        canonical_bytes as encoded,
        canonical_digest as identity,
        is_reparse_or_symlink,
        loads_strict,
        sha256_bytes as digest,
        sync_dir,
    )


VERSION = 'g60-source-service/1'
DEFAULT_EXCLUDED = {
    'review/external-review-original.md': 'External review is source data, not default construction guidance.',
    'coverage/historical-material.md': 'Locator catalogues are not locally retained argument bodies.',
    'coverage/recent-historical-material.md': 'Locator catalogues are not locally retained argument bodies.',
}
PROTECTED_PREFIXES = ('evidence/fleet/D12/', 'instruments/incident-corpus/D12/')

_RUNTIME_ENTRY = Path('library/reader/knowledge.py')
_LEGACY_RUNTIME_ENTRY = Path('runtime/source_library/knowledge.py')
_MAX_SEARCH_FILES = 8
_MAX_SEARCH_FILE_BYTES = 256 * 1024


def need(condition, message):
    if not condition:
        raise ValueError(message)


def relative(value):
    need(
        isinstance(value, str) and value and '\\' not in value and '\x00' not in value,
        'A POSIX relative source path is required.',
    )
    need(
        not PureWindowsPath(value).drive and not (len(value) >= 2 and value[1] == ':'),
        'Unsafe source path.',
    )
    p = PurePosixPath(value)
    parts = value.split('/')
    need(
        not p.is_absolute()
        and all(
            part not in {'', '.', '..'}
            and not PureWindowsPath(part).drive
            and not (len(part) >= 2 and part[1] == ':')
            for part in parts
        ),
        'Unsafe source path.',
    )
    need(p.suffix.lower() == '.md', 'Only complete Markdown sources are exposed.')
    need(not any(value.startswith(prefix) for prefix in PROTECTED_PREFIXES), 'Source is protected by this study exposure boundary.')
    return value


def _default_runtime_entry(base_root):
    base = Path(base_root)
    candidates = (
        base / _RUNTIME_ENTRY,
        base / 'src/managed_ai' / _RUNTIME_ENTRY,
        base / 'engine' / _RUNTIME_ENTRY,
        base / 'engine' / _LEGACY_RUNTIME_ENTRY,
    )
    for candidate in candidates:
        if candidate.exists() or candidate.is_symlink():
            return candidate
    return candidates[0]


def _search_bytes(path):
    path = Path(os.path.abspath(path))
    need(not any(is_reparse_or_symlink(part) for part in (path, *path.parents)),
         'Search code cannot pass through a symlink or reparse point: ' + str(path))
    need(path.is_file(), 'Required search code is not an ordinary file: ' + str(path))
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    flags |= getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        need(stat.S_ISREG(metadata.st_mode), 'Required search code is not an ordinary file: ' + str(path))
        need(metadata.st_size <= _MAX_SEARCH_FILE_BYTES, 'Search code exceeds byte bound: ' + str(path))
        data = stream.read(_MAX_SEARCH_FILE_BYTES + 1)
    need(len(data) <= _MAX_SEARCH_FILE_BYTES, 'Search code exceeds byte bound: ' + str(path))
    return data


def _library_root_for(entry):
    """Determine the bounded root directory for verified library relative imports."""
    parent = entry.parent
    if parent.name == 'reader' and (parent.parent / 'search').is_dir():
        return parent.parent
    return parent


class SearchImplementation:
    """Select and execute one bounded snapshot of the trusted lexical code."""

    def __init__(self, module_path=None):
        repo_root = Path(__file__).resolve().parents[3]
        self.runtime_entry = _default_runtime_entry(repo_root)
        if module_path is None:
            module_path = self.runtime_entry
        entry = Path(os.path.abspath(module_path))
        data = _search_bytes(entry)
        self.modules = {}
        self.pins = {}
        self.entry = entry
        self.library_root = _library_root_for(entry)
        self._sealed = False
        self.module = self._load(entry, data)
        self._sealed = True

        analyzer = self.module.FtsAnalyzer
        for implementation in (self.module.search_query, analyzer.identity, analyzer.create, analyzer.search):
            code = getattr(implementation, '__code__', None)
            need(code is not None and code.co_filename in self.pins,
                 'Selected search callable is outside the verified source files.')
        self.analyzer = analyzer.identity()

    def _load(self, path, data=None):
        path = Path(os.path.abspath(path))
        need(path.is_relative_to(self.library_root),
             'Search code dependency escapes bounded library root: ' + str(path))
        if path in self.modules:
            return self.modules[path]
        need(not self._sealed, 'Search cannot load a new code dependency after binding: ' + path.name)
        need(len(self.modules) < _MAX_SEARCH_FILES, 'Search code exceeds dependency count bound.')
        if data is None:
            data = _search_bytes(path)
        rel = path.relative_to(self.library_root).with_suffix('')
        dotted = '.'.join(rel.parts)
        package_parts = rel.parts[:-1]
        module = ModuleType('_g60_verified_source_library.' + dotted)
        module.__file__ = str(path)
        module.__package__ = (
            '_g60_verified_source_library.' + '.'.join(package_parts)
            if package_parts else '_g60_verified_source_library'
        )
        module.__dict__['__builtins__'] = {**vars(builtins), '__import__': self._import}
        self.modules[path] = module
        self.pins[str(path)] = {'bytes': len(data), 'sha256': digest(data)}
        try:
            exec(compile(data, str(path), 'exec', dont_inherit=True), module.__dict__)
        except SyntaxError as exc:
            raise ValueError('Invalid lexical source: ' + str(exc)) from exc
        return module

    def _import(self, name, globals=None, locals=None, fromlist=(), level=0):
        if level:
            need(level in (1, 2) and bool(fromlist),
                 'Search can bind only explicit bounded relative source imports: ' + name)
            caller_file = globals.get('__file__') if isinstance(globals, dict) else None
            need(isinstance(caller_file, str) and caller_file in self.pins,
                 'Relative search import originated outside verified source files.')
            base = Path(caller_file).parent
            if level == 2:
                base = base.parent
            need(base == self.library_root or base.is_relative_to(self.library_root),
                 'Search relative import escapes bounded library root: ' + name)
            if name:
                parts = name.split('.')
                need(1 <= len(parts) <= 2 and all(part.isidentifier() for part in parts),
                     'Search can bind only explicit bounded relative source imports: ' + name)
                target = base.joinpath(*parts[:-1], parts[-1] + '.py')
                return self._load(target)
            need(all(isinstance(item, str) and item.isidentifier() for item in fromlist),
                 'Search can bind only explicit bounded relative source imports.')
            namespace = ModuleType('_g60_verified_source_library._rel')
            for item in fromlist:
                setattr(namespace, item, self._load(base / (item + '.py')))
            return namespace
        root = name.split('.', 1)[0]
        need(root not in {'runpy', 'importlib'},
             'Search code must use a supported entrance and explicit sibling imports: ' + name)
        need(root in sys.stdlib_module_names,
             'Search dependency must be standard library code or an explicit sibling import: ' + name)
        return builtins.__import__(name, globals, locals, fromlist, level)

    @property
    def entry_pin(self):
        return self.pins[str(self.entry)]


class SourceService:
    """Scoped source service; body cache bounds do not bound the derived index/RSS."""

    def __init__(self, corpus_root, state_dir, max_cache_bytes=65536, *, search_module_path=None, _search=None):
        self.root = Path(corpus_root).absolute()
        need(self.root.is_dir() and not self.root.is_symlink(), 'Ordinary corpus directory required.')
        self.state_dir = Path(state_dir).absolute()
        need(not self.state_dir.is_relative_to(self.root), 'State must remain outside the source corpus.')
        self.state_dir.mkdir(parents=True, exist_ok=True)
        need(not self.state_dir.is_symlink(), 'Ordinary state directory required.')
        need(type(max_cache_bytes) is int and max_cache_bytes >= 0, 'Cache byte bound must be a nonnegative integer.')
        self.max_cache_bytes = max_cache_bytes
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.db = None
        self.members, self.row_paths, self.inventory_id = {}, {}, None
        self._stats = {
            'corpus_reads': 0, 'corpus_read_bytes': 0, 'preparation_read_bytes': 0,
            'acquisition_read_bytes': 0, 'cache_hits': 0, 'cache_misses': 0,
            'cache_evictions': 0, 'oversize_bodies_not_cached': 0,
            'receipt_write_bytes': 0, 'refreshes': 0, 'discoveries': 0,
            'refresh_seconds': 0.0, 'discovery_seconds': 0.0,
        }
        self.ledger_path = self.state_dir / 'receipts.json'
        self.reopen_receipts()
        implementation = _search or SearchImplementation(search_module_path)
        self.search = implementation.module
        search_code = {Path(path).name: pin for path, pin in implementation.pins.items()}
        need(len(search_code) == len(implementation.pins),
             'Search code dependencies must have distinct module filenames.')
        self.selector = {
            'service': VERSION,
            'implementation_sha256': digest(Path(__file__).read_bytes()),
            'acquired_search_sha256': implementation.entry_pin['sha256'],
            'search_code': search_code,
            'backend': 'native FtsAnalyzer, contentless body_docs',
            'query_parser': 'native search_query(query, fts5)',
            'analyzer': implementation.analyzer,
            'eligible_extension': '.md (case insensitive)',
            'default_excluded': DEFAULT_EXCLUDED,
            'protected_prefixes': list(PROTECTED_PREFIXES),
            'candidate_match': 'OR over parsed terms/phrases; ranked weighted BM25; path breaks ties',
            'preparation_policy': 'Known-path acquisition before preparation observes that body only; discover/inventory/refresh demand complete preparation.',
            'source_model': 'Serialized cooperative source changes; complete explicit refresh observations.',
        }
        self.selector_id = identity(self.selector)

    def checkpoint_state(self):
        """Return detached inventory/counter state for durable continuation checkpoints."""
        return {
            'source_inventory_id': self.inventory_id,
            'source_counters': deepcopy(self._stats),
        }

    def restore_state(self, inventory_id, counters, *, on_selector_mismatch=None):
        """Restore recorded inventory guard and counters without fabricating a fresh index."""
        ledger = self.ledger
        if inventory_id is not None:
            if not isinstance(inventory_id, str) or inventory_id not in ledger['inventories']:
                raise ValueError('Recovered source inventory is unavailable')
            inventory = ledger['inventories'][inventory_id]
            if inventory.get('selector_id') != self.selector_id:
                message = 'Recovered inventory uses another source selector'
                if on_selector_mismatch is not None:
                    on_selector_mismatch(message)
                raise ValueError(message)
            members = loads_strict(encoded(inventory['members']))
        else:
            members = {}
        if (
            not isinstance(counters, dict)
            or set(counters) != set(self._stats)
            or any(
                type(item) not in (int, float) or not math.isfinite(item) or item < 0
                for item in counters.values()
            )
        ):
            raise ValueError('Invalid completed source counters')
        self.members = members
        self.inventory_id = inventory_id
        self._stats = deepcopy(counters)

    def _ordinary(self, relative_path):
        path = self.root / relative(relative_path)
        for current in (self.root, *path.parents, path):
            need(not is_reparse_or_symlink(current), 'Source symlinks are unsupported.')
        need(path.is_file() and stat.S_ISREG(path.stat().st_mode), 'Ordinary source file required: ' + relative_path)
        return path

    def _body(self, relative_path, purpose):
        data = self._ordinary(relative_path).read_bytes()
        self._stats['corpus_reads'] += 1
        self._stats['corpus_read_bytes'] += len(data)
        self._stats[purpose + '_read_bytes'] += len(data)
        return data, data.decode('utf-8')

    def reopen_receipts(self):
        """Read the serialized owner's published image at acquisition or after uncertainty."""
        self._published = None
        need(not self.ledger_path.is_symlink(), 'Receipt symlinks are unsupported.')
        try:
            data = self.ledger_path.read_bytes()
        except FileNotFoundError:
            ledger = {'schema': VERSION, 'inventories': {}, 'selections': {}, 'sources': {}, 'uses': {}}
        else:
            ledger = loads_strict(data)
            need(
                isinstance(ledger, dict)
                and ledger.get('schema') == VERSION
                and set(ledger) == {'schema', 'inventories', 'selections', 'sources', 'uses'},
                'Unknown receipt ledger.',
            )
            for category in ('inventories', 'selections', 'sources', 'uses'):
                need(isinstance(ledger[category], dict), 'Invalid receipt category.')
                for ident, record in ledger[category].items():
                    need(identity(record) == ident, 'Receipt identity mismatch: ' + ident)
        self._published = ledger

    @property
    def ledger(self):
        if self._published is None:
            self.reopen_receipts()
        return self._published

    def _record(self, category, value):
        value = loads_strict(encoded(value))
        ident = identity(value)
        published = self.ledger
        if ident in published[category]:
            return ident

        candidate = {**published, category: {**published[category], ident: value}}
        data = encoded(candidate) + b'\n'
        temporary = self.ledger_path.with_name(self.ledger_path.name + '.tmp-' + uuid.uuid4().hex)
        try:
            with temporary.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.replace(temporary, self.ledger_path)
            except OSError as failure:
                # Replacement can have taken effect before its response was lost.
                try:
                    observed = self.ledger_path.read_bytes()
                except FileNotFoundError:
                    raise failure from None
                if observed != data:
                    raise
            else:
                sync_dir(self.ledger_path.parent)
        finally:
            if temporary.exists():
                temporary.unlink()
            # On interruption the next operation must reopen the actual image.
            self._published = None
        self._published = candidate
        self._stats['receipt_write_bytes'] += len(data)
        return ident

    def refresh(self):
        """Observe every eligible complete body; atomically replace the derived view.

        The cooperative owner must keep the corpus stable during this call. This
        is an explicit local observation, not a simultaneous distributed snapshot.
        Excluded and protected paths are inventoried without reading their bodies.
        """
        started = time.perf_counter()
        db = sqlite3.connect(':memory:')
        members, excluded, row_paths = {}, {}, {}
        try:
            self.search.FtsAnalyzer.create(db, 'body_docs')
            for path in sorted(self.root.rglob('*')):
                if path.suffix.lower() != '.md':
                    continue
                rel = path.relative_to(self.root).as_posix()
                if any(rel.startswith(prefix) for prefix in PROTECTED_PREFIXES):
                    excluded[rel] = 'Protected study exposure; no body read.'
                    continue
                if rel in DEFAULT_EXCLUDED:
                    excluded[rel] = DEFAULT_EXCLUDED[rel]
                    continue
                data, text = self._body(rel, 'preparation')
                heading = next((line.lstrip('#').strip() for line in text.splitlines() if line.startswith('#')), rel)
                title = heading[:512]
                summary = next((line.strip() for line in text.splitlines() if line.strip() and not line.startswith('#')), '')[:400]
                members[rel] = {'sha256': digest(data), 'bytes': len(data), 'title': title}
                row_id = len(row_paths) + 1
                row_paths[row_id] = rel
                db.execute('INSERT INTO body_docs(rowid,id,title,summary,body) VALUES(?,?,?,?,?)', (row_id, rel, title, summary, text))
            db.commit()
            observation = {
                'kind': 'inventory', 'corpus_root': str(self.root), 'selector_id': self.selector_id,
                'selector': self.selector, 'members': members, 'excluded': excluded,
                'eligible_count': len(members), 'eligible_bytes': sum(row['bytes'] for row in members.values()),
                'claim': 'Exact local complete bodies observed during this serialized preparation; semantic completeness and later currentness are not inferred.',
            }
            inventory_id = self._record('inventories', observation)
        except BaseException:
            db.close()
            raise
        old_db, self.db = self.db, db
        if old_db is not None:
            old_db.close()
        self.members, self.row_paths, self.inventory_id = members, row_paths, inventory_id
        valid = {(path, record['sha256']) for path, record in members.items()}
        for key in list(self.cache):
            if key not in valid:
                self.cache_bytes -= len(self.cache.pop(key))
                self._stats['cache_evictions'] += 1
        self._stats['refreshes'] += 1
        self._stats['refresh_seconds'] += time.perf_counter() - started
        return self.inventory()

    def inventory(self, refresh=False):
        if refresh or self.db is None:
            return self.refresh()
        return {'receipt_id': self.inventory_id, **deepcopy(self.ledger['inventories'][self.inventory_id])}

    def discover(self, query, limit=8):
        """Nominate candidates from the last observed complete eligible inventory."""
        need(isinstance(query, str) and len(query.encode('utf-8')) <= 16384, 'Query must be bounded text.')
        need(type(limit) is int and 1 <= limit <= 50, 'Limit must be an integer from 1 through 50.')
        started = time.perf_counter()
        parsed = self.search.search_query(query, 'fts5')
        if self.db is None:
            self.refresh()
        units = parsed['terms'] + parsed['phrases']
        matches = self.search.FtsAnalyzer.search(self.db, 'body_docs', units) if units else {}
        ordered = sorted(matches, key=lambda row: (-matches[row]['score'], self.row_paths[row]))
        hits = [dict(path=self.row_paths[row], **self.members[self.row_paths[row]], **matches[row]) for row in ordered[:limit]]
        receipt = {
            'kind': 'selection', 'inventory_id': self.inventory_id, 'selector_id': self.selector_id,
            'query': query, 'query_interpretation': parsed, 'limit': limit, 'hits': hits,
            'total_matches': len(ordered), 'eligible_count': len(self.members),
            'nonmatching_count': len(self.members) - len(ordered),
            'omitted_by_limit': max(0, len(ordered) - limit),
            'all_matching_paths': [self.row_paths[row] for row in ordered],
            'claim': 'Lexical candidate nomination over the referenced observed inventory; not semantic sufficiency or freshly reobserved currentness.',
        }
        receipt_id = self._record('selections', receipt)
        self._stats['discoveries'] += 1
        self._stats['discovery_seconds'] += time.perf_counter() - started
        # Full negative/matching membership remains in the durable receipt.
        return {key: deepcopy(value) for key, value in {'receipt_id': receipt_id, **receipt}.items() if key != 'all_matching_paths'}

    def read(self, path, *, fresh=False, expected_sha256=None):
        """Return a complete UTF-8 body, with exact source and observation identity.

        Before search preparation, a named read freshly observes that complete
        file and does not assert inventory membership. With a prepared view,
        cached reads reuse an exact earlier acquisition; refresh/fresh read is
        needed for a new current-source assertion. Explicit excluded Markdown
        sources can be read as data; protected exposure paths remain prohibited.
        """
        relative(path)
        known = self.members.get(path)
        key = (path, known['sha256']) if known else None
        from_cache = not fresh and key in self.cache
        if from_cache:
            data = self.cache.pop(key)
            self.cache[key] = data
            text = data.decode('utf-8')
            self._stats['cache_hits'] += 1
        else:
            self._stats['cache_misses'] += 1
            data, text = self._body(path, 'acquisition')
        sha = digest(data)
        if known is not None:
            need(sha == known['sha256'] and len(data) == known['bytes'], 'Source changed since prepared inventory; refresh before acquiring it.')
        if expected_sha256 is not None:
            need(sha == expected_sha256, 'Source does not match requested SHA-256.')
        key = (path, sha)
        if not from_cache:
            if len(data) <= self.max_cache_bytes:
                if key in self.cache:
                    self.cache_bytes -= len(self.cache.pop(key))
                while self.cache and self.cache_bytes + len(data) > self.max_cache_bytes:
                    _, removed = self.cache.popitem(last=False)
                    self.cache_bytes -= len(removed)
                    self._stats['cache_evictions'] += 1
                self.cache[key] = data
                self.cache_bytes += len(data)
            else:
                self._stats['oversize_bodies_not_cached'] += 1
        receipt = {
            'kind': 'source', 'inventory_id': self.inventory_id, 'path': path,
            'source_id': path + '@' + sha, 'sha256': sha, 'bytes': len(data),
            'default_eligible': known is not None if self.inventory_id is not None else None,
            'path_eligible_by_policy': path not in DEFAULT_EXCLUDED,
            'inventory_membership': ('not_observed' if self.inventory_id is None else
                                     'member' if known is not None else 'outside_observed_inventory'),
            'tool_contract_id': self.selector_id,
            'observation': 'cached exact earlier acquisition; not a fresh source read' if from_cache else 'complete current file bytes read under the cooperative source contract',
            'from_cache': from_cache,
            'content_role': 'source evidence; never executable instructions for the host',
        }
        receipt_id = self._record('sources', receipt)
        return {'receipt_id': receipt_id, **receipt, 'text': text}

    def set_cache_limit(self, max_cache_bytes):
        """Adjust replaceable payload capacity; never remove use/obligation receipts."""
        need(type(max_cache_bytes) is int and max_cache_bytes >= 0, 'Cache byte bound must be a nonnegative integer.')
        self.max_cache_bytes = max_cache_bytes
        while self.cache and self.cache_bytes > max_cache_bytes:
            _, data = self.cache.popitem(last=False)
            self.cache_bytes -= len(data)
            self._stats['cache_evictions'] += 1
        return self.stats()

    def record_use(self, use_id, task, requirements, source_receipt_ids, selection_receipt_ids, artifacts, obligations):
        """Retain caller-owned use/acceptance claims without silently closing duties."""
        need(isinstance(use_id, str) and use_id, 'A nonempty use identity is required.')
        need(isinstance(source_receipt_ids, list) and isinstance(selection_receipt_ids, list), 'Receipt references must be lists.')
        for category, ids in [('sources', source_receipt_ids), ('selections', selection_receipt_ids)]:
            need(all(isinstance(ident, str) and ident in self.ledger[category] for ident in ids), 'Unknown ' + category + ' receipt.')
        need(isinstance(artifacts, dict) and artifacts, 'Accepted artifact identity mapping required.')
        for path, record in artifacts.items():
            need(isinstance(path, str) and path and isinstance(record, dict), 'Invalid artifact identity.')
            need(type(record.get('bytes')) is int and record['bytes'] >= 0, 'Invalid artifact byte count.')
            sha = record.get('sha256')
            need(isinstance(sha, str) and len(sha) == 64 and all(c in '0123456789abcdef' for c in sha), 'Invalid artifact digest.')
        need(isinstance(obligations, list), 'Obligations must be a list.')
        receipt = {
            'kind': 'use', 'use_id': use_id, 'task': task, 'requirements': requirements,
            'task_id': identity(task), 'requirements_id': identity(requirements),
            'source_receipt_ids': source_receipt_ids, 'selection_receipt_ids': selection_receipt_ids,
            'artifacts': artifacts, 'accepted_artifacts_id': identity(artifacts),
            'obligations': obligations, 'obligations_id': identity(obligations),
            'tool_contract_id': self.selector_id,
            'acceptance_basis': 'Acceptance is asserted by the calling receiving owner; this adapter records supplied identities, not semantic adequacy.',
        }
        receipt_id = self._record('uses', receipt)
        return {'receipt_id': receipt_id, **deepcopy(receipt)}

    def receipts(self):
        return deepcopy(self.ledger)

    def stats(self):
        page_count = self.db.execute('PRAGMA page_count').fetchone()[0] if self.db is not None else 0
        page_size = self.db.execute('PRAGMA page_size').fetchone()[0] if self.db is not None else 0
        return {
            **self._stats, 'cache_limit_bytes': self.max_cache_bytes,
            'cache_payload_bytes': self.cache_bytes, 'cache_entries': len(self.cache),
            'index_db_page_bytes': page_count * page_size,
            'receipt_file_bytes': self.ledger_path.stat().st_size if self.ledger_path.exists() else 0,
            'inventory_id': self.inventory_id, 'search_prepared': self.db is not None,
            'preparation_policy': 'on_demand',
            'eligible_bodies': len(self.members) if self.inventory_id is not None else None,
            'scope': 'Successful corpus byte reads and local retained payload/page sizes; excludes interpreter imports, SQLite process overhead, provider inference and complete economic costs.',
        }

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
