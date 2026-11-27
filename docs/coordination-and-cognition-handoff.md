# Coordination & Cognition Integration Handoff (`codex3`)

This document specifies the exact pure-stdlib integration contract between **`managed-ai-information`** (durable knowledge store, CAS custody, pluggable `StorageBackend` + A/B `MultiRouteBackend`, and `managed-ai-coordination-contract/1`), **`membus`** v2 (verified live against `membus @ 2fa30e4b`), and **`cognition-workflows`** (`@ 42f5585d` reward-immutability, goal-blind views, model-write boundary, and evidence-custody invariants).

## 1. Architectural Ownership Boundary

| Layer / Repository | Owns | Must Never Do |
| --- | --- | --- |
| **`managed-ai-information`** | Durable records, CAS `ObjectStore`, `HeadIndex v3`, `StorageBackend` (`EmbeddedFileBackend` / `MembusV2Backend`), `MultiRouteBackend` + `SchemaMigrationController`, `CoordinationFacade` (`managed-ai-coordination-contract/1`), graph profiling & mechanical zero-loss repair. | Run agent loops, dispatch seats, mint model calls, or embed domain-specific rules in core `src/`. |
| **`managed-ai-coordination`** *(consumer)* | Task scheduling, swarm orchestration, worker lifecycle, and lease acquisition/renewal over `CoordinationFacade`. | Read/mutate `work/records/` or `exchange/objects/` directly, hydrate full chunked views into prompts, or bypass fenced leases. |
| **`cognition-workflows`** *(runtime @ `42f5585d`)* | Native Rust loop runtime (`cognition-account`, `cognition-runtime`, `owned-completion`), L1/L2 Orient–Decide–Act phases, goal-blind non-Decide projections, evidence handoffs, and process/Job settlement. | Let non-Decide views read Decide-private goals/paths, allow models to mutate user rewards, or reuse completed steps across changed commissions. |
| **`membus`** *(v2 daemon @ `2fa30e4b`)* | Durable KV with CAS (`"Absent"` / `{"Version": v}`), SHA-256 CAS blobs, Tantivy visibility-scoped search, fenced leases (`409 + lease_held`), SSE signals, and collection schema migration catalog. | Leak `Sealed` payloads into search indexes or accept plaintext HTTP on non-loopback hosts. |

## 2. `managed-ai-coordination-contract/1` (`store/coordination_contract.py`)

Import `CoordinationFacade` (or module-level helpers) from `managed_ai_information.store.coordination_contract`. All payloads enforce strict JSON (`allow_nan=False`, duplicate-key rejection) and `MAX_BYTES = 2 MiB`.

### 2.1 Four Narrow Operations

1. **`resolve_context_bundle(turn: str) -> BoundedBundleRef`** (`managed-ai-bounded-bundle-ref/1`)
   - Returns an immutable handle (`request_sha256`, `context_sha256`, `exposure_sha256`, `full_view_object`, `selected_records`, `outside_view_record_count`, `outside_view_record_objects`, `bundle_bytes`).
   - Enforces exact byte accounting (`bundle_bytes == len(encode(bundle.to_dict())) <= MAX_BYTES`) via fixed-point convergence without hydrating chunked outside-view objects.
2. **`attach_artifact(turn_id: str, payload: bytes, role: str, provenance: Mapping[str, Any]) -> ArtifactReceipt`** (`managed-ai-artifact-receipt/1`)
   - Validates `len(payload) <= MAX_BYTES`, non-empty `role`, and strict JSON `provenance`; writes immutable CAS bytes via `ObjectStore` and returns verified `sha256`, `bytes`, and `object_path`.
