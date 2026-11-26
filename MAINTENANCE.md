# Maintain and Verify the Current Shared System

Edit the responsible engine, domain, guidance, or store owner. Reconcile overlapping material by meaning before merging or removing it; byte equality or title similarity alone does not establish semantic redundancy.

## Changing code or domain material

1. Identify the producing component and the records or downstream uses that depend on it.
2. Update the canonical owner and run the relevant verification suites. Retain minimal adverse witnesses where they constrain the repaired invariant.
3. Refresh affected source pins (`python -B engine/release/refresh_guidance.py` for core guidance; update `catalogue.json` and `domain.json` for domain sources). Never discard a pin guard to bypass a mismatch.
4. Qualify and activate the updated shared engine on affected deployments (`stage` and `activate` in `engine/deployment/select_deployment.py`).
5. Update affected stores via native `configure` with the expected `system.json` SHA-256, preserving or explicitly migrating typed records when schemas change. See [engine/INSTANCES.md](engine/INSTANCES.md).

## Maintaining stores and retiring working copies

Each store owns its records, exchange packets, and local work. Treat indexes and derived views as regenerable derivatives (`system reindex`). Keep cross-instance imports pinned to exact source provenance and an explicit local receiving account; do not mutate source-owned records from a receiving store.

Default record retention is `current-use`: revisions advance while ordinary replacements do not keep unbounded predecessor chains; explicit contexts, information wrappers, and feedback pins retain the exact occurrences they depend on. (`append-history` remains opt-in per store.)

Completed turns can leave working materializations under `work/.system/turns/`. Use `system retire-turns plan` / `apply` (or guarded explicit retirement) only after every declared target is returned and published, no live record or turn references the working path, and immutable exchange objects remain recoverable. Never retire unresolved turns or uncertain effects.

## Selected-source verification and packaging

After finalizing owners and running native `configure`, run the read-only selected-source gate before packaging:

```sh
python -B domains/ai-system-building/operations/verify_selected_sources.py \
  --bundle . --store stores/work
```

For an external workspace deployment, verify with `--deployment` and `--engine`:

```sh
python -B domains/ai-system-building/operations/verify_selected_sources.py \
  --deployment /path/to/workspace --engine engine
```

The gate uses the shared native domain/catalogue loader to verify manifest and schema bindings, entry references, and every available core/domain/store body while skipping `sealed` entries without opening them. Exit status `0` means all structural and digest checks passed; nonzero identifies the exact drift to reconcile.

After the gate passes on a clean exported staging directory, build the release archive outside the source tree:

```sh
python -B engine/release/package.py --root . --output /path/to/managed-ai.zip --replace
```

`package.py` verifies source and component identities, store schemas, and deployment bindings, stamps package-time manifests and `engine/SOURCES.md`, and checks ZIP CRCs and member hashes before atomic replacement.
