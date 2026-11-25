#!/usr/bin/env python3
"""Browse, verify and acquire published source libraries.

The host interprets questions, composes modules and accepts effects. This local
adapter acquires declared knowledge and imports data; it never executes a body.
"""
from __future__ import annotations

import argparse
from collections import Counter, namedtuple
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import zipfile

sys.dont_write_bytecode = True
if not __package__:
    # Retained scripts and bare imports must resolve siblings in this edition.
    import hashlib
    import importlib.util

    owner = Path(__file__).resolve().parent
    __package__ = '_g60_source_library_' + hashlib.sha256(os.fsencode(owner)).hexdigest()
    if __package__ not in sys.modules:
        spec = importlib.util.spec_from_file_location(__package__, owner / '__init__.py')
        package = importlib.util.module_from_spec(spec)
        sys.modules[__package__] = package
        spec.loader.exec_module(package)

from .library_format import (FORMAT, IDENT, MAX_ARCHIVE, MAX_BODIES, MAX_BODY,
                             MAX_MANIFEST, MAX_MODULES, MAX_PACKAGE, SELECTION,
                             SHA, check_manifest, decoded, digest, encoded,
                             integer, need, ordinary, read_bytes, relative, sha,
                             text, unique)
from .lexical_search import FtsAnalyzer, SEARCH_POLICY, SQLITE_ERRORS, STOPWORDS, TOKEN, search_query, sqlite3
from .product_bundle import (bundle_bytes, context, export, import_bundle as _import_bundle,
                             inspect_bundle, product, resolve)

INDEX_FORMAT = 'knowledge-search-index/1'
MAX_INDEX = 40 * 1024 * 1024
MAX_BATCH_QUERIES = 32
MAX_BATCH_INPUT = 128 * 1024
MAX_BATCH_RESULT = 8 * 1024 * 1024


def batch_json_size(value, maximum, extra_indent=0):
    """Count the actual indented UTF-8 representation without a second large buffer."""
    size = extra_indent
    for chunk in json.JSONEncoder(sort_keys=True, ensure_ascii=False, indent=2).iterencode(value):
        size += len(chunk.encode()) + extra_indent * chunk.count('\n')
        need(size <= maximum, 'discovery batch exceeds its JSON byte bound')
    return size


class SearchIndex:
    """Disposable in-memory candidate preparation over already verified bodies."""
    def __init__(self, rows, backend):
        self.backend, self.db = backend, None
        self.ids = {row['id'] for row in rows}
        if backend == 'fts5':
            need(sqlite3 is not None, 'SQLite support unavailable; use --backend bm25')
            try:
                self.db = sqlite3.connect(':memory:')
                FtsAnalyzer.create(self.db, 'docs')
                self.db.executemany('INSERT INTO docs VALUES(?,?,?,?)',
                    [(r['id'], r['title'], r['summary'], r['text']) for r in rows])
            except SQLITE_ERRORS as exc:
                self.close()
                raise ValueError('SQLite FTS5 preparation unavailable; use --backend bm25: ' + str(exc)) from exc
            return
        self.fields, self.frequencies = {}, {}
        for row in rows:
            fields = [TOKEN.findall(row[k].casefold()) for k in ('id', 'title', 'summary', 'text')]
            self.fields[row['id']] = [' ' + ' '.join(xs) + ' ' for xs in fields]
            self.frequencies[row['id']] = Counter(t for xs in fields for t in xs)
        self.df = Counter(t for frequencies in self.frequencies.values() for t in frequencies)
        self.lengths = {ident: sum(xs.values()) for ident, xs in self.frequencies.items()}
        self.average = sum(self.lengths.values()) / max(1, len(rows)) or 1

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def __del__(self):
        self.close()

    def search(self, parsed):
        units = parsed['terms'] + parsed['phrases']
        if not units: return {}
        if self.backend == 'fts5':
            return FtsAnalyzer.search(self.db, 'docs', units)
        phrase_counts = {}
        for phrase in parsed['phrases']:
            pattern = re.compile('(?=' + re.escape(' ' + phrase + ' ') + ')')
            phrase_counts[phrase] = {ident: sum(len(pattern.findall(field)) for field in fields)
                                    for ident, fields in self.fields.items()}
        phrase_df = {p: sum(count > 0 for count in counts.values()) for p, counts in phrase_counts.items()}
        result, count = {}, len(self.ids)
        for ident, frequencies in self.frequencies.items():
            norm = 1.2 * (.25 + .75 * self.lengths[ident] / self.average)
            hits, score = [], 0.0
            for term in parsed['terms']:
                frequency = frequencies[term]
                if frequency:
                    hits.append(term)
                    score += math.log1p((count - self.df[term] + .5) / (self.df[term] + .5)) * frequency * 2.2 / (frequency + norm)
            for phrase in parsed['phrases']:
                frequency = phrase_counts[phrase][ident]
                if frequency:
                    hits.append(phrase)
                    score += math.log1p((count - phrase_df[phrase] + .5) / (phrase_df[phrase] + .5)) * frequency * 2.2 / (frequency + norm)
            if hits: result[ident] = {'score': score, 'matched_terms': hits}
        return result


