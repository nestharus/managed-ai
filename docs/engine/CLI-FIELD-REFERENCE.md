# Selected existing CLI and field reference

Runtime state, source derivatives, indexes and outputs belong to selected stores outside the engine directory. In the commands below, `G60_BASE` names the selected engine root; other uppercase tokens name caller-selected paths or retained IDs. In this package, select `src/managed_ai/deployment/select_deployment.py` or `src/managed_ai/store/manage_store.py`. The underlying field contracts remain useful; [instance contracts](INSTANCES.md) add the current deployment, domain, typed-record, import and retention interfaces.

## Read the configured store

From the package root, use `python3 -B src/managed_ai/library/cli.py --store STORE discover --query TEXT` for selected metadata and `python3 -B src/managed_ai/library/cli.py --store STORE show SOURCE_ID --text` for exact acquisition.

## Publish and discover bodies in an explicit knowledge library

The following source-map and indexing examples require the separate library workflow and its authoring inputs. A configured store can serve its registered bodies directly.

```json
{
  "format": "knowledge-source-map/1",
  "families": ["views"],
  "additional": [],
  "overrides": {},
  "modules": []
}
```

The source root contains complete `views/*.md` capsules. A first heading and first prose paragraph supply navigation title/summary. The body retains the original owner/span, original passage/view identities, conditions and any transparent errata. A capsule's own digest is not interchangeable with those identities.

```bash
python3 -B "$G60_BASE/knowledge-system/publish.py" --root SOURCE_TREE --source-map SOURCE_MAP publish --edition EDITION --output LIBRARY
python3 -B "$G60_BASE/knowledge-system/knowledge.py" --library LIBRARY prepare-index --output INDEX
python3 -B "$G60_BASE/knowledge-system/knowledge.py" --library LIBRARY discover --backend fts5 --index INDEX --index-sha256 INDEX_SHA256 --query QUESTION --limit 3
python3 -B "$G60_BASE/knowledge-system/knowledge.py" --library LIBRARY show BODY_ID --text
```

Keep the exact preparation receipt. Both index preparation and discovery accept `--scan-limit` and `--max-bytes`; their scope must agree for prepared reuse. Default scan limit 512 covers a 110-body library, but bounds and reported omissions still need inspection.

## Native runtime, Attention and receiving

```bash
python3 -B "$G60_BASE/agent-system/system.py" --root RUNTIME init --goal-text CONTRIBUTION
python3 -B "$G60_BASE/agent-system/system.py" --root RUNTIME put --record RECORD_JSON --expected 0
python3 -B "$G60_BASE/agent-system/system.py" --root RUNTIME track --spec ATTENTION_SPEC
python3 -B "$G60_BASE/agent-system/system.py" --root RUNTIME context TURN --request CONTEXT_REQUEST
python3 -B "$G60_BASE/agent-system/system.py" --root RUNTIME recover TURN --text
python3 -B "$G60_BASE/agent-system/system.py" --root RUNTIME view TURN
python3 -B "$G60_BASE/agent-system/system.py" --root RUNTIME receive TURN --reply ACTUAL_REPLY --producer ATTRIBUTION --proposal outputs/result.md=ACTUAL_PROPOSAL --assessment ASSESSMENT
python3 -B "$G60_BASE/agent-system/system.py" --root RUNTIME assess TURN --receipt ASSESSMENT --producer ATTRIBUTION
python3 -B "$G60_BASE/agent-system/system.py" --root RUNTIME adopt TURN --target outputs/result.md --reason CURRENT_RECEIVING_REASON
```

`receive --assessment` and a later `assess` are alternatives for recording that account, not two independent observations. Source and target paths inside requests are relative to `RUNTIME/work`; CLI JSON/reply paths are host-selected. The supplied record uses existing `id`, `kind`, `content`, optional `basis` and other ordinary record fields. Updating a record requires its actual current revision in `--expected`.

A sealed context that produces an actual outcome without an artifact can receive that account through `receive --reply ...` without a `--proposal`. `abandon-context` is narrower: it requires an incomplete local turn without an Exchange packet. It does not cancel an already sealed context. This distinction was encountered during actual form receiving when a later candidate arrived before a reply; the earlier sealed context received its factual intermediate outcome and the current context received the artifact.

