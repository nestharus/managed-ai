# One engine, separate stores and loadable domains

The same native `System` runs every instance. A deployment selects that code and one store; it contains no copied controller, engine or project tree. Domains supply versioned data, payload schemas, source references and declared tool contracts. The engine contains no branch for a particular domain.

## Store and deployment ownership

A native store has `system.json`, `exchange/` and `work/`. Its descriptor declares `store_schema: managed-ai-store/1`, `engine_api: managed-ai-runtime/1`, a stable `instance_id`, selected domain/catalogue hashes and typed-schema bindings. Its workspace binding is relative. Keep the package together when relocating it; captured old contexts remain exact evidence of their original occurrence, while new contexts use the current location. Relocation does not authorize replay of a pending external effect.

Direct exchange readers accept the integer workspace editions issued by the initializer. Version 1 names an absolute workspace. Version 2 binds `../work` to the native instance. Deployment requires version 2 and explicit matching store and engine editions. Direct native access also opens older store descriptors. If they omit both the store edition and retention policy, writes retain predecessor history.

The deployment format is `agent-system-deployment/2`. The descriptor names a relative engine path with the exact Python code identity and a relative store path with its stable instance identity. Invocation checks both. New code requires explicit compatibility assessment and a new selected code identity. Ordinary staging writes only `candidate.json` and carries forward the selected store. Activation compares the current descriptor's raw digest and the canonical encoding digest of the staged descriptor. It verifies the declared APIs and Python identity and checks the binding of the store named in staging. Identical bytes can recur across stages. If you receive staging from another producer, verify its resolved paths and intended store.

Code identity includes sorted Python paths and their content digests. Qualification checks the engine reference before resolving it. Use ordinary directories and files throughout the selected engine and its reference path. Links, reparse points, nonordinary entries and failed enumeration cause refusal. Keep importable engine modules in Python source. Ordinary interpreter cache files below `__pycache__` are omitted from the pin, and standalone bytecode or extension modules cause refusal. Both API declarations must be present in the hashed source. Descriptors must be JSON objects. Invalid declaration syntax is reported as an input error.

Deployment dispatch checks the selected source again before loading its native entry. The pin identifies the selected tree's Python source. The invoked controller loads its local continuation support, which Python can also reuse when dispatching into a different selected tree. Run with a trusted interpreter, import environment and interpreter-managed caches. Keep the engine source stable during qualification and use. Store and domain selection remain separate.

The invoked wrapper's local deployment controller issues the activation receipt. Selecting a newer engine through an earlier controller retains that controller's reporting behavior. A completed `activate` prints verified deployment status on stdout. If candidate removal or status readback raises a handled error after the descriptor write completes, `activate` exits 2 and emits JSON on stderr with `status` set to `committed_incomplete`, `operation` set to `activate`, the `error`, and the SHA-256 of the exact descriptor written (including the activation reason) in `descriptor_sha256`. A `failed_step` of `candidate_cleanup` means removal of the staged file reported an error before status readback. A `failed_step` of `status_readback` means removal succeeded and reading or verifying the resulting selection failed.

Run `deploy.py --deployment DEPLOYMENT status` and compare its `descriptor_sha256` with the reported digest. If status refuses, inspect the descriptor and its selected code and store binding for that deployment. A leftover `candidate.json` is staging data. Candidate removal has no separate directory sync, so recheck staging after crash recovery. For another selection, including a return to qualified earlier code, stage it afresh and activate with the observed current descriptor digest, the fresh candidate digest and a reason. A writer error can occur on either side of replacement. Guard release or output can also fail after the selection has been written. Reconcile the current selection and staging before retrying after an `unresolved` error or a missing receipt.

Deployment status verifies the engine pin and the store binding. Native `system status` also loads the store's selected domains and catalogues. Qualify changes to those store pins separately through `configure --spec FILE` with the current store descriptor digest and a receiving account.

From the package root:

```sh
python3 -B engine/runtime/select_deployment.py --deployment deployments/research system status
python3 -B engine/runtime/select_deployment.py --deployment deployments/novel system status
```

Create an additional instance only in a new location:

```sh
python3 -B engine/runtime/manage_store.py --root stores/example init \
  --instance-id example-instance --goal-text "Actual receiving contribution" \
  --domain /absolute/domain/domain.json --catalogue /absolute/instance-sources.json
python3 -B engine/runtime/select_deployment.py --deployment deployments/example init \
  --engine engine --store stores/example
```

`--prepared` allows already placed source/artifact data in an uninitialized store; it refuses an existing native descriptor, exchange or goal. It does not resume or overwrite a prior store.

## Domain and source data

A domain manifest declares `format: managed-ai-domain/1`, `id`, `version`, `engine_api`, optional `requires_domains`, `schemas`, `entry_sources`, `tools`, and a catalogue `{path, sha256}`. `source_root` resolves relative to the manifest. Tool rows can name `id`, `source_id`, `entry_source_id` and `contract_source_id`. Their source and contract identities must be selected; loading them executes no code. Use their declared CLI or the existing native trusted-command/return boundary when an actual contribution calls for execution.

A reference catalogue has `format: managed-ai-source-catalogue/1` and `sources`. Each row declares `id`, `path`, `sha256`, `bytes`, optional title/kind/summary, and `exposure` (`available` or `sealed`). Individual paths cannot escape the explicitly declared source root. Instance catalogues may declare their own `source_root` relative to the catalogue file. Complete acquisition checks current bytes against the selected identity. Sealed sources remain metadata only: ordinary source reading and context capture refuse their bodies.

