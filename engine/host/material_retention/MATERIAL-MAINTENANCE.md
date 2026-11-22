# Maintain material through identities, occurrences and current selections

`managed_material.py` is also the complete recovery program stored inside each export.
`retirement_plan` checks recoverability and the physical inventory. `retire` rechecks the
received plan before removal, records partial completion on failure, and installs the
successor selection and lifecycle receipt. Both operations use the same recipe and
manifest readers as exported verification and restore.


Use `host/material_retention/managed_material.py` when a body of work has repeated captured sources, failed and revised candidates, exchanged objects, releases or exported evidence. The maintained store owns exact content identities and their relationships selected for current use. Working directories and transport archives are materializations of that retained work. Their continued existence is a separate applicability decision.

This layer uses the same raw-content SHA-256 identities as the host and native exchange. It does not replace their operational formats, migrate a live runtime, infer acceptance from a hash, or make all retained history current. The store should live outside any source directory that it scans. Serialize store operations; there is no cross-process store lock or transaction with unrelated filesystem editors.

## Native record retention

New schema-governed stores also default ordinary record replacement to current-use retention. Revisions advance without an unconditional predecessor chain. Exact context, Information, feedback and interpretation pins remain retained for their actual receiving use. `history` reports any omitted prefix explicitly; explicit append-history remains available. This changes the producing policy and does not automatically delete old objects. See the [instance contract](../../INSTANCES.md).

## Select material deliberately

Before constructing another release or evidence product, identify the useful current knowledge, executable tools, live duties and selected supporting evidence. Keep an older body only when it still supports an actual use. Age, uniqueness or the ability to reconstruct every prior transport does not supply that use. Consolidate repeated accounts into their responsible current body and keep exact occurrence evidence only where its identity matters. Preserve the original input, producing means, first complete candidate, observed failure, receiving qualifications and changed result where they explain a consequential repair. Equal bytes can occur in different attempts or roles; retain those occurrences separately while sharing their content.

Do not recursively copy a previous project tree or wrap old evidence ZIPs inside a new evidence archive as the maintenance method. Capture selected paths into the managed store, preserve the original relationships as manifests, and export the required reference closure. An older transport can remain an exactly recoverable historical identity without remaining an opaque archive payload.

A missing original input stays unavailable. Later descriptions, quotations or reconstructed guesses do not become the missing source. Custody, semantic acceptance, present applicability and operational readiness are different findings.

## What is retained

The store distinguishes these representations:

| Representation | Meaning |
| --- | --- |
| `objects/…/*.deflate` | One verified raw-content identity, physically encoded with DEFLATE. Raw SHA-256 and byte count remain authoritative. |
| Encoded views in the index | A previous managed export's compressed object entry reuses the exact persisted encoding of its canonical raw body; it is not stored as another compressed payload. |
| Slice views in the index | Exact historical header/trailer identities address verified intervals in a packed framing body. They need no independent physical file per small header. |
| Recipe and record views in the index | Exact logical recipe bytes and immutable hash-identified manifest/selection/lifecycle records are reused as bodies. Mutable indexes and `CURRENT.json` are never represented as views of their present contents. |
| `recipes/<original-zip-sha>.json.deflate` | Exact historical ZIP composition, physically compressed: original framing, ordered member occurrences, shared body references and checked compression recipes or exact fallback fragments. Earlier uncompressed `.json` recipes remain readable. |
| `manifests/<sha>.json` | An immutable acquisition occurrence: logical and original source paths, bytes, modes, times, roles, materializations, producer identity, current choices and declared history. |
| `occurrences/<id>.json` | A stable named reference to one immutable manifest. An occurrence ID cannot be overwritten. |
| `selections/<sha>.json` and `CURRENT.json` | Exact present selection, distinct from retained history. An explicit historical capture or retirement operation preserves selected-state ancestry; the default current-use capture does not. |
| `lifecycle/<sha>.json` and `events.jsonl` | Explicit retirement receipts and operation outcomes. A failed or partially completed operation is not silently treated as success. |

Host objects, native exchange objects, working files and archive members with identical bytes share the same content identity. Their original paths and container/member occurrences remain separate. Existing exchange, return, repair and publication records are preserved as exact bodies; a repeated hash does not infer authorship, derivation or a receiving verdict.

## Capture an occurrence

Write a `managed-material-selection/1` JSON specification. `roots` is a deliberate source selection, not a command to copy directories. Directory traversal records occurrences and imports each distinct body once. A root can also be one file. Labels are unique single path components.

