"""Complete Markdown source access with disposable search/cache and durable use receipts.

One cooperative owner serializes calls and source changes. Known-path reads can
observe complete bodies without preparing search. Discovery/inventory requests
prepare an explicit complete view; refresh() supplies a new current observation.
Text is returned as evidence, never executed. No semantic acceptance is inferred.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import time


VERSION = 'g60-source-service/1'
DEFAULT_EXCLUDED = {
    'review/external-review-original.md': 'External review is source data, not default construction guidance.',
    'coverage/historical-material.md': 'Locator catalogues are not locally retained argument bodies.',
    'coverage/recent-historical-material.md': 'Locator catalogues are not locally retained argument bodies.',
}
PROTECTED_PREFIXES = ('evidence/fleet/D12/', 'instruments/incident-corpus/D12/')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def identity(value):
    return digest(encoded(value))


def need(condition, message):
    if not condition:
        raise ValueError(message)


def relative(value):
    need(isinstance(value, str) and value and '\\' not in value, 'A POSIX relative source path is required.')
    p = PurePosixPath(value)
    need(not p.is_absolute() and all(part not in {'', '.', '..'} for part in value.split('/')), 'Unsafe source path.')
    need(p.suffix.lower() == '.md', 'Only complete Markdown sources are exposed.')
    need(not any(value.startswith(prefix) for prefix in PROTECTED_PREFIXES), 'Source is protected by this study exposure boundary.')
    return value


def bundled_search_module_path(engine=None):
    """Locate the trusted lexical reader in the selected bundle."""
    engine = Path(engine) if engine is not None else Path(__file__).resolve().parents[2] / 'engine'
    runtime = engine / 'runtime'
    if not (runtime.exists() or runtime.is_symlink()):
        runtime = engine / 'prototype'
    return runtime / 'knowledge-system/knowledge.py'


class SourceService:
    """Scoped source service; body cache bounds do not bound the derived index/RSS."""

    def __init__(self, corpus_root, state_dir, max_cache_bytes=65536, *, search_module_path=None):
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
        if self.ledger_path.exists():
            need(not self.ledger_path.is_symlink(), 'Receipt symlinks are unsupported.')
            self.ledger = json.loads(self.ledger_path.read_text(encoding='utf-8'))
            need(self.ledger.get('schema') == VERSION and set(self.ledger) == {'schema', 'inventories', 'selections', 'sources', 'uses'}, 'Unknown receipt ledger.')
            for category in ('inventories', 'selections', 'sources', 'uses'):
                need(isinstance(self.ledger[category], dict), 'Invalid receipt category.')
                for ident, record in self.ledger[category].items():
                    need(identity(record) == ident, 'Receipt identity mismatch: ' + ident)
        else:
            self.ledger = {'schema': VERSION, 'inventories': {}, 'selections': {}, 'sources': {}, 'uses': {}}
        module_path = Path(search_module_path) if search_module_path else bundled_search_module_path()
        need(module_path.is_file() and not module_path.is_symlink(), 'Select the bundled trusted knowledge.py search implementation.')
        module_bytes = module_path.read_bytes()
        spec = importlib.util.spec_from_file_location('_g60_acquired_knowledge_' + digest(module_bytes)[:16], module_path)
        self.search = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.search)
        self.selector = {
            'service': VERSION,
            'implementation_sha256': digest(Path(__file__).read_bytes()),
            'acquired_search_sha256': digest(module_bytes),
            'backend': 'native FtsAnalyzer, contentless body_docs',
            'query_parser': 'native search_query(query, fts5)',
            'analyzer': self.search.FtsAnalyzer.identity(),
            'eligible_extension': '.md (case insensitive)',
            'default_excluded': DEFAULT_EXCLUDED,
            'protected_prefixes': list(PROTECTED_PREFIXES),
            'candidate_match': 'OR over parsed terms/phrases; ranked weighted BM25; path breaks ties',
            'preparation_policy': 'Known-path acquisition before preparation observes that body only; discover/inventory/refresh demand complete preparation.',
            'source_model': 'Serialized cooperative source changes; complete explicit refresh observations.',
        }
        self.selector_id = identity(self.selector)

    def _ordinary(self, relative_path):
        path = self.root / relative(relative_path)
        for current in (self.root, *path.parents, path):
            need(not current.is_symlink(), 'Source symlinks are unsupported.')
        need(path.is_file() and stat.S_ISREG(path.stat().st_mode), 'Ordinary source file required: ' + relative_path)
        return path

    def _body(self, relative_path, purpose):
        data = self._ordinary(relative_path).read_bytes()
        self._stats['corpus_reads'] += 1
        self._stats['corpus_read_bytes'] += len(data)
        self._stats[purpose + '_read_bytes'] += len(data)
        return data, data.decode('utf-8')

    def _persist(self):
        data = encoded(self.ledger) + b'\n'
        temporary = self.ledger_path.with_suffix('.json.tmp')
        with temporary.open('wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.ledger_path)
        self._stats['receipt_write_bytes'] += len(data)

    def _record(self, category, value):
        value = json.loads(encoded(value))
        ident = identity(value)
        if ident not in self.ledger[category]:
            self.ledger[category][ident] = value
            self._persist()
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
