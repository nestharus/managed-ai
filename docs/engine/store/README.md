# Use the agent system 0.5.0

This application joins persistent goals and extensible JSON records to captured context, actual returns, guarded artifact adoption, attention feedback and a source-bound research catalogue. It is a small work environment for an existing capable agent. It does not provide a model, infer every relevant relationship or make alignment judgments automatically.

Domain and catalogue loading live under [selection](../../../src/managed_ai/store/selection/).
[Turns](../../../src/managed_ai/store/turns/) holds the input capture, record scope, preparation journal and frozen delivery explanation.
[The installed catalogue](../../../src/managed_ai/store/catalogue.json) binds the core source IDs and their bodies.

## Start with one useful context

Use Python 3.11 or later on Linux, macOS or native Windows. From `src/managed_ai/store`:

```bash
python3 -B manage_store.py --root ../../../my-ai-work init --goal-text "Review the current design and deliver a useful next version."
python3 -B manage_store.py --root ../../../my-ai-work recover start --text
```

The first command creates a goal and a frozen `start` context. Its JSON return identifies the exact context and current goal. The second reads that original context in a fresh process. No additional records or research phases are required before useful work.

For a richer initial goal, use `init --goal goal.json`. The file is an object with a nonempty `contribution`; ordinary extra content can describe the recipient, use, authority, requirements and alternatives. Only the host interprets those descriptions. Keep credentials out of working sources and choose effect authority through the actual host boundary.

Use a new or empty runtime root outside the engine code tree. The app creates `work/` for current artifacts and `exchange/` for immutable captured material and receipts. It assumes one cooperative owner. The supplied core uses cross-platform cooperative file locking on POSIX and native Windows.

## Continue through one shared engine and a selected store

The deployment entrance points to the central engine and one independent native store. It does not copy a project, engine, controller or earlier deployment. The complete [instance/domain contract](../INSTANCES.md) defines portable descriptors, schema compatibility, source selection and current-use retention.

From the package root:

```bash
python -B src/managed_ai/deployment/select_deployment.py --deployment /path/to/deployment system status
```

Each store owns its selected domains, payload schemas and reference catalogues. Acquire a complete selected source with `system catalogue SOURCE_ID --text`, then nominate its logical ID in a context's `techniques`. Local workspace material still uses the context's `sources` paths. A source catalogue or an available tool description does not establish participant acquisition or authorize execution.

To select changed central code, use `stage EDITION --engine /absolute/engine`, inspect the returned candidate digest, then `activate EDITION --expected CURRENT_DESCRIPTOR_SHA --manifest-sha256 CANDIDATE_SHA --reason ACCOUNT`. Activation checks the candidate's declared APIs, exact code identity and selected store before replacing the descriptor. Future invocations use that selection. Frozen contexts and pending effects remain attached to their original occurrences; they are not replayed.

New stores and deployments use relative bindings, so the intact package can be relocated. Historical context text keeps its original byte identity, including any original paths. Prepare a new context through the current deployment for new work. Portable storage does not supply process migration, remote authentication or authority to repeat an uncertain effect.

## Complete a direct artifact task

Place the material you want the agent to inspect under `../my-ai-work/work/sources/`. Create a context request, for example `request.json` beside your runtime:

```json
{
  "task": "Review the supplied design and propose a concise review with justified changes.",
  "consumer": "current reviewing agent",
  "sources": ["sources/design.md"],
  "targets": ["outputs/review.md"]
}
```

```bash
python3 -B manage_store.py --root ../../../my-ai-work context review --request request.json
python3 -B manage_store.py --root ../../../my-ai-work recover review --text
```

For a new target such as `tools/review.py`, create its intended ordinary parent directory under the runtime workspace before adoption—for example, `../my-ai-work/work/tools`. Declaring a target does not create its parent directories. Leave a target captured as absent uncreated until adoption; a placeholder file would change that captured target basis.

Give the actual captured context to the agent. The agent writes its real response to `reply.md` and proposed deliverable to `proposed-review.md`. Those files are authored by the actual participant, not generated example answers. Receive them, inspect their contribution, and adopt the justified result:

```bash
python3 -B manage_store.py --root ../../../my-ai-work receive review \
  --producer "actual reviewing agent, caller-brokered" --reply reply.md \
  --proposal outputs/review.md=proposed-review.md
python3 -B manage_store.py --root ../../../my-ai-work adopt review \
  --target outputs/review.md --reason "The owner accepts this review for the requested use."
```

`receive` retains the output and proposal without writing the target. `adopt` is the owner's explicit authority for that one replacement. It checks the captured sources, selected records and target basis at the actual writer. A changed declared input refuses publication while leaving the original return available. Create a new context using the current basis and the old contribution when appropriate; do not relabel or erase the old occurrence.

A refusal stating `publication parent must already exist` occurs before the core records publication intent. Inspect the original turn and target first. If inspection confirms that no publication intent was recorded and the captured sources and target basis still hold, the owner can create the intended ordinary parent and adopt the already received proposal. Keep the existing receipt; this does not call for redispatching its producer. A recorded publication intent or uncertain effect instead requires its existing recovery procedure; creating a directory does not resolve that uncertainty.