```json
{
  "format": "managed-material-selection/1",
  "id": "maintenance-occasion-01",
  "retention_policy": "current-use",
  "roots": [
    {
      "label": "project",
      "path": "/absolute/project",
      "role": "observed-project-material",
      "materialization": "working-and-historical-copies"
    },
    {
      "label": "deliverables",
      "path": "/absolute/deliverables",
      "role": "retained-publication-occurrences"
    }
  ],
  "current": [
    {
      "role": "selected-engine",
      "path": "project/active-system/runtime/manage_store.py",
      "expected_sha256": "REPLACE_WITH_ACTUAL_VERIFIED_HASH"
    }
  ],
  "materialization_groups": [
    {"prefix": "project/runtime", "state": "active"},
    {"prefix": "project/active-system", "state": "active"},
    {"prefix": "project/obsolete-extraction", "state": "retirement-candidate"}
  ],
  "lineage": [
    {
      "relation": "declared-successor",
      "evidence": "Exact retained receiving record or other attributed basis",
      "account": "Explain the bounded relation; do not infer it from a filename."
    }
  ],
  "account": "Purpose, cutoff and unresolved work of this selection."
}
```

Paths in `current` are manifest paths under the root labels. A supplied `expected_sha256` must match. A current role is a deliberate choice for a purpose, not an automatic semantic pass. Do not list every historical file as current. Declare active operational prefixes broadly enough to protect the host, native exchange, selected engine, current working material and any still prepared consumer inputs.

Optional `exclude` patterns match full logical paths and are recorded. Omitting caches is a selection decision, not an assertion that their old bytes were retained. A retirement plan refuses a directory whose current inventory contains excluded or uncaptured paths. Empty directories and symbolic link targets are retained; special files are refused. Hardlink topology, ownership, ACLs, xattrs and birth/access times are not filesystem-image guarantees.

A successor defaults to `retention_policy: "current-use"`: its export contains the declared roots and their actual object dependencies, without automatically adding the previous manifest, selected-state ancestry, retirement receipts or old transport recipes. The manifest records this retention choice and its account. Declare active materialization groups again for the actual current selection; an omitted old group has no current disposition in this new boundary. No old stored object is deleted by capture or export.

For a real requirement to carry the entire previous selected history forward, set `retention_policy: "append-history"`. An explicitly supplied nonempty `history` list without a policy continues to select those exact prior manifests; this preserves earlier explicit-selection specifications. When that selection omits the previous current manifest, supply `history_account`. Combining explicit `current-use` with nonempty `history` is refused. Prefer selecting the few needed evidence files as roots over retaining a large old manifest. The `retain` operation is likewise an explicit choice to add exact evidence, not a routine publication step.

Under explicit historical retention, materialization state is derived from the retained selected-state ancestry and the new captured group selections. An absent retired copy remains retired across ordinary later captures. A group explicitly captured again as active can become active for the new occurrence while its actual earlier retirement receipt remains historical evidence. A file's mere reappearance is not a new applicability judgment. State identities retain the logical prefix and original physical source path so different materializations do not silently become one history.

```bash
python -B host/material_retention/managed_material.py snapshot \
  --store /absolute/managed-material-store \
  --spec /absolute/selection.json

python -B host/material_retention/managed_material.py verify \
  --store /absolute/managed-material-store
```

Capture checks each file before and after reading and rechecks all selected file identities before publishing the selection. It retains the loaded producer bytes. The claim is per-file stable custody and final recheck, not an atomic transaction across a changing project. Finish the selected producing and receiving work before choosing a publication cutoff; account for later operations as later occurrences.

## Export from the managed closure

```bash
python -B host/material_retention/managed_material.py export \
  --store /absolute/managed-material-store \
  --output /absolute/deliverables/consolidated-evidence.zip
```

Export verifies and follows the selected-state ancestry, every referenced occurrence manifest, declared body history, shared objects, ZIP recipes and lifecycle receipts. A receipt's source manifest and body closure are included even when the current selected state concerns a later occurrence. Verification checks these references and reports retained selection/receipt counts and the derived current materialization state; a missing or changed receipt refuses verification and export. Export never walks the project tree. It includes the standalone `managed_material.py` recovery bootstrap, exact manifests, object index, `RECOVERY.json` and an export-member hash manifest. Existing output files are refused rather than overwritten.

