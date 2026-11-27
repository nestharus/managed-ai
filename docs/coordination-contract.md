# Coordination, Ingestion & Backend Wire Contract

This document specifies the public pure-stdlib integration contract exposed by **`managed-ai-information`** for external coordinators, domain ingestion tools, and `membus-v2` storage backends.

## 1. Architectural Ownership Boundary

| Layer | Owns | Must Never Do |
| --- | --- | --- |
| **`managed-ai-information`** *(this library)* | Durable records, CAS `ObjectStore`, `HeadIndex v3`, pluggable `StorageBackend` (`EmbeddedFileBackend` / `MembusV2Backend`), `MultiRouteBackend` + `SchemaMigrationController`, `CoordinationFacade` (`managed-ai-coordination-contract/1`), `store.ingest` helpers, `graph` profiling/linting, and `impact` analysis. | Run agent loops, dispatch seats, mint model calls, or embed domain-specific rules in core `src/`. |
| **External Coordinators** *(consumers)* | Task scheduling, worker orchestration, and lease acquisition/renewal over `CoordinationFacade`. | Read or mutate `work/records/` or `exchange/objects/` directly, hydrate full chunked views into prompts, or bypass fenced leases. |
| **External Domain Packages** *(data + optional tools)* | Domain manifests (`domain.json`, `catalogue.json`, schemas, graph profile configs) and domain-specific ingestion tools built atop `managed_ai_information.store.ingest`. | Bypass store schema validation, open sealed payloads, or depend on private engine internals. |
| **`membus-v2`** *(optional storage daemon)* | Durable KV with optimistic CAS (`"Absent"` / `{"Version": v}`), SHA-256 CAS blobs, Tantivy visibility-scoped search, fenced leases (`409 + lease_held`), SSE signals, and collection schema migration catalog. | Leak `Sealed` payloads into search indexes or accept plaintext HTTP on non-loopback hosts. |

## 2. `managed-ai-coordination-contract/1` (`managed_ai_information.store.coordination_contract`)

Import `CoordinationFacade` or the module-level functions from `managed_ai_information.store.coordination_contract`. All payloads enforce strict JSON (`allow_nan=False`, duplicate-key rejection) and `MAX_BYTES = 2 MiB`.

1. **`resolve_context_bundle(system, turn: str) -> BoundedBundleRef`** (`managed-ai-bounded-bundle-ref/1`)
   - Returns an immutable handle (`request_sha256`, `context_sha256`, `exposure_sha256`, `full_view_object`, `selected_records`, `outside_view_record_count`, `outside_view_record_objects`, `bundle_bytes`).
   - Enforces exact byte accounting (`bundle_bytes == len(encode(bundle.to_dict())) <= MAX_BYTES`) via fixed-point convergence without hydrating outside-view chunk objects.
2. **`attach_artifact(objects, turn_id: str, payload: bytes, role: str, provenance: Mapping[str, Any]) -> ArtifactReceipt`** (`managed-ai-artifact-receipt/1`)
   - Validates `len(payload) <= MAX_BYTES`, non-empty `role`, and strict JSON `provenance`; writes immutable CAS bytes via `ObjectStore` and returns verified `sha256` and byte count.
3. **`commit_consolidation_delta(system, lease, expected_revisions, records, *, backend=None, acquire_owner_lock=True) -> CommitReceipt`** (`managed-ai-consolidation-commit-receipt/1`)
   - Verifies active lease expiry (`lease.expires_at_ms > now_ms`), optional live backend lease validity, and monotonic per-resource generation fencing persisted durably in `coordination-fences.json`.
   - Acquires `exclusive(system.ex.root / "owner.lock")` and delegates all mutations atomically to `system.put_batch(...)` with per-record optimistic-CAS `expected_revision` / `expected_sha256` pre-validation before mutating any record.
4. **`subscribe_signals(backend, namespace: str, *, prefix=None, since_seq=None, mode="each", timeout=None) -> Iterator[SignalEvent]`**
   - Cursor-resumable signal stream over `MembusV2Backend` SSE (bounded per-line reads) or `EmbeddedFileBackend` local ring buffer.

## 3. Domain-Neutral Store Ingestion API (`managed_ai_information.store.ingest`)

