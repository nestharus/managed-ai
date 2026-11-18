# Durable workbench contract

## Generic API

```python
run_durable_agent(*, state_dir, binding, task, sources, model, tools,
                  max_steps, resume=False, checkpoint=None, restore=None,
                  after_record=None, controller_dir=None)
inspect_durable_state(state_dir)
resolve_durable_operation(*, state_dir, operation_id, value, checkpoint,
                          evidence, expected_contract_sha256)
```

`task`, `sources`, `model`, `tools`, `max_steps` use the accepted controller contract: a string task; string-to-string source map; `model(request)` yielding `{"kind":"tool","name":str,"arguments":dict}` or `{"kind":"final","answer":str}`; named `tool(arguments)` callbacks; nonnegative integer model-call limit. Durable-route callback values must be finite JSON. Ordinary observed callback exceptions retain their type/message and terminal controller error. A process stop or other unobserved return leaves an unresolved intent.

`state_dir` is a separate local directory. `resume=False` creates a new journal and refuses an existing occurrence; `resume=True` requires one. `binding` is a finite JSON value supplied by the host and must bind the actual configuration, adapter code, model/transport contract and receiving inputs needed by this operation. The generic layer also binds task, complete initial sources, tool names, the original limit and actual generic/controller/evidence-helper code. The default unchanged controller lives in `controller/` next to this module. An explicit `controller_dir` may select another exact local copy.

`checkpoint()` returns finite JSON that represents the adapter state needed after the callback. `restore(snapshot)` restores that state once, before historical replay or a new callback. Both default to no-op/null. The initial checkpoint is persisted before the first callback. Each observed callback completion and its checkpoint are one atomic durable record. Historical replay does not restore older checkpoints over the newest state.

`after_record(record)` is an optional host observer after an actual callback's durable completion. It receives exactly the meaningful fields below (additional fields may be added compatibly):

```json
{"event":"callback_completed","operation_id":"000001","callback":"tool",
 "name":"artifact_write","logical_step":1,
 "completion_path":"/absolute/journal/calls/000001.return.json","resolved":false}
```

It is not called for historical replay. The observer may cause an actual process interruption. An ordinary observer failure does not rewrite a committed callback as a different callback outcome.

## Results and inspection

Normal execution returns the accepted controller result, augmented with `durable`. Its top-level `model_calls` and `tool_calls` remain logical invocations of the wrapper during this execution; they include recorded replay. `durable` separately reports `replayed_callbacks_this_invocation`, `actual_dispatches_this_invocation`, `dispatch_intents_total`, `observed_completions_total`, `unresolved_intents_total` (all split by model/tool), original `model_budget_limit`, `model_budget_charged`, `model_budget_remaining`, and the exact contract identity. A durable intent is a conservative charge, not proof of an external callback invocation.

Nonexecution dispositions have `status` equal to `binding_mismatch`, `unresolved`, or `state_error`, with `error.code` and details. An unresolved result names `error.operation` with `id`, `callback`, `name`, and `logical_step`. When resuming an already recorded pending operation, it dispatches no callbacks. A newly invoked callback may instead fail to persist its completion and return unresolved after an actual dispatch, which remains reported in the durable counters. Inspection returns manifest, ordered operations, pending operations, latest checkpoint and the durable counters, without invoking model/tools or performing semantic acceptance. Pending intent means that actual invocation may or may not have happened.

The optional resolution API records an owner-supplied observed JSON `value`, the matching post-operation checkpoint, and nonempty source/evidence account. It requires the exact contract digest and the pending operation ID. It preserves the original intent and records resolution provenance. It does not call the external operation, establish the supplied account's truth, or permit a blind retry. Continue only after inspecting the actual effect owner's sufficient outcome evidence. Leaving the occurrence unresolved is valid.

## Source-workbench adapter

The source owner supplies `resumable_workbench.py` with `run_workbench(config, *, resume=False, model=None, after_record=None)` and `inspect_workbench(state_dir)`, plus CLI `start --config`, `resume --config`, `inspect --state-dir`. Its generic journal is `STATE/durable`; original workbench evidence remains under `STATE`. Injected model use needs an explicit declared identity in configuration. Actual mailbox participation uses the established model bridge and stable logical call ordinal.

The adapter preserves declared requirements/obligations, source and selection associations, observed catalogue guard where relevant, exact accepted artifacts and receiving state. It binds the actual source/adapter/model/receiver inputs it consumes. Callback replay does not imply a fresh source observation or recreate a warm search index. Fresh discovery can incur new preparation independently of the recovered historical observation.

## Scope

One cooperative local owner, serialized with an advisory lock. No arbitrary callback exactly-once guarantee, distributed lease, adversarial sandbox, provider cancellation, latent-context restoration or complete provider-cost claim. Known results are reused from retained material state; a fresh actual model receives reconstructed logical history and the original remaining budget.


## Retained working-path correspondence

The source-workbench adapter now retains the identity of working paths actually read, written or named as receiver artifacts in its workbench checkpoint schema `g60-workbench-checkpoint/2`. The containing resumable-workbench checkpoint continues to carry the workbench snapshot and model transport ordinal. The generic recovery API, journal representation and accepted controller are unchanged.

Before a resumed new callback, the adapter compares those tracked current working paths with their latest committed observations. A changed or missing path returns `status: "binding_mismatch"`, `error.code: "workspace_changed"`, and an `error.changed_paths` list containing the path and expected/observed identity. It invokes no new model or tool callback, does not overwrite the edit and preserves the original occurrence.

A later model/tool write that was itself durably recorded updates the latest working observation. Earlier accepted artifact bytes remain separately retained under their immutable accepted-object identities. A historical acceptance therefore does not assert that mutable workspace bytes still equal that acceptance. The guard concerns tracked working paths, not complete workspace membership or unrelated files.

Use a warranted restoration of the prior working basis to continue the same occurrence, or explicitly select a new commission that includes the changed work. The API supplies no force-resume or automatic acceptance of the edit.
