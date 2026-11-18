# Maintain one current system

Edit the responsible engine, domain, guidance or store owner. Reconcile the meaning of overlapping material before merging or removing it. Equal bytes can have different authority roles, and related bodies can be complementary; neither byte equality nor a title match decides semantic redundancy.

## Change code or domain material

1. Identify the actual producing component and the records or later uses that depend on it.
2. Change the canonical owner and validate the consequential behavior. Keep a small adverse witness when it constrains the repaired claim.
3. Refresh the affected source catalogue or domain selection from the current bodies. A stale source pin is a refusal to be resolved, not a reason to discard the guard.
4. Qualify and select the current shared-engine identity for affected deployments. The deployment describes its engine and store; it does not copy either tree.
5. Receive affected work under its actual applicable inputs. Update the responsible current account and reopening condition.

The deployment's `stage` and `activate` commands select a qualified engine reference. The native `configure` route selects domain/catalogue manifests with the expected SHA-256 of the current `system.json` descriptor. Use their current `--help` and the [operating contract](engine/INSTANCES.md) for required fields. A payload-schema change must preserve or explicitly migrate existing typed records.

The optional `--root` for `engine/release/refresh_guidance.py` names the engine directory (`/absolute/managed-ai/engine`); omitting it selects the script's own engine. The deployment `stage --engine` argument resolves relative paths from the caller's current directory, so use that same absolute engine path when staging from another directory.

## Maintain current storage

Each store owns its records and current work. Treat generated views and indexes as derivatives. Keep cross-instance observations with exact provenance and an explicit receiving account. Do not silently revise a source-owned record from the receiving store.

Select current useful material when maintaining or delivering a store. Preserve exact prior evidence where a live basis, unresolved return or obligation actually requires it. Do not inherit every previous projection, lifecycle receipt, source tree or archive merely to make another continuation.

The three stores select `record_retention: current-use`. Record revision numbers still advance, but ordinary replacements do not automatically retain a predecessor chain. Explicit context, information and feedback pins retain the exact evidence they require. A history read reports the retained boundary honestly. The engine supports `append-history` when a particular store has an explicit reason for that policy; it is not the policy of these deployments.

The writing source projector maintains consequential attribution and coverage. Repeating an unchanged projection must not extend a chronological chain. Availability, another participant's account, direct acquisition, adapted use and effective construction remain distinct.

## Retire completed preparation copies

A completed turn can leave working materializations under `work/.system/turns/` as well as its exact native packet and immutable objects. Current-use record retention does not remove these duplicate copies. Retire only an explicitly nominated completed working directory after every declared target is returned and fully published, no current owner or cross-turn input references the working path, and exact captured bodies remain recoverable. Preserve active captures, specifically referenced methods, packet/context/return/publication evidence and immutable objects. Verify native status, view and recovery before and after removal. If any dependency or outcome is uncertain, retain that named copy until its owner resolves the dependency.

The actual [current retirement](stores/novel/work/receiving-evidence/completed-working-copy-retirement.json) removed 23 superseded completed working directories under these guards. It introduces no general garbage collector or chronological retention quota. Ordinary previous claims remain only where current state, captured receiving or an explicit duty requires them.

## Verify selected sources and publish the current bundle

Finalize the responsible current bodies before refreshing their source catalogue rows. Register accepted new owners once, preserve existing identities, and refresh only the selected changed pins and descriptions. Then refresh the affected domain/catalogue manifest selections through native `configure` with the exact current descriptor guard. Do not leave an earlier pending-adoption description beside an actually adopted current body.

Run the read-only selected-source gate after those final edits and configurations, before packaging:

```sh
python3 -B domains/ai-system-building/operations/verify_selected_sources.py \
  --bundle . --store stores/novel --store stores/research --store stores/discovery
```

This operation uses the shared native domain/catalogue loader. It checks the selected manifest and schema bindings, entry identities, required tool/guidance references and every available registered core/domain/store body. It reports sealed entries from metadata without opening their bodies. A failing return names the source or selection that needs reconciliation; fix the responsible accepted owner or its stale selection, then run the same gate again. Do not discard the pin to make a changed source pass.

The gate writes no canonical files or native state. JSON on standard output is its actual result; exit status zero means its declared checks passed and nonzero means they did not. Mechanical body verification grants no semantic acquisition, receiving or completion credit and is not an atomic snapshot against independent concurrent writers. An available source can be byte-current while its meaning or applicability still needs receiving.

The need for this gate was exposed by a received archive whose novel retention inventory had a current body but an older catalogue pin. Existing native body acquisition correctly refused that source. Package generation had passed because its domain/store checks verified selected manifest metadata and record schemas, without opening every registered body. An isolated package refresh and native configuration did not change the inventory. Finalizing an owner, refreshing its catalogue selection and checking all available bodies addresses that observed workflow gap. The retained original failing receipt is historical evidence; each later delivery requires its own actual passing gate return.

After the selected-source gate passes, build the current archive outside the source directory:

```sh
python -B engine/release/package.py --root . --output /absolute/output/managed-ai.zip --replace
```

The package producer verifies its declared source/component identities, loaded configuration, exact current member set, ZIP CRCs and member hashes, and rejects nested transport archives. It reads the current owner tree, not a previous ZIP. The separate selected-source gate is a required maintenance step; package generation does not run it implicitly. All three deployments must still resolve to the same engine and independent stores after extraction into another directory.

Replace the existing current archive and small continuation entrance. Retire superseded standalone exports after their useful material is available through its maintained owner. Keep receipts sufficient to state what actually succeeded; a readable handoff must never embed an archive or become another long historical ledger.

Source release, initialized storage and continuous model execution are distinct. The archive supplies the common code and loaded stores. Actual capable participation uses the selected native/host or workbench entrance and the caller's configured model/tool transport.
