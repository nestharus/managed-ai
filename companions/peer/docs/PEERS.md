# Source-backed peer workbench

This optional worker connects the existing finite peer harness to the accepted model/tool workbench. A capable model can construct an owned contribution, read a peer's captured contribution, negotiate through messages and revise its own output. The supplied model performs the interpretation. The harness still schedules the workers and admits their exact structural returns.

Keep this complete directory together: `peer_workbench.py`, `peer_protocol.py`, `peer_harness.py`, `COMPONENTS.json`, the docs in `docs/`, and the exact pinned workbench components under `../workbench/{core,controller}/`. The source corpus is the separately unpacked current research archive.

## Start a declared peer construction

Copy `configs/example-peer-config.json` and `configs/example-harness-config.json` into your new work area. Replace the declared placeholders with absolute paths:

| Placeholder | Meaning |
| --- | --- |
| `/__PRODUCT_ROOT__` | This complete peer-workbench directory |
| `/__CORPUS_ROOT__` | Unpacked optimized research archive |
| `/__WORK_ROOT__` | Writable working directory outside both packages |
| `/__PYTHON__` | Actual Python executable |

Replace the example brief, each owner's input, complete source bodies and task with the useful work you actually commission. The example has two owners and a mailbox transport. It requires a host to answer the emitted model requests; it does not invoke a provider by itself. `receivers:{}` means there is no configured semantic artifact receiver. Supply actual receiving commands and their exact input/artifact scope when acceptance is needed.

Run the unchanged harness:

```sh
python3 /absolute/managed-ai/companions/peer/peer_harness.py \
  --config /absolute/work/harness-config.json \
  --run-dir /absolute/work/new-peer-run
```

The run directory must be new. The harness provides one current JSON request to each worker invocation. Each owner keeps its private workspace across turns; every model/workbench turn gets a fresh evidence directory. Other owners can run concurrently up to the selected harness capacity.

## Supply an actual model

With the mailbox transport, observe exact request envelopes at:

`EVIDENCE_ROOT/OWNER/revision-NNNNNN/model/0001.request.json`

Use the shared helper `model_bridges.py` function `write_mailbox_response(directory, step, action, producer=...)` to deliver an actual model action bound to that request. The action has the accepted controller shape:

```json
{"kind":"tool","name":"workspace_write","arguments":{"files":{"component.py":"complete source text"}}}
```

or:

```json
{"kind":"final","answer":"The current owner turn is ready."}
```

Use the model-generated action, not a host-authored substitute labeled as model work. A new peer turn creates a new directory and starts its callback numbering at0001. The original entry sources, peer request and this turn's observations are delivered to subsequent callbacks within that turn. Prior model histories are retained in evidence but are not copied automatically into the next turn.

For a caller-supplied command transport, configure `model.kind:"command"`, `argv` and optional `cwd`/timeout. The command reads the actual controller request on stdin and writes one model action JSON on stdout. See the copied transport's exact interface and the public contract. The command and its provider implementation remain caller-owned.

## Construct and return an owned contribution

The accepted workbench tools support complete source acquisition, workspace reads/writes and actual configured receiving. Three additional tools expose peer work:

- `peer_context` returns the exact current harness request, including prior owner state, delivered inbox and launch-time peer snapshots.
- `peer_read` reads selected files from the exact captured peer revision in that snapshot. It verifies response, manifest and selected bodies. A later peer revision or mutable working file does not replace that captured source. The return's structural status does not certify semantic correctness.
- `peer_stage_return` prepares the owner's full next return: status, model state, finite current obligations, explicit delivered-message acknowledgments, directed messages and selected owner-workspace file paths. It captures exact bytes under those same paths. A successful ordinary model final action then permits the adapter to return the staged protocol object. Staging alone does not send or acknowledge anything.

Stage after relevant edits. If a selected file changes between staging and the model final, the turn fails explicitly. If you revise the intended file set or messages, stage a new complete candidate before final. A failed, malformed or step-limited model turn produces no successful peer response. The unchanged harness then applies its normal whole-run failure or acceptance rule; this adapter adds no silent retry.

A model final answer completes only its bounded workbench turn. `waiting`, `continue` and `complete` belong to the peer owner. Even a complete owner can be reactivated by a later admitted peer message before the finite run closes. Only the actual harness may decide its configured run has no pending admitted work. Its final captured submission still requires the real whole-product receiving task.

## Preserve source and duty distinctions

The state envelope carries model-selected useful state, exact independently continuing duties and cumulative accepted-turn observations. The original-history duty objects supplied as configuration `obligations` remain unchanged in `state.continuing_duties` and in each workbench basis. They are separate from the peer owner's finite `obligations` array, which must be empty for that owner to declare complete.

Every turn creates a fresh source service. Named reads, discovery preparation, receipts and local receiving outcomes retain their actual turn and source basis. Older receipt/acceptance records remain in their evidence directories. They are not relabeled current, erased by the new turn, or inferred merely from a copied path. The owner workspace persists and can be read through the workbench tools.

## Accounting and limits

The owner configuration's `max_steps` is a per-turn attempted-model-call bound. `adapter-result.json` records actual local calls, source statistics, peer reads, returned bytes and any failure. The returned state sums successful returned-turn model/tool/peer-read observations. It supplies no independent run-wide model cap. The harness separately bounds its finite worker invocations, local time, output capture and artifact bytes; base64 peer-return text consumes its output allowance.

The tool is for cooperative local workers. It is not a hostile-process sandbox, provider client, source-truth evaluator, external-effect authorization mechanism or arbitrary remote cancellation/exactly-once service. Inspect the [public interface](PEER-CONTRACT.md) and [representation clarification](PEER-CONTRACT.md) for the complete receiving contract.
