# Source-bound continuation exchange

## What this component supplies

`work.py` (or installed `g60-work`) connects selected inputs to actual returns and optional single-file publication. A model, person or command supplies the work; its caller selects the inputs, interprets the outcome and owns effect authority. The [API](api.md) identifies the common host boundary.

The exchange supports both an external command adapter and a caller-brokered model/person return. Those are different transport modes. A caller-import is attributed as supplied, not authenticated as an independent agent. The command path uses the included collector; its stdout, stderr, exit and request remain available. A completed process is not an accepted interpretation.

## Storage and source boundary

Choose two existing/supplied locations: a caller-owned workspace for methods, materials and outputs; and a disjoint external exchange directory for operational records. Neither belongs inside this distribution. Research about the component is not stored in the product. Runtime records are the component's functional output, owned by its caller.

All selected workspace paths are relative and ordinary files; traversal and symlinks are rejected. A selected file is captured, not a live reference substituted later. Equal byte objects may share storage while requests retain their distinct consumer, path, source selection and receiving occurrence. UTF-8 content is rendered into the context. Binary content is retained but not made perceptible to a text-only interpreter by that fact alone.

Each selected file, reply or proposal is limited to 2 MiB; selected/rendered context is limited to 8 MiB. This is a bounded local transport, not a large-corpus search service. The caller must construct another view when a selected context exceeds that scope. Method and material roles are transport labels; they do not impose a domain ontology, observation taxonomy, or sequence of mental steps.

For a complete executable example with no placeholder response files, see [quickstart.md](quickstart.md).

## When the destination belongs to a native runtime

Use `system.py --root RUNTIME adopt TURN --target OUTPUT --reason ACCOUNT` for a native publication. Use its `run` command for an approved worker dispatch. The same Exchange file may hold that packet, but the native turn also binds governing work and routed feedback. A standalone publisher checks whether the actual destination lies in a recognized native runtime. It refuses such a write even when a different Exchange selected the target from above or below the runtime. Read-only recovery of the saved packet remains available. Standalone tasks with separately owned workspaces retain their ordinary direct publication. If a workspace contains the running installation and another project, publication can target the independent project. The actual destination inside the running installation is protected.

A packet dispatch resumes only its own admitted worker. After an interruption before launch, use the still-current turn and the same command to resume. A different valid packet cannot launch the old worker, and a raw carrier cannot resume a native queue. A separately owned standalone carrier can still resume its admitted jobs together. Launch attempts already recorded retain their original uncertainty and recovery rules.

Use this edition for every cooperative writer addressing the native state. Older executables and arbitrary same-user file writes are outside this entrypoint guarantee. Existing native stores keep their on-disk format. An existing deployment pins its core bytes. Stop old writers, preserve the old deployment for recovery, initialize a fresh deployment from the replacement product against the same runtime, then stage and select the intended edition.

## A first use

From the standalone prototype folder:

```bash
BASE="$(mktemp -d)"
mkdir "$BASE/work"
printf 'Explain the current material without inventing missing facts.\n' > "$BASE/work/brief.md"
printf 'A source for the current receiving use.\n' > "$BASE/work/source.md"
python -B exchange_packets.py --root "$BASE/exchange" init --workspace "$BASE/work"
python -B exchange_packets.py --root "$BASE/exchange" prepare first \
  --consumer reader --brief brief.md --material source.md --target answer.md
```

The returned `context` path contains the selected material. The returned `request` path contains the source and object bindings. Give those actual files to the chosen interpreter. Merely preparing them is not delivery, attention, interpretation, or fulfillment.

For a model or person working through another interface, save its actual response and any proposed replacement as external files. Then use:

```bash
python -B exchange_packets.py --root "$BASE/exchange" receive first \
  --producer 'actual interpreter, caller-brokered' --reply /external/response.md \
  --proposal answer.md=/external/proposed-answer.md
python -B exchange_packets.py --root "$BASE/exchange" inspect first
python -B exchange_packets.py --root "$BASE/exchange" publish first \
  --target answer.md --reason 'Caller authorizes this particular contribution.'
```

