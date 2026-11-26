"""Query interpretation and the shared FTS5 analyzer for lexical nomination."""

import hashlib
import json
import re
import unicodedata
from typing import Any, ClassVar

from .config import DEFAULT_LIBRARY_CONFIG, SearchConfig

try:
    import sqlite3
except ImportError:  # Default acquisition and BM25 do not require this optional build.
    sqlite3 = None  # type: ignore[assignment]

SQLITE_ERRORS: tuple[type[BaseException], ...] = (sqlite3.Error,) if sqlite3 is not None else ()

SEARCH_POLICY = DEFAULT_LIBRARY_CONFIG.search.policy
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "should",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)
TOKEN = re.compile(r"\w+")


def need(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _encoded(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def search_query(
    query: str,
    backend: str,
    *,
    config: SearchConfig = DEFAULT_LIBRARY_CONFIG.search,
) -> dict[str, Any]:
    """Interpret ordinary text as data; double-quoted spans are token phrases."""
    need(backend in {"bm25", "fts5", "literal"}, "unknown discovery backend")

    if backend == "literal":
        return {
            "terms": list(dict.fromkeys(query.casefold().split())),
            "phrases": [],
            "ignored_terms": [],
            "tokenization": "casefolded whitespace substrings",
            "language": "no stemming or stopword policy",
        }

    need(query.count('"') % 2 == 0, "unclosed double quote in query")
    terms: list[str] = []
    phrases: list[str] = []
    ignored: list[str] = []

    for match in re.finditer(r'"([^"]*)"|([^\s"]+)', query):
        if match.group(1) is not None:
            phrase = " ".join(TOKEN.findall(match.group(1).casefold()))
            if phrase:
                phrases.append(phrase)
        else:
            for term in TOKEN.findall(match.group(2).casefold()):
                (ignored if term in STOPWORDS else terms).append(term)

    terms = list(dict.fromkeys(terms))
    phrases = list(dict.fromkeys(phrases))
    ignored = list(dict.fromkeys(ignored))
    need(
        len(terms) + len(phrases) <= config.max_distinct_units,
        f"query exceeds {config.max_distinct_units} distinct terms and phrases",
    )

    return {
        "terms": terms,
        "phrases": phrases,
        "ignored_terms": ignored,
        "tokenization": "whole Unicode word tokens; quoted token phrases",
        "language": (
            "English stopwords; English porter stemming"
            if backend == "fts5"
            else "English stopwords; no stemming"
        ),
    }


class FtsAnalyzer:
    """Shared lexical index schema, query, and recorded identity for both storage modes."""

    weights = DEFAULT_LIBRARY_CONFIG.search.fts_weights
    tokenizer = DEFAULT_LIBRARY_CONFIG.search.fts_tokenizer
    keys: ClassVar[dict[str, str]] = {"docs": "id", "body_docs": "rowid", "module_docs": "rowid"}

    @classmethod
    def identity(cls, config: SearchConfig = DEFAULT_LIBRARY_CONFIG.search) -> dict[str, Any]:
        if sqlite3 is None:
            raise ValueError("SQLite support unavailable; use ordinary BM25 discovery")
        return {
            "tokenizer": config.fts_tokenizer,
            "weights": list(config.fts_weights),
            "detail": "full",
            "columnsize": 1,
            "unicode_version": unicodedata.unidata_version,
            "sqlite_version": sqlite3.sqlite_version,
            "word_pattern": TOKEN.pattern,
            "stopwords_sha256": _digest(_encoded(sorted(STOPWORDS))),
        }

    @classmethod
    def create(
        cls, db: Any, table: str, config: SearchConfig = DEFAULT_LIBRARY_CONFIG.search
    ) -> None:
        key = cls.keys[table]
        content = ",content=''" if key == "rowid" else ""
        db.execute(
            f"CREATE VIRTUAL TABLE {table} USING fts5("
            f"id,title,summary,body{content},tokenize='{config.fts_tokenizer}')"
        )

    @classmethod
    def search(
        cls,
        db: Any,
        table: str,
        units: list[str],
        config: SearchConfig = DEFAULT_LIBRARY_CONFIG.search,
    ) -> dict[Any, dict[str, Any]]:
        key = cls.keys[table]
        quoted = ['"' + term.replace('"', '""') + '"' for term in units]
        condition = f" FROM {table} WHERE {table} MATCH ?"
        weights = ",".join(str(w) for w in config.fts_weights)
        rows = db.execute(
            f"SELECT {key},bm25({table},{weights})" + condition, (" OR ".join(quoted),)
        )
        hits: dict[Any, dict[str, Any]] = {
            ident: {"score": -score, "matched_terms": []} for ident, score in rows
        }

        for term, expression in zip(units, quoted, strict=True):
            for (ident,) in db.execute(f"SELECT {key}" + condition, (expression,)):
                hits[ident]["matched_terms"].append(term)

        return hits