```sh
python3 -B engine/knowledge.py --store stores/research show SOURCE_ID --text
python3 -B engine/runtime/select_deployment.py --deployment deployments/novel system catalogue SOURCE_ID --text
```

The installed catalogue uses `agent-system-catalogue/1` and also reads its untagged layout. Primary IDs across the core and selected references are unique. A primary ID takes precedence over a compatibility alias. Each alias names one primary target and becomes available when that target is selected. Listing, body lookup and context capture use this same resolution.

The source catalogue records availability and custody. Registration, hashing, importing metadata and building a source index do not establish a participant's acquisition or application. Existing acquisition counts remain owned by their source-account records.

## Structural payload schemas

`schemas` maps record kinds to a bounded structural-schema dialect. It supports `type`, `properties`, `required`, `additionalProperties`, `items`, `enum`, `const`, string/array length limits, numeric limits, `pattern`, `allOf`, `anyOf`, `oneOf`, local `$ref` with `$defs`/`definitions`, and descriptive `$id`, `$schema`, `title`, `description`. It is **not a complete JSON Schema implementation**. Unsupported keywords are refused before initialization or selection. There is no network reference resolution or schema code execution.

Every declared type must be supported, including in unused kinds. Domains that declare the same kind must agree on its exact schema binding. Research and Writing each declare `obligation`. Select compatible editions together when changing that schema. Both accept the optional `basis_change` object. The native record owner checks its account and prior revision when an owed duty replaces or drops existing evidence.

The native record owner enforces declared kinds on ordinary writes and imports. A typed record cannot evade its schema through ordinary reclassification. A generic Information envelope retains its own exact interpretation contract; the domain schema checks its substantive payload. Other open native record kinds remain extensible. Structural validity does not establish semantic adequacy or a universal ontology.

To requalify changed domain/catalogue files, run `configure --spec FILE` through the selected native entrance. The specification supplies `expected_sha256` of the current `system.json`, a nonempty `account`, and replacement `domains` and/or `catalogues` path lists. The operation checks existing records against the proposed schema selection and refuses removal of a schema still owning records. It then atomically replaces the store descriptor. Source changes do not silently alter earlier captures.

For delayed receipt of an exact reviewed plan, check that the selected native `configure --help` advertises `managed-ai-reviewed-configure/1`. Request that format for the store in the [source-maintenance planner](../domains/ai-system-building/operations/SOURCE-MAINTENANCE.md). Pass the emitted file to the same `configure --spec FILE` command. The store owner compares the receiving instance, prior descriptor digest and ordered domain and catalogue paths and digests before replacing the descriptor. Inspect the returned `reviewed_plan` and descriptor digest for each store. A later store can refuse even after an earlier one has accepted. Use the path-list specification when the task calls for the current compatible edition.

## Current state, imports and retained evidence

New stores default to `record_retention: current-use`. Ordinary replacement advances the revision without automatically saving and linking every prior record. `pin ID`, context capture, Information conversion, feedback references and whole-goal interpretation retain the exact selected evidence they need. No background deletion is performed.

`history ID` reports the retained chain and a truthful `retention_boundary` when earlier ordinary revisions were not retained. It does not claim complete history at that boundary. Use `init --record-retention append-history` or an explicit `configure` selection when a task needs a continuing predecessor chain. Legacy feedback duties lacking an explicit carried disposition must recover it from their verified old chain before a new current-use write; missing recovery evidence causes refusal. Historical audit tools refuse a complete-history conclusion across a retention boundary.

`import --spec FILE` takes `{format: managed-ai-import/1, id, records}`. It validates the complete proposed batch before writing. Its receipt supports resumption and identical retries; a changed import under the same identity is refused. It is a current selection, not a multi-record transaction or automatic predecessor archive import.

A completed import keeps its as-issued revision and digest for every row. Retrying requires the proposed inputs to still match and every acknowledged head to remain at that exact edition. Restoring its content at a later revision therefore refuses. Reading or enumerating completed acknowledgements still returns the original identities.

A new valid import first records its instance, ID and exact specification digest with an empty progress list in the existing incomplete receipt. After interruption, repeat the original specification through the same store. The owner checks the binding and current records before continuing, including when the first head exists and the progress list is still empty. A different valid specification under that ID refuses before another head write. A pre-existing head with no import receipt supplies no evidence of which import created it.

## Cross-instance receiving

Export a selected native record with `transfer export --record ID --output FILE`. Receive that exact bundle with `transfer receive --bundle FILE --id LOCAL_OBSERVATION --expected REVISION --account ACCOUNT`. `transfer inspect LOCAL_OBSERVATION` returns the source record behind the observation.

A source pin names `instance_id`, `record_id`, `revision` and `sha256`. The receiver retains the exact supplied bytes, source identity and receiving account. Identical intake replays without another revision. The source store keeps ownership of its record and duty; the receiver owns its observation and any resulting issue. Supplied provenance is not a remote authentication service or a claim that the source head has not subsequently changed.

Use ordinary native feedback to construct the receiving comparison and record its actual result. Arrival does not accept a claim, fulfill a duty or prove that the finding transfers to another domain. Cross-store atomic acceptance and automatic semantic reconciliation are not supplied.
