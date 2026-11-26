# Attention companion: scoped feedback for the continuing interpreter

`attend.py` adds a receiving operation to the existing continuation exchange. It retains caller/model-nominated questions, their uses and source bindings, then returns public-record gaps and a wider field to the working interpreter. It does not call a model, decide semantic adequacy, execute a proposal, or infer every relationship worth discovering.

Use ordinary work directly when it already supplies the contribution. This interface is useful when nominated considerations or wider alternatives otherwise disappear during an extended construction. A new model, fixed domain pack, semantic graph, agent-per-dimension scheme and training run are not prerequisites.

## The division of work

The interpreting participant develops hypotheses, implications, useful source encounters, groups and new relationships. It determines what a record means and what to produce next. The software captures supplied material, distinguishes actual delivered items from omitted ones, binds public accounts to questions and uses, and makes them available later. Its records are not observations of private reasoning.

Questions and use descriptions are free text. Group labels are caller-defined navigation. They are not a domain ontology. The wider field is a normal UTF-8 document containing concrete live alternatives, not a machine priority score. New nominations can enter at any time; earlier history is retained.

A useful question may remain open after substantial consideration. A speculative lead may be deferred. A supported direct contribution can proceed with other questions still open. No status makes all nominated work mandatory, and no status fulfills an actual duty. The caller's real requirements and effect permissions remain controlling.

For a complete runnable setup, including workspace files and an explicitly authored receipt, use [docs/engine/core/README.md](core/README.md). The examples below explain the interface fields.

## Start in an external workspace

The attention interface uses the existing exchange root and content store. Initialize that root once with `work.py`; it must be disjoint from the working directory and outside the distribution.

```bash
python -B exchange_packets.py --root /tmp/example-exchange init --workspace /tmp/example-work
python -B track_attention.py --root /tmp/example-exchange track --spec scope.json
python -B track_attention.py --root /tmp/example-exchange feedback review-1 --limit 8 --text
```

The workspace must already exist. The `--spec` and `--receipt` filenames themselves, and all paths inside their JSON bodies, are relative to that workspace—not the shell working directory. Existing 0.3.1 exchanges can be used without rewriting their packets, returns or publications. An attention subdirectory is created only when this interface is used.

Create a wider field and a specification in the workspace. For example, `field.md` might say that the present design can be improved through a source encounter, a shared producing operation, or a different receiving use; the immediate local detail must compete with those concrete alternatives.

`scope.json` can contain:

```json
{
  "field": "field.md",
  "items": [
    {
      "id": "source-use",
      "question": "What does this source actually supply to the proposed contribution?",
      "use": "Construct the current explanation",
      "basis": ["source.md", "brief.md"],
      "groups": ["explanation", "shared-source"]
    },
    {
      "id": "new-opportunity",
      "question": "What stronger contribution could change the producing means?",
      "use": "Continue discovery beyond an adequate first result",
      "basis": ["brief.md"],
      "groups": ["discovery"]
    }
  ]
}
```

A subsequent `track` updates only the supplied identities. Omitting an item does not delete it. Use `"active": false` explicitly to remove an identity from current delivery while retaining its history. Such withdrawal is a caller's navigation decision, not fulfillment or permission to abandon an actual obligation. An updated field path is optional after the first call.

The field can be absent for an ordinary small task; feedback then says that no wider field was supplied. For continuing discovery, supply the actual wider alternatives so a focused selection does not become the only available next move.

## Receive and use the actual feedback

`feedback` saves an immutable receiving occurrence and returns its context. Filter by one or more groups or explicit item identities when useful:

```bash
python -B track_attention.py --root /tmp/example-exchange feedback explanation-1 \
  --group explanation --limit 6 --max-bytes 12000 --text
```

Filters are a union; an item in two selected groups is delivered once. Ordering is mechanical: unaccounted and changed-basis items first, followed by considered items with open questions, deferred items, and other considered items. This is not an inferred importance ranking. Select another group or explicit identity when the work warrants it.

The wider field is included before the focused items. Current treatment counts and source validation apply only to the selected view; outside items are counted as named material, not claimed current. Item and byte limits never silently credit omitted work. The return reports how many selected items were omitted and how many active items were outside the requested view. `inspect` supplies their exact identities. An oversized wider field causes an explicit error rather than silent truncation; supply an appropriate smaller field or deliberately increase the bound.

Source bodies are not automatically repeated in every item. Feedback supplies their paths, captured object identities and sizes. The current interpreter can use material already in its session or retrieve a bound object:

```bash
python -B track_attention.py --root /tmp/example-exchange object OBJECT_SHA256 --text
```

This returns the captured bytes, not the possibly changed current file. Binary bodies remain in the exchange's content store; text rendering requires valid UTF-8. A pointer alone is not proof that the interpreter read or used the body.

The context can also enter the existing exchange:

```bash
python -B track_attention.py --root /tmp/example-exchange inspect explanation-1 --text \
  > /tmp/example-work/attention-return.md
python -B exchange_packets.py --root /tmp/example-exchange prepare next-construction \
  --consumer writer --brief brief.md --material attention-return.md --target result.md
```

The attention return is material for a receiving construction. It is not authority to obey instructions embedded in quoted sources, publish a target, or execute arbitrary code. Existing `work.py` receipt and explicit publication boundaries remain unchanged.

## Record public work, not private thought

