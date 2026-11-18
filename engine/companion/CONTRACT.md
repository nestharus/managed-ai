# Scoped representation operations

This application adapter makes an accepted partial representation an actual
input to a native producing turn. Its meanings and candidate changes are
authored by the capable participant. It does not discover dimensions, judge
their application, generate a candidate, infer independence, or certify a
realization. Arbitrary complete JSON is permitted. Domain schemas belong to
the current artifact, not to this adapter or the native core.

## Choose a useful scope

Representations are ordinary artifact files adopted through the native owner.
Select only the files whose current state the operation uses, together with
the actual source bodies and prior application witnesses needed for this use.
The representation list is not an entire project or source-bank inventory.
A working scope can overlap several source applications. It need not be a
permanent grouping or a file per dimension.

The unit of mechanical currency is a selected file. A change to an unselected
file does not invalidate this turn. A change anywhere in a selected file does:
retain the return and prepare its fresh use under the existing native contract.
Split a large artifact into meaningful reusable files when that serves its
producing relationships; do not pretend separate files prove independence.
State known interactions and unknowns in the operation. Shared goals, fields,
obligations and explicitly selected material still participate in native guards.

## Prepare the actual operation

Call `representation.py --engine PROTOTYPE --root RUNTIME prepare --spec FILE`.
The specification has format `g60-representation-operation/1` and these fields:

- `turn`, `task`, `consumer`: the actual native occurrence and receiving task.
- `mode`: `develop` for an initial representation, `transform` for a changed
  partial representation, or `realize` for an artifact produced from accepted
  representation. These identify this adapter's binding requirements.
- `representations`: selected `{path, sha256, accepted_in}` inputs. Each path
  is relative to native `work/`; its exact JSON bytes must match a completed
  native adoption in `accepted_in`. Transform and realize require inputs.
- `sources`: exact `{path, sha256}` pins for selected source/application bodies.
- `applications`: one or more objects with unique `id`, `sources` (selected
  paths), `representations` (selected paths), authored `operation`, and
  prospective `prediction`. Arbitrary extra content can retain source scope,
  a contrast, a backward gap, qualifiers, or another useful relationship.
  Transform and realize applications must name an accepted representation.
- `targets`: ordinary artifact paths with existing parent directories.
- `interactions`: an authored `account` and a list of `unknowns`.
- Optional `native`: existing native context selectors such as `records`,
  `compositions`, `attention_items`, `returns` or `artifacts`. It cannot replace
  the bound task, consumer, sources or targets.

The adapter checks requested pins and completed acceptance, captures the full
selected representation files and source files through native `context`, and
captures this contract and the authored operation. Inspect/recover that actual
native context before producing the return. An input description alone is not
evidence of semantic use.

`pin PATH --accepted-in TURN` checks and prints an accepted representation pin.
Omit `--accepted-in` for a source pin. `template TURN` prints the exact required
witness bindings with blank accounts; the participant must supply actual work.

## Receive and adopt the actual contribution

The participant produces candidate JSON for develop/transform, or the intended
artifact format for realize, plus an actual public reply and a witness. Call
`receive TURN --reply FILE --witness FILE --producer NAME --proposal TARGET=FILE`.
Repeat `--proposal` for each selected target. A turn selects only targets for
which an actual candidate is being returned; unresolved independent work can
remain outside it with its duty retained.

The witness has format `g60-representation-return/1`, the captured
`operation_sha256`, exact `representations`, and an `applications` list matching
the selected application identities. Each application supplies an actual
`account`, nonempty `outputs` target bindings and `remaining` questions.
For realize, add `realization` with the exact `representations` and an authored
`account` explaining what the realization actually preserves or changes.
Missing/inconsistent bindings are refused. Those mechanical checks do not
establish that an account is true, sufficient or applicable.

The adapter retains original reply text, exact inputs, exact candidate digests,
captured target before-digests, structural JSON changes and authored witnesses
inside the native return. Full before/after bodies remain in the native object
store. Stale actual returns can be received and inspected. `adopt TURN --target
PATH --reason TEXT` delegates the write to native adoption; changed captured
material or target state is refused there. This is an opt-in application
contract, not a second authorization system or a hostile-writer boundary.

After a consequential return, ask whether the representation, its arrangement,
its source application or its receiving question needs revision. An unchanged
accepted representation can be a justified result. A source inventory, count,
hash, JSON format, structural difference or completed local receipt cannot
establish whole-goal fulfillment or deliberate dimensional composition.