Records, interpretations and internal context files have reserved namespaces. Use their owning commands rather than proposing replacements for them. Declared missing sources and delivered Attention source/product observations also participate in the writer's checks; an absent source appearing can change the basis just as an existing source changing does. Publication is one file at a time, not an atomic multi-file transaction. The underlying [continuation core contract](../core/README.md) explains exact custody, bounds, failures and publication recovery.

## Represent what the work actually needs

Records are ordinary JSON objects with open `kind`, content and relationship labels:

```json
{
  "id": "review-question",
  "kind": "question",
  "content": {
    "question": "Can a proposal based on an old source overwrite the accepted result?",
    "use": "Decide whether the actual writer needs correction.",
    "next_observation": "Exercise the authoritative adoption boundary."
  },
  "relations": [{"relation": "serves", "target": "goal"}],
  "basis": ["sources/design.md"],
  "active": true
}
```

```bash
python3 -B manage_store.py --root ../../../my-ai-work put --record question.json --expected 0
python3 -B manage_store.py --root ../../../my-ai-work show review-question
```

Use expected revision `0` for creation; use the actual current revision when changing the record. The app refuses a conflicting revision. With the default `current-use` policy, ordinary replacement does not automatically retain the previous body or a complete predecessor chain. Explicit pins, captured contexts, Information conversions and required feedback retain their selected exact evidence; select `append-history` only when that continuing history is actually needed. The [instance contract](../INSTANCES.md) owns this policy distinction. `basis` names workspace-relative sources and captures their byte identities. Selected records bring their declared available basis into later contexts. Relation labels remain public host-authored claims; the runtime does not infer a dependency graph from arbitrary labels.

An artifact can represent a hypothesis, observer, process, criterion, capability, result, question or another useful thing. Add one when it carries an actual relationship or duty. There is no mandatory inventory or fixed workflow. `active` is navigation metadata, not proof of fulfillment.

The reserved `goal` record has `kind: "goal"` and object content with `contribution`. Revise it through `put` using its current revision. Optional goal `obligations` are objects with `id`, `text` and `disposition`; omitted old obligations survive goal revision. `fulfilled` or `released` requires an `account`. A separate record with `kind: "obligation"` can carry its own duty. Supply an account when marking it fulfilled or released. A historical separate closure without an account stays on the owed list and its evidence remains available in ordinary work. An open separate duty may be inactive for navigation and still governs ordinary work. Close it with an account before retyping it. A continuing open duty inherits its pins when an edit omits `basis`. Supply `content.basis_change` with `from_revision` and a nonempty `account` when replacing or removing existing evidence pins. The account records the owner’s judgment. Check the receiving effect where it matters. The current goal, obligation records and records named `field` or `frontier` are automatically exposed in each context, so still-owed work is not lost just because a caller requests a narrow view.

## Inspect a composition and keep the wider contribution field available

Use [Compose the system and its code](COMPOSITION.md) to develop the receiving relationship, examine this app's component composition and construct a successor-system prediction. An ordinary record can opt into the `g60-composition/1` operational profile. This view does not require other records, artifacts or projects to use a task graph.

For example, save this record specification as `composition.json` and use an actual current runtime:

```json
{
  "id": "explanation-system",
  "kind": "composition",
  "content": {
    "format": "g60-composition/1",
    "purpose": "Produce a source-qualified explanation while an optional broader comparison waits.",
    "elements": [
      {"id": "claim", "account": "The selected source claim and its limits."},
      {"id": "explanation", "account": "A useful explanation for a maintainer."}
    ],
    "relations": [
      {"id": "claim-to-explanation", "from": ["claim"], "to": ["explanation"],
       "prediction": "The reader can form a system prediction and identify the producer of a failed relation.",
       "observation": "unassessed"}
    ],
    "premises": [
      {"id": "current-source", "account": "The actual source is available and has been interpreted for this use.",
       "disposition": "available", "basis": ["sources/claim.md"]},
      {"id": "general-comparison", "account": "No adequate independent general efficacy comparison is supplied.",
       "disposition": "unavailable"}
    ],
    "opportunities": [
      {"id": "qualified-explanation", "at": ["relation:claim-to-explanation"],
       "contribution": "Construct the conditional explanation from the selected source.",
       "requires": [["current-source"]], "disposition": "open",
       "reads": ["source:claim"], "writes": ["proposal:explanation"],
       "footprint": "declared", "phase": "construction"},
      {"id": "broader-claim", "at": ["element:claim"],
       "contribution": "Assess a supplementary general efficacy claim.",
       "requires": [["general-comparison"]], "disposition": "held",
       "footprint": "unknown", "phase": "unselected broader assessment"}
    ],
    "unknowns": ["The supplied frame may omit a necessary receiving relationship."]
  },
  "basis": ["sources/claim.md"]
}
```

The source path must name the actual material for this contribution. Calling it available is a supplied interpretation; its bytes alone do not establish the claim. Create or revise the ordinary record through its expected-revision owner, then inspect it:

