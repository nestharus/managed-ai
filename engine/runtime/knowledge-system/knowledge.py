#!/usr/bin/env python3
"""Bounded discovery and complete-body product interchange for a capable host.

The host interprets questions, composes modules and accepts effects. This local
adapter acquires declared knowledge and imports data; it never executes a body.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import zipfile

try:
    import sqlite3
except ImportError:  # Default acquisition and BM25 do not require this optional build.
    sqlite3 = None
SQLITE_ERRORS = (sqlite3.Error,) if sqlite3 is not None else ()

sys.dont_write_bytecode = True
FORMAT = 'knowledge-library/1'
SELECTION = 'knowledge-selection/1'
MAX_MANIFEST = 4 * 1024 * 1024
MAX_BODY = 2 * 1024 * 1024
MAX_BODIES = 2048
MAX_MODULES = 256
MAX_PACKAGE = 32 * 1024 * 1024
MAX_ARCHIVE = 40 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{64}\Z')
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,239}\Z')
SEARCH_POLICY = 'knowledge-search/1'
INDEX_FORMAT = 'knowledge-search-index/1'
MAX_INDEX = 40 * 1024 * 1024
MAX_BATCH_QUERIES = 32
MAX_BATCH_INPUT = 128 * 1024
MAX_BATCH_RESULT = 8 * 1024 * 1024
STOPWORDS = frozenset(('a an and are as at be been being but by can could did do does '
    'for from had has have how i if in into is it its may me my of on or our should '
    'so than that the their them then there these they this those to was we were '
    'what when where which who why will with would you your').split())
TOKEN = re.compile(r'\w+')


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def need(condition, message):
    if not condition:
        raise ValueError(message)


def batch_json_size(value, maximum, extra_indent=0):
    """Count the actual indented UTF-8 representation without a second large buffer."""
    size = extra_indent
    for chunk in json.JSONEncoder(sort_keys=True, ensure_ascii=False, indent=2).iterencode(value):
        size += len(chunk.encode()) + extra_indent * chunk.count('\n')
        need(size <= maximum, 'discovery batch exceeds its JSON byte bound')
    return size


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, 'duplicate JSON key: ' + key)
        result[key] = value
    return result


def decoded(data):
    def refuse_constant(value):
        raise ValueError('non-JSON numeric constant: ' + value)
    return json.loads(data, object_pairs_hook=unique, parse_constant=refuse_constant)


def ordinary(path):
    path = Path(path).expanduser().absolute()
    for part in (*reversed(path.parents), path):
        need(not part.is_symlink(), 'symlink path is unsupported: ' + str(part))
    return path


def read_bytes(path, limit):
    path = ordinary(path)
    need(path.is_file() and stat.S_ISREG(path.stat().st_mode), 'ordinary file required: ' + str(path))
    need(path.stat().st_size <= limit, 'file exceeds byte bound: ' + str(path))
    with path.open('rb') as stream:
        # A read(limit + 1) can allocate the entire allowed bound even for a
        # small file. Bound each allocation while still detecting growth past
        # the limit and consuming short reads until actual EOF.
        chunks, remaining = [], limit + 1
        while remaining:
            chunk = stream.read(min(64 * 1024, remaining))
            if not chunk: break
            chunks.append(chunk); remaining -= len(chunk)
        body = b''.join(chunks)
    need(len(body) <= limit, 'file grew beyond byte bound: ' + str(path))
    return body


def relative(value):
    need(isinstance(value, str) and IDENT.fullmatch(value), 'invalid relative identity')
    need(not PurePosixPath(value).is_absolute() and all(x not in {'', '.', '..'} for x in value.split('/')), 'unsafe relative identity')
    return value


def text(value, label, maximum=16384):
    need(isinstance(value, str) and value.strip() and len(value.encode()) <= maximum, 'invalid ' + label)
    return value


def sha(value):
    need(isinstance(value, str) and SHA.fullmatch(value), 'invalid SHA-256')
    return value


def integer(value, label, low, high):
    need(type(value) is int and low <= value <= high, 'invalid ' + label)
    return value


def check_manifest(value):
    need(isinstance(value, dict) and set(value) == {'format', 'edition', 'bodies', 'modules', 'provenance'}, 'invalid library fields')
    need(value['format'] == FORMAT, 'unsupported library format; explicit migration required')
    text(value['edition'], 'edition', 512)
    need(isinstance(value['provenance'], dict), 'provenance must be an object')
    bodies, modules = value['bodies'], value['modules']
    need(isinstance(bodies, list) and 1 <= len(bodies) <= MAX_BODIES, 'invalid body count')
    need(isinstance(modules, list) and len(modules) <= MAX_MODULES, 'invalid module count')
    by_id = {}
    for row in bodies:
        need(isinstance(row, dict) and set(row) == {'id', 'title', 'summary', 'object', 'sha256', 'bytes', 'source', 'link_base', 'references'}, 'invalid body fields')
        ident = relative(row['id'])
        need(ident not in by_id, 'duplicate body identity: ' + ident)
        text(row['title'], 'title', 1024); text(row['summary'], 'summary', 4096)
        h = sha(row['sha256'])
        need(row['object'] == 'objects/' + h + '.txt', 'body object is not content-addressed')
        integer(row['bytes'], 'body bytes', 0, MAX_BODY)
        need(isinstance(row['source'], dict) and set(row['source']) == {'path', 'sha256'}, 'invalid body source')
        relative(row['source']['path']); relative(row['link_base'])
        need(row['source']['sha256'] == h, 'complete copied source must have the body digest')
        need(isinstance(row['references'], list) and len(row['references']) <= 2048, 'invalid references')
        for ref in row['references']:
            need(isinstance(ref, dict) and set(ref) == {'target', 'anchor', 'kind'}, 'invalid reference')
            text(ref['target'], 'reference target', 8192)
            need(isinstance(ref['anchor'], str) and len(ref['anchor']) <= 1024, 'invalid reference anchor')
            need(isinstance(ref['kind'], str) and ref['kind'] in {'source', 'url'}, 'invalid reference kind')
        by_id[ident] = row
    need(sum(row['bytes'] for row in bodies) <= MAX_PACKAGE, 'library exceeds declared total body byte bound')
    module_ids = set()
    for row in modules:
        need(isinstance(row, dict) and set(row) == {'id', 'title', 'purpose', 'members', 'requires', 'parents', 'external_participation'}, 'invalid module fields')
        ident = relative(row['id']); need(ident not in module_ids, 'duplicate module identity: ' + ident)
        module_ids.add(ident)
        text(row['title'], 'module title', 1024); text(row['purpose'], 'module purpose')
        for key in ('members', 'requires'):
            need(isinstance(row[key], list) and len(row[key]) <= MAX_BODIES, 'invalid module ' + key)
            for member in row[key]: relative(member)
            need(len(row[key]) == len(set(row[key])), 'duplicate module ' + key)
        need(row['members'], 'a module needs at least one complete member body')
        need(all(x in by_id for x in row['members']), 'module has an unavailable required body: ' + ident)
        need(isinstance(row['parents'], list) and len(row['parents']) <= MAX_MODULES, 'invalid parents')
        for parent in row['parents']:
            need(isinstance(parent, dict) and set(parent) == {'id', 'role'}, 'invalid parent relation')
            relative(parent['id']); text(parent['role'], 'parent role', 4096)
        need(isinstance(row['external_participation'], list) and len(row['external_participation']) <= 256, 'invalid external participation')
        for item in row['external_participation']: text(item, 'external participation')
    for row in modules:
        need(all(x in module_ids for x in row['requires']), 'module has an unavailable required module: ' + row['id'])
    return value


def search_query(query, backend):
    """Interpret ordinary text as data; double-quoted spans are token phrases."""
    need(backend in {'bm25', 'fts5', 'literal'}, 'unknown discovery backend')
    if backend == 'literal':
        return {'terms': list(dict.fromkeys(query.casefold().split())), 'phrases': [],
                'ignored_terms': [], 'tokenization': 'casefolded whitespace substrings',
                'language': 'no stemming or stopword policy'}
    need(query.count('"') % 2 == 0, 'unclosed double quote in query')
    terms, phrases, ignored = [], [], []
    for match in re.finditer(r'"([^"]*)"|([^\s"]+)', query):
        if match.group(1) is not None:
            phrase = ' '.join(TOKEN.findall(match.group(1).casefold()))
            if phrase: phrases.append(phrase)
        else:
            for term in TOKEN.findall(match.group(2).casefold()):
                (ignored if term in STOPWORDS else terms).append(term)
    terms, phrases, ignored = (list(dict.fromkeys(xs)) for xs in (terms, phrases, ignored))
    need(len(terms) + len(phrases) <= 128, 'query exceeds 128 distinct terms and phrases')
    return {'terms': terms, 'phrases': phrases, 'ignored_terms': ignored,
            'tokenization': 'whole Unicode word tokens; quoted token phrases',
            'language': 'English stopwords; English porter stemming' if backend == 'fts5'
                        else 'English stopwords; no stemming'}


class FtsAnalyzer:
    """Shared lexical index schema, query and recorded identity for both storage modes."""
    weights = (1, 5, 2, 1)
    tokenizer = 'porter unicode61'
    keys = {'docs': 'id', 'body_docs': 'rowid', 'module_docs': 'rowid'}

    @classmethod
    def identity(cls):
        return {'tokenizer': cls.tokenizer, 'weights': list(cls.weights),
                'detail': 'full', 'columnsize': 1, 'unicode_version': unicodedata.unidata_version,
                'sqlite_version': sqlite3.sqlite_version, 'word_pattern': TOKEN.pattern,
                'stopwords_sha256': digest(encoded(sorted(STOPWORDS)))}

    @classmethod
    def create(cls, db, table):
        key = cls.keys[table]
        content = ",content=''" if key == 'rowid' else ''
        db.execute(f"CREATE VIRTUAL TABLE {table} USING fts5(id,title,summary,body{content},tokenize='{cls.tokenizer}')")

    @classmethod
    def search(cls, db, table, units):
        key = cls.keys[table]
        quoted = ['"' + term.replace('"', '""') + '"' for term in units]
        condition = f' FROM {table} WHERE {table} MATCH ?'
        weights = ','.join(str(w) for w in cls.weights)
        rows = db.execute(f'SELECT {key},bm25({table},{weights})' + condition, (' OR '.join(quoted),))
        hits = {ident: {'score': -score, 'matched_terms': []} for ident, score in rows}
        for term, expression in zip(units, quoted):
            for ident, in db.execute(f'SELECT {key}' + condition, (expression,)):
                hits[ident]['matched_terms'].append(term)
        return hits


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
            for index in self._search_cache[1:]: index.close()
            self._search_cache = None

    def __del__(self):
        if hasattr(self, '_search_cache'): self.close()

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
            with staged.open('rb') as stream: os.fsync(stream.fileno())
            # Link publishes a complete fresh file and cannot replace an existing
            # output, including one another cooperating builder just published.
            os.link(staged, output)
            directory_fd = os.open(output.parent, os.O_RDONLY)
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
        text(query, 'query', 2048)
        integer(limit, 'result limit', 1, 100); integer(scan_limit, 'scan limit', 1, MAX_BODIES)
        integer(max_bytes, 'search bytes', 1, MAX_PACKAGE)
        need((index is None) == (index_sha256 is None), 'prepared index path and expected digest must be supplied together')
        need(index is None or backend == 'fts5', 'prepared index requires --backend fts5')
        if index is not None: sha(index_sha256)
        parsed = search_query(query, backend)
        rows, documents = [], []
        searchable = bool(parsed['terms'] or parsed['phrases']) or query in self.bodies or query in self.modules
        examined, omitted, byte_count = self.search_scope(scan_limit, max_bytes) if searchable else ([], list(self.bodies), 0)
        key = (self.sha256, SEARCH_POLICY, backend, tuple(examined))
        reusable = self._search_cache is not None and self._search_cache[0] == key
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
            # Every selected body was read and verified above, including cache hits.
            # One cached scope avoids accumulating indexes for arbitrary selections.
            reused = reusable
            if not reused:
                self.close()
                bodies_index = SearchIndex(documents, backend)
                try: modules_index = SearchIndex(module_documents, backend)
                except Exception:
                    bodies_index.close()
                    raise
                self._search_cache = (key, bodies_index, modules_index)
            matches = self._search_cache[1].search(parsed)
            module_matches = self._search_cache[2].search(parsed)
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


def resolve(libraries, selection, limit=MAX_PACKAGE):
    integer(limit, 'selection body bytes', 1, MAX_PACKAGE)
    need(isinstance(selection, dict) and set(selection) == {'format', 'purpose', 'bodies', 'modules'}, 'invalid selection fields')
    need(selection['format'] == SELECTION, 'unsupported selection format')
    text(selection['purpose'], 'receiving purpose')
    by_hash = {lib.sha256: lib for lib in libraries}
    chosen, chosen_modules, seen = {}, {}, set()
    for key in ('bodies', 'modules'):
        need(isinstance(selection[key], list) and len(selection[key]) <= MAX_BODIES, 'invalid selected ' + key)
        for pin in selection[key]:
            need(isinstance(pin, dict) and set(pin) == {'library_sha256', 'id', 'sha256'}, 'invalid source pin')
            h = sha(pin['library_sha256']); sha(pin['sha256']); relative(pin['id'])
            need(h in by_hash, 'selected library unavailable or changed')
            lib = by_hash[h]
            available = lib.bodies if key == 'bodies' else lib.modules
            need(pin['id'] in available, 'selected identity unavailable')
            expected = lib.pin(available[pin['id']], 'body' if key == 'bodies' else 'module')
            need(pin == expected, 'selected source pin changed')
            if key == 'bodies': chosen[(h, pin['id'])] = (lib, available[pin['id']])
            else:
                pending = [pin['id']]
                while pending:
                    ident = pending.pop()
                    if (h, ident) in seen: continue
                    seen.add((h, ident)); row = lib.modules[ident]
                    chosen_modules[(h, ident)] = (lib, row)
                    for body_id in row['members']: chosen[(h, body_id)] = (lib, lib.bodies[body_id])
                    pending.extend(row['requires'])
    need(chosen, 'selection needs at least one complete body')
    total, data = 0, {}
    for key, (lib, row) in chosen.items():
        need(row['bytes'] <= limit - total, 'selected complete bodies exceed byte bound')
        body = lib.body(row['id']); total += len(body); data[key] = body
    return chosen, chosen_modules, data, total


def context(libraries, selection, max_bytes=8 * 1024 * 1024):
    integer(max_bytes, 'context bytes', 1, MAX_PACKAGE)
    rows, modules, data, total = resolve(libraries, selection, max_bytes)
    result = {'format': 'knowledge-context/1', 'purpose': selection['purpose'], 'selection': selection,
            'body_bytes': total, 'modules': [{'pin': lib.pin(row, 'module'), 'entry': row} for lib, row in modules.values()],
            'bodies': [{'pin': lib.pin(row), 'entry': row, 'text': data[key].decode()} for key, (lib, row) in rows.items()],
            'scope': 'Complete selected source exposure. No inferred applicability, model invocation, publication or effect authority.'}
    need(len(encoded(result)) <= MAX_PACKAGE + MAX_MANIFEST, 'rendered context exceeds total byte bound')
    return result


def product(libraries, selection, edition):
    rows, modules, data, _ = resolve(libraries, selection)
    body_map, module_map, objects = {}, {}, {}
    for key, (_, row) in rows.items():
        need(row['id'] not in body_map or body_map[row['id']] == row, 'body identity collision; select one version explicitly: ' + row['id'])
        body_map[row['id']] = row; objects[row['object']] = data[key]
    for _, row in modules.values():
        need(row['id'] not in module_map or module_map[row['id']] == row, 'module identity collision; select one interpretation explicitly: ' + row['id'])
        module_map[row['id']] = row
    refs = [{'from': row['id'], **ref} for row in body_map.values() for ref in row['references']
            if ref['kind'] == 'url' or ref['target'] not in body_map]
    manifest = {'format': FORMAT, 'edition': text(edition, 'edition', 512),
        'bodies': sorted(body_map.values(), key=lambda x: x['id']),
        'modules': sorted(module_map.values(), key=lambda x: x['id']),
        'provenance': {'selection': selection, 'source_libraries': sorted({lib.sha256 for lib, _ in rows.values()}),
            'outside_reference_routes': refs,
            'scope': 'The required bodies/modules declared by this selection are included. Other reference routes are disclosed, not asserted to be supplied prerequisites. Parent roles do not imply inherited permission or execution.'}}
    check_manifest(manifest)
    raw = encoded(manifest); need(len(raw) <= MAX_MANIFEST, 'product manifest exceeds byte bound')
    return raw, objects


def export(libraries, selection, edition, output):
    raw, objects = product(libraries, selection, edition)
    output = ordinary(output)
    need(output.parent.is_dir(), 'existing output parent required')
    need(not output.exists(), 'output exists; inspect it rather than overwrite')
    with output.open('xb') as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted({'catalogue.json': raw, **objects}.items()):
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)); info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16; archive.writestr(info, data)
    return inspect_bundle(output)


def bundle_bytes(path):
    archive_bytes = read_bytes(path, MAX_ARCHIVE)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        infos = archive.infolist(); names = [x.filename for x in infos]
        need(len(names) == len(set(names)) and len(names) <= MAX_BODIES + 1, 'duplicate or excessive archive members')
        need('catalogue.json' in names, 'bundle catalogue absent')
        for info in infos:
            need(not info.is_dir() and not info.flag_bits & 1, 'unsupported bundle member')
            mode = info.external_attr >> 16
            need(stat.S_IFMT(mode) in {0, stat.S_IFREG}, 'bundle member is not an ordinary file')
            bound = MAX_MANIFEST if info.filename == 'catalogue.json' else MAX_BODY
            need(info.file_size <= bound, 'bundle member exceeds byte bound')
        need(sum(x.file_size for x in infos) <= MAX_PACKAGE + MAX_MANIFEST, 'bundle total exceeds byte bound')
        def get(name, bound):
            with archive.open(name) as stream: data = stream.read(bound + 1)
            need(len(data) <= bound, 'bundle member grew beyond byte bound')
            return data
        raw = get('catalogue.json', MAX_MANIFEST); manifest = check_manifest(decoded(raw))
        expected = {'catalogue.json'} | {row['object'] for row in manifest['bodies']}
        need(set(names) == expected, 'bundle has missing or unselected members')
        objects = {}
        for row in manifest['bodies']:
            if row['object'] not in objects: objects[row['object']] = get(row['object'], MAX_BODY)
            data = objects[row['object']]
            need(len(data) == row['bytes'] and digest(data) == row['sha256'], 'bundle body/source mismatch: ' + row['id'])
            data.decode('utf-8')
    return archive_bytes, raw, manifest, objects


def inspect_bundle(path):
    archive, raw, manifest, objects = bundle_bytes(path)
    return {'format': 'knowledge-package-inspection/1', 'archive_sha256': digest(archive),
        'archive_bytes': len(archive), 'library_sha256': digest(raw), 'edition': manifest['edition'],
        'bodies': len(manifest['bodies']), 'modules': len(manifest['modules']),
        'body_bytes': sum(len(x) for x in objects.values()), 'complete_declared_closure': True,
        'provenance': manifest['provenance'], 'scope': 'Verified data package; no semantic acceptance or executable installation.'}


def import_bundle(path, store):
    archive, raw, manifest, objects = bundle_bytes(path)
    store = ordinary(store); need(store.is_dir(), 'existing caller-owned store directory required')
    ident = digest(archive); destination = store / ident
    # POSIX file locking serializes short store commits; it is not a semantic supervisor.
    need(os.name == 'posix', 'cooperative import commit requires POSIX')
    import fcntl
    lock = ordinary(store / 'import.lock')
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(lock, flags, 0o600)
    try:
        need(stat.S_ISREG(os.fstat(fd).st_mode), 'import lock must be an ordinary file')
        fcntl.flock(fd, fcntl.LOCK_EX)
        need(not destination.is_symlink(), 'import destination is a symlink')
        if destination.exists():
            existing = Library(destination); existing.verify()
            need(existing.raw == raw, 'content-addressed import destination differs')
            expected = {'catalogue.json'} | set(objects)
            actual = {p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file() or p.is_symlink()}
            need(actual == expected, 'existing import has unexpected members')
            status = 'already-present-verified'
        else:
            temporary = Path(tempfile.mkdtemp(prefix='.' + ident + '.', dir=store))
            try:
                (temporary / 'objects').mkdir()
                for name, body in {'catalogue.json': raw, **objects}.items():
                    with (temporary / name).open('xb') as stream:
                        stream.write(body); stream.flush(); os.fsync(stream.fileno())
                for directory in (temporary / 'objects', temporary):
                    directory_fd = os.open(directory, os.O_RDONLY)
                    try: os.fsync(directory_fd)
                    finally: os.close(directory_fd)
                os.rename(temporary, destination)
                directory_fd = os.open(store, os.O_RDONLY)
                try: os.fsync(directory_fd)
                finally: os.close(directory_fd)
                status = 'imported-data'
            except BaseException:
                if temporary.exists(): shutil.rmtree(temporary)
                raise
    finally:
        os.close(fd)
    return {'format': 'knowledge-import/1', 'status': status, 'archive_sha256': ident,
        'library_sha256': digest(raw), 'library': str(destination), 'edition': manifest['edition'],
        'bodies': len(manifest['bodies']), 'modules': len(manifest['modules']),
        'authority': 'Data only. No source owner, credentials, effect permission, current selection or executable capability is imported.'}


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
