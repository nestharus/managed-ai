"""Catalogue-pinned FTS5 discovery with an explicit in-process lifetime."""

from copy import deepcopy

import knowledge


class Producer:
    """Serve serial JSON-compatible requests using the issued native library."""

    def __init__(self, library_directory):
        library = knowledge.Library(library_directory)
        self.directory = library.directory
        self._libraries = {library.sha256: library}
        self._closed = False

    def _observe_current(self):
        observed = knowledge.Library(self.directory)
        retained = self._libraries.get(observed.sha256)
        if retained is not None:
            observed.close()
            return retained
        self._libraries[observed.sha256] = observed
        return observed

    def _select(self, request):
        pin = knowledge.sha(request["library_sha256"])
        use = request["use"]
        knowledge.need(isinstance(use, str) and use in {"current", "historical"},
                       "use must be current or historical")
        if use == "current":
            library = self._observe_current()
            knowledge.need(library.sha256 == pin,
                           "current catalogue differs from supplied pin; observed " + library.sha256)
            return library
        knowledge.need(pin in self._libraries, "unknown historical catalogue pin: " + pin)
        return self._libraries[pin]

    def _retention(self):
        catalogues = []
        for library in self._libraries.values():
            cache = library._search_cache
            catalogues.append({
                "library_sha256": library.sha256,
                "catalogue_bytes": len(library.raw),
                "search_prepared": cache is not None,
                "prepared_body_ids": list(cache[0][3]) if cache is not None else [],
                "prepared_module_count": len(library.modules) if cache is not None else 0,
            })
        return {
            "lifetime": "This Producer instance, ending at close().",
            "policy": "Retain every acquired exact catalogue and at most one native FTS5 scope per catalogue.",
            "catalogues": catalogues,
        }

    def _describe(self, library):
        return {
            "status": "ok",
            "operation": "describe",
            "library_sha256": library.sha256,
            "operations": {
                "describe": {"required": ["operation"]},
                "discover": {
                    "required": ["operation", "query", "library_sha256", "use"],
                    "defaults": {"limit": 20, "scan_limit": 512, "max_bytes": 8 * 1024 * 1024},
                    "bounds": {
                        "query_utf8_bytes": [1, 2048],
                        "distinct_terms_and_phrases": 128,
                        "limit": [1, 100],
                        "scan_limit": [1, knowledge.MAX_BODIES],
                        "max_bytes": [1, knowledge.MAX_PACKAGE],
                    },
                    "query_semantics": "Native knowledge FTS5 parser, including English stopwords, quoted token phrases and exact logical identities. Blank queries are refused.",
                },
                "show": {"required": ["operation", "id", "library_sha256", "use"]},
            },
            "use": {
                "current": "Read and validate catalogue.json on this request and require the supplied SHA-256 to agree.",
                "historical": "Select an exact catalogue acquired by this instance. Fresh acquisition uses canonical object paths within the supplied library directory.",
            },
            "source_observation": {
                "catalogue_observed_on_this_request": True,
                "catalogue_read_bytes": len(library.raw),
                "canonical_bodies_read": 0,
                "canonical_body_read_bytes": 0,
            },
            "preparation": self._retention(),
            "source_guarantees": {
                "discovery": "First use of a searchable scope reads and validates its canonical bodies through native Library.discover. Subsequent use of that exact retained corpus queries the same in-memory FTS5 indexes.",
                "scope": "A cache binds catalogue SHA-256, native search policy, FTS5 backend and ordered selected body identities. Actual request bounds and omissions accompany every discovery result.",
                "acquisition": "Every successful show freshly reads, length-checks, SHA-256-checks and UTF-8-decodes the selected canonical object.",
                "currentness": "Currentness is the catalogue observation within this request. Later changes require another observation.",
                "availability": "New discovery scopes and every show require their canonical objects. Previously returned nominations retain their original source basis.",
            },
            "request_fields": "Unknown fields are refused. Requests are served serially. Call close() to release all retained indexes and catalogues.",
        }

    def _discover(self, library, request):
        query = knowledge.text(request["query"], "query", 2048)
        limit = knowledge.integer(request.get("limit", 20), "result limit", 1, 100)
        scan_limit = knowledge.integer(request.get("scan_limit", 512), "scan limit", 1, knowledge.MAX_BODIES)
        max_bytes = knowledge.integer(request.get("max_bytes", 8 * 1024 * 1024),
                                      "search bytes", 1, knowledge.MAX_PACKAGE)
        parsed = knowledge.search_query(query, "fts5")
        searchable = bool(parsed["terms"] or parsed["phrases"]) or query in library.bodies or query in library.modules
        selected, omitted, body_bytes = library.search_scope(scan_limit, max_bytes)
        key = (library.sha256, knowledge.SEARCH_POLICY, "fts5", tuple(selected))
        cache = library._search_cache
        if not searchable or cache is None or cache[0] != key:
            return library.discover(query, limit=limit, scan_limit=scan_limit,
                                    max_bytes=max_bytes, backend="fts5")

        # The issued Library owns this already-verified corpus and both indexes.
        # Query it directly to retain its historical observation across calls.
        preparation = {
            "storage": "in-memory Library instance only",
            "reused": True,
            "index_sha256": None,
            "index_read_bytes": 0,
            "index_verified": False,
            "source_verification": "No canonical source bodies read on this call. The same in-memory FTS5 indexes retain bodies read and hash-verified when this catalogue and selected corpus were prepared. Use show for fresh exact acquisition.",
            "lifetime": "Retained until a different corpus replaces this catalogue's index, or close() ends the instance.",
        }
        return library._discovery_result(
            query, parsed, cache[1].search(parsed), cache[2].search(parsed),
            selected, omitted, body_bytes, limit=limit, scan_limit=scan_limit,
            max_bytes=max_bytes, backend="fts5", preparation=preparation, indexed=True,
        )

    def handle(self, request):
        try:
            knowledge.need(not self._closed, "producer is closed")
            knowledge.need(isinstance(request, dict), "request must be an object")
            operation = request.get("operation")
            knowledge.need(isinstance(operation, str) and operation in {"describe", "discover", "show"},
                           "unsupported operation")
            required = {"operation"}
            optional = set()
            if operation == "discover":
                required |= {"query", "library_sha256", "use"}
                optional = {"limit", "scan_limit", "max_bytes"}
            elif operation == "show":
                required |= {"id", "library_sha256", "use"}
            knowledge.need(required <= request.keys(), "missing required request fields")
            knowledge.need(request.keys() <= required | optional, "unknown request fields")
            if operation == "describe":
                return self._describe(self._observe_current())

            library = self._select(request)
            if operation == "show":
                ident = knowledge.relative(request["id"])
                knowledge.need(ident in library.bodies, "body unavailable in this library: " + ident)
                result = deepcopy(library.show(ident))
                body_count, body_bytes = 1, library.bodies[ident]["bytes"]
            else:
                result = self._discover(library, request)
                body_count = result["coverage"]["verified_bodies"]
                body_bytes = result["coverage"]["examined_body_bytes"]
            current = request["use"] == "current"
            return {
                "status": "ok",
                "operation": operation,
                "library_sha256": library.sha256,
                "use": request["use"],
                "result": result,
                "source_observation": {
                    "catalogue_observed_on_this_request": current,
                    "catalogue_read_bytes": len(library.raw) if current else 0,
                    "catalogue_basis": "fresh current observation" if current else "retained exact catalogue",
                    "canonical_bodies_read": body_count,
                    "canonical_body_read_bytes": body_bytes,
                },
                "lifetime": "This Producer instance retains acquired catalogues and one native index scope per catalogue until close().",
            }
        except (ValueError, OSError, *knowledge.SQLITE_ERRORS) as exc:
            return {"status": "refused", "error": str(exc)}

    def close(self):
        for library in self._libraries.values():
            library.close()
        self._libraries.clear()
        self._closed = True
