# Configuration inspection and launch

Run the standalone preflight before starting the restartable workbench:

```sh
python3 -B inspect_config.py --config /absolute/config.json
```

Run from the directory containing `inspect_config.py`, or give its absolute path.
The command prints one JSON object with `ok` and `issues`. Exit 0 means the
inspected static conditions pass; exit 1 means an issue needs attention. Each
issue names a field or path and an actionable problem. Invalid JSON and ordinary
invalid configurations receive controlled diagnostics.

## Prepare the example

Copy `example-resume-config.json` to your chosen config file. Replace every occurrence
of these absolute path prefixes in its JSON string values:

| Prefix | Replace with |
| --- | --- |
| `/__PRODUCT_ROOT__` | The installed product root containing `resumable_workbench.py`, its companion modules, and `controller/` |
| `/__CORPUS_ROOT__` | The directory of Markdown sources to read. The installed Workbench locates the trusted reader in the package’s shared engine. |
| `/__WORK_ROOT__` | A fresh user-controlled root outside both packages, containing distinct `workspace` and `state` locations |

Use actual absolute paths. The example places the mailbox at `state/model` and
uses the source-state default `state/source-service`. These paths may be absent:
preflight does not create them. Keep the configuration available across a restart.
Replace the example task, requirements and complete source text with your actual
commission before starting. Its small task can also be run as written. Its empty
receiver map is valid, but supplies no artifact acceptance gate; configure a real
receiver when your commission requires acceptance.

## What preflight checks

This inspector uses the Python standard library and supports public `mailbox`
and `command` configurations. It requires an explicit absolute `controller_dir`,
even though the workbench itself defaults that field to its own `controller/`.
`source_state_dir` may be omitted and defaults to `state_dir/source-service`.
Configured filesystem paths must be absolute; a bare command executable may be
resolved on `PATH`.

| Area | Inspected conditions |
| --- | --- |
| Core data | JSON object; string task; sources mapping string IDs to string bodies; reserved `workbench-tool-contract` ID excluded; list of obligations; string run ID if supplied; finite JSON values |
| Limits | Integer `max_steps` and `max_cache_bytes` at least 0; positive integer `max_file_bytes`; positive finite numeric timeouts; booleans rejected as limits |
| Required code | Existing controller directory with readable `agent_runtime.py` and `evidence_support.py`; existing corpus directory and the ordinary trusted `knowledge.py` in the installed engine |
| Model | Mailbox or command kind and transport directory; nonempty command argv; available executable, directly named Python script and declared fixed input files; suitable optional working directory |
| Receivers | Named command specifications, fixed input files, positive timeouts and nonempty lists of clean relative artifact paths; duplicate, escaping or existing symlink artifact paths reported |
| Placement | Editable workspace disjoint from retained state, source-state and transport storage; mutable storage disjoint from corpus and controller; named fixed command/input files outside the workspace |

If the controller's parent contains `resumable_workbench.py`, the inspector also
checks the adjacent operating modules and protects that whole package from
mutable storage. With a separately located controller, confirm the actual
operating package placement yourself. Put retained components in distinct
subdirectories; the example's mailbox and source-state may both be inside state.

Zero cache and zero model-call allowance are valid. A zero-call run cannot
construct an output. Defaults are 12 model calls, 65,536 cached payload bytes and
2,097,152 bytes per workspace file; model and receiver timeouts default to 900
and 60 seconds. The call limit counts attempted model calls, and the cache bound
covers acquired UTF-8 payloads, not the index, process memory or model context.

Named receiver argv is executed without a shell by the workbench. Only
`{workspace}` and `{receiving_dir}` are substituted at receiving time; a child
such as `{receiving_dir}/subject` gives each check a fresh output location.
Keep fixed receiver code/contracts in `inputs`, mutable checked outputs in
`artifacts`, and changing effect logs out of fixed inputs. For command models,
requests arrive as JSON on stdin, one action JSON goes to stdout, and diagnostics
go to stderr. Declare relevant imported/dynamic dependencies in `model.inputs`
and receiver `inputs`; directly bound code is not the whole dependency graph.
An injected Python callable requires a Python host and is outside this CLI's
preflight scope.

### Read-only scope and limits

Preflight reads the configuration and checks named path metadata. It does not
import or start the workbench, initialize a source service, create configured
directories, run model/receiver commands, or read/scan research corpus bodies.
Future storage is allowed when its existing path ancestry is suitable. Passing
inspection does not prove writable future storage, provider access, meaningful
or complete source content, semantic task adequacy, receiver truth, complete
transitive dependency identity, SQLite FTS5 availability, or successful launch.
It does not decide whether existing state can start or resume. Keep the complete
product modules together and provide the runtime's SQLite FTS5 capability.

