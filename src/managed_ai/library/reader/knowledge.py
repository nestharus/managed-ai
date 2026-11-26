#!/usr/bin/env python3
"""Compatibility re-export shim for managed_ai.library.{catalogue,index,bundle,format,cli}."""
from __future__ import annotations

from pathlib import Path
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    _src_root = str(Path(__file__).resolve().parents[3])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai.library.bundle import (
    bundle_bytes,
    context,
    export,
    import_bundle,
    inspect_bundle,
    product,
    resolve,
)
from managed_ai.library.catalogue import (
    INDEX_FORMAT,
    Library,
    MAX_BATCH_INPUT,
    MAX_BATCH_QUERIES,
    MAX_BATCH_RESULT,
    MAX_INDEX,
    batch_json_size,
)
from managed_ai.library.cli import run_library_cli as main
from managed_ai.library.contract import SearchScope
from managed_ai.library.format import (
    FORMAT,
    IDENT,
    MAX_ARCHIVE,
    MAX_BODIES,
    MAX_BODY,
    MAX_MANIFEST,
    MAX_MODULES,
    MAX_PACKAGE,
    SELECTION,
    SHA,
    check_manifest,
    decoded,
    digest,
    encoded,
    integer,
    need,
    ordinary,
    read_bytes,
    read_bytes_under,
    relative,
    sha,
    text,
    unique,
)
from managed_ai.library.index import PreparedIndex, RetainedSearch, SearchIndex
from managed_ai.library.lexical import (
    FtsAnalyzer,
    SEARCH_POLICY,
    SQLITE_ERRORS,
    STOPWORDS,
    TOKEN,
    search_query,
    sqlite3,
)

if __name__ == "__main__":
    main()