```bash
python3 -B manage_store.py --root ../../../my-ai-work put --record composition.json --expected 0
python3 -B manage_store.py --root ../../../my-ai-work compose --record explanation-system
python3 -B manage_store.py --root ../../../my-ai-work compose --record explanation-system \
  --focus relation:claim-to-explanation --limit 6 --pair-limit 100
```

| Profile member | Declared interpretation |
|---|---|
| `format`, `purpose` | Required profile identity and intended contribution. Other collections may be absent or empty. |
| `elements` | Unique local `id`, nonempty `account`, optional arbitrary `content`. Elements can represent code, explanations, systems, videos, observations or another needed artifact. |
| `relations` | Unique `id`, element-ID lists `from` and `to`, nonempty `prediction`; optional `observation` (`unassessed`, `supported`, `contradicted`), `account`, `basis` and arbitrary `content`. An asserted observation requires an account. It remains the author's observation. |
| `premises` | Unique `id`, nonempty `account`, supplied `disposition` (`available`, `unavailable`, `unassessed`), optional `basis` and `content`. Named basis paths refer to bindings in this record's ordinary `basis`; missing, unbound or changed evidence requires qualification. A premise without evidence remains expressly caller-supplied. |
| `opportunities` | Unique `id`, nonempty `contribution`, optional `at` element/relation references, `requires` alternatives, disposition (`open`, `held`, `complete`), `reads`, `writes`, `conflicts`, `footprint` (`declared`, `unknown`), `phase`, `account` and `content`. These are proposed contributions, not commands. |
| `unknowns`, `coverage`, `content` | Optional stated unknowns, the author's coverage account and other arbitrary content. Missing rows never imply exhausted discovery. |

Within `at` and `--focus`, use explicit references such as `element:claim`, `relation:claim-to-explanation`, `premise:current-source` or `opportunity:qualified-explanation`; `at` specifically locates elements/relations. Relation endpoints and prerequisite/conflict entries use local IDs of their respective collections. Unresolved references remain explicit. General relation cycles are permitted and are not interpreted as task deadlocks.

`requires: [["a", "b"], ["c"]]` means either both nominated premises `a` and `b`, or the alternative `c`. Empty `requires` declares no prerequisite. It does not establish usefulness or complete feasibility. An opportunity's completed disposition does not manufacture another premise's availability. The returned availability is qualified as **under declared premises**, separate from the supplied open/held/complete disposition. A blocked realization need not block another contribution at the same artifact or relation.

When a wide field is being screened for declared overlaps, use `compose --record ID --interaction-map`. A resource row names the writers and readers of one resource whenever a writer has a distinct peer. It represents every writer/writer and writer/reader pair on that resource. A separate row records explicit conflicts; an edge named by either party appears once. The view pins the record revision and counts unknown footprints, missing conflict references, and groups omitted by the row or byte limit. Use `--limit` to choose up to 200 groups. Use `show ID` for the complete authored record. Reading this projection does not requalify its basis or reserve a physical resource.

Reads, writes and conflicts compare exact authored resource identities. `phase` states the operation to which that footprint applies. No declared interaction is not guaranteed independence: actual contexts also depend on governing goal, field, obligations, selected materials and all selected targets. Updating the field can make a prior publication basis stale while its retained proposal remains useful. Select that proposal through a new context's `artifacts`, assess its current applicability, and receive/adopt the justified body; unchanged bytes can be the correct contribution.

An optional context selector captures the view, complete composition record and its ordinary source basis:

```json
{
  "task": "Construct the qualified explanation and challenge any missing receiving relationship.",
  "compositions": [{"record": "explanation-system", "focus": "relation:claim-to-explanation", "limit": 6, "pair_limit": 100}],
  "sources": ["sources/claim.md"],
  "targets": ["outputs/explanation.md"]
}
```

Composition records are added to the explicit selection. If `record_limit` would omit one, preparation refuses before creating that turn. The captured view also exposes the actual context's publication prerequisites, so declared contribution footprints cannot silently erase shared guards. `view TURN` recovers the exact captured projection; a new `compose` call is a new observation of current evidence. An existing exact focus is first in its own collection before related rows at the detail bound; a missing focus remains an explicit unresolved reference. The returned byte bound can still refuse an oversized view. The view retains wider counts and omitted identities; focus does not define the whole possible field. The complete authored record remains available through `show ID` or the original capture.

The profile and output use explicit software bounds, reported with the view. Detail limits and pair limits are different; `--pair-limit 0` deliberately skips pair comparisons and reports them omitted. At most twenty composition views and one MiB of combined view JSON are selected into a context. A bound or unresolved reference is a request for deliberate further selection, not evidence of irrelevance. `compose` consumes ordinary `record.content` directly and explicitly refuses information wrappers; it does not silently reinterpret a pinned contract. No source body or profile executes code, chooses a schedule, certifies alignment or authorizes an effect.

## Version an information interpretation and migrate an actual record