The last invocation is explicit effect authority from the caller, not a judgement inferred from the model's response. `receive` alone never changes the workspace target. No example response is generated or presented as a real model return by these commands.

## Command adapters

A caller-selected trusted command can receive `{context}` or `{packet}` in its argv. The command executes with the workspace as working directory. Shell interpolation is not performed by the exchange.

```bash
python -B exchange_packets.py --root "$BASE/exchange" dispatch first -- \
  /path/to/python /path/to/your_adapter.py '{context}'
python -B exchange_packets.py --root "$BASE/exchange" receive first \
  --producer 'name of actual adapter' --wait 20
```

Use this command path instead of a caller-import for that packet. A dispatched packet cannot be replaced by a brokered return. A configured provider CLI can be wrapped by an adapter, but no provider authentication, API, session migration, token accounting, or provider-specific behavior is implemented here.

The adapter is responsible for its actual source consumption. The host establishes that it passed a captured path, not that the recipient attended to every byte. Trusted commands can access other files and network services under their inherited environment; the exchange is not a sandbox. Explicit source selection is not an information-isolation guarantee.

The collector preserves uncertainty after interrupted dispatch; the exchange does not automatically retry. `receive` first checks the dispatch against this packet's identity and frozen request. `receive --wait` then polls only that known job during this invocation. Missing or malformed targeted state remains `record-unresolved`; unrelated job history is not inspected. Pending or unresolved carrier views remain visible without creating a returned contribution.

## Recover captured command evidence

An accepted local-command return stores a compact carrier view with
`carrier_format` set to `material-objects-v1`. Each present material retains its
path, presence, SHA-256 identity and byte count in the receipt. Its raw bytes are
durably retained in the Exchange's content-addressed objects before `return.json`
is published. Equal originating/current contexts and a stdout body already held
as the reply share the same object. Other observations, including request and
attempt identities, timestamps, exit status, signals and interpretation, remain
attached to the captured occurrence.

Recover the complete original Collector view explicitly through either entrance.

```bash
python -B exchange_packets.py --root "$BASE/exchange" recover-transport first
python -B agent-system/system.py --root /external/native-runtime recover-transport TURN
```

The result restores the original `base64` and `utf8_text` fields. Empty byte
strings, invalid UTF-8, missing material, unavailable current context and a
different current brief retain their captured shapes and observations. Recovery
reads retained evidence and works in a fresh process after the carrier directory
has been removed. Existing inline local-command records remain readable in their
original representation. A caller-import return yields JSON `null`.

Return inspection, listing, reuse and publication validate each retained body by
identity and length. Missing or changed objects make the return unresolved or
raise an integrity error. Recovery preserves the captured effect-authority scope.
Current publication still requires the receiving occasion's source, target and
authority checks. Historical recovery remains available after a current-source
change.

The ordinary 2 MiB reply and proposal bounds continue to apply. Transport
retention accommodates the prepared context's 8 MiB bound and finite captured
stderr above 2 MiB. Standalone Collector views and pending Exchange observations
continue to expose their complete material directly. Retain the Exchange packet,
receipt and referenced objects for historical recovery.

## Continuing through actual returns

`list` recovers retained packet identities and shows prepared, dispatch-attempted, returned and terminal-without-return transport occurrences, optionally for one consumer. These are not task-completion verdicts. `inspect` preserves access to the originating return when the current workspace files are absent, exposing that changed basis instead of erasing the historical occurrence.

## Record a terminal command outcome

A launch failure has a verified Collector terminal observation without a child exit. A completed command can also produce stdout above the ordinary 2 MiB reply bound. The owner can retain either observation with an explicit account.

```sh
python -B exchange_packets.py --root /external/exchange close-transport PACKET --account 'Retain the failed attempt and continue through a fresh occasion.'
python -B agent-system/system.py --root /external/native-runtime close-transport TURN --account 'Retain the failed attempt and continue through a fresh occasion.'
```

The new `transport-outcome.json` binds the frozen request, owner account, reason and complete captured carrier evidence. The ordinary reply bound remains effective. The original dispatch and Collector records stay intact. `inspect` exposes the outcome, and `recover-transport` restores its complete captured evidence after carrier-directory removal.

