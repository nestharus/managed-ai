# Collective work under local receiving owners

This small Python component retains source-qualified contributions, selective peer handoffs and the conditions for each local acceptance. Several participants can prepare different contributions while a new interpretation reopens one receiving use. The participants choose what matters, construct the relation and judge the return; the component makes those declared relationships durable and consequential.

Use it when an actual shared activity needs recoverable peer inboxes or target-specific acceptance that ordinary files and the current session host do not already supply. A direct capable producer or sufficient peer message remains a valid simpler arrangement. The implementation is a standard-library, single-host adapter. It does not call models, choose work, discover semantic dependencies, execute imported code or provide an authentication service.

## The contribution being coordinated

The primary entities are ordinary artifacts and their uses. A target might be a research explanation, a component, a package promise, a proposed system or the account joining several accepted pieces. Claims are optional short reservations for accepting one target; they are not a task ontology.

A source can remain byte-identical while a participant discovers that its use in another composition needs reconsideration. A reverse index over known references does not discover that relation. The participant can retain a finding and address an `offer` to the receiving artifact. Its owner decides in advance which participants' addressed offers require reconsideration. This `reconsider_from` policy supplies a receiving duty, not an assertion that every offer is relevant or correct.

The receiver can acknowledge arrival immediately. A required offer remains pending until the owner publishes an actual proposal with a treatment for that use. The owner can inspect and stage required judgments independently before committing the proposal. The owner can change the result, retain an adequate result with a qualified account, or explain non-applicability. Those accounts are fallible semantic contributions. A completed command does not certify their adequacy.

A wider composition has its own target, selected sources and receiving owner. Accepting all its leaves does not accept that whole. No one participant must interpret every artifact in this store.

The selected source can itself be a compact qualified view or a local answer for the receiving question. Complete capture means complete acquisition of those nominated units, not all underlying information in the collective. Broader judgments can compose such contributions while local detailed inquiry continues independently. A new local distinction can change the view, its signal or the joining relationship. This choice happens when constructing the receiving arrangement; it does not clear existing mandatory notices through a summary, acknowledgment or another target. The logical knowledge source `research/patterns/scoped-understanding.md` develops the general pattern.

## Run the owned local store

Requires Python 3.11 or newer with SQLite 3.38 or newer and JSON support. Keep mutable state outside this product directory. The database is one local owner's durable admission boundary; do not treat copied databases as independent continuations of the same authority.

The component enforces exclusion of its own directory. In the complete prototype distribution, use the root `prototype/collective.py` entry, which additionally enforces that enclosing product's boundary for initialization and later opening. Another host can provide its own boundary before calling this component, or place this file at the root of its complete standalone product. The component does not infer an enclosing root from a fixed number of parent directories. The commands below assume this standalone component directory is the current directory.

```sh
python3 -B collective.py --db /absolute/runtime/collective.sqlite init
python3 -B collective.py --db /absolute/runtime/collective.sqlite call --request /absolute/requests/create.json
python3 -B collective.py --db /absolute/runtime/collective.sqlite inbox --actor product-composer
python3 -B collective.py --db /absolute/runtime/collective.sqlite show product-promise
python3 -B collective.py --db /absolute/runtime/collective.sqlite notice 1
```

Each `call` reads exactly one JSON object with `actor`, `command` and `op`. The trusted host supplies the actor identity and permits effects. An actor string is not authentication. Every new attempt has a new command identity. Reusing an identity with the exact same request returns the historical result, including a refusal; a changed request under that identity conflicts.

Exit codes are 0 for successful calls/reads, 2 for a recorded refusal, and 3 for an unresolved input/store error. If response delivery is uncertain, inspect `receipt --actor ACTOR --command COMMAND` or send the exact same command. This recovers only this database operation. It supplies no permission to repeat a model call, file publication or external effect performed elsewhere.

The Python interface is `Collective.initialize(path)`, `store = Collective(path)`, `store.call(request)`, `store.show(id, revision=None)`, `store.retained('context'|'proposal', id)`, `store.notice(id)`, `store.receipt(actor, command)` and `store.view('inbox'|'changes', actor=None, after=0, limit=20)`.

## Exact operations

