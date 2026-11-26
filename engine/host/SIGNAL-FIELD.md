# Continuing observation service

`signal_field/queue.py` is an optional host adapter for **receiving uses**, their supplied observing agreements and recoverable service. It is not an observer library, an all-dimension checklist, an effect authority, or a provider client. One use may consume several dimensions; one dimension may require several local, aggregate or global observations. The writing loader owns those meanings and the qualified coverage projection.

## Responsible change and existing means

The maintained Attention implementation captures caller-nominated questions, complete selected source bindings, actual public treatment and omitted-item identities. `Attention.feedback` orders by status and then bounds delivery. It has no persistent service age across repeatedly changed early items. Its current metadata and source scans also retain costs outside the bounded returned context. Keep that interface's meaning intact.

The new host adapter supplies the missing continuing pull/receive relationship. It can sit beside `engine/host/continuing_work/continuing_work.py`, which currently prepares and reconciles explicit scopes but does not call a model or schedule semantics. `engine/host/material_retention/managed_material.py` still owns byte custody and selected retention. `engine/companion_representation/representation.py` still binds authored construction to native captured inputs and guarded adoption. Existing `collective-work` watches, required offers, cursor inboxes and fenced claims remain adequate mechanisms for actual shared source/receiver admission. This queue does not replace them or grant permission for an external effect.

The proposal follows existing `research/patterns/collective-knowledge.md` (supply invocation explicitly), `continuous-consolidation.md` (give each receiving use another turn), `cards/change-signals.md` (recoverable changes and current admission), `retaining-useful-state.md` (retention by receiving use), and `scoped-understanding.md` (partial qualified views and signal lifetimes). No domain name or dimension count occurs in producer behavior.

## Input and output boundary

A domain loader supplies source observations and use definitions as data:

```json
{
  "sources": [
    {"source":"current-field", "epoch":"owner-instance", "position":5,
     "value":{"path":"work/field.json", "sha256":"actual reviewed body pin"}}
  ],
  "uses": [
    {"id":"whole-dependency-view", "mode":"live",
     "question":"What does the current qualified dependency field imply for this receiving task?",
     "purpose":"Support the declared broad review and locate consequential disagreements.",
     "sources":["current-field"],
     "observer":{"definition":"complete domain-owned instrument route", "consumes_dimensions":["supplied identities"]},
     "scope":{"coverage":"the defined artifact/relations", "coordinate":"manuscript position"}}
  ]
}
```

The example's pin is a placeholder. Enroll actual selected source pins. Values, observer definitions and scopes are arbitrary finite JSON supplied by their responsible owner. A pin or route does not establish that a worker read the referenced body. The worker must recover the complete units required for its actual question and return qualified evidence. Acquisition, applicability, observation, unresolved disagreement and semantic acceptance remain separate domain records.

The generic enrollment fields are exactly `id`, `mode`, `question`, `purpose`, `sources`, `observer`, and `scope`. A change to the observing definition, purpose or scope changes its contract identity. A change to a declared source changes the live receiving cut. Source position is monotone within a caller-owned stream epoch; a different epoch requires an explicit reset. Older positions cannot become current. This contract assumes the source adapter actually supplies authoritative positions and epochs; asserted strings are not authentication.

`pull` returns one eligible use, its complete supplied definition and current declared source cut, plus a generation-bound attempt token. It does not expand every referenced source into a giant prompt. Complete material acquisition belongs to the configured observing worker. The token only correlates queue service; native publication and actual effects keep their existing owner checks.

A worker returns JSON such as:

```json
{"state":"complete", "account":"Actual scoped work and its limitations.",
 "observation":{"definition":"actual instrument identity", "coverage":"actual coverage", "result":"actual supplied result"},
 "nominations":["actual new shared-use proposals, if any"]}
```

`state: complete` means the selected service returned. It does not certify observation quality or all-dimension coverage. `state: blocked` preserves an explicit unresolved receiving account. Other result fields are opaque. A domain consumer must inspect and accept actual results, update its coverage projection, and admit new source/use nominations. The queue never infers coverage from a delivered item, source identity or worker count.

## Lifetimes

| Receiving mode | Pending behavior | What survives |
|---|---|---|
| `live` | Coalesce repeated rechecks to the newest complete declared basis; preserve the oldest pending ticket until actual service. | Current qualified return, at most one pending use, and its most recent rejected-return account. |
| `historical` | Capture the original source cut when enrolled. Later source changes do not rewrite that cut. | The actual returned historical account. Explicitly named history can also retain a live observation needed for a later use. |
| `each-change` | Keep a distinct job for each declared source occurrence. Withdrawal and restoration remain two obligations even if the latest value is unchanged. | Every outstanding occurrence or missing-replay condition until its own receiving service. |