3. **`commit_consolidation_delta(lease: LeaseGrant, expected_revisions: Mapping[str, int], records: Sequence[Mapping[str, Any]], *, acquire_owner_lock: bool = True) -> CommitReceipt`** (`managed-ai-commit-receipt/1`)
   - Verifies active lease expiry (`lease.expires_at_ms > now_ms()`), optional live backend lease validity, and monotonic per-resource generation fencing persisted durably in `coordination-fences.json` (surviving restarts even when `backend=None`).
   - Acquires `exclusive(system.ex.root / "owner.lock")` and delegates all mutations atomically to `system.put_batch(...)` with per-record optimistic-CAS `expected_revision` / `expected_sha256` pre-validation BEFORE mutating any record.
4. **`subscribe_signals(namespace: str, *, prefix: str | None = None, since_seq: int | None = None, mode: str = "each", timeout: float | None = None) -> Iterator[SignalEvent]`**
   - Cursor-resumable signal stream over `MembusV2Backend` SSE (`readline(MAX_BYTES + 1)` bounded) or `EmbeddedFileBackend` local ring buffer.

## 3. Live `MembusV2Backend` & `MultiRouteBackend` Wire Contract (`membus @ 2fa30e4b`)

Verified end-to-end against live `membus serve` via `scripts/ci/verify_membus_live.py` and `tests/core/test_membus_live_integration.py`:

- **CAS Expectations:** `expected_version=0` serializes as `"Absent"`; `expected_version > 0` serializes as `{"Version": v}`; `None` serializes as `"Any"`. Note that real `membus` assigns `RecordMeta.version = global commit seq`; use `expected_version=0` (`"Absent"`) or `None` (`"Any"`) when dual-writing across `EmbeddedFileBackend` (per-key versions) and `MembusV2Backend` (global seq versions).
- **Visibility Isolation (`SearchVisibility`):**
  - `Public`: indexes metadata + decoded text body.
  - `MetadataOnly`: indexes metadata only (`body = ""`).
  - `Sealed`: emits `search_op: {"Delete": {"namespace": ns, "doc_id": k}}` so `Public -> Sealed` downgrades immediately tombstone any prior Tantivy document (`0` hits!). `delete_record()` likewise emits a `Delete` tombstone.
- **Fenced Leases & Error Mapping:** `map_http_error()` checks `423`/`429`/`"lease"`/`"contend"` **before** generic `409`, mapping live `membus` `409 + {"error": "lease_held"}` to `BackendContendedError` while version mismatches map to `BackendConflictError`. Non-loopback plaintext `http://` endpoints fail closed with `ValueError`.
- **A/B Multi-Route & Governed Cutover (`core/multi_route.py`):**
  - Modes: `single`, `shadow-async` (lazy-spawned worker + `BoundedSemaphore`-capped `ThreadPoolExecutor`; Route B errors/timeouts/saturation never stall or fail Route A), `dual-sync` (explicitly reports `route_a already committed` on Route B divergence), and `canary-cutover`.
  - `SchemaMigrationController`: enforces `begin_expand` (`target_sha256` as `[u8; 32]` int list, `target_schema_bytes` as `Vec<u8>` int list; lossy downgrades refused) -> `enable_dual_write` -> `record_verification` (`mismatch_count == 0`, `anomalies == 0`, monotonic `verified_through`) -> `commit_cutover` (requires `verified_through >= cutover_seq` **and** `dropped_shadow_ops == 0` on attached router!) or `abort_rollback` (`{"target_version": v}`, restoring `single` mode).

## 4. Mapping `cognition-workflows` (`@ 42f5585d`) Invariants onto `managed-ai-information`