class PreparedIndex:
    """Query exact pinned derivative bytes, without claiming fresh source reads."""
    def __init__(self, path, expected_sha256, basis):
        self.db, self.temporary = None, None
        need(sqlite3 is not None, 'SQLite support unavailable; use ordinary BM25 discovery')
        data = read_bytes(path, MAX_INDEX)
        need(digest(data) == sha(expected_sha256), 'prepared index differs from expected digest')
        self.bytes, self.sha256, self.basis = len(data), expected_sha256, basis
        try:
            self.db = sqlite3.connect(':memory:')
            if hasattr(self.db, 'deserialize'):
                self.db.deserialize(data)
            else:
                # Python 3.10 / SQLite builds without deserialize still query the
                # exact verified bytes, not a path that may change after hashing.
                self.db.close(); self.db = None
                self.temporary = tempfile.TemporaryDirectory(prefix='knowledge-index-')
                copied = Path(self.temporary.name) / 'verified.sqlite'
                copied.write_bytes(data)
                self.db = sqlite3.connect(copied.as_uri() + '?mode=ro', uri=True)
            self.db.execute('PRAGMA query_only=ON')
            self.db.execute('PRAGMA trusted_schema=OFF')
            row = self.db.execute("SELECT value FROM metadata WHERE key='basis'").fetchone()
            need(row is not None and isinstance(row[0], str) and len(row[0].encode()) <= MAX_MANIFEST,
                 'invalid prepared index basis')
            need(decoded(row[0]) == basis, 'prepared index basis differs; rebuild for this catalogue, analyzer and scope')
            for table, identities in (('body_docs', basis['bodies']), ('module_docs', basis['modules'])):
                found = [r[0] for r in self.db.execute('SELECT rowid FROM ' + table + ' ORDER BY rowid')]
                need(found == list(range(1, len(identities) + 1)), 'prepared index row coverage differs')
        except Exception:
            self.close()
            raise

    def search(self, table, parsed):
        need(table in {'body_docs', 'module_docs'}, 'invalid prepared index table')
        units = parsed['terms'] + parsed['phrases']
        if not units: return {}
        ids = self.basis['bodies' if table == 'body_docs' else 'modules']
        return {ids[row - 1]: hit for row, hit in FtsAnalyzer.search(self.db, table, units).items()}

    def close(self):
        if self.db is not None:
            self.db.close(); self.db = None
        if self.temporary is not None:
            self.temporary.cleanup(); self.temporary = None

    def __del__(self):
        self.close()


# One prepared corpus per library. Its identity includes the ordered examination scope.
SearchScope = namedtuple('SearchScope', 'catalogue_sha256 policy backend body_ids')


class RetainedSearch:
    def __init__(self, scope, documents, modules):
        self.scope = scope
        self.bodies = SearchIndex(documents, scope.backend)
        try:
            self.modules = SearchIndex(modules, scope.backend)
        except Exception:
            self.bodies.close()
            raise

    def search(self, parsed):
        return self.bodies.search(parsed), self.modules.search(parsed)

    def close(self):
        self.bodies.close()
        self.modules.close()


