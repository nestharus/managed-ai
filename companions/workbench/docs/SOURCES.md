# Complete research source access

`source_tools.py` supplies complete Markdown acquisition, lexical nomination and
durable source/use records for a capable caller. It uses the trusted reader in
`../../engine/runtime/source_library/knowledge.py`, reached directly or through
the retained prototype entrance. A legacy bundle can supply its standalone
`knowledge.py` instead. The workbench loads a bounded snapshot and includes the
actual reader and its local Python dependencies in the inventory selector.
Alternate trusted readers supplied through `search_module_path` can declare
local dependencies with explicit sibling imports.

The service uses Python's standard library and requires SQLite FTS5. It does not
silently switch search semantics when that facility is unavailable.

## Minimal use

```python
from source_tools import SourceService

with SourceService('/absolute/managed-ai/domains/research/sources',
                   '/absolute/work/source-state',
                   max_cache_bytes=65536) as sources:
    candidates = sources.discover('current source historical obligation', limit=5)
    result = sources.read(candidates['hits'][0]['path'])
    complete_text = result['text']
    exact_identity = result['source_id']
```

The host serializes access and keeps the source stable during an observation.
`state_dir` must be outside the corpus. One service owns one state directory at a
time. Source changes occur between operations; this is not a distributed atomic
snapshot or a hostile-filesystem security boundary. Escaping relative paths,
source symlinks and protected exposure paths are refused.

## Scope and observations

All ordinary UTF-8 Markdown bodies are default candidates except the original
external review and the two historical locator catalogues. The excluded paths
and reasons are explicit in each inventory. Those three excluded bodies can be
addressed explicitly with `read`; their content remains source data.

`evidence/fleet/D12/` is protected for this study. Its paths can be inventoried,
but their bodies are neither indexed nor acquired. The adapter does not open
held-out case or key semantics to make a discovery result look complete.

Construction binds the trusted code and opens the receipt ledger; it does not
scan the Markdown corpus or build an index. A known-path `read` freshly observes
that complete body without paying for global preparation. Its `inventory_id`
and `default_eligible` are null and its membership is `not_observed`. The separate
`path_eligible_by_policy` flag describes only the declared path rule, not observed
membership in a complete current inventory.

The first `discover` or `inventory` request, and each explicit `refresh()`, read
every eligible complete body, record its exact path/hash/length, and build a
contentless native FTS index. Queries after that preparation use that
**observed edition**. They do not reobserve the filesystem or establish its later
currentness. Call `refresh()` for the new receiving occasion; it replaces the
derived index only after complete preparation succeeds. The inventory records
eligibility, excluded paths and exact selector implementation/analyzer identity.

`discover(query, limit=8)` returns candidates and a `receipt_id`, `inventory_id`
and `selector_id`. It preserves the original query, parsed terms/phrases,
candidate cutoff, total matches and nonmatches. The durable selection record
also contains all matching paths, so returning a short list does not erase the
negative or cutoff basis. Candidate nomination does not establish relevance,
semantic sufficiency or recall for a meaning the lexical selector does not model.

`read(path, fresh=False, expected_sha256=None)` returns the complete UTF-8 `text`
and its path, SHA-256, byte count, source ID and acquisition receipt. No snippet
is substituted for a complete body. Before a full inventory is prepared, each
known-path read is fresh and makes no full-catalogue assertion. Once a view is
prepared, a cache hit reuses the exact earlier body and is labeled as such. An
uncached/fresh read that differs from the prepared
eligible body refuses and requires a new `refresh()`; it never silently pairs
a new body with an old selection. Explicitly addressed excluded bodies have no
default-search membership and are recorded as such.

## Cache and retained duties

`max_cache_bytes` bounds **complete acquired UTF-8 byte payloads in the in-process
LRU cache**. A body larger than the limit is returned completely without caching.
`set_cache_limit(n)` evicts replaceable payloads as necessary. It does not delete
source, selection, use or obligation records.

The contentless index, metadata, receipt file, interpreter and model history are
outside that payload bound. `stats()` reports preparation bytes, acquisition
bytes, cache hits/misses/evictions, cache payload, index page bytes and receipt
write bytes separately. Index page bytes are not total process RSS. These local
measurements do not represent provider tokens, inference calls or complete cost.

Cache and index are process-local and disappear when the service closes. A new
service prepares search only when discovery or an explicit inventory operation
requires it. Reusing `state_dir` retains prior receipts, not a warm body cache or
index. `stats()` and `receipts()` remain cheap inspection operations and do not
prepare a view. An unprepared service reports `search_prepared: false`, null
inventory/count fields and zero derived index pages; this is unobserved
membership, not an observation that the corpus is empty.

Payload retention still uses ordinary LRU. The preparation rule changed from
eager construction to demand-triggered preparation after the initial actual
workload exposed its cost and the next duty supplied exact source paths. This is
an explicit model-assisted production decision, not an autonomous learned policy.
Normal eviction or an imposed cache bound is a separate event and is not its
cause. When discovery does occur, its observed interval includes any preparation
it demands; preparation and discovery timers overlap and must not be summed.

## Use records and continuation

`record_use(use_id, task, requirements, source_receipt_ids,
selection_receipt_ids, artifacts, obligations)` records the caller's actual use.
The two receipt lists must reference available records. `artifacts` maps accepted
output paths to `{"sha256": ..., "bytes": ...}`. Task, requirements and obligations
are detached JSON values with their own identities. The record keeps the actual
accepted artifact identity rather than assuming identical prompts imply
identical model outputs.

The calling receiver owns acceptance. This adapter records its declaration and
does not infer semantic adequacy, authenticate an original event, close an owed
effect or replace a receiving judgment. A later changed task, requirement,
accepted output, selected inventory or tool contract can be inspected by the
continuation assessor. Independently owed work has its own retained record.

`receipts()` returns a detached dictionary with `inventories`, `selections`,
`sources` and `uses`, each keyed by a canonical-JSON SHA-256 receipt identity.
`state_dir/receipts.json` persists these records by atomic replacement. Prior
records remain when a use ID appears again with changed inputs. The host owns
eventual pruning of obsolete receipt history; body-cache eviction does not make
that decision. File persistence is scoped to this serialized local owner, not a
claim of arbitrary crash recovery or multi-writer transactions.