| Operation | Additional input | Result and acceptance condition |
|---|---|---|
| `create` | `id`, `body`, optional `reconsider_from` | Create an owner-controlled artifact at revision 1. The body is ordinary JSON. The standing receiving policy is a unique list of participant IDs, or `['*']`; its default is empty. |
| `watch` | `subject`, `reason` | Subscribe this actor to future accepted changes. Return the current full artifact or `null` plus an event cursor in the same transaction. Future changes create durable optional notices. |
| `offer` | `target`, `basis`, `account`, `body` | Retain a complete attributed impact and route it to the target owner. `basis` pins must identify retained versions. The target policy at this admission determines whether reception is required. |
| `capture` | `id`, `target`, optional `sources`, `notices`, `staged` | Capture the current target and selected sources. The default includes all its pending required offers. With `staged: true`, the owner captures explicitly selected notices and can review additional offers separately against the proposal. |
| `propose` | `id`, `context`, `body` | Retain the actual owner-supplied return and its context, even if the context has become stale. This does not accept the target. |
| `claim` | `target`, `revision`, optional `seconds` | Owner reserves admission for the current target revision, receiving a fenced token. Duration is 1–300 seconds, default 60. Competing live claims are refused. |
| `release` | `target`, `token` | Release a current unexpired claim. Expired/superseded tokens are refused; a new claim can replace an expired claim. |
| `publish` | `proposal`, `account`, optional `token`, `treatments` | Accept the proposed body against its unchanged captured target and sources, current claim if any, and treatment of all pending required impacts. Artifact, notice receipts, change events and command receipt commit together. |
| `review` | `proposal`, `notice`, `basis`, `disposition`, `account` | Stage one receiving judgment for the proposal and the complete original offer. Supply the inspected current pin for each source cited by that offer. The notice stays pending until publication. |
| `qualify` | `target` (pin), `prior` and `current` (source pins), `disposition` (`renew` or `release`), `account`, optional `token` | Reconsider one source relation for the continuing use owned by this target. The event retains the evidence and the account. Issue a fresh approval before use. |
| `acknowledge` | `notice`, `account` | Retain delivery acknowledgment. A required notice stays pending. An optional notice leaves the pending inbox but can still be selected and semantically treated later. |
| `take` | `approval` (exact receiver-target pin), optional `token` | Admit one ready decision at this receiver-owned point of use. Recheck the receiver target, all captured source pins, two source roles, pending required notices and the claim. Install its taken revision and emit one `received-use` event in the same transaction. |

The current store format is `g60-collective-work/2`. This adapter uses Python 3.11+ and SQLite 3.38+ with JSON functions. New installations initialize a v2 database. For a v1 database, stop all older writers, make a consistent SQLite backup, and run `python3 -B collective.py --db PATH upgrade` once. The transaction adds the proposal-review and receiving-source tables, imports sources from current v1 ready approvals, moves the writable command receipt ledger and updates the format. Existing artifacts, notices, receipts and unresolved duties stay in place. New v1 connections refuse the upgraded store. Already-open v1 API clients can read old receipts through a read-only view. A v1 command attempting a new effect fails at its receipt insert and its transaction rolls back. Earlier v1 hold records do not declare which former source relations should continue. Reconstruct such a receiving commission from the retained evidence before reissuing it. Repeating the upgrade reports that the store is current.

Pins are `{ "id": "source", "revision": 1, "sha256": "..." }`. The SHA-256 is over canonical UTF-8 JSON for the body: sorted keys, compact separators, Unicode preserved and nonfinite numbers refused. A same-body acceptance gets a new revision and account even though its body hash is unchanged. `show ID --revision N` retrieves its preserved prior occurrence.

### Apply a decision at the receiver's point of use

If a receiver can check the current material and publish the actual installation in one transaction, ordinary `capture`, `propose` and `publish` already provide the source-change check. When the desk approves earlier and a separate operator takes the approved item later, connect that operator to a named receiver-owned target.

1. The producer creates its candidate. The receiver creates an observation artifact containing what that desk measured, with the edition, source and limitations needed to interpret it. The receiver creates the target for the real use. Configure trusted sender identities and the target in the host. A string in a request is an asserted identity.
2. The receiver captures the target with the candidate and observation IDs in `sources`. Compare what the candidate would actually do with what this receiver owes. A hold or cancellation can be published to the target as an ordinary body with a reason. If the evidence calls for a change, send an `offer` to the producer with its exact basis. The producer can reopen its feedback scope. Its historical judgment stays readable.
3. When the receiver chooses to proceed, publish a decision body like the one below. The names refer to the editions pinned by the captured context. Other fields may carry the receiver's instructions. The candidate must have a different owner. The observation belongs to the receiving desk.