The resulting export has no historical ZIP payload files. Historical ZIP identities are recipe targets. Original local headers, data descriptors, member ordinals, duplicate names, central-directory bytes, comments and surrounding bytes are preserved. Supported member streams reuse the canonical body's exact persisted encoding where possible. Other matching DEFLATE recipes record parameters and the actual verified zlib runtime; unsupported or unmatched streams retain exact compressed fragments. Invalid original ZIPs remain explicit raw failed material.

Structural data needs maintenance too. A large archive's headers and trailer are packed into one retained body with verified slice references; existing header identities remain individually addressable. ZIP recipe JSON is physically compressed. If a transport contains already retained immutable recipe or occurrence/selection/lifecycle records, verified views reuse those bytes instead of creating a second body encoding. Each view checks the byte identity and dependency closure; cycles refuse. Encodings of virtual body views are reconstructed only when they match the pinned encoded identity under the stated codec boundary. A compressed recipe entry from a retained managed export is recognized against the registered persisted recipe encodings, both after loading a store and immediately after creating a recipe. Reuse checks the actual persisted bytes, decoded logical recipe identity and exact body encoding before creating its recipe and encoded views. A filename alone establishes no equivalence.

Exact old publication/index metadata can still require new retained bytes. This legitimate cost is measured separately from duplicated physical representation and from genuinely new source work. Do not treat a successful small reimport case as evidence that structural growth remains adequate at tens of thousands of entries.

If a previously delivered managed export itself has a consequential historical transport identity, it can be selected for retention. Its encoded object entries become verified encoded views of the existing raw bodies, including when importing into a fresh store. Its particular manifests, framing and publication state remain distinct history. Do not retain an export merely because another export is being produced; select the history that supplies an actual recovery or provenance duty.

For a specifically required first result or failed transport, `retain --store STORE --path FILE --label ONE-NAME --account ACCOUNT` adds an exact historical occurrence and its reference closure while leaving the main current roles and occurrence unchanged. The successor selection names this additional manifest and retains the actual previous selected state. This is explicit evidence retention, not a reason to retain every export.

**Exact encoder boundary:** a `deflate-recipe` must reproduce the pinned compressed-member SHA-256 and byte count. A different encoder that produces different bytes is refused. The exercised environment used Python 3.12.14 and zlib 1.3.2. This is exact recovery under the verified compatible encoder, not a promise that every future zlib version emits identical transport bytes. Ordinary retained bodies and persisted compressed streams remain independently decodable. No recipe falls back to an externally retained original archive.

ZIP processing has explicit limits: depth 12, 200,000 members per archive, 1 GiB per member and 4 GiB cumulative decoded member bytes in one ingestion. A bound failure does not publish a completed selection. Larger or special corpora need an explicit changed bound and receiving exercise.

## Recover without the source directories

Extract the consolidated export into an otherwise empty location. From that directory:

```bash
python -B managed_material.py verify --store .
python -B managed_material.py restore --store . --output ../restored-material
```

`restore` requires a nonexistent destination, reconstructs files from retained identities, preserves promised modes and modification times, and checks each recovered file. `--occurrence ID` chooses a retained historical occurrence; `--prefix logical/path` restores one selected subtree. `get --object SHA --output PATH` materializes one exact body, including an original historical ZIP.

Restore retains the original content of records containing absolute paths. Operational relocation requires separate new bindings or a relocation account; rewriting historical records to make them look native to a new location would destroy their identity. A successful byte restoration alone does not demonstrate that a native runtime has resumed.

## Regenerate an existing physical store

An improved representation does not change a retained body's raw identity. Ordinary ingestion may correctly reuse an existing identity, including an older inefficient recipe. To apply changed producing means to an existing store, use the explicit rebuild route rather than editing its index or records:

```bash
python -B host/material_retention/managed_material.py rebuild \
  --source-store /absolute/old-managed-store \
  --output /absolute/new-managed-store \
  --expected-index ACTUAL_RECEIVED_SOURCE_INDEX_SHA256 \
  --expected-selection ACTUAL_RECEIVED_CURRENT_SELECTION_SHA256 \
  --account "Purpose and qualified basis of this physical representation transition"
```

The target must not exist and must be separate from the source. Rebuild reads exact original transports and content, regenerates their physical representations through the changed producer, preserves every source object identity, copies only immutable metadata records, and reads back every original identity in the target. It rechecks that the original index, selection and operation history did not change during the operation. A failed attempt remains in its new target directory and never becomes permission to remove the source.