The transport state becomes `terminal-without-return`. Native status stops counting that occurrence as awaiting a transport observation. Goal fulfillment and independently owed obligations retain their own records. The captured context remains available through `context` or native `recover`.

An exact repeat returns the original outcome. A different account is refused. The packet retains its dispatch guard and has no received contribution available for publication. A new command or derived contribution uses a fresh occasion. Running, uncertain and integrity-unresolved observations cannot be closed through this operation. A bounded reply from a command with a nonzero exit retains the ordinary receiving and explicit adoption route.

Closure uses the existing owner and current-feedback dispatch authority check. If current feedback has invalidated that native occasion, preserve it and construct a fresh current use. This operation records the terminal observation and its owner account without launching work.

A subsequent `prepare` can use current workspace methods and include a previous packet with `--return first`. The originating consumer, producer, transport and request remain attached. Two current consumers can include the same return while retaining different methods, duties and responses.

A method is an ordinary caller-selected file. To revise one, name it as a permitted target, record an actual proposed replacement, and explicitly publish it. A later packet can then capture that changed method. No self-modification is inferred merely from a proposed description. Software checks byte participation; the interpreting model or person remains responsible for meaning and why the change is useful.

## Choose the receiving view

Different uses need different parts of an earlier occurrence:

| Needed contribution | Explicit route |
|---|---|
| Original producing brief, methods and selected material | `context PACKET`, or `context PACKET --text` for the exact prepared text |
| Actual reply and origin bindings | `prepare ... --return PACKET` |
| One recorded proposed file body | `prepare ... --artifact PACKET:TARGET` |
| Return plus current publication observations | `inspect PACKET` |
| Complete captured command carrier evidence | `recover-transport PACKET` |

`context` validates the frozen request, text and referenced objects, then returns
the original view with its packet, consumer, request/context hashes and byte
count. That view includes what was selected then, including target baselines;
it does not substitute today's files or claim to contain the whole workspace.
It remains readable when current files are missing or publication is unresolved.
Corrupt immutable inputs remain an error.

When a later interpretation needs those producing instructions, recover the view
and select it as material in that occasion:

```bash
python -B exchange_packets.py --root "$BASE/exchange" context first --text \
  > "$BASE/work/origin-context.md"
```

Pass `--material origin-context.md` to the new `prepare` call when needed.
The read itself creates no packet, return, completion record or file effect.
The shell redirection above explicitly creates the selected local material.
An old brief or target remains historical content; the new occasion has its own
instructions and authority. `--return` and `--artifact` continue to include only
their selected contributions.

## Continue from an unpublished artifact

A reply and a proposed file are separate contributions. `--return first` includes the reply and lists the recorded proposals available for explicit selection; it does not insert their bodies automatically. To receive a particular proposal, use its originating packet and target path:

```bash
python -B exchange_packets.py --root "$BASE/exchange" prepare another-use \
  --consumer operator --brief another-use.md \
  --return first --artifact first:answer.md --target operator-note.md
```

The artifact body comes from the bound return, not a file currently stored at `answer.md`. The original proposal need not have been published; its target and caller-supplied response file can be absent or different now. The context retains the originating packet, consumer, producer attribution, request binding, return binding, and proposal path. Receipt or publication in the new occasion does not change the original return or declare that its proposal was adopted.

Returned artifacts are material, not inherited permissions. The new recipient can explain, revise, compare or otherwise use them under the new brief. Proposed code is not executed by selection. An original target does not become a new permitted target; the current occasion must declare its own publication targets. The caller remains responsible for interpreting whether an old proposal is adequate for a new use and whether its dependencies need additional participation.

Two occasions can refer to the same captured object while retaining different consumers and briefs. Context rendering can repeat its contents; sharing storage does not establish fewer model tokens or less semantic work. Binary artifacts remain captured objects with no fabricated text interpretation. Per-object and total-context limits still apply.

The current reader accepts retained version-1 requests. Newly prepared version-2 requests can carry explicit artifact references. The older 0.2 reader is not a supported reader of version-2 requests. A stored return's body, not its current publication status, is the selected contribution.

