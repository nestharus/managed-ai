"""Compatibility re-export shim for managed_ai.library.lexical."""
from __future__ import annotations

from ..lexical import FtsAnalyzer, SEARCH_POLICY, SQLITE_ERRORS, STOPWORDS, TOKEN, search_query, sqlite3

__all__ = ["FtsAnalyzer", "SEARCH_POLICY", "SQLITE_ERRORS", "STOPWORDS", "TOKEN", "search_query", "sqlite3"]
