# Restartable source-backed construction

`resumable_workbench.py` adds a local start, inspect and resume entrance to the
source-backed construction workbench. A fresh process uses retained callback
results and workbench state to continue the same declared construction. The host
supplies the actual model through the mailbox, a command or an identified Python
callable. A fresh model receives the reconstructed logical history; no hidden
model context is restored.

The durable adapter reuses the unchanged callable controller in `controller/`.
This optional route is separate from the native system. Keep these
files together:

- `core/resumable_workbench.py`, `core/durable_agent.py` and `core/workbench_agent.py`;
- `core/source_tools.py`, `core/model_bridges.py`, `core/config.py` and `core/io_utils.py`;
- `controller/agent_runtime.py` and `controller/evidence_support.py`.

Python's standard library and SQLite FTS5 suffice. The system bundle supplies the
trusted search implementation in `../../../engine/library/` (`engine/library/reader/knowledge.py`
and its `reader/`, `search/`, and `bundles/` dependencies). The selected domain supplies
source data. A known source-body read can run before full search preparation.

The durable binding includes the effective search files and analyzer identity.
Older runs whose recorded bindings refer to superseded layouts remain inspectable
as historical evidence without rewriting their original bindings.

## Start a construction

Place the editable task workspace and retained run state outside the operating
and research packages. The state directory must also be outside the editable
workspace. Use a new state directory for each new occurrence. The following is
a configuration example; supply the actual task, complete sources, receiver and
paths for the work being performed.

```json
{
  "run_id": "package-inspector-01",
  "corpus_root": "/absolute/managed-ai/domains/research/sources",
  "workspace": "/absolute/work/package-inspector",
  "state_dir": "/absolute/retained/package-inspector-01",
  "task": "Construct the requested package configuration inspector and its usable guide and example.",
  "requirements": "The complete current receiving commission belongs here.",
  "sources": {"receiving-commission": "Complete source text, not a path or a snippet."},
  "obligations": [{"id": "independent-output", "status": "owed"}],
  "max_steps": 16,
  "max_cache_bytes": 65536,
  "max_file_bytes": 2097152,
  "model": {
    "kind": "mailbox",
    "directory": "/absolute/retained/package-inspector-01/model",
    "timeout_seconds": 900
  },
  "receivers": {
    "inspector": {
      "argv": ["python3", "-B", "/absolute/receive-inspector.py", "--workspace", "{workspace}", "--output", "{receiving_dir}/subject"],
      "inputs": ["/absolute/receive-inspector.py", "/absolute/inspector-contract.json"],
      "artifacts": ["inspect_config.py", "README.md", "example.json"],
      "timeout_seconds": 60
    }
  }
}
```

`controller_dir` defaults to this product's `controller/`. An explicit absolute
path may select another exact copy of the accepted controller and its helper.
`source_state_dir` defaults to `state_dir/source-service`; a separately supplied
source-state directory may preserve earlier source receipts. It does not provide
a warm cache or index. Receiver `inputs` name the code and fixed receiving
material, while `artifacts` name the mutable outputs checked at each invocation.
Do not put a receiver's changing effect log in its fixed `inputs` list.

```sh
python3 -B resumable_workbench.py start --config /absolute/config.json
```

The CLI returns a concise status, counters and the path to that exact attempt's
result. `complete` means the model returned its final answer. Artifact acceptance
is separately recorded by the named receiver; a final answer alone does not
accept an output.

## Supply the actual model

A mailbox request is written as `0001.request.json`, then the process waits for
the bound response. Present the exact request to the actual participant and
save its returned action as JSON. A valid action is one tool request or a final
answer, under the unchanged callable-controller contract.

```sh
python3 -B resumable_workbench.py respond \
  --mailbox /absolute/retained/package-inspector-01/model \
  --step 1 --action /absolute/action-0001.json \
  --producer "actual model participant"
```

The response is bound to the exact outstanding request and cannot overwrite an
earlier response. Host transportation does not count as model inference. When a
fresh participant continues, it uses the original task, sources, logical history
and remaining call allowance supplied in the new request.

For a command model use:

```json
{
  "kind": "command",
  "argv": ["python3", "-B", "/absolute/model-adapter.py"],
  "inputs": ["/absolute/model-adapter.py", "/absolute/provider-contract.json"],
  "directory": "/absolute/retained/package-inspector-01/model",
  "timeout_seconds": 900
}
```

The command receives one request JSON on stdin and returns one action JSON on
stdout. Diagnostics belong on stderr. The caller owns provider access. Include
relevant imported or dynamic code/configuration in `model.inputs`; the adapter
automatically binds the executable and a directly named Python script, but does
not discover every transitive code dependency. Directly specified receiver
executables/scripts and declared receiver inputs are likewise bound.

For an in-process callback use
`run_workbench(config, model=callback)`, with
`model: {"kind": "injected", "identity": "your-declared-contract", "inputs": [...]}`
in the config. The identity must be nonempty, and code must be bound through an
inspectable source file or explicit input files. This is a cooperative caller
contract, not authentication of a model's internal implementation.

## Inspect and continue

```sh
python3 -B resumable_workbench.py inspect \
  --state-dir /absolute/retained/package-inspector-01

python3 -B resumable_workbench.py resume --config /absolute/config.json
```

Inspection is read-only. It exposes the original manifest, ordered operation
states, latest committed checkpoint, pending operation and counters. It does
not execute the model, replay effects, acquire fresh source bodies or accept
artifacts.

Resume requires the original declared inputs and relevant code/receiver
identities. A changed task, requirement, model contract, tool implementation,
receiver input or original limit produces `binding_mismatch` before any new
callback. This disposition preserves the earlier occurrence. Use an explicitly
new occurrence for a changed commission, carrying the still-owed work and
qualified prior artifacts into it as appropriate; there is no force-resume flag.

