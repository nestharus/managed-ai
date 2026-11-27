# Platform Architecture: Storage Backends, A/B Routing, and Coordination Contract

This document specifies the pure-stdlib platform seams in `managed-ai-information` that decouple durable knowledge storage from external daemons, govern A/B backend and schema transitions, and define the versioned information-layer boundary consumed by `managed-ai-coordination`.

## 1. Pluggable `StorageBackend` (`core/storage_backend.py`)

`StorageBackend` is a pure-stdlib `Protocol` covering versioned KV records, content-addressed blobs, visibility-scoped search, fenced leases, live signal streams, and health reporting:

- **`EmbeddedFileBackend` (default, zero-daemon):** Wraps `ObjectStore` and local workspace files for offline portability with zero external runtime dependencies.
- **`MembusV2Backend` (pure-stdlib HTTP/JSON/SSE client):** Targets `membus` v2 `/v2/*` routes (`/v2/kv`, `/v2/blobs`, `/v2/search`, `/v2/leases`, `/v2/signals/{ns}/stream`, `/v2/catalog/collections/{ns}/migrations/{begin,advance,commit,abort}`, `/v2/health`).

### Security & Integrity Invariants

1. **Visibility Isolation (`SearchVisibility`):**
   - `Public`: Indexes metadata and payload text.
   - `MetadataOnly`: Indexes metadata only (`body` is stripped to `""`).
   - `Sealed`: **Never** indexes payload text or metadata into search, and sealed entries are never emitted in search hits.
2. **Non-Loopback TLS Fail-Closed:** Plaintext `http://` endpoints are permitted **only** on loopback hosts (`localhost`, `127.0.0.0/8`, `::1`). Any non-loopback `http://` URL, embedded credentials, or non-HTTP(S) scheme raises `ValueError`.
3. **CAS & Size Integrity:** Blob puts and gets enforce `<= MAX_BYTES` (`2 MiB`) and verify exact SHA-256 content hashes before returning data.
4. **Typed Error Mapping:** Upstream HTTP/transport errors map deterministically to `BackendConflictError` (`409` / version conflict), `BackendContendedError` (`423`/`429` / lease contention), and `BackendUnavailableError` (`502`/`503`/`504` / transport failure).

## 2. A/B `MultiRouteBackend` & Schema Migration Governance (`core/multi_route.py`)

`MultiRouteBackend` routes operations across an authoritative `route_a` and an optional candidate `route_b` in four declarative modes:

| Mode | Authoritative Route | Behavior |
| --- | --- | --- |
| `single` | `route_a` | Direct synchronous execution on `route_a`; zero `route_b` overhead. |
| `shadow-async` | `route_a` | Synchronous `route_a` return; mirrors to `route_b` via a bounded non-blocking queue with per-op `route_b_timeout_ms`. Route B errors, timeouts, or queue saturation **never** fail or stall `route_a`. |
| `dual-sync` | `route_a` | Executes `route_a` and `route_b` synchronously and verifies canonical SHA-256 parity. |
| `canary-cutover` | `route_b` | Routes authoritatively to `route_b` while retaining `route_a` for immediate rollback. |

Every shadowed or dual-written operation emits a structured `ParityEvent` (`op`, `key`, `a_sha`, `b_sha`, `a_ms`, `b_ms`, `b_error`, `match`, `recorded_at`). Aggregate diagnostics are exposed via `route_status()` and `parity_summary()`, matching the loopback-only `/control/route-status` and `/control/route-parity` routes in `deploy/local/gateway.conf`.

### Governed Schema Lifecycle (`SchemaMigrationController`)

`SchemaMigrationController` enforces **Expand → Dual-Write → Verify → Contract / Abort**:

1. **`begin_expand`**: Registers `from_version -> target_version` (`target_version > from_version`; lossy downgrades are refused) and notifies the catalog backend (`POST .../migrations/begin`).
2. **`enable_dual_write`**: Transitions `expanding -> dual-write` and switches `MultiRouteBackend` to `shadow-async` or `dual-sync`.
3. **`record_verification`**: Advances `verified_through` monotonically (`>=` prior watermark) **only** when `mismatch_count == 0` and `anomalies == 0` (`POST .../migrations/advance`).
4. **`commit_cutover`**: Allowed **only** from `verified` when `verified_through >= cutover_seq`; calls `POST .../migrations/commit` and flips the router to `canary-cutover`.
5. **`abort_rollback`**: Cleanly aborts from `expanding`, `dual-write`, or `verified`, calls `POST .../migrations/abort`, and restores `MultiRouteBackend` to `single` (`route_a`).

## 3. `managed-ai-coordination` Information-Layer Contract (`store/coordination_contract.py`)

`managed-ai-information` owns durable records, CAS blobs, bounded context bundles, and lease/schema invariants; `managed-ai-coordination` owns task routing, scheduling, and agent execution without reading `work/records` directly.

`CoordinationFacade` (`managed-ai-coordination-contract/1`) exposes four narrow operations:

- **`resolve_context_bundle(turn) -> BoundedBundleRef`:** Returns a strictly bounded (`<= MAX_BYTES`) immutable handle (`request_sha256`, `context_sha256`, `exposure_sha256`, `full_view_object`, `selected_records`, `outside_view_record_count`, `outside_view_record_objects`) without hydrating chunked outside-view inventories.
- **`attach_artifact(turn_id, payload, role, provenance) -> ArtifactReceipt`:** Stores an immutable CAS blob (`<= MAX_BYTES`) with verified SHA-256 and strict JSON provenance.
- **`commit_consolidation_delta(lease, expected_revisions, records) -> CommitReceipt`:** Validates active lease expiry, optional backend lease validation, monotonic per-resource generation fencing, and per-record optimistic-CAS `expected_revisions` before writing any record.
- **`subscribe_signals(namespace, prefix, since_seq, mode) -> Iterator[SignalEvent]`:** Exposes cursor-resumable SSE/local signal subscriptions for coordination wakeups.