External domain repositories build deterministic, idempotent ingestors on the public `managed_ai_information.store.ingest` helpers without depending on private engine modules:

- **`DIGEST_REL_DIR`** (`"sources/repo-digests"`): relative workspace directory for source digest pins.
- **`slugify_id(prefix, raw)`**, **`canonical_bytes(payload)`**, **`dump_json(payload)`**: deterministic record IDs (`<= 120` chars) and strict JSON serialization (`allow_nan=False`).
- **`file_digest_entry(root, rel_path, ...)`**, **`git_head_sha(repo_root)`**, **`compute_digest_basis(record_id, source_repo=..., files=...)`**: symlink-safe streaming SHA-256 hashing and pure in-memory digest-pin computation (`O_NOFOLLOW` / `is_ordinary_file_stat` + containment checks).
- **`load_existing_records(system)`**, **`record_matches_desired(existing, desired, desired_basis_shas)`**, **`apply_desired_records(system, desired_records, basis_shas_by_id, *, batch_prefix, pending_digests=None, dry_run=False)`**: dry-run-truthful, idempotent delta application under `exclusive(owner.lock)` via `system.put_batch(...)` + `system.reindex()` + structural anomaly counting.

## 4. `MembusV2Backend`, `MultiRouteBackend` & Visibility Wire Contract

- **CAS Expectations:** `expected_version=0` serializes as `"Absent"`; `expected_version > 0` serializes as `{"Version": v}`; `None` serializes as `"Any"`. When dual-writing across `EmbeddedFileBackend` (per-key versions) and `MembusV2Backend` (global commit sequence), use `expected_version=0` or `None`.
- **Visibility Isolation (`SearchVisibility`):**
  - `Public`: indexes metadata and decoded text body.
  - `MetadataOnly`: indexes metadata only (`body = ""`).
  - `Sealed`: writes an opaque custody envelope (`managed-ai-sealed-custody/1`) and emits `search_op: {"Delete": {"namespace": ns, "doc_id": k}}` so `Public -> Sealed` transitions immediately tombstone prior search index entries (`0` hits in `Bm25` and `LiteralSubstring`). `delete_record()` likewise emits a `Delete` tombstone.
- **Fenced Leases & Error Mapping:** `423`/`429` and `409 + {"error": "lease_held"}` map to `BackendContendedError`; version mismatches map to `BackendConflictError`. Non-loopback plaintext `http://` endpoints fail closed with `ValueError`.
- **A/B Multi-Route & Governed Migration (`core/multi_route.py`):**
  - Modes: `single`, `shadow-async` (bounded worker pool; Route B faults never stall or fail Route A), `dual-sync`, and `canary-cutover`.
  - `SchemaMigrationController`: enforces `begin_expand` (lossy downgrades refused) -> `enable_dual_write` -> `record_verification` (`mismatch_count == 0`, `anomalies == 0`, monotonic `verified_through`) -> `commit_cutover` (`verified_through >= cutover_seq` and `dropped_shadow_ops == 0`) or `abort_rollback` (restoring `single` mode).
- **Selectable Store Backend (`store/records/backend_bridge.py`):**
  - Zero-daemon embedded files remain the default whenever `system.json` omits `"backend"` or sets `"type": "embedded"`.
  - Configure and manage optional `membus-v2` mirroring via `managed-ai-information --root <store> backend status|sync|cutover|rollback` and Tantivy BM25 discovery via `discover --search-mode lexical|bm25|auto`.

## 5. Safety & Durability Invariants

- **Strict Finite JSON & Type Guards:** `strict_json_loads(..., allow_nan=False)` rejects `NaN`/`Infinity` and duplicate keys across all store, backend, and CLI boundaries; JSON booleans are explicitly rejected in integer/version/limit validators.
- **Symlink & Containment Guards:** All store, config, digest-pin, target, and `EmbeddedFileBackend` paths enforce `check_ordinary_ancestors`, `is_ordinary_file_stat(lstat)`, `is_relative_to`, and bounded `O_NOFOLLOW` reads (`<= MAX_BYTES`).
- **Custody != Authority:** Records, CAS artifacts (`attach_artifact`), and `BoundedBundleRef` handles record authored evidence and custody only; no read-only query, dry-run plan, or `impact` report mutates store state or grants external execution authority.