The latest checkpoint also retains identities for working paths actually read,
written or named as receiver artifacts. A changed or missing tracked path gives
`binding_mismatch` with `error.code: "workspace_changed"` and a `changed_paths`
list before any new callback. A model-recorded later edit updates this current
working observation while preserving earlier accepted bytes separately. This
check does not audit unrelated files or promise complete workspace membership.
Restore the warranted prior working bytes to continue that same occurrence, or
make the changed work part of an explicitly new commission.

For known completed callbacks, the adapter supplies recorded returns to the
unchanged controller without invoking the original callback again. After replay
reaches the last completed operation, new work can use the recovered workbench
state. The original call budget applies across the occurrence. Reopening a run
does not allocate another full allowance.

The result separates the counts:

| Field | Meaning |
| --- | --- |
| `model_calls`, `tool_calls` | Logical controller invocations, including historical replay |
| `durable.replayed_callbacks_this_invocation` | Recorded model/tool results consumed during this invocation |
| `durable.actual_dispatches_this_invocation` | New callback attempts by this invocation |
| `durable.dispatch_intents_total` | Persisted dispatch intents, including unresolved attempts |
| `durable.observed_completions_total` | Durably recorded returns or observed exceptions |
| `durable.model_budget_remaining` | Original allowance minus conservatively charged model intents |

A persisted intent is a conservative charge. If the process stops before an
observed return is saved, the intent alone does not prove whether execution
started or completed. Provider-internal calls, tokens and prices remain
unmeasured.

## Source observations and accepted artifacts

The checkpoint retains source acquisition IDs, selection IDs, receiving count,
accepted manifests and their exact use basis. It also retains the ID of any
observed complete inventory. That inventory's historical membership/hash guard
is reconstructed from its durable receipt; the in-process cache and search
index remain absent after restart.

Replayed source text is the earlier observation, not a fresh reading. A new read
of a member of the recovered inventory checks the observed hash and byte count.
If that body changed, the read refuses until a new explicit refresh establishes
the current view. A new discovery or inventory operation prepares the full
requested view and charges its actual body reads. A known-path read before any
inventory observation continues to make only its narrower complete-body claim.

Source counters describe the completed retained observations plus new measured
work. Cache/index sizes describe the current process. Work performed inside an
unresolved operation may not be fully represented in the last committed
counters. These figures exclude implementation imports/binding reads, model
context, provider use and complete operating cost.

Successful receiving snapshots the exact accepted bytes under
`accepted-objects/`, keyed by SHA-256. Later workspace edits do not replace
those historical objects. The `accepted_artifacts` tool exposes accepted
manifests. The `accepted_read` tool takes `check_id` and optional `paths` and
returns their exact retained UTF-8 bodies within the configured read bound.
It does not accept different current workspace bytes. Binary accepted objects
remain retained, but this body-reading tool is a UTF-8 interface.

## Resolve a genuinely unknown outcome

If a process stops after intent persistence and before a matching completion,
resume returns `unresolved` and dispatches nothing. An external operation could
already have committed. The absence of a local completion is not permission to
repeat it.

Obtain the particular effect owner's outcome or deduplication/status evidence.
If it supplies an adequate result, an authorized local operator can provide an
explicit resolution JSON:

```json
{
  "operation_id": "000008",
  "expected_contract_sha256": "the exact digest returned by inspect",
  "value": {"the": "actual observed callback return"},
  "checkpoint": {"the": "matching complete post-operation adapter checkpoint"},
  "evidence": "The source and interpretation that warrant this specific outcome."
}
```

```sh
python3 -B resumable_workbench.py resolve \
  --state-dir /absolute/retained/package-inspector-01 \
  --resolution /absolute/observed-resolution.json
```

Then resume with the original config. Resolution preserves the original intent
and records the supplied outcome and evidence; it does not call the operation
or verify the source account's truth. Its checkpoint must include the workbench
associations and transport ordinal appropriate to that actual outcome. If the
outcome or checkpoint cannot be warranted, retain the unresolved duty. A future
adapter for a specific effect owner can automate this boundary under that
owner's actual contract. This route makes no arbitrary callback exactly-once
promise.

Ordinary callback exceptions whose results were observed remain recorded
terminal controller errors. They are not automatically retried. Unexpected
process death before a durable completion remains distinct from such a known
exception.

## Operating files and scope

`STATE/entry-basis.json` owns the workbench commission. `STATE/durable/` owns
immutable callback intents and paired completion/checkpoint records.
`STATE/receiving/` retains exact receiver invocations and results.
`STATE/accepted-objects/` retains accepted bytes. Source receipts, model transport
files and exact attempt results remain separate. Preserve the whole state and
the relevant workspace; do not infer a complete occurrence from one result file.

The API's optional `after_record` observer is for host instrumentation. It runs
after a new callback's result and checkpoint commit and receives its operation
ID, kind, name, logical step and completion path. It does not run during replay.
A controlled process interruption there can qualify material continuation
without pretending it is an actual model encounter.

The supported arrangement is one cooperative serialized local owner. An
advisory journal lock detects competing operations, while the host remains
responsible for serialized source/workspace changes. This is neither a
distributed lease nor a hostile-filesystem sandbox. The promise concerns
process interruption on the current local filesystem; no power-loss or
arbitrary remote-effect guarantee is inferred. Callback count limits do not
bound callback duration, provider cost or semantic correctness.

Construction qualification and the actual interrupted model use have their own
retained records. Their outcomes should be read at their stated scope.