An Attention specification uses exactly:

```json
{
  "field": "sources/field.md",
  "items": [
    {
      "id": "source-view-a",
      "question": "What could this qualified source change in the proposed contribution?",
      "use": "Current construction and continuing source discovery",
      "basis": ["sources/views/a.md"],
      "groups": ["one-discovered-relationship", "another-discovered-relationship"],
      "active": true
    }
  ]
}
```

`field` is optional. Subsequent `track` calls update only supplied IDs. `active: false` removes an item from current navigation, not from historical custody or any actual duty. Question, use and observed source snapshots determine the treatment key; group changes alone do not erase treatment.

An actual context can select:

```json
{
  "task": "The live contribution and current receiving decision",
  "consumer": "Attribution for the actual participant",
  "sources": ["sources/selected-encounter.json"],
  "attention_items": ["source-view-a", "current-joint-use"],
  "attention_groups": ["one-discovered-relationship"],
  "attention_limit": 6,
  "attention_max_bytes": 12000,
  "targets": ["outputs/result.md"]
}
```

Item and group selectors form a union. Only known active IDs/groups are accepted. Empty selectors mean all active items. Source bodies are not expanded merely because an item names them as Attention basis; explicit `sources`, selected record basis, and governing records can expose them separately. `records`, `artifacts`, `returns`, `compositions` and `source_pins` are available when the current use actually needs them. Keep a whole bank out of always-delivered governing bodies and open-obligation basis.

A receipt names each delivered item actually being credited:

```json
{
  "id": "receiving-account-a",
  "assessments": [
    {
      "item": "source-view-a",
      "disposition": "considered",
      "account": "Actual public conclusion, evidence, method or changed work for this use.",
      "remaining": ["A concrete unresolved condition, if any."],
      "products": ["outputs/result.md"]
    }
  ]
}
```

`disposition` is `considered` or `deferred`; a deferral needs an actual account and is not fulfillment. `products` is optional and captures existing bytes. During `receive --assessment`, do not name an unadopted target as though it already existed; use an existing proposal/evidence file in the workspace or assess after actual adoption when appropriate.

## Composition and factorized overlap

When reviewing a child edit, a `recompose` request can include `"consumer_scope": ["named-lot", "count"]`. Supply the local record IDs authorized for this decision. The review reads those heads under the target's owner guard and reports their identities against the request's remaining scan and byte budgets. A missing record or exhausted budget requires a fresh selection. An exact source digest in a records-only body search can help locate candidates when the names are unknown.

In `consumer_scope.candidates`, each binding locates a consumer row and its captured source pin. The projection compares the value at that coordinate with the proposed child content. For a composition collection it follows the authored row ID across a reorder. A missing value, a different captured edition or a pointer to the future record envelope needs interpretation. Array indices inside arbitrary row data retain their positional meaning. The `affected` list follows the consumer's declared row dependencies from changed, relocated and unresolved coordinates. An unchanged value still carries its original edition.

A delayed consumer can resolve an earlier edition from its own native `basis` capture of the source record path. To use another local holder's capture, select that holder's exact native head and `/basis/N` pointer in the ordinary `sources` list. The review verifies the captured object's length and digest and the enclosed record's ID, revision and digest before following the typed row. The binding reports the holder and object used. Each distinct consulted object costs one scan and its declared bytes, including a failed verification. An unresolved binding reports the reason. The review uses existing captures and leaves record retention with the source owner.

An exact native evidence pointer can also select a complete `managed-ai-transfer/1` export already incorporated by a local holder. Its declared source instance and record ID must match the reviewed owner. The transfer reader verifies its encoded bytes, object digest and enclosed record identity. The holder head is charged to the source budget. A matched binding identifies this coordinate as `transfer_source`. Research file pins continue to use their domain reader.

The accepted child and each parent have separate decisions. To revise a parent, use its returned `target`, the newly installed child head and the verified coordinate in an ordinary parent proposal. A maximum count can survive a change that invalidates a named release. A matching binding locates the decision and the source needed to check it. A parent whose use was never recorded still needs direct investigation.