```json
{"format":"g60-receiver-use/1","disposition":"ready","candidate":"panel","observation":"measured-bay","quarter_turns":1}
```

4. At actual local admission, send `{"actor":"bench","command":"take-shift-7","op":"take","approval":APPROVAL_PIN}`, replacing `APPROVAL_PIN` with the pin returned by the receiver's publication. A new required notice, changed observation, changed candidate or superseded approval stops that take. Inspect the current source or inbox and obtain a new receiving decision. Repeating an identical command returns its original result; a second fresh take of the same approval is refused.
5. Consume the `received-use` event for the configured receiver **and target**, using the exact event and approval in the successful receipt. Read the candidate and approval with `show ID --revision N` to recover the payload and instructions. A newly authored body that says “taken” cannot create that event. Use the event identity for deduplication at a later external service.

The admission and retained editions are local to this store. A reported observation can be inaccurate. A holder of the receiver's write authority can consequently approve a bad delivery. The actual receiver needs evidence suitable for the job. If a machine acts later in another system, that system must own its own admission and deduplication or transact its effect there. A source change registered after admission cannot retract a use that has already happened. Shared access to the underlying database grants authority outside this adapter.

### A receiving sequence

Create the promise under the participant responsible for that promise:

```json
{
  "actor": "product-composer",
  "command": "create-product-promise",
  "op": "create",
  "id": "product-promise",
  "body": {"purpose": "Describe the selected knowledge package and its supplied host participation."},
  "reconsider_from": ["source-reader"]
}
```

Create needed source artifacts through their actual local owners. Then the composer captures a proposed use before independent source reading:

```json
{
  "actor": "product-composer",
  "command": "capture-product-1",
  "op": "capture",
  "id": "product-context-1",
  "target": "product-promise",
  "sources": ["selected-module", "host-participation"]
}
```

Supply the complete return through `propose`. Its body is whatever the actual participant produced, not an inferred answer:

```json
{
  "actor": "product-composer",
  "command": "retain-product-1",
  "op": "propose",
  "id": "product-proposal-1",
  "context": "product-context-1",
  "body": {"statement": "The actual proposed product statement belongs here."}
}
```

If a source reader discovers another affected use, it issues `offer` with the actual target, full finding and inspected source pins. The source reader may also find that the existing arrangement is adequate; no new route is required merely because the interface can store one. Copy pins from actual `create`, `publish` or `show` results; do not invent hashes.

```json
{
  "actor": "source-reader",
  "command": "offer-discovered-relation-1",
  "op": "offer",
  "target": "product-promise",
  "basis": [],
  "account": "The actual source-based reason this receiving use may need reconsideration belongs here.",
  "body": {"finding": "The complete actual finding and its producing question belong here."}
}
```

The empty basis above is valid only when the offer claims no pinned source basis. For source-grounded work, supply the exact nominated pins. The offer response contains its `notice` identity and whether the standing policy makes it required.

The composer can `acknowledge` that notice, inspect it with `notice ID`, and inspect its pending inbox. An old publication remains refused with `reconsideration-required` even after acknowledgment. It retains the original proposal. A new `capture` automatically includes the required offer, current versions of its named sources and the historical versions that the reader actually cited.

The receiver then retains a new proposal, possibly with the exact earlier body where that body remains adequate. It publishes the actual receiving judgment:

```json
{
  "actor": "product-composer",
  "command": "accept-current-product-2",
  "op": "publish",
  "proposal": "product-proposal-2",
  "account": "The actual receiving judgment about the product as a whole belongs here.",
  "treatments": [
    {
      "notice": 1,
      "disposition": "retained-adequate",
      "account": "The actual reason this unchanged result is adequate under the offered interpretation belongs here."
    }
  ]
}
```

Use the returned notice identity rather than assuming 1. Other permitted dispositions are `incorporated` and `not-applicable`. These labels describe the owner's account; they are not automatic correctness judgments. A proposed impact can be settled only by publication at its nominated receiving target. Publishing another target owned by the same actor cannot clear it.

Claims are optional. If multiple invocations of the same owner may compete, issue `claim` with the target's current revision shortly before admission and include the returned `token` in `publish`. A live or expired outstanding claim prevents tokenless admission; acquire a fresh claim after expiry. Semantic construction should ordinarily precede the short reservation. A stale claimant cannot publish through a replacement claim.

## Source selection and scope

