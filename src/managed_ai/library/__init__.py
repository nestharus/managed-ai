"""Published source library reader, lexical search engines, bundle interchange, and producer."""
from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "BatchConfig": (".config", "BatchConfig"),
    "DEFAULT_LIBRARY_CONFIG": (".config", "DEFAULT_LIBRARY_CONFIG"),
    "FtsAnalyzer": (".lexical", "FtsAnalyzer"),
    "Library": (".catalogue", "Library"),
    "LibraryConfig": (".config", "LibraryConfig"),
    "LibraryLimits": (".config", "LibraryLimits"),
    "LibraryPin": (".contract", "LibraryPin"),
    "LibraryReader": (".contract", "LibraryReader"),
    "PreparedIndex": (".index", "PreparedIndex"),
    "Producer": (".producer", "Producer"),
    "RetainedSearch": (".index", "RetainedSearch"),
    "SEARCH_POLICY": (".lexical", "SEARCH_POLICY"),
    "SQLITE_ERRORS": (".lexical", "SQLITE_ERRORS"),
    "STOPWORDS": (".lexical", "STOPWORDS"),
    "SearchBackend": (".contract", "SearchBackend"),
    "SearchConfig": (".config", "SearchConfig"),
    "SearchIndex": (".index", "SearchIndex"),
    "SearchScope": (".contract", "SearchScope"),
    "SourceProducerService": (".contract", "SourceProducerService"),
    "TOKEN": (".lexical", "TOKEN"),
    "bundle_bytes": (".bundle", "bundle_bytes"),
    "context": (".bundle", "context"),
    "export": (".bundle", "export"),
    "import_bundle": (".bundle", "import_bundle"),
    "inspect_bundle": (".bundle", "inspect_bundle"),
    "product": (".bundle", "product"),
    "resolve": (".bundle", "resolve"),
    "search_query": (".lexical", "search_query"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    mod_name, attr_name = target
    value = getattr(importlib.import_module(mod_name, __name__), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