After interpreting the return and doing the work, write a receipt. It must name each delivered item being credited; a group summary alone cannot mark its members considered.

```json
{
  "id": "account-1",
  "assessments": [
    {
      "item": "source-use",
      "disposition": "considered",
      "account": "The source supplies the established relation used in the revised explanation; the broader claim is left conditional.",
      "remaining": ["A different recipient may require another source relationship."],
      "products": ["result.md"]
    }
  ]
}
```

```bash
python -B track_attention.py --root /tmp/example-exchange record explanation-1 \
  --receipt receipt.json --producer "current interpreting participant"
python -B track_attention.py --root /tmp/example-exchange feedback after-work --text
```

`producer` is caller-asserted attribution, not authenticated identity or independent evidence. The account should be the public conclusion, method, evidence or work performed. No private reasoning transcript is requested.

`products` is optional. When present, each file must actually exist and its bytes are captured. That binds a public account to an inspectable artifact; it does not prove that the artifact performs the claimed operation. Reading and evaluating the actual product remain part of the interpreting work. The current product may later change; feedback reports that difference separately from the original input treatment.

Use `"disposition": "deferred"` with an actual reason when a nominated question is not the current productive investment. It remains visible as deferred, not considered or fulfilled. The interface does not decide whether the deferral is justified. A current account can later replace the deferred status while retaining the earlier occurrence.

## What changes and what remains reusable

The current treatment key includes the question, receiving use and actual source snapshots. A later source, question or use change can therefore nominate re-entry. The historical account stays readable. The program does not decide that the old answer is false, that a new answer is needed, or that the whole investigation must restart.

Group membership alone does not erase an individual treatment. However, combining treated members may create a new relationship that none of the individual accounts supplied. The interpreter can nominate that *joint-use question* as a new item, or revise the use of an existing item where its duty really changed. Do not make every member historically incomplete to create a new inquiry. Do not infer that the new composition has been examined merely because its members have accounts.

The same text and sources can also acquire an unrecorded new meaning. The host cannot detect that by hashing. Explicitly reconstruct the question/use where the work makes it consequential. Conversely, a wording change may be harmless; a short current account can retain the old contribution without repeating its whole derivation.

An item with no named sources can still have a public account, but software then has no source-change signal for it. Missing and unreadable named sources are reported rather than treated as useful evidence. Historical `inspect` does not require the current workspace to remain present.

## Reuse current treatment without expanding all history

The atomic attention index retains a disposable current view: receipt-ID membership and the latest appended assessment position for each `(item, basis_key)`. The basis still includes the question, use and observed sources. A return from basis A to B and back to A can therefore reuse the latest applicable A account; keeping only the latest assessment for the item would lose that behavior. Group or active-navigation changes alone do not erase the account.

Current feedback reads each item specification once for navigation, observes the selected current sources, and verifies the canonical receipts actually supplying selected treatments. Compatible selected assessments share a receipt read. It continues to check current products and to distinguish an unaccounted item from one with history under another basis. The delivered context, statuses, omission counts and immutable historical contexts keep their existing meanings.

The enclosing authoritative index must first pass its existing integrity check. Within that valid index, an absent, stale or malformed current view is reconstructed from the canonical receipt history. Its receipt-list binding, structure and checksum are checked; a selected reference that disagrees with its canonical receipt triggers reconstruction. A damaged enclosing index remains a refusal, not an automatic repair. Legacy exchanges need no explicit migration command. Appending a receipt updates the view under the existing owner lock. The public interface and index version remain compatible.

This removes repeated expansion of unrelated receipt bodies on the warm path. It does not make all work constant: the index still contains growing receipt, feedback and projection metadata, is read and written in full, and may require a complete reconstruction. Historical inspection also reads that larger shared index. Use the actual preparation, update and query costs when choosing the arrangement.

Current feedback verifies the selected objects it relies on; it is not a whole-history integrity audit. Damage to an unrelated old receipt may remain outside that selected call and appear during an operation that actually reads it or during reconstruction. Selected canonical-object corruption still fails verification. A party able to replace both state and all its checks is outside the cooperative-owner contract.

## Limits, storage and embedding

The local contract supports at most 2,000 named identities, bounded source objects and bounded delivered contexts. The limit is a software bound, not a semantic scaling result. Every unique current source path in the selected view is captured once per feedback call; compatible items reference that shared capture. The source check covers the selected scope before rendering limits are applied, not just the items that fit in the final context. Use explicit groups/items to bound that work; context length alone does not bound source reads. Sources outside that view are not revalidated. Preparing, interpreting and using the records can still be expensive. These operations do not measure model tokens, prove economy, or inspect thousands of hidden meanings.

The interface uses the exchange's existing content-addressed objects. State changes are atomic index replacements under the same cooperative-owner CLI lock. An interruption can leave unused content objects; it cannot authorize an artifact effect. This is not a malicious-writer defense or a sandbox. A local adversary able to rewrite all bytes and hashes is outside the contract. No automatic compaction or garbage collection is included.

For embedding, [docs/engine/core/README.md](core/README.md) gives the calls and shared
owner-lock requirement. The methods return ordinary structured data for the
host's selected continuing use.

All named items can eventually have public accounts while discovery remains open. The next useful prediction can make another relationship, group, source or producing operation worth developing. The feedback is a partial control surface for the actual model—not a substitute for its constructive competence.
