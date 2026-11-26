"""Query interpretation and the shared FTS5 analyzer for lexical nomination."""
import re
import unicodedata

from ..reader.library_format import digest, encoded, need

try:
    import sqlite3
except ImportError:  # Default acquisition and BM25 do not require this optional build.
    sqlite3 = None
SQLITE_ERRORS = (sqlite3.Error,) if sqlite3 is not None else ()

SEARCH_POLICY = 'knowledge-search/1'
STOPWORDS = frozenset(('a an and are as at be been being but by can could did do does '
    'for from had has have how i if in into is it its may me my of on or our should '
    'so than that the their them then there these they this those to was we were '
    'what when where which who why will with would you your').split())
TOKEN = re.compile(r'\w+')


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