## Inspect an uncertain publication

`inspect PACKET` validates the retained request, context and return before exposing them. A current-basis error does not erase an otherwise intact return: `basis` is null and `basis_error` records the unavailable effect check. Corruption of the immutable request, captured objects or return still makes inspection fail; this path does not bypass those checks.

The `publications` array reports each retained publication record and, where readable, the current declared target's byte identity. `matches_base` and `matches_proposal` describe present byte comparisons only. An intent record remains an intent even when current bytes match the proposal. A matching current file does not establish the historical write sequence, durability under a system crash, or permission to replay. If the target cannot be examined, `observation_error` remains explicit and no current hash is invented.

Inspection is read-only and does not clear intent, create a completion receipt, retry a write, or remove the original publication guard. Pause competing activity within the single-owner contract, preserve the original records, and construct the next authorized use from the actual observation. `prepare --return PACKET --artifact PACKET:TARGET` can supply the intact earlier contribution to a new occasion without adopting it. A separate explanatory or recovery note can be published under its own target authority; repairing or repeating the original effect still requires the caller's explicit reconciliation. Do not delete the intent merely to make another attempt pass.

Current target observations are not an atomic snapshot against uncooperative external writers. An unreadable or malformed publication record remains visible as an error; inspection does not authenticate it or decide that the original task is finished.

## Publication and changed use

Publication checks all declared source/brief/method/target bindings and the received proposal. If an external writer has changed a declared input, publication is refused, while the original request and return remain available. Construct a new occasion from the current material and the old return where useful. This is a conservative effect boundary, not an instruction to rerun every semantic task after every byte change. The host does not determine whether a change is meaningful.

Unselected changes are not a reason for blanket rejection. Equally, this does not establish that the selected sources were semantically complete. A relevant omission can require reopening even when all selected hashes match.

Each `publish` replaces one file, preserving the original captured bytes and recording intent before the effect. Several file publications are separate actions, not an all-or-nothing transaction. A crash between recorded intent and completed receipt requires manual inspection; no automatic replay is attempted. A prior successful publication by the same packet is recognized when publishing another declared proposal. Concurrent external editing is outside the single-cooperative-writer contract: a file check followed by replacement is not an atomic multiwriter compare-and-swap.

Records and hashes provide integrity checks within trusted local storage, not authentication against a party able to replace the records and hashes together. Permissions, retention, cleanup, source adequacy and semantic acceptance remain caller responsibilities.

## Reconcile an observed publication target

After inspecting an interrupted publication, the owner can acknowledge an expected current target identity. Use the SHA-256 observed during inspection, or the literal `absent` when the captured base and current target are both absent.

```sh
python -B exchange_packets.py --root /external/exchange reconcile-publication PACKET --target result.txt --expected-sha256 SHA256 --account 'Accept this current observation while retaining the original uncertainty.'
python -B agent-system/system.py --root /external/native-runtime reconcile-publication TURN --target outputs/result.txt --expected-sha256 SHA256 --account 'Accept this current observation while retaining the original uncertainty.'
```

The operation requires the original bound intent and received proposal, the existing publication authority, the expected current target identity and a nonempty account. The observed bytes must match the captured base or proposal. Divergence requires a fresh receiving occasion. A changed expectation is refused before writing a reconciliation record.

The additional `reconcile-*.json` receipt binds the exact original intent bytes, frozen request, received contribution, observed target identity and owner account. Its historical completion remains `unestablished`. The original `publish-*.json` stays at `intent`, and the operation performs zero target writes.

An exact repeat returns the original observation and account. A different account or expected identity is refused. `inspect` shows the original intent, reconciliation and current target separately. Later sibling publication uses the acknowledged target identity in its ordinary basis check and still passes current source, target and native authority checks. The original attempted target retains its replay guard. A later effect on that target uses a fresh occasion.

Reconciliation is scoped to one cooperative owner and the existing publication authority. It supplies a durable current-byte acknowledgement while preserving the limits of the original interrupted occurrence.