`capture` returns full selected bodies and an immutable context. Every current source in that capture becomes a publication guard. Sources named by captured offers are included with their current heads, while their historical cited bodies are also retained for interpretation. Choose a consequential, sufficient basis: unnecessarily broad declarations can create unnecessary coupling. This adapter does not decide that a common constraint is irrelevant simply to keep an edge moving.

Known watches provide mechanical routing of accepted revisions. Directed offers provide mechanical routing of a participant's newly proposed relation. Neither operation discovers the recipient or infers that all affected uses were found. A participant can inspect the retained artifact inventory through known IDs, event history and the surrounding knowledge system; it must still construct consequential missing relations.

A new offer to one target does not mutate a global goal/field source or invalidate every proposal. Targets that share an actually selected changed source still fail their source check. Different output names alone do not establish semantic independence.

An inbox view reports selected records, `remaining_count`, `next_after` and `complete`. Its cursor is a pagination position, not a mark of semantic consideration. `after=0` returns the current pending region from its beginning. A required acknowledged notice remains there until accepted treatment. A bounded empty region does not establish completion of the broader goal or absence of unregistered relevance.

## Bridge to knowledge packages

The separately supplied knowledge system retains source-relative body identities and exact original digests. Its optional wider route is the logical source identity `prototype/knowledge-system/README.md`; this standalone component does not require that sibling directory. A package receipt concerns selected data; it does not create a collective artifact, execution permission or effect authority.

A host may explicitly create a local source artifact containing the package body, complete text and original source identity/digest. For example, its body can contain `source_id`, `source_sha256`, `text` and `import_receipt`. The collective pin hashes that whole JSON body, so it differs from the original text's digest. Keep both. A future source update is a new owner-reviewed local version, not a silent rebase.

A module is a view and a receiving promise, not the exclusive owner of every shared body. Several promises can select the same source artifact. A discovered consequence can address one or several actual promises. Product integration must connect the selected material to the host participation required for its intended effect; package completeness under a declared export rule does not supply that semantic connection.

## Review required work in pieces

Start with ordinary capture and publish when the receiving material fits. For a large existing backlog, or a single offer whose historical and current evidence cannot fit together, the owner can take the following route.

1. Capture the target and the immediate sources for the candidate using `"staged": true`. An existing proposal for the current target can also receive reviews. Propose the actual body to be used.
2. Run `python3 -B collective.py --db PATH next-review PROPOSAL`. Open the returned `notice ID`. For each source in its basis, use `show ID --revision N --content` for the cited edition and `show ID --content` for its current edition. Each read returns one body, its owner and its exact pin. The full revision and its audit history remain available through ordinary `show`.
3. Compare those observations with the actual proposal. Submit `review` with the notice number, the proposal ID, each inspected current source pin, a disposition and the reason it applies. The allowed dispositions are `incorporated`, `retained-adequate` and `not-applicable`. If the evidence calls for changing the proposal, create the changed proposal and judge the affected work against it. Reviews of the other proposal remain available in its command receipts.
4. Publish once the required work has a current treatment. Changes or arrivals between polling and publication are checked inside the write transaction. A successfully published revision names the proposal and the staged count. Open individual notices to recover each original offer and its treatment. For a separate ready-to-use decision, `take` checks both the directly enrolled sources and the source editions used by its staged judgments.

A compact review request can look like this when the offer itself contained the observation and cited no other artifacts.

```json
{"actor":"desk","command":"evaluate-63-on-plan-4","op":"review","proposal":"plan-4","notice":63,"basis":[],"disposition":"not-applicable","account":"The closure concerns the upper bay. This delivery uses the separately inspected lower bay."}
```

A staged judgment leaves the notice pending. An exact command retry returns the earlier receipt. A changed account or source edition uses a fresh command. A new publication revisits currentness and refuses uncovered work in the same transaction that would install the body and settle the offers. The numbered offer anchors the original sources. The staged account and its current source pins identify what this owner used to judge this candidate. A false account can satisfy those structural conditions. The actual receiver still has to measure the intended effect.

### When the same receiving job gets a new approval

An unfinished receiver-use target carries the directly enrolled and reviewed source pins of its ready approvals. Its own predecessor stays available as historical evidence. The current target pin already fences substitution. Holds, edits, new proposal identities and new ready approvals on that target preserve its continuing relations. A separate target has its own enrollments.