The runtime envelope `g60-agent-system/0.1`, an individual record's revision, an optional payload interpretation, and the selected application version have different jobs. Open content can remain ordinary JSON. When a repeated information type needs an explicit meaning, make its definition an ordinary record with `kind: "content-contract"` and nonempty prose or JSON content. Give it a useful name and stated version in that definition. The runtime does not infer the meaning from the version label.

For example, a definition can state that an inspection observation's `status: "complete"` means its source inspection and report are complete. This does not assert that a repair was implemented or that a later receiver used it. Store the definition and an ordinary observation with existing `put`, then use their exact current identities from `show` in a wrapping specification:

```json
{
  "source": {"id": "inspection-note", "revision": 1, "sha256": "EXACT_RECORD_DIGEST_FROM_SHOW"},
  "contract": {"id": "inspection-meaning", "revision": 1, "sha256": "EXACT_DEFINITION_DIGEST_FROM_SHOW"},
  "account": "Attach the authored observation to the exact convention it uses."
}
```

```bash
python3 -B manage_store.py --root ../../../my-ai-work info wrap --spec wrap.json
python3 -B manage_store.py --root ../../../my-ai-work info inspect inspection-note
```

Replace the explanatory digest placeholders with the actual 64-character values. `wrap` preserves the entire original content as `payload`, retains its original record object, and writes an opt-in `g60-information/1` envelope. The envelope pins the definition's ID, revision, record digest and exact retained body object. The definition can later change without redefining this older observation. A new context selecting the wrapped record automatically captures its exact pinned definition, deduplicated by object. `info inspect` exposes that definition and any last explicit conversion specification. Ordinary `show` remains a raw record view.

Suppose a new receiver needs to distinguish inspection, implementation and receiving use. Revise the definition through `put`, inspect both current identities, and author a complete migration proposal:

```json
{
  "source": {"id": "inspection-note", "revision": 2, "sha256": "EXACT_WRAPPED_RECORD_DIGEST"},
  "contract": {"id": "inspection-meaning", "revision": 2, "sha256": "EXACT_NEW_DEFINITION_DIGEST"},
  "payload": {
    "inspection": {"status": "complete", "report_ref": "outputs/inspection.md"},
    "implementation": {"status": "unassessed"},
    "receiving_use": {"status": "unassessed"}
  },
  "preserve": [
    {"from": "/status", "to": "/inspection/status"},
    {"from": "/report_ref", "to": "/inspection/report_ref"}
  ],
  "change": "Separate the observations needed by the next maintenance decision.",
  "unknowns": "The old record supplies no implementation or receiving-use assessment."
}
```

```bash
python3 -B manage_store.py --root ../../../my-ai-work info migrate --spec migration.json
```

The owner reviews this concrete specification before invoking it. The command checks the current source and target definition against their exact nominated identities, compares the nominated JSON-pointer values without inserting defaults, preserves the original occurrence and specification, and writes one successor record through the existing owner. An absent pointer is refused; `null` does not equal zero. `preserve` may be empty when the accepted transformation has no exact-value invariant, but its account still needs a supplied justification. The command does not execute stored code or establish that an omitted preservation condition was unnecessary.

By default, inherited source-basis files must still match. An explicit `basis` list permits an owner-reviewed successor basis; the old record retains its original basis. A source or definition changed after specification preparation requires a new reviewed specification. Inspect actual record history after an uncertain command result instead of blindly issuing the conversion again. A migration changes one named information record, not the runtime format or a whole collection transaction.

App 0.4.0's aware `put` refuses silent removal or switching of an existing information contract and refuses unknown information-envelope versions. Same-contract payload edits remain ordinary explicit record revisions. Goal, obligation, field/frontier and definition owners retain their established shapes; this optional wrapper has no adapter for those built-in readers. Older engines can store open JSON but do not enforce this new convention. Reverting an engine therefore requires a real receiver-compatibility judgment; a matching runtime envelope is insufficient.

These are exact binding, lineage and nominated-preservation checks. The definition can describe a schema, but no general JSON Schema interpreter or automatic semantic validator is installed. A capable participant or explicitly supplied domain procedure still assesses payload shape, truth, missing information, compatibility and useful receiving activity. A direct edit, read adapter, additional source or changed question may serve a duty better than migration.

## Choose context and retain what is outside it

A request can contain `task`, `consumer`, `records`, `sources`, `project_sources`, `techniques`, `targets`, `returns`, `artifacts`, `attention`, `attention_groups`, `attention_items`, `discovery`, `source_pins`, `compositions`, `record_limit`, `attention_limit` and `attention_max_bytes`. `project_sources` requires a selected deployment as described above. Identity/path collections must be JSON lists of nonempty strings. The count/byte limits are positive integers. Defaults select at most 20 explicitly requested records and at most 8 Attention items in 16,000 bytes; governing records remain included. Closed obligation records also remain complete and governing, but their supporting basis bodies are no longer automatically expanded. Explicitly selecting the closed record within `record_limit`, selecting its source path, or another selected record requiring the same source exposes it. `outside_record_basis` in `view TURN` retains the exact old evidence binding; this is historical custody, not current-path validation. Source files and rendered contexts also obey the core's 2 MiB per-object and 8 MiB context bounds.

