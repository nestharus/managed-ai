# Continue a callable construction from material state

`durable_agent.py` adds a local start, inspection and continuation boundary around the accepted controller in `controller/`. It preserves the controller's source exposure, logical history, error handling and original step limit. It reuses recorded callback outcomes after a process restart instead of calling those callbacks again.

For the integrated source-backed construction entrance, use `resumable_workbench.py`. This guide documents the generic component for another adapter author. `RESUME-CONTRACT.md` defines the agreed public contract.

## Supply the actual continuing state

```python
from durable_agent import run_durable_agent

result = run_durable_agent(
    state_dir="/absolute/work/run/durable",
    binding={
        "model_contract": "my-current-model-bridge-v1",
        "adapter_sha256": "actual-adapter-code-digest",
        "receiving_input_sha256": "actual-receiver-input-digest",
    },
    task=task,
    sources=complete_entry_sources,
    model=model,
    tools=tools,
    max_steps=12,
    resume=False,
    checkpoint=export_adapter_state,
    restore=restore_adapter_state,
)
```

The binding values in this example are placeholders: compute actual identities for the inputs your receiving operation consumes. A callable's name is insufficient to identify its implementation, transport, external authority or configured receiver. The generic layer automatically binds its own code, controller/helper bytes, exact entry task/sources, tool names and original model limit. Changing any bound condition returns `binding_mismatch` before dispatch. Do not edit a stopped occurrence's files or reduce its manifest to bypass this refusal. Use an explicitly revised occurrence or a separately justified migration.

Call the same function with `resume=True` in the fresh process. Keep all governing arguments identical and supply the adapter's fresh callback instances. A fresh actual model receives the reconstructed complete logical history at its next unused model step. No hidden or latent model context is restored.

`checkpoint()` returns detached finite JSON describing the material adapter state needed after the callback. `restore(snapshot)` restores it once before replay. The snapshot can refer to independently retained exact bodies, accepted artifacts and open obligations. The adapter must validate those references and preserve the required bodies. A path to mutable working bytes does not preserve a previously accepted artifact by itself.

The generic wrapper records an initial checkpoint before its first callback. Later it commits each observed callback outcome and its post-callback checkpoint in one immutable completion file. Restoring the latest checkpoint once is deliberate: replaying old callbacks must not roll the adapter back through obsolete checkpoints. The integrated adapter uses this to retain source observations, selected-evidence associations, accepted artifacts, receiving state and model transport position.

## Understand inspection and interruption

```sh
python durable_agent.py inspect --state-dir /absolute/work/run/durable
```

`inspect_durable_state(path)` provides the same read-only view. It includes the exact manifest, ordered operation identities, completed or pending disposition, latest committed checkpoint, any complete controller result and budget/accounting. It invokes no model or tool.

An optional `after_record` observer receives a small `callback_completed` event after the outcome and checkpoint are durable. It is not invoked during replay. This can support progress reporting or a controlled actual process interruption. An observer error does not change the outcome of the already committed callback.

A process can stop after a callback's external effect but before its return is durably retained. Resume then returns `unresolved` and names the operation. It invokes no callback. The retained intent does not establish whether the effect started, succeeded, failed or happened partly.

If the effect owner supplies sufficient outcome evidence, `resolve_durable_operation` can record its observed JSON return and matching checkpoint. It requires the pending operation ID, exact contract digest and a nonempty evidence account. This is an explicit owner assertion, not independent validation of that assertion. The function performs no retry. Leaving the occurrence unresolved is correct when the necessary observation cannot be obtained.

An ordinary exception that was actually caught and durably recorded is different. It retains the accepted controller's terminal error behavior and can be replayed without repeating the callback. Its occurrence does not imply that the callback had no side effects. Durable-route callback returns must be finite JSON; unsupported values become an observed terminal callback error at this boundary.

## Read the accounting correctly

The unchanged controller's top-level call counts describe logical invocations of the wrappers during this execution. They include historical replay. The `durable` account separately exposes:

- Recorded callbacks replayed in this invocation.
- Actual callbacks invoked in this invocation when a result can be returned.
- All durable dispatch intents, observed completions and unresolved intents, split by model and tool.
- The original model limit, conservatively charged model intents and remaining allowance.

A pending intent can lie between persistence and invocation. Its count is an upper bound on those unresolved actual dispatches; a journal cannot honestly infer exact provider activity from it. The model budget conservatively charges the intent, and no automatic retry spends another slot. Logical replay uses the original step positions, so repeated restarts do not replenish the model budget.

The implementation retains full finite callback requests and returns. This is simple and inspectable, but can repeat history and source bodies across records. It is not a context-compression or economic optimization. Working evidence should be kept outside a distributed code release; preserve the selected arguments and exact necessary evidence rather than shipping repeated disposable run histories.

## Operating scope and evidence

This implementation uses immutable JSON records, atomic local publication with file and directory synchronization, and a nonblocking advisory owner lock. It assumes one cooperative local owner and an ordinary supporting local filesystem. It does not supply a distributed lease, power-loss theorem, adversarial isolation, remote cancellation or arbitrary external-effect exactly-once guarantee. Sources, receiving authority and truth remain with their appropriate owners.

The first producer check stopped a process after a durable tool result, then used a fresh process to finish. The resumed process replayed one model and one tool callback, invoked only the next model, retained the original two-call budget and open duty, and left the observed file effect at one. A second focused check retained and replayed an observed model exception without another actual call. These are producer checks. The independent receiver and actual model-driven construction have their own evidence and acceptance; this guide does not preclaim their results.