A successful result retains a hash-identified representation-transition record, the actual producer and source event history, and a successor selected state linked to the old selected state. Every immutable metadata file declared by that transition is a recovery dependency: verification checks its safe path, exact hash and byte count, and export includes its exact bytes. Declared earlier transition records carry their own record and evidence dependencies onward. These retained historical records do not change current-role selection or make an unselected state current. The main current roles, lifecycle receipts, retired states and original occurrence bodies stay intact. The source index is pinned as a historical input; it is not falsely presented as an immutable view of a mutable current index. If exact recovery of that former physical index is a consequential duty, explicitly retain its historical export or the index bytes and measure that added input separately.

Receive the actual same-input regeneration and exact original transport/body recovery before adopting the new store. Retaining a newly observed overgrown failed transport is a further input and has its own measured cost. Keep those comparisons separate. Rebuild does not delete or rename the source store, switch an active native runtime, or claim that a previous failure was never produced.

The old physical store remains a pending transition copy until the owner verifies the replacement, confirms that no active operation still uses its path, and explicitly receives its retirement. Do not silently leave successive stores accumulating or claim that canonical storage has been consolidated while several active copies remain. A supported ordinary materialization retirement can be used only where its captured recovery and protection contract actually applies; otherwise record the old store's physical retirement as pending and perform a separately received owner operation.

## Retire an obsolete materialization

First establish that the copy is no longer an active consumer dependency and nominate it as `retirement-candidate` in a captured occurrence. This command preserves the store objects required by its declared recovery contract. Retiring a materialization removes a replaceable filesystem copy; it does not garbage-collect the store. When old exact recovery no longer serves a duty, select and verify a current-use export and explicitly retire the superseded store under the owner’s current retention decision. Do not make historical byte recovery a universal prerequisite for forgetting unneeded material.

Write a `managed-material-retirement-proposal/1` JSON specification:

```json
{
  "format": "managed-material-retirement-proposal/1",
  "prefixes": ["project/obsolete-extraction"],
  "protected_prefixes": ["project/runtime", "project/active-system"],
  "account": "Why this declared copy is no longer used, and which retained selection recovers it."
}
```

```bash
python -B host/material_retention/managed_material.py retirement-plan \
  --store /absolute/managed-material-store \
  --spec /absolute/retirement-proposal.json > /absolute/received-retirement-plan.json
```

This operation deletes nothing. It verifies the complete recovery closure, requires a declared candidate group, rejects current selected bodies and protected active prefixes, checks that the entire live inventory still matches the captured inventory, and verifies each file hash and mode. Its output is ready for the owner's receiving decision, not automatic permission to remove paths.

After the owner receives the actual plan and confirms applicability, either execute an explicit equivalent owner operation or use the separate guarded command:

```bash
python -B host/material_retention/managed_material.py retire \
  --store /absolute/managed-material-store \
  --plan /absolute/received-retirement-plan.json \
  --expected-plan ACTUAL_SHA256_OF_RECEIVED_PLAN_FILE \
  --expected-selection ACTUAL_CURRENT_SELECTION_SHA256
```

The command requires those exact identities, performs the full live/recovery recheck again, and removes only the declared materialization roots. It records durable operation events, retains a retirement receipt and publishes a successor current selection with the retired state. It removes no canonical objects. A stale plan, changed source, changed selection or protected overlap refuses. A filesystem failure can leave a partial retirement; events name completed removals, retained bodies remain recoverable, and the operator must receive that actual result before continuing. External editors are not locked, so serialize this operation with active work on those copies.

Export after retirement to carry the lifecycle receipt and current state. Later captures carry that selected-state ancestry only when explicitly retaining its manifest history. A later occurrence can explicitly select a newly needed materialization without making all old copies current or erasing the earlier retirement. Do not claim that accumulation is resolved merely because an archive became smaller: receive the actual canonical store, useful current selection, recoverability and physical retirement results separately.

## Receiving questions

Use these questions at the applicable scope rather than creating a universal review phase:

- Does this selection preserve the exact consequential failure and changed result while identifying which material is current for this purpose?
- Do identical bodies share an identity without collapsing distinct occurrences, attributed judgments or lineage?
- Does the exported closure recover the original byte identities from its own contents, including historical transports under the declared encoder boundary?
- Can an obsolete working copy be removed without breaking an active consumer, and has its current inventory been rechecked against the received recovery plan?
- Does the result distinguish physical custody, semantic usefulness, current applicability, runtime operation and completion of the larger commission?

These questions apply to research, software, prose, datasets, evidence and other composed materials. They do not require dimensions invented for one story or project.