These are different consumption contracts, not three mutually exclusive meanings of an entire source. The same external report can supply all three receiving uses. Live invalidation coalescing must not stand in for a threshold-crossing detector, an occurrence-counting trend or a correction owed to another participant.

A source update and its each-change jobs commit together. `notify: false` can simulate a missed optional live wakeup; it cannot discard required occurrence jobs. `reconcile` compares live uses against their current supplied source cut and reconstructs missing rechecks. If an occurrence stream jumps over positions or resets, the adapter retains an explicit blocked replay gap. A latest snapshot cannot reconstruct the missing intermediate occurrences. This version does not implement replay-gap discharge; recover the actual required events through the source owner or supply a reviewed duty-change operation before extending that interface.

There is no universal TTL. Source validity, observer applicability, interpretive uncertainty, a particular effect's authority and retention for historical evidence can expire differently. When an actual use consumes clock time or an expiry boundary, the host must supply that temporal condition as a dependency or check it at the effect owner. This adapter has no timer daemon and does not infer elapsed-time validity from unchanged bytes.

Trend coordinate belongs in the observing agreement. Position within a manuscript, revision number, observation time and reader experience are different axes. A revision series cannot silently be labeled a within-book arc, and repeated agent accounts are not independent reader measurements.

## Repeated service, restart and changed work

`enroll` admits dynamic shared uses. `change` visits only enrolled reverse dependencies for that source. `pull` takes the oldest eligible ticket under a local SQLite write transaction. Updating an already pending live use changes its desired basis without moving its pending ticket. Actual service moves a newly changed obligation behind older waiting work. A repeatedly hot early subset therefore cannot hold every bounded turn merely by remaining in `changed-basis` status.

A returned live result must match the current use, definition, generation and declared source cut. A stale result cannot overwrite the live observation. If a live wakeup was missed while the callback ran, its actual stale return refreshes that affected job to the current declared basis before further service; it preserves an already pending ticket. Its queue account is retained once for diagnosis; the complete returned candidate belongs to its actual native owner when it has continuing use. A retry explicitly replaces the attempt token, so a late former worker cannot consume the new attempt. This is queue fencing, not downstream effect fencing.

Outstanding jobs, the exact captured in-flight packet and eligibility survive process restart. `pending` supplies cursor-based bounded job recovery, including the old captured attempt and any newer desired basis. `inspect` recovers one selected use, job or named historical account and reports exact declared-basis/definition matches. These are trusted host operations, not semantic validity or effect authorization. Output counts/bytes are bounded; database work and acquiring a selected record remain separate costs. An in-flight worker remains visible after restart; the host must inspect its actual outcome before selecting `retry`. Do not blindly repeat an external effect. The existing collective/native receipt and admission mechanisms own that recovery boundary.

The host can call `run` with explicit worker `argv`, an invocation budget and per-call timeout. The worker receives one JSON packet on standard input; the subprocess uses no shell. The adapter reconciles the current live field, repeatedly pulls and receives, and stops on the actual known state: budget exhaustion, in-flight work, blocked work, or caught-up service for the current enrolled field. It never reports global semantic completion. A timeout, failed process or malformed worker return preserves the in-flight attempt and returns `worker-unresolved`. The host must inspect the actual callback/native outcome before explicitly selecting a retry; the runner never automatically repeats an uncertain invocation. The timeout must be finite and positive before any work is claimed.

A fresh new source or coverage revision can make work eligible again. Newly discovered shared uses enter through `enroll`. A capable worker may publish an accepted observation through the existing native owner; the host then admits its new source pin with `change`, waking dependent uses. Shared observation reuse is explicit source/consumer data. No one-job-per-dimension plan is required.

### Actual invocation remains an installed capability

A Python queue cannot call a model merely because its record says “continue.” The host must provide a permitted invocation adapter. The retained scheduling study used a synthetic worker to exercise subprocess delivery; its historical fixture is not an installed model or a callable operation in this package. An active ChatGPT session can serve the returned work using its actual tools while the session runs. A persistent external runner requires its own installed model/provider adapter, lifecycle, permission and budget. This contribution does not claim background execution after chat ends.

## Aggregation and updating

The domain owns aggregation semantics: instrument/unit identity, coverage denominator, temporal or spatial coordinate, overlap/deduplication, uncertainty, alternatives and the combination's receiving purpose. The host retains and routes those contracts; it does not average arbitrary semantic scores.