When an enrolled source changes, `use-source TARGET` returns a stale prior pin and its current edition. `use-source TARGET SOURCE` inspects a named relation. Reopening has two routes. Explicitly capture and expose the full current source in the newly published proposal. The owner’s publication account then records which prior and current pins it reconciled. Alternatively use `qualify` for one affected relationship at a time. Supply the exact target pin, prior and current source pins, and a reason to renew the source or to release it from this commission. A different application or a false owner statement can still give a poor reason. The system enforces attribution, identity and a fresh issue before use.

A successfully committed native `take` completes this local commission and clears its live enrollments. Earlier use events, original offers, published revisions and qualification events retain the evidence for later investigation. A body merely labelled “taken” cannot end a receiving commission. The gate checks sources admitted by this target. A change that no participant observes or enrolls needs independent discovery.

## Code composition and admission boundary

| Producing code | Required receiving relationship |
|---|---|
| `Revisions.get`, `Revisions.verify`, immutable `versions` | Recover exact prior occurrences and distinguish an unchanged body from a changed acceptance basis. |
| `Proposals.capture`, `Proposals.propose` | Give the participant complete selected material; preserve the actual returned body independently of current admissibility. |
| `NoticeBoard.offer`, `NoticeBoard.watch`, `notices` | Retain declared routes, complete attributed findings and recoverable change delivery. |
| `NoticeBoard.acknowledge`, `ImpactReviews.settle` inside `Session.publish` | Separate arrival from actual accepted reception; bind treatment to the correct target. |
| `Claims.claim`, `Claims.check` | Fence expired or replaced admission reservations for one target. |
| `Collective.call`, `CollectiveDatabase.execute`, `command_receipts` | Check current conditions and commit local mutation, event/outbox records and exact replay result atomically. |
| `view`, `show`, `retained`, `notice` | Return inspectable bounded views with exact recovery rather than a completeness verdict. |

SQLite's [transaction contract](https://www.sqlite.org/lang_transaction.html) permits one simultaneous write transaction. `BEGIN IMMEDIATE` acquires that local write boundary before conditional checks. The implementation uses ordinary rollback journaling. Participant interpretation occurs outside these transactions; database-driven capture, source acquisition and admission occur inside them. Target-specific acceptance conditions therefore coexist with a shared serialized writer. Another target's proposal can remain valid while its write waits for this operation. This does not implement the acquired common-information control theorem or a distributed database.

Ordinary capture reads at most 129 required notices before deciding that its 128-notice context cannot fit. Staged capture can omit that automatic selection. Publication asks the database for a single uncovered obligation or a changed reviewed source. It commits every individual receipt before releasing the writer. Processing a large finite set therefore takes database work proportional to the examined duties and basis. This design bounds the material the application has to receive in one interpretive context. Wall-clock cost, database reads, write-lock time and fairness have separate limits.

Capture now checks the union of current source identities before expanding history and reads each distinct cited `(id, revision)` once. It first charges the complete target-body/notice frame against the existing 2 MiB context allowance. Each historical or current source record is then projected by SQL only if its stored byte length fits the remaining allowance; an oversized record returns a small refusal witness before Python acquires or decodes its body. Complete records, current pins and list separators consume that allowance. The final complete-context check remains in place. Successful ordinary-store captures preserve the original full contents and list order; their output format and source-currentness guards are unchanged.

The joint historical/current source-record payload transferred from SQLite into Python during a fresh capture is therefore at most 2 MiB, and usually less after charging the existing frame. This is a **source-record acquisition bound**, not a whole-call memory or storage-I/O bound. The earlier target read, capped notice reads, request and receipt processing, SQLite length/cast work, Python object overhead and result serialization retain separate costs. The initialized store writes canonical JSON in UTF-8. A corrupted or manually rewritten noncanonical store has no before/after equivalence guarantee. A sufficient count or byte witness can now return before a later source error, including a user-nominated missing current source in an otherwise valid store. Universal refusal precedence is not preserved; earlier ownership and nominated-notice validations remain before these witnesses. Valid offered historical pins identify immutable retained versions. Existing exact command receipts still replay before any new capture work. `captured_at` is now sampled when the frame is formed, before source expansion; it is a capture observation time, not a completion timestamp.

The source projection uses SQLite's established [BLOB byte length](https://www.sqlite.org/lang_corefunc.html#length) and [lazy CASE evaluation](https://www.sqlite.org/lang_expr.html#the_case_expression), avoiding a newer SQLite function or a new schema. These SQL contracts establish the returned application payload; they do not prove that an internal cast avoids all disk reads. A measured repeated-history capture fell from 128 historical payload reads to one while preserving its complete result, accepted treatment and final tables. The logical knowledge source `research/evidence/continuing-obligations.md` retains the comparison and its scope.