Choose a new identity for each distinct receiving turn. If preparation fails or is interrupted, inspect `status` and follow [Recover context preparation](#revising-a-large-record-and-recovering-context-preparation). Select source bodies needed for this interpretation. The complete inventory remains recoverable.

`returns: ["review"]` selects the actual reply from an earlier turn. `artifacts: ["review:outputs/review.md"]` selects that retained proposed body even when it was never adopted or the current workspace file differs. Neither route inherits the earlier effect authority. `recover review --text` reads the exact original prepared context.

Each context exposes selected material, current whole-goal judgment and Attention feedback, with an exact recovery route for the complete view inventory. `view TURN` retrieves that inventory through the captured object's hash. Omitted requested records and wider records/techniques remain distinguishable. The working context shows the outside catalogue count rather than rendering every unselected identity. An outside identity is retained availability, not an assertion that the agent read it. The core does not select semantically sufficient sources for you.

Before delegating a frozen turn, use `system explain-context TURN` through the same native or deployment entrance. It reports every actual input's role, path, digest and byte count, the selected-body total and complete rendered-context size. New captures retain inclusion reasons such as a domain entry, explicit source or selected record's basis; shared paths keep all observed reasons without multiplying their delivered bytes. The reason map is retained in the complete view, outside the ordinary model exposure. Older captures report missing origins as unknown. A matching record-basis path is only correspondence: the explanation shows both the record's expected pin and the actually captured pin, which can differ. It does not reconstruct history from live preparation files or today's heads.

This command validates retained packet and object bytes without writing state or creating lock files. The selected installation must still be valid; it is not historical source authority. Verification reads and full-view sidecar bytes are additional work, outside the reported delivery totals. Repeated bodies in separate roles remain separate delivery entries. The result does not establish reading, relevance, alignment or a useful token/latency saving. If the selection is unsuitable, prepare a fresh receiving turn using a truthful current request. Governing work remains included; a domain-entry change requires its owning selection decision. Full bodies and wider inventory remain available through `recover TURN --text` and `view TURN`.

The integrated app reads `--request`, `--spec`, `--assessment` and `assess --receipt` JSON files as host file paths; an absolute path avoids dependence on the caller's working directory. It copies Attention inputs into its internal workspace before invoking that component. Paths inside Attention `basis` and `products` and ordinary context `sources` remain relative to runtime `work/`; `project_sources` remains relative to the selected accepted project. The core Attention command's input-file convention is therefore not the integrated app's input-file convention.

Use `attention` for nominated considerations that need source/use-bound accounts. Optional `attention_groups` and `attention_items` lists select the union of named active groups/items; unknown selectors are refused. Empty selectors retain the all-active view. These selectors bound which Attention sources are observed; the item/byte output limits alone do not. A supplied wider field is still observed. Outside questions remain recoverable and are not freshly assessed by this selection. Its format and `receive --assessment account.json` are defined in the complete [Attention guide](../attention.md). `track --spec FILE` and `assess TURN --receipt FILE --producer NAME` expose the same operations separately. In particular, `groups` is a list, not a string. Arbitrary record content remains open; the stricter Attention interface owns its declared schema. Do not equate an Attention account with whole-goal acceptance. If an optional assessment fails after receipt, the actual reply remains received and the returned recovery command addresses that assessment alone.

## Discover current sources and maintain useful landmarks

Use a known exact path or operation directly when it is already adequate. For a concern that needs navigation, `discover --request discovery.json` searches selected local scopes and follows authored record relations. For example:

```json
{
  "query": "evolving views",
  "content": true,
  "scopes": ["records", "project", "techniques"],
  "anchors": ["record:representation-route"],
  "hops": 2,
  "limit": 12,
  "scan_limit": 4000,
  "max_bytes": 4194304
}
```

The optional anchor in this example must be a record you actually created. Omit `anchors` for lexical search. `project` requires the selected deployment entrance. The three namespaces are `record:ID`, `project:RELATIVE_PATH` and `technique:CATALOGUE_ID`; a catalogue ID can itself contain a colon. They respectively identify mutable runtime records, the current accepted project, and the frozen acquired repertoire. A current project match is not silently substituted for a historical technique.

A landmark is an ordinary revisable record that explains why a route may help a particular concern. Its optional relations can use labels such as `may-reuse`, `contrasts-with` or `requires-source` with namespaced targets. The host supplies those meanings and can revise or reject them. Any record can be an anchor; there is no compulsory landmark kind or parent hierarchy. An existing bare relation target resolves only when it names a known record. Unknown or out-of-scope endpoints remain reported unresolved references.

Results expose each candidate's source identity, match reason, traversed declared relation, usable ordinary context selector and acquisition limits. `direction` selects outgoing, incoming or both relation directions; `relation_labels` can narrow them; `include_inactive` explicitly widens the default active-record view. The traversal detects revisits and reports hop/frontier bounds. Entry/relation scan bounds, source-byte bounds and result limits have separate meanings. The complete response retains inventory, unexamined, skipped and omitted identities. Inventory construction is still a local scan; this is a low-scale on-demand implementation, not a sublinear index or evaluated semantic ranker.

Within one query, catalogue references with the same body path, expected digest and byte count reuse their verified body observation. Each reference keeps its own title, metadata, relations and result identity. `examined_body_bytes` charges acquired source bytes, including record JSON, while `examined_body_reference_bytes` counts verified body bytes for each logical reference. `examined_body_sources` and `reused_body_references` expose the reuse. There is no cross-query body cache: the next query verifies fresh source bytes. Scan limits still apply to distinct entries even when their body work is shared. A larger logical coverage return therefore need not imply less elapsed time; compare the actual acquired work and receiving result.

Inspect the relevant complete bodies before deciding fit. The returned `context_request` contains candidate selectors, not an automatic instruction to select every result. To preserve the discovery-to-reading relationship, copy each chosen candidate's exact `{ "ref": ..., "source": ... }` into the next context request's optional `source_pins`, together with its ordinary selector. Context preparation rechecks the source identity and refuses drift or a pin whose body is not selected. A changed edition, even with the same file bytes, needs a deliberate new selection. Rediscover and qualify the changed source; keep the earlier report as its own occurrence.

A context request can also contain a `discovery` request. It captures a candidate summary and retains the complete report through the immutable `view TURN` route; it does not automatically select candidate bodies. A no-match or bounded result means only no returned match under that search. It does not prove absence, irrelevance, completeness or novelty. Similarity and declared links never grant effect authority.

### Choose lexical nomination order

Lexical scanning defaults to `scan_order: "inventory"`: requested scopes in order, with identities sorted within each scope. A large earlier scope can consume the entry allowance before a later scope is examined. Choose a useful narrower scope or exact anchor when known. For mixed-scope exploration, `scan_order: "scope-round-robin"` visits one next entry per nonempty scope in turn. For a reproducible different region, `scan_order: "seeded"` requires an explicit integer `seed` from 0 through 2^64−1. Other modes reject a supplied seed. Seeded order sorts SHA-256 keys of canonical `{seed, ref}` objects with identity ties; it is pseudorandom nomination, not a relevance or statistical-coverage guarantee.

Anchor traversal still comes first. `coverage.examined_entries_by_scope` reports actual examination. `coverage.lexical_nomination` gives policy/version, algorithm, seed (null for nonseeded coverage), ordered-domain digest and count; it is null for an anchor-only search. Normalized nonseeded requests omit the seed field so the returned request can be replayed. Body acquisition and result cutoff keep their separate bounds. Repeating a seed repeats the order for the same inventory; changed membership can change its prefix. All policies enumerate metadata; seeded sorting costs O(N log N). No body bound makes that metadata work sublinear.

```json
{"query":"economy","scan_order":"scope-round-robin","scan_limit":1000,"limit":25}
```

To explore another reproducible order, use `"scan_order":"seeded","seed":0` in the request. A different seed nominates another order; it does not certify fresh independent sampling or absence of semantic gaps.

## Retrieve the acquired research

`catalogue` lists available identities in pages; `catalogue ID --text` returns the complete hash-checked body declared by that entry. Search metadata with `catalogue --query "continuation" --limit 25 --offset 0`, optionally filter with `--kind operation`, or include complete text in the lexical search with `--content`. The result retains the total match count, page omissions and next offset. A literal match is a retrieval aid, not an applicability judgment. The current catalogue contains selected complete guidance, retained method bodies, and explicitly qualified operation descriptions and historical locators. Its entries state what is supplied. The current app guide accompanies new contexts; the paired research archive owns the broader theory and evidence collection.

Catalogue calls may coexist under shared, nonblocking cooperative locks. The deployment entrance holds its deployment lock before the store owner lock; the direct native entrance holds the store owner lock. The owner guard covers descriptor/domain selection and complete requested acquisition. Opening the existing cooperative guard can create a missing lock file, as on the exclusive route. Writers retain their exclusive locks, so an overlapping writer or reader may still receive an explicit lock refusal. A failed call does not establish a negative search result. Inspect the current outcome and retry a read when appropriate; acquisition never grants authority to replay a mutation.

This exception applies only to `catalogue`; other native commands retain their existing admission contracts, including the frozen `explain-context` route. Engine/source verification, catalogue parsing and content scans still run. Shared reads provide neither a fairness or throughput guarantee nor protection against filesystem edits outside the cooperative owners. Each result is its observed selected occurrence, not perpetual source currency or evidence that a participant interpreted it.

Select useful identities in a context's `techniques` list to capture their complete declared bodies. Selection is use-specific: making a technique discoverable does not mean executing it on every task. Read `body_role`, `availability`, prerequisites and scope before treating a returned body as an original source or an executable operation. A locator catalogue is not the historical argument it names.

```bash
python3 -B manage_store.py --root ../../../my-ai-work catalogue --kind operation --limit 25
python3 -B manage_store.py --root ../../../my-ai-work catalogue operation:observer-model
```

The retained observer-model entry supplies its complete operation description and exact archive/member identities. Its operation archive is unavailable in this selected package, so it cannot currently be materialized. Where a catalogue actually supplies a verified operation archive, `materialize SLUG` verifies that archive and every declared member before installing source under `work/operations/SLUG/`. Materialization does not execute or authorize the operation. Read its contract and prerequisites, then apply it only when it serves the actual contribution. Some operations need optional libraries or a particular installed environment. Historical examples remain evidence within their original scope; the general operating method does not adopt their client context.

### Historical source boundary

The historical LC-L07 locator is retained here only as a qualified identity note; its former all-history catalogue is not installed. The original body declared by that locator has SHA-256 `6361c33f8aaa3541197423d5e4cfc5a32300d3cf32c9507b2143e771954ff399`, member `cybernetics-research/theory/constructive_repair.md` in `G35-inside-G59`; its LC-L07 argument occupies original lines 55–82. Returning the locator does not return that original body.

The paired research archive retains a primary constructive-repair source (`research/references/primary/cybernetics-research/theory/constructive_repair.md`) under its own current file identity, and a current construction account (`research/evidence/construction-in-use.md`), both routed in [navigation.json](../../../scripts/release/navigation.json). These are useful current routes, not a claim that the locator's exact historical body or all of its dependencies are installed. The maintained primary file and the locator's declared original have different byte identities.

No predecessor archive or historical recovery command is required for ordinary current system use. A historical recipe retained in catalogue metadata describes an earlier environment and is not an installed recovery service. If a contribution specifically needs an omitted original, acquire that exact body, verify its declared identity and qualify its original premises before relying on it. A present account can support current use without being passed off as a byte-identical historical source.

## Receive a trusted command

For an installed adapter or a finite local operation, create an argv JSON list such as `["python3", "/absolute/path/to/adapter.py", "{context}"]`, then:

```bash
python3 -B manage_store.py --root ../../../my-ai-work run review --argv-json argv.json
python3 -B manage_store.py --root ../../../my-ai-work receive review --producer "actual installed adapter" --wait 20
```

Choose this instead of a caller-brokered return for that turn. The command runs from `work/`; the Exchange substitutes `{context}` or `{packet}` without shell interpolation. The included Collector retains stdout, stderr, exit and request. A trusted command can reach other files and services under its inherited environment; captured context is not isolation. Model credentials, provider behavior, source adequacy and external effects remain the host's responsibility. Interrupted dispatch or publication can remain uncertain; inspect the original occurrence before deciding a next authorized action. No automatic retry is supplied.

Some managed hosts terminate child processes when a tool invocation ends. In that environment, use the bounded combined command so dispatch and collection share one live invocation:

```bash
python3 -B manage_store.py --root ../../../my-ai-work run review --argv-json argv.json \
  --wait 20 --producer "actual installed adapter"
```

A positive wait requires explicit producer attribution and must be finite and nonnegative. This dispatches once and collects that same carrier. If the bound expires, the result remains pending or uncertain; it does not authorize another dispatch. Preserve the original occurrence and decide a next action from the actual effect contract. For an already dispatched turn, use `receive` to inspect/collect it, not another `run`. The caller-brokered return path remains available when the host cannot preserve a command's required lifetime.

For a prepared artifact target, add `--proposal outputs/review.md=proposed-review.md` to a positive bounded run. Repeat the flag for distinct prepared targets. Proposal file paths are relative to the caller's working directory. The runtime checks the mappings before launch and reads the files after the carrier completes. It verifies stdout as the reply and records each proposal as a caller nomination. Only a separate `adopt` can publish a proposal under the current authority and basis checks.

A pending carrier or an unavailable proposal file leaves the turn open. The result includes a `recovery` command for the same turn. For a bounded run with nominations, `receive --run-nominations -- TURN` reads the producer, ordered files and original directory from that turn's dispatch. You can issue this command after an interrupted handoff. Collection never starts a carrier. An explicitly repeated dispatch can finish an interrupted admission only with its original inputs. A retained dispatch without this binding requires its original explicit receive arguments and file origin. Use the receive route when the carrier and files are available. When dispatch and collection are separate from the start, pass the same flags to `receive`. The first complete return binds the reply and proposals together.

## Judge the whole and continue

`status` exposes the current goal, changed record bases, still-owed obligations, turns and current whole-goal interpretation. Write a public account explaining what the actual use establishes and what remains. Then bind it to the actual goal revision:

```bash
python3 -B manage_store.py --root ../../../my-ai-work interpret --goal-revision 1 \
  --disposition continue --account current-use.md --evidence outputs/review.md
```

Use `met`, `continue` or `wait`; `wait` also requires `--condition`. The software retains the host-authored account. It does not infer success from item status or validate the semantic adequacy of a `met` claim. A changed goal, declared evidence or separate obligation membership/revision requires a new interpretation; useful old results and still-owed effects remain recoverable. Status gives exact reassessment reasons. Older accounts without a separate-obligation binding remain historical and explicitly require reassessment; unrelated notes and optional field changes alone do not invalidate a current judgment.

For restart, retain the complete runtime root and use `status`, `show ID` and `recover TURN`. Current artifacts are under `work/outputs/`; original objects and returns remain under `exchange/`. No automatic garbage collection or semantic compaction is implemented. Explicit maintenance commands remain caller-authorized: `reindex` builds an HMAC-verified `HeadIndex v3` binary mmap index (`exchange/.system/head-index.current.json` -> `head-index.gen<N>.bin` + `.delta.jsonl` sidecar) accelerating `status`, `graph`, and `discover`; `pack-objects` (dry-run by default, `--apply --expected-plan` under exclusive lock) packs loose immutable objects into reserved append-only shard slabs (`exchange/objects/packs/CURRENT.json`); and `audit-objects` / `gc-objects` plan and remove unreachable objects (logical `.occ` tombstones and physical shard repacking once dead bytes exceed 30%). A handoff should name the actual current goal, accepted version, source routes, unresolved effects and selected next contribution. The complete [working guidance](../../../src/managed_ai/store/GUIDE.md) explains the constructive route when new relationships must be developed.

## Verify the delivered contract

From the repository root, run `python -B -m unittest discover -s tests/store`. The core's tests and [overview](../core/README.md) cover its declared mechanisms. These checks establish particular software behavior; actual agent use and interpretation remain separate evidence.

For the deployment addition, run `python3 -B agent-system/check_deployment.py`. Its isolated public-CLI checks cover actual source/instruction reselection, stable continuation commands, exact earlier-context recovery, expected-selection and manifest guards, complete membership, workspace publication boundaries, same-runtime binding and one actual bounded command lifetime. Its project text and replies are declared fixtures. A later real agent consuming the selected edition remains a separate receiving use.

For optional information, discovery and scoped consideration, run `python3 -B agent-system/check_information.py`, `python3 -B agent-system/check_discovery.py` and `python3 -B agent-system/check_context_scope.py`. They exercise isolated defined boundaries with authored fixtures; actual useful self-inspection and later maintenance uptake require their own observed returns.

## Revising a large record and recovering context preparation

A native record has a 2 MiB encoded limit including generated metadata, basis, previous-revision identity and UTF-8 content. Keep large external observations as separate bounded sources and nominate the relevant pieces. A historical owner may already have accepted a larger record. Replace that head through its normal owning operation using its current revision and a bounded successor. New stores use current-use record retention. Explicitly pin a bounded current source before replacing it when its exact bytes remain useful. If a legacy oversized predecessor is required, select append-history retention or preserve that exact body separately before replacement. A non-null `previous_object` is a retained-chain link; a current-use boundary does not claim that every predecessor was saved. Large historical objects stay outside ordinary context and transport limits. Each complete packet also has its own combined budget.

A context has one local preparation owner. It builds the candidate Attention selection privately and freezes the complete Exchange packet. After a refusal or interruption, inspect `status`. Before sealing, a refusal leaves current Attention untouched. For an unused ID or an incomplete turn in `draft` or `inputs`, correct the request and rerun `context TURN` with that same ID. For a `sealed` turn use `resume-context TURN`. It checks the frozen packet, installs its matching Attention occurrence and makes the original packet visible. Later changes to source files cannot rewrite the selected bytes. Publication of a proposed edit still checks its actual current basis.

If an independent Attention update arrived before the sealed selection installed, resumption refuses to overwrite that work. Inspect the affected turn, use `abandon-context TURN --account 'why a new selection is needed'`, and select a fresh ID. Once the occurrence is installed, resumption preserves subsequent independent work and abandonment is refused. A legacy `attention-started` or unclassified turn keeps its earlier inspect-and-abandon route. `recover TURN` reads a published packet. All of these transitions presume the cooperative local owner lock.

### Retain terminal failures and reconcile publication observations

`close-transport TURN --account ACCOUNT` records a verified launch failure or a completed stdout body above the 2 MiB reply limit. The result contains `transport_outcome` and current `status`. The turn becomes `terminal-without-return` and leaves `status.gaps.pending_returns`. Its original request, dispatch, actual terminal evidence and goal obligations retain their records. Running, uncertain and integrity-unresolved observations are refused. A nonzero process exit with an acceptable reply retains the ordinary receive and explicit adoption route.

`recover-transport TURN` restores the complete retained carrier evidence from either a received contribution or a terminal outcome. This route works in a fresh process after active carrier removal. `recover TURN` still returns the original captured context.

`reconcile-publication TURN --target OUTPUT --expected-sha256 HASH --account ACCOUNT` acknowledges a currently observed target matching the captured base or proposal. Use `absent` for an absent captured base and current target. The result contains `reconciliation` and current `status`. The receipt retains the original intent identity and marks historical completion `unestablished`. Original target bytes and the intent record are preserved. A sibling adoption can use that acknowledged identity through its ordinary basis and current-authority checks. The original target retains its replay guard.

Exact repeated requests return their original records. Changed accounts or observations are refused. Terminal closure uses existing owner and current-feedback dispatch checks. Reconciliation uses existing publication authority. Divergent targets and changed governing work require a fresh current receiving occasion. The [continuation core contract](../core/README.md) states the complete recovery conditions.