A current aggregate must have enough state for its allowed future updates. An append-only mean can retain sum and count. Arbitrary named-contributor retraction needs the removed contribution and membership, supplied either by retained contributor state or by a trusted removal delta. Two sets `{x:0,y:10}` and `{x:2,y:8}` have the same mean, sum and count; removing `x` yields different means. A low-dimensional current value alone is therefore insufficient for that update contract.

A graph factor can require topology or higher-order relationships that scalar local summaries discard. Preserve the needed relationship sufficient for the selected global question, or make missing coverage explicit. Component freshness alone does not establish that a compound result was formed from a compatible semantic cut. Aggregating observations must retain their qualified underlying bases and disclose any mismatch or lag. If a current whole claim depends directly on raw-source currency before local rereading completes, enroll that dependency or the required receiving offer at the whole consumer; a future leaf update does not retroactively install it.

## Cost and retained state

The actual SQLite operation counter counts executed adapter statements; returned-row counts count Python-visible selected rows. They are not disk-page reads, provider tokens or total interpretation cost. A fresh `reconcile` scans all enrolled live uses and their supplied dependencies. Incremental `change` follows an indexed source-to-use relation. `status` scans the known pending region and returns counts. These costs are explicit; bounded worker packets do not imply bounded total work.

Current live pending storage is bounded by enrolled live uses. Historical selections grow when deliberately retained. Distinct unserved occurrences grow with their actual owed rate. If required occurrences arrive faster than they can be served, no lossless fixed-size queue can promise eventual service without another premise—more service capacity, backpressure, a changed receiving contract or an admissible sufficient compression. The adapter preserves the debt and reports the blocked/budget state rather than claiming all signal lifetimes cost O(dimensions).

The SQLite file can retain allocated pages after rows are deleted; current row counts are not physical shrinkage. Selected history, current observations and still owed occurrence support have different retention rules. Do not retain every old queue delivery or input snapshot by default. Actual native evidence remains selected by its receiving purpose.

## Retained evidence and current invocation

The received scheduling study (`stores/research/work/findings/signal-field-evidence/runtime-stress.json`) and runtime receiving account (`stores/research/work/findings/signal-field-evidence/runtime-receiving.md`) preserve the actual earlier result. The study used the loaded 110 registry IDs only as a **metadata scheduling workload**. It compared the acquired Attention ordering with carried eligibility and exercised restart, withdrawal/restoration, late source/use/attempt returns, missed live wakeups, missing required occurrences, blocked-neighbor progress and subprocess delivery. Its larger workload used 1,100 metadata uses, including synthetic identities. These are historical engineering observations, not a new execution or a 110-dimensional manuscript evaluation. The discarded study runner and worker fixture are not package commands.

For actual enrollment, use the `Field` API under its local owner or send one CLI request:

```bash
python3 -B engine/host/signal_field/queue.py --db /absolute/host/signal-field.sqlite --request /absolute/operation.json
```

An operation is `{"op":"enroll","spec":...}`, `{"op":"change","source":...,"epoch":...,"position":...,"value":...}`, or `{"op":"run","argv":["/absolute/permitted-worker"],"budget":5,"timeout":60}`. A budget is a resource bound, not a mandated five-dimension batch. `pull`, `receive`, `retry`, `retain`, `pending`, `inspect`, `reconcile` and `status` are also available. A change of receiving lifetime requires a distinct use identity so that an existing owed lifetime is not silently repurposed. Keep the runtime database outside shipped source; the queue is one host's continuing state, not another copy of the core engine.

For one actual caller-brokered service, send an operation with only `op: pull` and retain the complete returned packet. A null return means no eligible job at that cut. Give the packet's exact source/use agreement and the complete required material to the capable participant. Its actual result must contain `state` (`complete` or `blocked`) and a nonempty `account`; other fields retain domain-owned observations or nominations. Then send `op: receive` with `packet` equal to that entire unchanged pulled object and `result` equal to the actual result object. The returned `status`, `service_state` and `current_contract` describe host receiving, not semantic acceptance. Never reconstruct a token or replace the captured packet with a current status view.

When the contribution requires publication, inspect and complete the native owner's adoption before acknowledging that effect in the host account. After interruption, inspect the original native publication and in-flight host attempt before retrying. The writing [accepted-return preparation contract](../../domains/writing/operations/SIGNAL-CONTINUATION.md) stages the domain request/field/host translation; it leaves those distinct effect owners and actual host-state checks in place. A domain owner must explicitly admit accepted nominations. A quiet host cannot infer that an unrepresented signal or still-owed duty is complete.
