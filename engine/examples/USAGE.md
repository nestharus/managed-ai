# Receiving example

`receiving_example.py` is a standard-library host example for the preserved G60
root `collective.py`. It requires the engine's Python 3.11+ / SQLite 3.38+
environment. The caller supplies actor authority, actual candidate and observation
bodies, and an explicit semantic reason for this receiving use. The example does
not execute the candidate or judge the account's adequacy.

## Prepare

Import `prepare(engine, db, spec)` or run:

```sh
python -B receiving_example.py --engine /path/to/prototype --db /path/to/runtime/use.sqlite --operation prepare --input prepare.json
```

`prepare.json` supplies the following fields. Substitute actual bodies and an
actual account; neither payload has a required domain or internal schema.

```json
{
  "namespace": "a-unique-receiving-job",
  "producer": "candidate-owner",
  "receiver": "receiving-owner",
  "candidate": {"purpose": "The actual proposed work, result, and source conditions"},
  "observation": {"use": "The actual receiving use", "account": "What this receiver established and what remains"},
  "account": "The caller's actual semantic reason to approve this use",
  "sources": [],
  "reconsider_from": []
}
```

The database and retained request/transport files must be outside the engine.
An absent store is initialized through its root CLI. The namespace deterministically
names distinct candidate, observation, approval, context, proposal and command
identities. The candidate belongs to `producer`; the observation and approval
belong to the different `receiver`. Their supplied JSON bodies remain intact.

`sources` lists existing additional source artifact IDs. Complete current bodies
join the capture and its publication/use guards. `reconsider_from` declares the
actors whose addressed findings require reception at this approval target; the
core also supports `["*"]`. No notice is automatically treated or settled. A
required finding can refuse publication or later admission, leaving its duty
pending for an actual receiving judgment through the core.

A successful result contains `candidate`, `observation` and `approval` pins,
`sources`, the retained `context` and `proposal` identities, and the actual
publication `receipt`. Its `status` comes from that receipt. The ready approval
uses `g60-receiver-use/1`, names the two source roles, records the exact captured
source pins, and preserves the supplied account in both body and publication.
The core owns the enrolled source relationships.

On an underlying refusal or unresolved result, preparation stops and returns that
actual outcome. Earlier accepted steps remain retained; preparation is not one
transaction. Do not silently retry after an uncertain result or supply new
semantics under an old namespace. An exact repeated preparation recovers its
historical operation receipts, including an earlier publication; it does not
assert that the approval is still usable now.

## Admit later

Import `take(engine, db, spec)` or run:

```sh
python -B receiving_example.py --engine /path/to/prototype --db /path/to/runtime/use.sqlite --operation take --input take.json
```

The request contains the receiving owner, a stable command identity for this
exact attempt, and the actual returned approval pin:

```json
{
  "receiver": "receiving-owner",
  "command": "this-exact-admission-attempt",
  "approval": {"id": "COPY_RETURNED_ID", "revision": 2, "sha256": "COPY_RETURNED_SHA256"}
}
```

`take` returns the native core receipt unchanged. A successful receipt identifies
the actual `received-use` event, exact approval, candidate and observation pins,
and the installed taken revision. The core rechecks current source editions,
approval identity, required findings, roles, and any claim inside its local write
transaction. Changed sources, a superseded or consumed approval, or a required
finding retain their actual refusal.

The same actor/command with the same request recovers its prior local result.
A different command cannot freshly consume an already consumed approval. A
changed request under an existing command conflicts. For uncertain delivery,
inspect the authoritative result through:

```sh
python -B /path/to/prototype/collective.py --db /path/to/runtime/use.sqlite receipt --actor receiving-owner --command this-exact-admission-attempt
```

A root receipt lookup that recovers a recorded refusal returns that refused
dictionary with exit code `2`; this is a found historical result.

Only a successful admission receipt supports consumption at this local receiver
and target. Retrieve its pinned payload using `show ID --revision REV` through
the same root entry. The host must interpret and consume the declared result.
A later effect in another system needs that system's admission and deduplication,
using the received-use event identity; a replay is not permission to repeat it.

CLI exit codes are `0` for accepted, `2` for refused, and `3` for unresolved.
`--input -` reads stdin; an inline JSON object is also accepted. Request and
transport JSON are retained in a sibling `DATABASE.receiving-example` directory
for inspection. They never replace core state or receipts. There is no automatic
retry, authentication, SQL access, core modification, or external action.

## Resume an unfinished target

Import `resume(engine, db, spec)` or use `--operation resume` with the same CLI
arguments. This continues the existing target after a hold or changed receiving
account. It does not create a substitute target or automatically admit the use.

```json
{
  "receiver": "receiving-owner",
  "command_prefix": "a-new-exact-continuation-attempt",
  "target": "ACTUAL_EXISTING_APPROVAL_ID",
  "candidate": "ACTUAL_CURRENT_CANDIDATE_ID",
  "observation": "ACTUAL_CURRENT_OBSERVATION_ID",
  "sources": [],
  "account": "The caller's actual reason to approve the changed use",
  "source_decisions": [],
  "treatments": []
}
```

Supply current artifact IDs, not invented pins. Candidate and observation bodies
remain untouched. `sources` deliberately selects additional complete current
bodies for the new capture. A warranted receiving account with that current
capture can reconcile a changed continuing source relationship. Omission does
not release an older relationship; the core preserves it through holds and new
proposals. A ready publication can therefore still fail the later `take`.

For each explicitly judged relationship, `source_decisions` may contain
`{"source":"ACTUAL_ENROLLED_SOURCE_ID","disposition":"renew","account":"ACTUAL_REASON"}`
or the same shape with `"disposition":"release"`. The program reads the actual
target, enrolled prior occurrence and current full source through the public
entry, then calls native `qualify` with those exact pins and your account. It
returns each actual qualification receipt and publishes a fresh ready decision.
A release removes that continuing relationship for this target under the stated
judgment; its history remains. Selecting the source again in the new capture or
using it as finding evidence can enroll it again under the core's normal rules.

For each actual finding judgment, `treatments` supplies its returned numeric
`notice`, one of `incorporated`, `retained-adequate`, or `not-applicable`, and the
actual `account`. Those notices are explicitly captured and the exact treatments
are passed to native publication. An empty list settles no required finding.
Wrong-target treatments, new required findings and changed source conditions
retain the core's actual protections.

Success returns current candidate/observation pins, the new approval pin, the
actual publication `receipt`, context/proposal identities, captured `sources`
and actual `qualifications`. Captured `sources` describes this capture; it is
not a claim to enumerate every continuing enrollment. A later `take` still owns
actual admission. A failed stage returns its actual refused/unresolved outcome
and stops; earlier accepted qualifications remain part of the core's history.

Keep the sibling request directory with the database. It retains this exact
resume specification and first qualification requests, including their observed
pins, so an explicit repeated invocation sends those same requests to the core.
It never substitutes a local success flag for a core call. Different input under
the same receiver/`command_prefix` is unresolved; use a new prefix for a changed
judgment. Exact repetition can recover a historical publication, even after it
has been superseded or consumed. It grants no fresh use or external effect.