Every admitted command, including an operation-level refusal, retains an exact receipt. A savepoint rolls back partial operation writes before recording a refusal. The store makes no external effects. An error before a receipt is written remains unresolved until the caller inspects or retries the exact local command.

## Executed checks and boundaries

Run the concrete contract checks from this directory:

```sh
python3 -B -m unittest -v test_collective.py test_staged_receiving.py test_receiver_use.py
python3 -B run_study.py --directory /absolute/new-study-directory
```

The sixteen checks retain the ten earlier contract cases covering stale sources with proposal preservation, unchanged source/new receiving duty, acknowledgment without uptake, optional offers, wrong-target clearance, exact response recovery and command conflict, expired claim fencing, watcher recovery, bounded inbox omissions, malformed input rollback, and two processes competing for one claim. Three cases cover the required-notice retrieval ceiling, nominated-notice validation and exact refusal replay with pending duties preserved, and complete capture at the admitted boundary. Three further cases cover complete repeated history without repeated acquisition, distinct-history overflow with exact refusal and preserved duties, and byte-based admission of multibyte current sources before decoding. The original admission comparison retains complete before/after public results and database states at 128, 129 and 4,096 required notices. The later source comparison checks five distinct history/current-source cases; neither is the twelve-process study below.

The separate mechanical study starts twelve OS processes, each capturing and retaining a contribution before admission. A scripted new impact addresses one target while the source bytes and revision stay unchanged. Eleven other targets publish; that target's old publication is refused. Its later fresh context accepts the same proposal bytes with a receiving account, and exact retry recovers the recorded result. The study does not contain twelve independently interpreting agents and does not establish semantic discovery, whole-product adequacy, throughput advantage or enterprise scalability.

This version deliberately bounds each body to 256 KiB, each request/capture to 2 MiB, source and selected notice sets to 128, and each view to 100 entries/1 MiB. Exceeding the default capture bound leaves the duty pending. Proposal-specific reviews supply the alternate receiving path for count and source-byte overflow. Long-running scheduling, claim renewal, policy changes, authentication, remote transport, fair dispatch, automatic relevance discovery, garbage collection and cross-store atomic acceptance are not implemented. Those needs require an actual further receiving arrangement. No limit or empty queue is evidence that all research has been covered.

The existing shared-workspace, change-signal, coordination-boundary and frontier patterns already explain the generic mechanisms. Optional wider knowledge routes are the logical source identities `research/patterns/cards/shared-workspaces.md`, `research/patterns/cards/change-signals.md`, `research/patterns/coordination-boundaries.md` and `research/patterns/cards/frontier-navigation.md`. Discover and acquire those complete bodies through an available knowledge system when their participation matters; they are not filesystem prerequisites of this standalone package. This adapter is a scoped engineering realization. Actual participants' discovery, reception and later integrated use determine the research result.

## When the receiving party is still unknown

An owner can put one short observation or unresolved need where participants in this shared store can encounter it. Create or revise the underlying artifact first. Submit an `announce` command carrying your actor and command identities, the exact current `artifact` pin (id, revision and sha256), and an `account` of up to 2 KiB. Explain what you observed or need, the conditions, and one consequence someone could investigate. Retain more detail in the source. A successful announcement creates an event without naming a receiver or creating an inbox obligation.

Run `collective.py --db STATE encounters --after CURSOR --limit 4 --scan-limit 32` to inspect a bounded slice. Use the initial cursor 0 for the beginning. Save `next_after` when the returned slice has been considered. A slice can have no announcements and still have more events. The reported cursor belongs to this store. A crash before saving it can show the same event again; use its event identity and issuing revision to recognize that delivery. Continue looking beyond the known contacts when an important use remains unmet. Limited attention, lack of a shared store or an owner who has not posted can all leave work undiscovered.

A participant who can explain how an observation and a need relate can retrieve the source with `show ID --revision REV`, compare its current edition, and send an ordinary addressed `offer` to the discovered target. Keep distinct duties and unrelated material available. The recipient decides whether the offer changes the actual work. The event feed has no semantic ranking or automatic acceptance. It shares the existing local store's trust model and application-owned authority. Its accounts are visible to the store's participants.