Put an ordinary record with `content.format: "g60-composition/1"` and a free-text `purpose`. Supported collections are `elements`, `relations`, `premises`, `opportunities`; `unknowns`, `coverage` and arbitrary `content` are optional. All row IDs are participant-authored.

- Element: `id`, required `account`, optional `content`.
- Relation: `id`, required `prediction`, optional `from`, `to`, `basis`, `observation`, `account`, `content`. **`from` and `to` are lists of bare element-ID strings**, even for one endpoint: `"from": ["source"], "to": ["result"]`. Do not use scalar strings or `element:` prefixes here. `basis` is a list of source paths bound by the ordinary record. Observation is `unassessed`, `supported` or `contradicted`; an asserted observation needs its account.
- Premise: `id`, required `account`, optional `disposition`, `basis`, `content`. Disposition is `available`, `unavailable` or `unassessed`; `basis` is a list of paths bound by the ordinary record.
- Opportunity: `id`, required `contribution`, optional `at`, `requires`, `disposition`, `reads`, `writes`, `footprint`, `conflicts`, `phase`, `account`, `content`. **`at` is a list of explicit element/relation references**, such as `["element:result", "relation:source-to-result"]`. It is not a scalar or a bare ID. `requires` is a list of alternative lists of bare premise IDs: any one alternative can supply the declared prerequisites; each alternative requires all its members. `conflicts` is a list of bare opportunity IDs. `reads` and `writes` are lists of exact authored resource strings. `footprint` is `declared` or `unknown`; opportunity disposition is `open`, `held` or `complete`.

The `elements`, `relations`, `premises` and `opportunities` collections are lists of objects, not dictionaries keyed by ID. The collection-local IDs and their declared meanings are caller-authored. This small domain-generic example has the exact reference types:

```json
{
  "id": "current-composition",
  "kind": "composition",
  "content": {
    "format": "g60-composition/1",
    "purpose": "Inspect a source contribution and its receiving use.",
    "elements": [
      {"id": "source", "account": "An acquired contribution."},
      {"id": "result", "account": "The proposed receiving result."}
    ],
    "relations": [
      {"id": "source-to-result", "from": ["source"], "to": ["result"],
       "prediction": "The selected contribution supplies a consequential distinction.",
       "observation": "unassessed"}
    ],
    "premises": [
      {"id": "current-use", "account": "Current applicability remains to be judged.",
       "disposition": "unassessed"}
    ],
    "opportunities": [
      {"id": "qualify", "contribution": "Judge the selected distinction in its actual use.",
       "at": ["relation:source-to-result"], "requires": [["current-use"]],
       "reads": ["source:current"], "writes": ["result:current"],
       "footprint": "declared", "disposition": "open"}
    ]
  }
}
```

`--focus` accepts an explicit `element:`, `relation:`, `premise:` or `opportunity:` reference; `at` specifically locates elements or relations. A record's successful `put` only stores ordinary content; `compose` validates this optional profile. Missing references remain unresolved rather than becoming valid dependencies. An interaction map is a bounded declared-resource projection; use the ordinary composition projection when the question also needs prerequisite and `at` reference qualification.

```bash
python3 -B "$G60_BASE/agent-system/system.py" --root RUNTIME compose --record COMPOSITION_ID --interaction-map --limit 12
python3 -B "$G60_BASE/agent-system/system.py" --root RUNTIME compose --record COMPOSITION_ID --focus relation:RELATION_ID --limit 6 --pair-limit 0
```

`--interaction-map` accepts `--record` and optional `--limit`; it cannot be combined with focus, changed-since or a nondefault pair-limit. It factors shared resources without all-pairs enumeration. A direct context `compositions` selector also selects the **complete record and basis**, even with a small focus projection. Use it for an intentionally small current composition, or deliver an explicitly qualified standalone bounded projection as ordinary material.

## Prospect, bounded source encounter, observation and recall