## Start, supply a model, inspect and resume

After replacing roots and passing preflight, use the actual product path:

```sh
python3 -B /absolute/product/resumable_workbench.py start --config /absolute/config.json
python3 -B /absolute/product/resumable_workbench.py inspect --state-dir /absolute/work/state
python3 -B /absolute/product/resumable_workbench.py resume --config /absolute/config.json
```

Use `start` once with a fresh state directory. The mailbox waits for the actual
caller-supplied model. Present each exact request envelope to that participant
and save its returned action JSON. Deliver the action for that request's step:

```sh
python3 -B /absolute/product/resumable_workbench.py respond \
  --mailbox /absolute/work/state/model --step 1 \
  --action /absolute/action-0001.json --producer "actual model participant"
```

An action is one `tool` with `name` and `arguments`, or `final` with an `answer`.
The response is bound to the request digest and cannot replace an earlier
response. This example provides no provider client. A final model status of
`complete` does not accept artifacts; that requires an observed successful
named receiving check against the declared unchanged output bytes.

Workbench `inspect` is a separate read-only view of retained operation states,
checkpoint, pending operation and counters. It neither executes callbacks nor
refreshes sources or acceptance. `resume` reconstructs logical history and
replays known recorded callback returns without running those callbacks again.
Only work after the last completed operation is newly dispatched. A fresh model
receives reconstructed observations, not hidden prior model context. The
original call allowance continues across processes; restarting does not reset it.

Resume requires the original commission, limits and relevant code/input
identities. Changes cause `binding_mismatch`; changed tracked working paths can
report `workspace_changed`. Preserve the full retained state and relevant
workspace. Restore warranted prior working bytes or begin an explicitly new
occurrence for a changed commission, carrying still-open duties forward.
Historical accepted bytes remain separately retained even after a recorded
workspace edit. Replayed source text and restored inventory membership describe
past observations. Cache/index payloads are not restored, and a current-source
claim requires the appropriate new observation or explicit source refresh.

## An unresolved operation needs outcome evidence

If intent was saved but no matching outcome was durably recorded, resume returns
`unresolved` and dispatches nothing. The effect may already have occurred.
Ask that particular effect owner for outcome or deduplication/status evidence;
missing local completion is not permission to repeat the operation.

Only an authorized operator with a warranted actual return and matching complete
post-operation checkpoint should supply a resolution JSON. Its fields are
`operation_id`, `expected_contract_sha256` from inspection, `value`, `checkpoint`
and `evidence`. Then run:

```sh
python3 -B /absolute/product/resumable_workbench.py resolve \
  --state-dir /absolute/work/state --resolution /absolute/observed-resolution.json
python3 -B /absolute/product/resumable_workbench.py resume --config /absolute/config.json
```

Resolution records the supplied account and preserves the intent; it does not
execute the operation or certify the evidence's truth. If the outcome or
checkpoint cannot be warranted, retain the unresolved duty. Recorded ordinary
callback exceptions remain terminal errors without automatic retries. These
contracts assume one cooperative serialized local owner and do not promise
arbitrary remote effects exactly once.

## Observed receiving and preserved duties

The first `inspector` receiving invocation, `check-0001`, returned exit 0 with
`accepted: true` and receiver `ok: true`. All eight selected cases passed: mailbox
with zero allowances and future directories, command transport, invalid JSON,
malformed core shapes/bounds, missing controller code, empty command argv, unsafe
receiver declaration and crossed workspace/state/package placement. All reported
read-only checks passed. The receiver confirmed that guide and example were
absent then and that the independent artifact stayed exact. This is a bounded
static CLI/read-only result, not a provider or launch-success claim.

The accepted inspector has SHA-256
`9f8629fb149bafeb57dc866e31cf50ec404f815d49c328cf2008190d068578f2`.
This guide and example were authored from the reconstructed successful return
after continuation, without repeating the completed inspector gate. The named
`complete` receiver separately checks preserved inspector bytes, these artifacts
and one instantiated example preflight.

`independent-method.md` remains byte-exact at SHA-256
`328e721b1a46e19492ae846aa634bdcd4f6666f7266213e0f33097ddc50d3cab`.
Both original duties, `current-original-history-duty` and `manifest-history-duty`,
remain open with their original owners, revisions, epochs and record hashes;
the example carries their complete unchanged records. This construction does
not fulfill either duty.

Source basis: the supplied implementation and current `RESUME.md`, plus
the complete acquired `studies/84/product/AGENT.md` and
`studies/84/product/SOURCES.md`. Earlier documentation's non-restartable
`run` route is distinct from the current `start`/`inspect`/`resume` route above.
