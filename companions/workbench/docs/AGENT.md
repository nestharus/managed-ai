# Source-grounded construction assistant

`workbench_agent.py` connects the unchanged accepted Study83 `run_agent` controller
to the complete-source service in `source_tools.py`, a local artifact workspace,
and explicit receiving commands. The caller supplies the capable model. The
adapter is ordinary standard-library Python; it includes no provider client.

## Run a task

Keep `workbench_agent.py`, `model_bridges.py` and `source_tools.py` together. Provide
the directory containing the accepted `agent_runtime.py` and `evidence_support.py`.
Prepare a JSON configuration such as:

```json
{
  "run_id": "construction-01",
  "controller_dir": "/absolute/release/companions/workbench/controller",
  "corpus_root": "/absolute/managed-ai/domains/research/sources",
  "workspace": "/absolute/work/construction",
  "state_dir": "/absolute/observations/construction-01",
  "source_state_dir": "/absolute/observations/source-service",
  "task": "Construct and exercise the requested source-change assessor.",
  "requirements": "The complete current receiving contract goes here.",
  "sources": {"receiving-contract": "The complete public interface and acceptance conditions go here."},
  "obligations": [{"id": "independent-output", "status": "owed", "artifact": "independent.json"}],
  "max_steps": 12,
  "max_cache_bytes": 65536,
  "model": {
    "kind": "mailbox",
    "directory": "/absolute/observations/construction-01/model",
    "timeout_seconds": 900
  },
  "receivers": {
    "current-contract": {
      "argv": ["python3", "-B", "/absolute/receiver.py", "--workspace", "{workspace}", "--output", "{receiving_dir}/subject"],
      "inputs": ["/absolute/receiver.py", "/absolute/receiving-contract.json"],
      "artifacts": ["assessor.py", "independent.json"],
      "timeout_seconds": 60
    }
  }
}
```

Use actual absolute paths, complete task sources and the actual authorized
receiver. The example does not implement or supply a receiving contract.
`{workspace}` expands to the configured artifact workspace. `{receiving_dir}`
expands to this invocation's unique `state_dir/receiving/check-NNNN` directory;
use a child such as `{receiving_dir}/subject` when the receiver requires a fresh
output directory. A subsequent check receives a new directory and retains the
first check unchanged. These two path substitutions do not enable a shell.

```bash
python3 -B workbench_agent.py run --config /absolute/config.json
```

The initial model request includes the complete task and entry sources plus the
tool contract. A model action is either:

```json
{"kind":"tool","name":"source_discover","arguments":{"query":"receiving source change","limit":5}}
```

or:

```json
{"kind":"final","answer":"The exact model-produced answer."}
```

There is at most one tool action per model call. The unchanged controller records
the model and tool interactions. A final answer marks model completion; receiving
acceptance is recorded separately.

## Host-mediated mailbox

The mailbox emits `0001.request.json`, then waits for `0001.response.json`. Each
request envelope contains the controller's exact `request` and its canonical
JSON digest. The host presents that request to the actual model participant and
saves the participant's returned action as a JSON file. Deliver it atomically:

```bash
python3 -B workbench_agent.py respond \
  --mailbox /absolute/observations/construction-01/model \
  --step 1 --action /absolute/model-action-0001.json \
  --producer "actual collaboration participant"
```

Do not author a model action on behalf of a participant and label it that
participant's return. The response helper binds the supplied action to the exact
outstanding request. It refuses to replace an earlier response. Use a new state
and mailbox directory for each run; existing requests and entry records are not
overwritten.

The host transports requests and actions. It does not have to make semantic
source edits. Count callback attempts and observed intervals as such; these are
not provider-internal inference, token, or price measurements.

## Command model alternative

Replace the `model` configuration with:

```json
{"kind":"command","argv":["/absolute/model-adapter"],"directory":"/absolute/observations/run/model","timeout_seconds":900}
```

The configured command receives one complete controller request as JSON on stdin
and returns one action JSON object on stdout. Diagnostics belong on stderr. Its
actual raw stdin/stdout/stderr and exit status are retained. A nonzero exit,
timeout, invalid JSON, or invalid model action remains a failed occurrence; the
adapter does not silently repair it or retry. The supplied command owns model
access and any provider-specific terms.

## Source and artifact tools

| Tool | Input and purpose |
| --- | --- |
| `source_discover` | `query`, optional `limit`; prepare the complete inventory if needed, then select candidates and retain the selection receipt. |
| `source_read` | `paths` array; acquire complete source bodies and exact identity receipts. Known-path reads before search preparation observe only the requested bodies. |
| `source_inventory` | Optional `refresh`; obtain the complete eligibility/hash inventory, preparing it if needed. |
| `source_refresh` | Reobserve current eligible sources before a currentness claim. |
| `source_receipts` | Retrieve retained inventory, selection, acquisition and use receipts. |
| `source_stats` | Observe cache and acquisition counts. |
| `workspace_list` | Inspect exact file membership and hashes in the selected workspace. |
| `workspace_read` | `paths` array; read complete local artifact bodies within the configured file bound. |
| `workspace_write` | `files` mapping of relative paths to complete UTF-8 bodies. Each replacement is atomic; the batch is not a transaction. |
| `receiving_check` | `name` of one host-configured command. The model cannot supply arbitrary argv through this tool. |
| `accepted_artifacts` | Read this run's exact accepted artifact manifests and their source/use basis. |

The service starts without a prepared search inventory. A direct known-path read
then freshly observes only that complete body and returns `inventory_id: null`
with membership explicitly unobserved. It does not establish which other sources
were eligible or would have matched a query. Requesting discovery or the complete
inventory prepares that stronger observation; explicit refresh reobserves it.
`source_stats` can report preparation state without triggering preparation.

Once prepared, discovery uses that observed inventory. A cached body is exact to
its recorded source observation; cache availability is not evidence that the
outside source is still current. Call `source_refresh` when a new full-source
assessment is needed. A shared `source_state_dir` preserves durable receipts for
later runs. The payload cache and derived index are process-local; a new run
prepares the index only when it requests the corresponding service. Actual
preparation reads are counted separately from acquired bodies delivered to the
model. Eviction of cached bodies does not erase obligations or accepted artifact
records. No warm-start or provider-cost saving is inferred from those counts.

The caller supplies a run ID, receiving requirements and independently owed
obligations. A successful receiving command records their exact basis, the
source-selection/acquisition receipts, and the configured accepted artifact
paths with their hashes and sizes. The command inputs are pinned at entry and
checked again before invocation. Acceptance requires exit zero and unchanged
named artifact bytes across the command. It does not infer successful receiving
from a final model status, a file's mere existence, or an old acceptance of
different bytes.

## Recorded results and operating scope

The state directory contains `entry-basis.json`, exact model transport records,
each receiving invocation and return, `accepted-artifacts.json` when applicable,
`source-receipts.json`, and `run-result.json`. Receipts identify source bodies,
task, requirements, tool/receiver contracts and the actual accepted output. The
source service records the caller's acceptance; it does not independently judge
the artifact. Preserve independently owed outputs under their own requirements
when another product changes.

The workspace path checks reject escaping paths and symlinks. This is a
cooperative local tool boundary, not an operating-system sandbox: configured
receiver and model commands run as trusted local programs. Their time limits
bound ordinary subprocess waits, not every possible descendant or remote effect.
The controller's model-call limit does not certify semantic adequacy or complete
cost. Keep raw repeated interaction history with working evidence; the reusable
release needs the source files, usable guide, conclusions and selected witnesses.