| `cognition-workflows` Invariant & Source | Enforcement in `managed-ai-information` |
| --- | --- |
| **Goal-blind non-Decide scope & withholding counts** (`docs/GOAL_BLIND_SCOPE.md`: non-Decide steps never see goal rows, Decide-addressed rows, or Decide output paths; `withheld.json` records counts/categories without bodies; retained phases freeze original views) | Store Decide-private / user-reward material with `SearchVisibility="Sealed"` (zero Tantivy indexing; `Public -> Sealed` tombstoned; `HeadIndex v3` / `graph profile` / `repair_graph_health.py` perform **0** sealed object reads). Deliver non-Decide views via immutable `resolve_context_bundle(turn)` handles (`outside_view_record_count` without hydrating bodies). |
| **User-reward immutability & goal-field refusal in handoffs** (`docs/EVIDENCE_HANDOFF.md`: goal/question fields refused at any depth of new marked `evidence_handoff` rows; invalid batch stores none of its rows; `README.md`: `founder` cannot be runtime keeper) | `commit_consolidation_delta()` validates every record against domain JSON schemas (`additionalProperties: false` on strict kinds) via atomic `put_batch()` pre-validation before writing any record, and refuses stale/unowned writes via persisted monotonic generation fences (`coordination-fences.json`). |
| **Model-write boundary & receiving != authority** (`docs/LOOP_ARCHITECTURE.md`, `contracts/observer.md`: receiving records and `evidence_handoff` suites are authored evidence/requests, never execution authority; `README.md`: private snapshot callbacks & kill-on-close Jobs) | `managed-ai-information` exposes data/evidence custody only (`attach_artifact` CAS blobs + `BoundedBundleRef`). No record write, receipt, or `repair_graph_health.py` dry-run invokes models, mutates external workspaces, or grants effect authority; mutations require explicit `--apply` + `exclusive(owner.lock)`. |
| **Immutable evidence custody & attribution** (`docs/EVIDENCE_HANDOFF.md`: exact commit/SHA versions, `exercise`/`unchecked` lists, `execution_receipts`; `PROVENANCE.md`: adapted sources keep separate identities) | Every CAS blob (`attach_artifact`, `ObjectStore`) verifies exact SHA-256 on write and readback; `put_batch(pin_predecessors=True)` retains prior revisions in `exchange/objects` (verified by 9-layer transitive `audit_objects`); `repair_graph_health.py` records full pre/post relations and supports fail-closed `--rollback`. |
| **Strict finite measurement & raw-envelope preservation** (`docs/JUDGE_REFUSAL_RESETS.md`: nonfinite numbers, booleans, and out-of-range epochs refused/left unknown; raw provider envelopes retained) | `strict_json_loads(..., allow_nan=False)` rejects `NaN`/`Infinity` and duplicate keys across all store, backend, and CLI boundaries; `bool` is explicitly rejected in integer/version/seq validators (`isinstance(x, bool)` guards). |
| **Symlink / reparse / path-containment safety** (`README.md`: view privacy resolves links/junctions against configured private roots) | All store, config, baseline, link-spec, receipt, and `EmbeddedFileBackend` paths enforce `check_ordinary_ancestors`, `is_ordinary_file_stat(lstat)`, and `O_NOFOLLOW` bounded reads (`read_bytes(..., limit=MAX_BYTES)`). |

## 5. WSL Integration Checklist for `codex3`

1. **Install / import pure-stdlib wheel** (`managed_ai_information`, zero third-party runtime dependencies, Python `>= 3.14` / `3.15.2`):
   ```bash
   python3 -B -c "from managed_ai_information.store.coordination_contract import CoordinationFacade; print('OK')"
   ```
2. **Verify live `membus` v2 E2E harness** (spawns isolated ephemeral `membus serve` on `127.0.0.1:0` with a temp `--data-dir`; zero impact on production state):
   ```bash
   python3 -B scripts/ci/verify_membus_live.py --membus-bin /path/to/membus
   ```
3. **Verify coordination & multi-route unit suites**:
   ```bash
   python3 -B -W default::DeprecationWarning -m unittest \
     tests.store.test_coordination_contract \
     tests.core.test_storage_backend_and_multi_route \
     tests.core.test_membus_live_integration -v
   ```
4. **Graph-health gate for research / software / CRW stores** (strictly read-only unless `--apply` is explicitly supplied):
   ```bash
   python3 -B -m managed_ai_information.store.manage_store --root /path/to/store \
     graph profile --profile-config scripts/consolidation/profiles/research_graph_profile.json
   python3 -B scripts/consolidation/repair_graph_health.py --store /path/to/store
   ```