```bash
python3 -B "$G60_BASE/patterns/prospective-reception/discovery/turn.py" JOURNAL open PROSPECT_SPEC
python3 -B "$G60_BASE/patterns/prospective-reception/discovery/turn.py" JOURNAL lookup LOOKUP_SPEC
python3 -B "$G60_BASE/patterns/prospective-reception/discovery/turn.py" JOURNAL issue ISSUE_SPEC
python3 -B "$G60_BASE/patterns/prospective-reception/discovery/turn.py" JOURNAL observe OBSERVATION_SPEC
python3 -B "$G60_BASE/patterns/prospective-reception/discovery/recall.py" JOURNAL NEW_PROSPECT_ID --limit 3
python3 -B "$G60_BASE/patterns/prospective-reception/discovery/turn.py" JOURNAL transfer TRANSFER_SPEC
```

Paths inside turn specs resolve from the specification file's directory, not the process working directory or a native runtime's `work/` directory. A file-valued field is either an ordinary nonempty UTF-8 file path or a window object. In particular, **`use.authority` is a file/window containing the caller's actual authority account; it is not inline prose**. Free-text `decision`, `warrant`, `challenge`, `result` and narrative fields remain strings. A supplied authority account is captured evidence, not authentication or an independent grant of permission.

The exact current schemas are:

- `open`: `target`, `gap`, `prediction`, `rival`, `partial`, `query`. Prediction and rival differ. `partial` is a file/window.
- Single-library `lookup`: `prospect`, `reader`; optional `library`, `index`, `index_sha256`, `scan_limit`, `max_bytes`. The `reader` is the trusted native `knowledge.py`; an index requires its exact digest. This route creates a journal `nominations` record with at most three results.
- `issue`: `prospect`, **one to three `sources`**, and **one to four `uses`**; optional `nominations` and the complete continuation triple `parent`, `relation`, `because`. Alternatively, an already known-source issue supplies `target`, `gap`, `prediction`, `rival`, `partial` instead of `prospect`; this inline form has no `query` or `nominations` field.
- Each source is `{ "role": "distinct role", "path": FILE_OR_WINDOW }`, optionally with nominated `identity` and `shard`. Roles must be distinct. **`identity` is the selected nomination's `pin.id` string, not the whole pin object.** It requires the issue's `nominations` to be the ID of an existing lookup record **in this journal and for this same prospect**. The frozen complete owner's hash must equal that nominee's `pin.sha256`, even when only a window travels. Federated results additionally carry their exact returned `shard`; omit `shard` for a single-library result.
- Each use is `{ "name": "distinct use", "decision": "the actual receiving decision", "authority": FILE_OR_WINDOW }`. Names must be distinct. `relation` is `reuse`, `reopen` or `split`; `parent` is an observation ID. The three continuation fields are supplied together. `reuse` requires a supporting prior observation and checks the same encounter/decision/authority conditions; a changed use needs appropriate requalification rather than a relabeled repeat.
- `observe`: `issue`, `use`, `evidence`, `result`, `verdict`. `use` names exactly one issued use; `evidence` is a file/window containing the actual returned material. Verdict is `supports`, `contradicts` or `unresolved`.
- `transfer`: `parent`, `prospect`, **one `use` object with the same file-valued authority contract**, `warrant`, `challenge`; optional `roles` is a list of one to three distinct existing source-role strings. This produces a new issue carrying selected prior sources and changed-authority information; a new actual observation is still required.
- A window is `{ "path": "owner.md", "start": 0, "end": 200 }`, with real zero-based half-open UTF-8 byte bounds. Both boundaries must preserve valid UTF-8 text. Whole owners are retained in custody; selected text alone travels. A plain file and each window are at most 32 KiB; a windowed owner can be up to 16 MiB. The **complete encoded output**, including ID and newline, must also fit 32 KiB, so several individually acceptable windows can still exceed the packet bound. The input JSON spec has its own 32 KiB limit.

### Choose the acquisition contract actually performed

A direct `knowledge.py discover/show` call does not create a journal lookup record. Two legitimate routes remain available:

1. If the journal nomination check is required for this use, call `turn.py JOURNAL lookup LOOKUP_SPEC`, acquire a useful nominated complete owner, and supply that returned record ID plus the selected `pin.id` string in `issue`. Do not label a direct-reader result as a journal nomination or fabricate a lookup ID. A newly performed lookup has its own current timing.
2. For an already known or externally acquired source, omit `identity`, `shard` and `nominations`. The journal still freezes and identifies the exact acquired owner/window, but performs **no nomination-to-owner check**. Preserve any direct discovery pins and their verification separately, and state that narrower warrant. This supported source route does not retrospectively claim a check that was refused.

A complete generic known-source issue can be written as follows. `partial.md`, `selected-source.md` and `authority.md` must contain the actual nonempty text and exist beside this specification before invoking it:

```json
{
  "target": "Decide whether the selected contribution serves the next use",
  "gap": "Current applicability has not yet been received",
  "prediction": "The contribution supplies the distinction this use needs",
  "rival": "The next use needs a distinction absent from this contribution",
  "partial": "partial.md",
  "sources": [{"role": "selected contribution", "path": "selected-source.md"}],
  "uses": [{"name": "current receiving use", "decision": "Retain, revise or reject the contribution", "authority": "authority.md"}]
}
```

For the nomination route, obtain `open_result` and `lookup_result` from actual commands, choose a row in `lookup_result["bodies"]`, and construct the specification using their returned values. This example shows the exact field types, not permission to select the first candidate automatically:

```python
selected = chosen_nomination  # one actually selected row from lookup_result["bodies"]
source = {"role": "selected contribution", "path": acquired_owner_path,
          "identity": selected["pin"]["id"]}
if "shard" in selected:
    source["shard"] = selected["shard"]
issue_spec = {
    "prospect": open_result["id"],
    "nominations": lookup_result["id"],
    "sources": [source],
    "uses": [{"name": "current receiving use", "decision": "Judge current applicability",
              "authority": authority_file_path}]
}
```

### When a selected encounter does not fit this journal

The one-to-three source-entry and one-to-four use-entry limits belong to this bounded encounter adapter. They are not a G60 rule that a task may have only three sources or four dimensions, and they do not justify numerical batches. In the actual Study94 repair, two adjacent navigation units were represented by their one exact contiguous window in the already retained manuscript owner. That preserved the required material and its original ownership; it did not divide the semantic task.

Use a compact derivative or a single owner window only when it faithfully serves the receiving question, with its new identity and original recovery route explicit. Do not concatenate a whole bank or conceal omitted conditions just to fit the list count. If the coherent encounter genuinely requires more independent source bodies, select a capable broader native context or a separately justified adapter extension and preserve its own prospective/receiving evidence. Do not claim that a smaller journal issue captured the larger encounter. Inspect the actual delivered text as well as mechanical byte limits.

The authoritative field contracts are `patterns/prospective-reception/discovery/INTERFACE.md` and `turn.py` (`issue`, `one_use`, `frozen`); the broader native context remains governed by `agent-system/README.md`.

`recall.py` searches the new prospect's query against indexed actual observed uses. It returns bounded candidates, prior verdict/conditions, later differing verdicts, branches, and exact shared-evidence co-uses when present. It does not read unselected owner windows or establish that a candidate applies to the new use.

## Changed-read routing

The public CLI accepts receipts and supplied changes:

```bash
python3 -B "$G60_BASE/patterns/prospective-reception/discovery/read_routes.py" RECEIPTS_JSON CHANGES_JSON
```

Receipts are an object mapping use identity to its named input observations; changes map named input identity to current observation. Each observation is a SHA-256 hexadecimal string or `null` for observed absence:

```json
{
  "use-a": {"input-x": "SHA256_OF_PRIOR_X", "input-y": null},
  "use-b": {"input-x": "SHA256_OF_PRIOR_X"}
}
```

The Python API is `ReadRoutes(receipts).affected(changes)` and `.replace(use, reads)`. `affected` returns use IDs mapped to sorted changed input keys. A missing key in the changes object says nothing about that source. The router nominates affected consumers; their current-use judgment and later dependent effects still require the receiving operation.