class Library:
    def __init__(self, directory):
        self.directory = ordinary(directory)
        need(self.directory.is_dir(), 'library directory required')
        self.raw = read_bytes(self.directory / 'catalogue.json', MAX_MANIFEST)
        self.sha256 = digest(self.raw)
        self.catalogue = check_manifest(decoded(self.raw))
        self.bodies = {row['id']: row for row in self.catalogue['bodies']}
        self.modules = {row['id']: row for row in self.catalogue['modules']}
        self._search_cache = None

    def close(self):
        if self._search_cache is not None:
            self._search_cache.close()
            self._search_cache = None

    def __del__(self):
        if hasattr(self, '_search_cache'): self.close()

    @property
    def prepared_scope(self):
        return self._search_cache.scope if self._search_cache is not None else None

    def pin(self, row, kind='body'):
        return {'library_sha256': self.sha256, 'id': row['id'],
                'sha256': row['sha256'] if kind == 'body' else digest(encoded(row))}

    def body(self, ident):
        need(ident in self.bodies, 'body unavailable in this library: ' + ident)
        row = self.bodies[ident]
        data = read_bytes(self.directory / row['object'], MAX_BODY)
        need(len(data) == row['bytes'] and digest(data) == row['sha256'], 'body differs from declared source: ' + ident)
        data.decode('utf-8')
        return data

    def show(self, ident):
        return {'pin': self.pin(self.bodies[ident]), 'entry': self.bodies[ident], 'text': self.body(ident).decode()}

    def verify(self):
        total = 0
        for ident in self.bodies:
            total += len(self.body(ident))
            need(total <= MAX_PACKAGE, 'library exceeds total verified byte bound')
        return {'library_sha256': self.sha256, 'edition': self.catalogue['edition'], 'bodies': len(self.bodies),
                'modules': len(self.modules), 'body_bytes': total,
                'scope': 'Exact complete bodies and declared required closure; no semantic adequacy claim.'}

    def search_scope(self, scan_limit, max_bytes):
        integer(scan_limit, 'scan limit', 1, MAX_BODIES)
        integer(max_bytes, 'search bytes', 1, MAX_PACKAGE)
        selected, omitted, total = [], [], 0
        for ident, row in self.bodies.items():
            if len(selected) >= scan_limit or row['bytes'] > max_bytes - total:
                omitted.append(ident)
            else:
                selected.append(ident); total += row['bytes']
        return selected, omitted, total

    def index_basis(self, scan_limit, max_bytes):
        need(sqlite3 is not None, 'SQLite support unavailable; use ordinary BM25 discovery')
        selected, _, total = self.search_scope(scan_limit, max_bytes)
        return {'format': INDEX_FORMAT, 'library_sha256': self.sha256,
                'policy': SEARCH_POLICY, 'backend': 'fts5',
                'analyzer': FtsAnalyzer.identity(),
                'bodies': selected, 'modules': list(self.modules), 'body_bytes': total,
                'limits': {'bodies': scan_limit, 'body_bytes': max_bytes}}

    def prepare_index(self, output, scan_limit=512, max_bytes=8 * 1024 * 1024):
        """Build a fresh immutable derivative outside the delivered library."""
        basis = self.index_basis(scan_limit, max_bytes)
        # Reject symlinks first, then normalize dot/dotdot aliases for the
        # containment comparison. Path.absolute() alone retains those aliases.
        output = ordinary(output).resolve()
        library_directory = ordinary(self.directory).resolve()
        need(output.parent.is_dir(), 'prepared index parent directory must exist')
        need(library_directory != output and library_directory not in output.parents,
             'prepared index must be outside the library')
        need(not output.exists(), 'prepared index output already exists')
        db, staged = None, None
        try:
            descriptor, name = tempfile.mkstemp(prefix='.knowledge-index-', dir=output.parent)
            os.close(descriptor); staged = Path(name)
            db = sqlite3.connect(staged)
            db.execute('PRAGMA page_size=4096')
            db.execute('PRAGMA max_page_count=' + str(MAX_INDEX // 4096))
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('INSERT INTO metadata VALUES(?,?)', ('basis', encoded(basis).decode()))
            for table in ('body_docs', 'module_docs'):
                FtsAnalyzer.create(db, table)
            for number, ident in enumerate(basis['bodies'], 1):
                row = self.bodies[ident]
                body = self.body(ident).decode()
                db.execute('INSERT INTO body_docs(rowid,id,title,summary,body) VALUES(?,?,?,?,?)',
                           (number, ident, row['title'], row['summary'], body))
            for number, row in enumerate(self.modules.values(), 1):
                db.execute('INSERT INTO module_docs(rowid,id,title,summary,body) VALUES(?,?,?,?,?)',
                           (number, row['id'], row['title'], row['purpose'], ''))
            for table in ('body_docs', 'module_docs'):
                db.execute('INSERT INTO ' + table + '(' + table + ") VALUES('optimize')")
                db.execute('INSERT INTO ' + table + '(' + table + ") VALUES('integrity-check')")
            need(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'prepared index integrity check failed')
            db.commit()
            # optimize can leave old FTS segments on the freelist. This immutable
            # artifact has no future updates that could reuse those pages.
            db.execute('VACUUM')
            db.close(); db = None
            data = read_bytes(staged, MAX_INDEX)
            with staged.open('r+b') as stream: os.fsync(stream.fileno())
            # Link publishes a complete fresh file and cannot replace an existing
            # output, including one another cooperating builder just published.
            os.link(staged, output)
            if hasattr(os, 'O_DIRECTORY'):
                directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(directory_fd)
                finally: os.close(directory_fd)
            _, omitted, _ = self.search_scope(scan_limit, max_bytes)
            return {'format': 'knowledge-search-preparation/1', 'library_sha256': self.sha256,
                    'index': {'path': str(output), 'sha256': digest(data), 'bytes': len(data), 'format': INDEX_FORMAT},
                    'basis': basis, 'omitted_body_ids': omitted,
                    'source_verification': 'Every indexed complete body read and hash-verified during this build.',
                    'scope': 'Disposable lexical derivative, not source custody. Select this actual build receipt; a self-declared index digest does not warrant its construction. Exact acquisition still uses show.'}
        finally:
            if db is not None: db.close()
            if staged is not None:
                staged.unlink(missing_ok=True)
                Path(str(staged) + '-journal').unlink(missing_ok=True)

    def discover(self, query, limit=20, scan_limit=512, max_bytes=8 * 1024 * 1024, backend='bm25',
                 index=None, index_sha256=None):
        """Verify each examined body on ordinary searches, even with warm indexes."""
        return self._discover(query, limit, scan_limit, max_bytes, backend, index, index_sha256,
                              allow_retained_scope=False)

    def discover_retained_scope(self, query, limit=20, scan_limit=512, max_bytes=8 * 1024 * 1024):
        """Query the retained FTS5 observation, acquiring bodies on a scope miss."""
        return self._discover(query, limit, scan_limit, max_bytes, 'fts5', None, None,
                              allow_retained_scope=True)

    def _discover(self, query, limit, scan_limit, max_bytes, backend, index, index_sha256,
                  *, allow_retained_scope):
        text(query, 'query', 2048)
        integer(limit, 'result limit', 1, 100); integer(scan_limit, 'scan limit', 1, MAX_BODIES)
        integer(max_bytes, 'search bytes', 1, MAX_PACKAGE)
        need((index is None) == (index_sha256 is None), 'prepared index path and expected digest must be supplied together')
        need(index is None or backend == 'fts5', 'prepared index requires --backend fts5')
        if index is not None: sha(index_sha256)
        parsed = search_query(query, backend)
        documents = []
        searchable = bool(parsed['terms'] or parsed['phrases']) or query in self.bodies or query in self.modules
        examined, omitted, byte_count = self.search_scope(scan_limit, max_bytes) if searchable else ([], list(self.bodies), 0)
        scope = SearchScope(self.sha256, SEARCH_POLICY, backend, tuple(examined))
        reusable = self.prepared_scope == scope
        if allow_retained_scope and searchable and reusable:
            matches, module_matches = self._search_cache.search(parsed)
            preparation = {
                'storage': 'in-memory Library instance only', 'reused': True,
                'index_sha256': None, 'index_read_bytes': 0, 'index_verified': False,
                'source_verification': 'No canonical source bodies read on this call. The same in-memory FTS5 indexes retain bodies read and hash-verified when this catalogue and selected corpus were prepared. Use show for fresh exact acquisition.',
                'lifetime': "Retained until a different corpus replaces this catalogue's index, or close() ends the instance.",
            }
            return self._discovery_result(query, parsed, matches, module_matches, examined, omitted, byte_count,
                limit=limit, scan_limit=scan_limit, max_bytes=max_bytes, backend=backend,
                preparation=preparation, indexed=True)

        if index is None:
            for ident in examined:
                data = self.body(ident)
                # Verification is still owed on every ordinary call. Retaining
                # a second full-text materialization is not owed on a cache hit.
                if not reusable: documents.append(dict(self.bodies[ident], text=data.decode()))
        module_documents = [dict(id=row['id'], title=row['title'], summary=row['purpose'], text='')
                            for row in self.modules.values()]
        reused, index_bytes = False, 0
        if not searchable:
            matches, module_matches = {}, {}
        elif index is not None:
            prepared = PreparedIndex(index, index_sha256, self.index_basis(scan_limit, max_bytes))
            try:
                matches, module_matches = prepared.search('body_docs', parsed), prepared.search('module_docs', parsed)
                index_bytes, reused = prepared.bytes, True
            finally: prepared.close()
        elif backend == 'literal':
            def literal(items):
                found = {}
                for row in items:
                    surface = '\n'.join(row[k] for k in ('id', 'title', 'summary', 'text')).casefold()
                    matches = [term for term in parsed['terms'] if term in surface]
                    if matches: found[row['id']] = {'score': len(matches), 'matched_terms': matches}
                return found
            matches, module_matches = literal(documents), literal(module_documents)
        else:
            reused = reusable
            if not reused:
                self.close()
                self._search_cache = RetainedSearch(scope, documents, module_documents)
            matches, module_matches = self._search_cache.search(parsed)
        preparation = {'storage': 'pinned external contentless index' if index is not None else 'in-memory Library instance only',
            'reused': reused, 'index_sha256': index_sha256, 'index_read_bytes': index_bytes,
            'index_verified': bool(index_bytes),
            'source_verification': 'No canonical source bodies read on this call. Indexed nomination relies on the selected build; index_verified states whether its bytes and basis were checked. Use show or verify for current exact acquisition.' if index is not None
                else 'Every examined complete body read and hash-verified on this call.'}
        return self._discovery_result(query, parsed, matches, module_matches, examined, omitted, byte_count,
            limit=limit, scan_limit=scan_limit, max_bytes=max_bytes, backend=backend,
            preparation=preparation, indexed=index is not None)

    def discover_batch(self, queries, *, index, index_sha256, limit=20, scan_limit=512,
                       max_bytes=8 * 1024 * 1024, max_result_bytes=MAX_BATCH_RESULT):
        """Nominate bounded questions from one explicitly shared, verified index image."""
        need(isinstance(queries, list) and 1 <= len(queries) <= MAX_BATCH_QUERIES,
             'queries must be a list of 1 to ' + str(MAX_BATCH_QUERIES) + ' questions')
        integer(limit, 'result limit', 1, 100); integer(scan_limit, 'scan limit', 1, MAX_BODIES)
        integer(max_bytes, 'search bytes', 1, MAX_PACKAGE)
        integer(max_result_bytes, 'batch result bytes', 1, MAX_BATCH_RESULT)
        need(index is not None, 'prepared index path is required for a batch')
        sha(index_sha256)
        questions = []
        for query in queries:
            text(query, 'query', 2048)
            parsed = search_query(query, 'fts5')
            searchable = bool(parsed['terms'] or parsed['phrases']) or query in self.bodies or query in self.modules
            questions.append((query, parsed, searchable))
        batch_json_size(queries, MAX_BATCH_INPUT - 1)
        prepared = None
        try:
            if any(searchable for _, _, searchable in questions):
                prepared = PreparedIndex(index, index_sha256, self.index_basis(scan_limit, max_bytes))
                selected, omitted, byte_count = self.search_scope(scan_limit, max_bytes)
            result = {'format': 'knowledge-discovery-batch/1', 'library_sha256': self.sha256,
                'preparation': {'storage': 'one private prepared index image for this batch',
                    'index_sha256': index_sha256, 'index_verified': prepared is not None,
                    'index_opens': int(prepared is not None),
                    'index_read_bytes': prepared.bytes if prepared is not None else 0,
                    'canonical_body_read_bytes': 0,
                    'lifetime': 'This call only; the shared image is closed before return or refusal.',
                    'source_verification': 'No canonical source bodies read. Searchable questions share one verified derivative and its exact catalogue/analyzer/scope basis. The selected build warrants the postings; use show or verify for current exact acquisition.'},
                'limits': {'queries': MAX_BATCH_QUERIES, 'query_bytes': 2048,
                    'input_bytes': MAX_BATCH_INPUT, 'result_bytes': max_result_bytes},
                'results': []}
            # A nonempty results array adds a newline before/after its children
            # and two spaces before its closing bracket. Each child is indented
            # four more spaces than a standalone result; later children add ',\n'.
            result_bytes = batch_json_size(result, max_result_bytes - 1) + 1
            for query, parsed, searchable in questions:
                if searchable:
                    matches = prepared.search('body_docs', parsed)
                    module_matches = prepared.search('module_docs', parsed)
                    examined, excluded, body_bytes = selected, omitted, byte_count
                else:
                    matches, module_matches = {}, {}
                    examined, excluded, body_bytes = [], list(self.bodies), 0
                preparation = {'storage': 'private index image shared within this explicit batch',
                    'reused': searchable, 'index_sha256': index_sha256, 'index_read_bytes': 0,
                    'index_verified': searchable,
                    'index_acquisition': 'Shared batch preparation; zero additional image reads for this question.' if searchable
                        else 'No searchable units or exact identity; this question did not require the image.',
                    'source_verification': 'No canonical source bodies read. Image verification and acquisition, when needed, are accounted for once in the enclosing batch. Use show or verify for current exact acquisition.'}
                found = self._discovery_result(query, parsed, matches, module_matches, examined, excluded, body_bytes,
                    limit=limit, scan_limit=scan_limit, max_bytes=max_bytes, backend='fts5',
                    preparation=preparation, indexed=True)
                separator_bytes = 4 if not result['results'] else 2
                result_bytes += separator_bytes + batch_json_size(found,
                    max_result_bytes - result_bytes - separator_bytes, extra_indent=4)
                result['results'].append(found)
            return result
        finally:
            if prepared is not None: prepared.close()

    def _discovery_result(self, query, parsed, matches, module_matches, examined, omitted, byte_count,
                          *, limit, scan_limit, max_bytes, backend, preparation, indexed):
        """Shared ranking and coverage assembly for ordinary and batch discovery."""
        rows = []
        if query in examined:
            matches.setdefault(query, {'score': 0.0, 'matched_terms': []})
        if query in self.modules:
            module_matches.setdefault(query, {'score': 0.0, 'matched_terms': []})
        for ident, match in matches.items():
            row = self.bodies[ident]
            rows.append({'pin': self.pin(row), 'title': row['title'], 'summary': row['summary'],
                         **match, 'bytes': row['bytes'], 'exact_identity': query == ident})
        # Exact logical identity is a direct route, but never bypasses scan scope.
        rows.sort(key=lambda row: (not row['exact_identity'], -row['score'], row['pin']['id']))
        matched_ids = {row['pin']['id'] for row in rows}
        modules = []
        for row in self.modules.values():
            hits = [x for x in row['members'] if x in matched_ids]
            if hits or row['id'] in module_matches:
                match = module_matches.get(row['id'], {'score': 0.0, 'matched_terms': []})
                modules.append({'pin': self.pin(row, 'module'), 'title': row['title'], 'purpose': row['purpose'],
                                'matching_members': hits, 'members': len(row['members']), **match})
        if backend != 'literal':
            modules.sort(key=lambda row: (row['pin']['id'] != query, -row['score'], -len(row['matching_members']), row['pin']['id']))
        return {'format': 'knowledge-discovery/1', 'library_sha256': self.sha256,
                'bodies': rows[:limit], 'modules': modules[:limit],
                'query': {'text': query, 'backend': backend, 'policy': SEARCH_POLICY, **parsed,
                    'empty_after_filtering': not (parsed['terms'] or parsed['phrases'])},
                'preparation': preparation,
                'coverage': {'examined_bodies': len(examined), 'examined_body_bytes': 0 if indexed else byte_count,
                    'body_examination': 'prepared text-index representations' if indexed else 'complete source bodies',
                    'indexed_body_bytes': byte_count if indexed else 0,
                    'verified_bodies': 0 if indexed else len(examined),
                    'matched_bodies': len(rows), 'omitted_body_count': len(omitted), 'omitted_body_ids': omitted,
                    'body_results_truncated': len(rows) > limit, 'module_results_truncated': len(modules) > limit,
                    'limits': {'results': limit, 'bodies': scan_limit, 'body_bytes': max_bytes}},
                'scope': 'Ranked lexical candidates and authored module membership nominate routes within the examined scope. Scores are not relevance probabilities. Complete-body reading, semantic fit and effect authority remain separate.'}


def import_bundle(path, store):
    return _import_bundle(path, store, open_library=Library)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', action='append', type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('verify'); sub.add_parser('modules')
    show = sub.add_parser('show'); show.add_argument('identity'); show.add_argument('--text', action='store_true')
    discover = sub.add_parser('discover'); discover.add_argument('--query', required=True)
    discover.add_argument('--limit', type=int, default=20); discover.add_argument('--scan-limit', type=int, default=512)
    discover.add_argument('--max-bytes', type=int, default=8 * 1024 * 1024)
    discover.add_argument('--backend', choices=('bm25', 'fts5', 'literal'), default='bm25')
    discover.add_argument('--index', type=Path, help='Nominate from this prepared derivative without rereading canonical bodies; requires FTS5 and expected digest')
    discover.add_argument('--index-sha256', help='Exact expected digest from the selected prepare-index receipt')
    batch = sub.add_parser('discover-batch', help='Share one verified prepared image across a bounded JSON array of questions')
    batch.add_argument('--queries', type=Path, required=True, help='UTF-8 JSON array of 1 to 32 query strings')
    batch.add_argument('--index', type=Path, required=True)
    batch.add_argument('--index-sha256', required=True)
    batch.add_argument('--limit', type=int, default=20); batch.add_argument('--scan-limit', type=int, default=512)
    batch.add_argument('--max-bytes', type=int, default=8 * 1024 * 1024)
    batch.add_argument('--max-result-bytes', type=int, default=MAX_BATCH_RESULT)
    prepare = sub.add_parser('prepare-index', help='Verify selected sources and build an immutable contentless FTS5 derivative')
    prepare.add_argument('--output', type=Path, required=True)
    prepare.add_argument('--scan-limit', type=int, default=512)
    prepare.add_argument('--max-bytes', type=int, default=8 * 1024 * 1024)
    ctx = sub.add_parser('context'); ctx.add_argument('--selection', type=Path, required=True)
    ctx.add_argument('--max-bytes', type=int, default=8 * 1024 * 1024)
    exp = sub.add_parser('export'); exp.add_argument('--selection', type=Path, required=True)
    exp.add_argument('--edition', required=True); exp.add_argument('--output', type=Path, required=True)
    ins = sub.add_parser('inspect'); ins.add_argument('--bundle', type=Path, required=True)
    imp = sub.add_parser('import'); imp.add_argument('--bundle', type=Path, required=True); imp.add_argument('--store', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == 'inspect': result = inspect_bundle(args.bundle)
        elif args.action == 'import': result = import_bundle(args.bundle, args.store)
        else:
            libraries = [Library(path) for path in (args.library or [Path(__file__).parent / 'library'])]
            if args.action == 'verify': result = {'libraries': [lib.verify() for lib in libraries]}
            elif args.action == 'modules': result = {'libraries': [{'library_sha256': lib.sha256, 'modules': [{'pin': lib.pin(row, 'module'), 'entry': row} for row in lib.modules.values()]} for lib in libraries]}
            elif args.action == 'show':
                found = [lib for lib in libraries if args.identity in lib.bodies]
                need(found, f'identity absent from the selected libraries: {args.identity}')
                need(len(found) == 1, f'identity is present in several selected libraries: {args.identity}; select one with --library')
                result = found[0].show(args.identity)
                if args.text: sys.stdout.write(result['text']); return
            elif args.action == 'prepare-index':
                need(len(libraries) == 1, 'prepare-index requires one selected library')
                result = libraries[0].prepare_index(args.output, args.scan_limit, args.max_bytes)
            elif args.action == 'discover':
                need(args.index is None or len(libraries) == 1, 'prepared discovery requires one selected library')
                result = {'libraries': [lib.discover(args.query, args.limit, args.scan_limit, args.max_bytes, args.backend,
                            args.index, args.index_sha256) for lib in libraries]}
            elif args.action == 'discover-batch':
                need(len(libraries) == 1, 'discover-batch requires one selected library')
                queries = decoded(read_bytes(args.queries, MAX_BATCH_INPUT))
                result = libraries[0].discover_batch(queries, index=args.index, index_sha256=args.index_sha256,
                    limit=args.limit, scan_limit=args.scan_limit, max_bytes=args.max_bytes,
                    max_result_bytes=args.max_result_bytes)
            else:
                selection = decoded(read_bytes(args.selection, MAX_MANIFEST))
                result = context(libraries, selection, args.max_bytes) if args.action == 'context' else export(libraries, selection, args.edition, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, UnicodeError, RuntimeError, zipfile.BadZipFile, *SQLITE_ERRORS) as exc:
        print(json.dumps({'status': 'refused', 'operation': args.action, 'error': str(exc)}), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
