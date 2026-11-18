# Peer workbench contract

The adapter composes the finite peer harness, callable controller and source workbench. It does not add scheduling, provider inference, dynamic membership or semantic acceptance. The intended environment is cooperative local processes on the same filesystem.

## Entrances and shared components

CLI: `python3 /absolute/peer_workbench.py --config /absolute/owner-config.json`. Read one exact harness `peer-request/1` JSON object from stdin. On success write only one `peer-return/1` object to stdout and exit 0. A completed local construction that cannot produce a valid peer return exits 1 with a concise JSON diagnostic on stderr, no stdout response, and retained evidence. Configuration/request/setup rejection exits 2. The harness remains responsible for accepting or rejecting the returned protocol object and its files.

Callable: `run_peer_turn(config: dict, request: dict, *, model=None) -> dict`. An optional callable `model` supplies the actual `run_agent` model callback; otherwise the configured transport is used. The return has `ok` boolean, `response` (peer-return object or null), `report` (local observation summary) and `evidence_dir` (absolute path). After a successfully allocated turn, ordinary model/tool/finishing errors are recorded and return `ok:false`. Invalid configuration/request or inability to allocate a fresh evidence directory raises `ValueError` or `OSError`. The CLI maps those setup exceptions to exit 2.

The product uses the shared `workbench_agent.py`, `source_tools.py`, `model_bridges.py`, and `controller/{agent_runtime,evidence_support}.py` in this directory. `COMPONENTS.json` pins their current byte counts and SHA-256. There is one owner for each implementation. Launch the separate `../harness/peer_harness.py`; each configured worker receives its actual owner and workspace from the harness.

## Owner configuration

Required keys are `schema:"peer-workbench-config/1"`, `task` (string), `sources` (mapping string IDs to complete string bodies), `corpus_root` (absolute existing research root), `evidence_root` (absolute directory outside the editable workspace), and `model` (object). Optional keys are `max_steps` (nonnegative integer, default12), `max_file_bytes` (positive integer, default2097152), `max_cache_bytes` (nonnegative integer, default65536), `receivers` (workbench receiving-command mapping, default{}), and `obligations` (array of exact independent duty objects, default[]). Booleans are not integer limits. Unknown top-level configuration fields are rejected.

`model` is either `{"kind":"mailbox","timeout_seconds":900}` or `{"kind":"command","argv":["/absolute/executable","/absolute/model.py"],"cwd":"/optional/absolute/directory","timeout_seconds":900}`. Timeout is positive finite. Directory selection is adapter-owned and cannot be supplied: each turn uses its own fresh `model/` directory. An injected callable replaces transport dispatch but leaves its declared configuration/source basis visible. No provider credentials are discovered or supplied by this product.

The exact JSON-compatible configuration is bound by a canonical digest. A later turn under this adapter must carry its previous adapter state from the same owner/configuration. Root bindings are part of this configuration identity; changing them requires a new declared run rather than silently rewriting an old occurrence.

Receiver entries use the accepted Workbench interface: `argv` strings, optional `inputs` exact pinned files, `artifacts` relative owner-workspace paths and `timeout_seconds`. `{workspace}` and `{receiving_dir}` are replaced by the acquired workbench. Receivers execute with the owner workspace as cwd and must obey the cooperative scope. A receiving callback's accepted status has the scope of that configured receiver, separate from the harness's structural acceptance.

## Request, turn state and evidence

The request follows the existing frozen peer harness protocol exactly. The adapter validates required fields and types, owner/revision consistency, current workspace location and canonical finite JSON. Its complete detached object is exposed in the model's entry sources under reserved `peer-request`; `peer_context` also returns that same object. The original task and complete user sources are supplied alongside reserved `peer-workbench-contract` and `workbench-tool-contract`. The latter contains the complete instantiated tool contract, including configured receiver definitions. User source IDs cannot replace these three reserved bodies.

The workbench uses the harness-supplied persistent owner workspace. Every invocation creates a fresh directory `evidence_root/OWNER/revision-NNNNNN/`; existing directories are refused. Workbench state lives under `workbench/`; model transport under `model/`. Each turn starts a new bounded model history and a new source service. Prior workspace files and the exact harness-delivered owner state remain available. Prior source/receiving records remain in prior evidence directories; they are not silently presented as fresh reads, restored cache/index state or new acceptance.

The returned `state` is an adapter envelope with schema `peer-workbench-state/1`, owner, revision, exact `worker_config_sha256`, model-supplied `model_state`, exact `continuing_duties` from configuration, `completed_turns`, cumulative local callback/read counts and `last_turn` evidence locator. The next model sees that whole prior envelope in the exact request. A previous envelope must identify the immediately previous accepted revision; revision1 requires null prior state.

**Independent history duties are not the harness owner's finite completion inventory.** They appear unchanged as `continuing_duties` in every successful peer state and as the workbench's source/receiving basis obligations. The model's staged `obligations` field is the separate current peer-owner inventory; `complete` requires that field to be empty. A complete peer owner does not close the continuing history duties. Incoming messages are delivered in the exact request and only explicit admissible acknowledgments remove them at the host.

Retain `request.json`, `config.json`, `input-identities.json`, `run-result.json`, `adapter-result.json`, actual selected peer-read records, and on success exact `peer-response.json`. Workbench and transport retain their own actual receipts. The adapter must not overwrite an earlier turn.

## Model tools

All accepted Workbench tools remain available under their existing names and contracts: source discovery/read/inventory/refresh/receipts/stats; workspace list/read/write; configured receiving; accepted artifact listing/read. The adapter adds three tools:

### `peer_context {}`

Returns `{"request": <exact detached request>}`. This is the launch-time request, not a subscription or fresh query for peer progress.

### `peer_read {"owner":"PEER","paths":["relative/file.py"]}`

The peer must be a different owner in this request's peer snapshot with a positive captured revision. Paths must be unique normalized relative file paths, and the selected files must be present in that checkpoint's manifest. No arbitrary absolute or mutable peer-workspace read is exposed.

The adapter derives the expected checkpoint from the actual harness root and verifies that the supplied peer `return_path` and `artifacts_dir` identify it. It validates the checkpoint's response owner/revision, its full captured manifest and decoded returned mappings against each other, and each selected on-disk captured body against its manifest/returned bytes. Symlink traversal and paths outside that checkpoint are refused. Per selected file, the configured `max_file_bytes` bound applies.

The result includes owner/revision, exact response and manifest byte identities, selected `files` mapping with complete UTF-8 `body` or complete `base64` when not UTF-8, byte counts and SHA-256, and the explicit scope “captured structurally accepted peer contribution; no semantic acceptance or latest-currentness claim.” An observed mismatch or missing checkpoint is a tool error, not a fallback to mutable work. Actual successful read calls and selected body bytes are recorded. Caller-declared peer snapshots are trusted only under the cooperative accepted-harness boundary; these checks are not authentication against a malicious host.

### `peer_stage_return {"status":"continue|waiting|complete","state":<JSON>,"obligations":[],"ack":[],"send":[],"files":["relative/file.py"]}`

All six fields are required; no extra fields. `ack` is a unique subset of the inbox IDs actually delivered to this request. Each send item is exactly `{"id":"sender-local-id","to":"configured-owner","payload":<JSON>}` under the harness's identifier rules. Changed reuse of a send identity within this staged return is refused; the harness retains cross-turn authority. Status complete requires an empty current `obligations` array. Other statuses preserve that array exactly. The model's `state` becomes the envelope's `model_state`.

`files` is a unique list of normalized owner-workspace file paths, possibly empty. The tool reads and captures their exact bytes, checks per-file and current harness artifact allowance, and stages the complete returned mapping. Symlinks, non-files, escapes and file/directory conflicts are refused. This operation does not send messages, acknowledge the inbox at the host, invoke the receiver or close the owner. A later successful staging replaces this turn's earlier staged candidate; both tool observations remain in the local trace.

The tool returns the staged status, counts and exact file identities. The model must then finish through the ordinary controller's final action. On final completion the adapter requires a staged return and verifies the selected workspace files still match their captured bytes. If selected files changed after staging, it refuses finishing; the model should stage after its last relevant edit. Failed or step-limited model execution creates no successful peer response, even if a candidate was staged. Root must not convert it into a semantic success.

The final `peer-return/1` receives owner/revision from the exact request, the staged status/obligations/ack/send, the adapter state envelope and base64 encoding of the captured selected files. The model's final answer is retained in local evidence. Model final, staged owner status, host acceptance and whole-product receiving remain four distinct observations.

## Accounting and scope

`max_steps` bounds attempted model callbacks for this turn only. The report records actual controller `model_calls` and `tool_calls`, actual successful peer-read calls/selected body bytes, current source-service counters, returned-file bytes and material evidence identities. The state envelope adds cumulative model/tool/peer-read counts over successfully returned owner turns. A failed turn's counts remain in its local evidence and are not relabeled as accepted predecessor state. These cumulative values are observations, not a separately enforced run-wide limit.

The unchanged harness separately bounds worker turns, local time, captured stdout/stderr and submitted artifact bytes. Its per-turn stdout limit includes the full base64 peer return. No model-token, provider-call, price, remote cancellation, sandbox, broad truth, native advantage or large-scale performance claim follows. Ordinary artifact acceptance and source currentness retain their acquired boundaries.


## Return representation

`peer_read` returns:

```json
{
  "owner": "peer-owner",
  "revision": 1,
  "response_identity": {"bytes": 123, "sha256": "..."},
  "manifest_identity": {"bytes": 123, "sha256": "..."},
  "files": {
    "component.py": {"bytes": 123, "sha256": "...", "encoding": "utf-8", "body": "complete text"}
  },
  "scope": "captured structurally accepted peer contribution; no semantic acceptance or latest-currentness claim"
}
```

A non-UTF-8 file uses `encoding:"base64"` and `base64:"..."` instead of `body`. The response/manifest identities measure their actual captured file bytes, including their original JSON formatting; file identities measure decoded artifact bytes.

Every attempted peer read records `peer-read-NNNN.json` inside this turn's evidence directory. It includes `attempt`, exact `arguments`, `successful`, `io_bytes` (all bytes actually read from response, manifest and selected captured files), and `selected_body_bytes`. Successful records also have `owner`, `revision`, `response_identity`, `manifest_identity` and `selected_files` identities. A failed read records its actual `error` type/message; successful-read counters do not relabel a failed partial read as a successful observation.

`adapter-result.json` is the callable return's `report` object. Its principal keys are `schema:"peer-workbench-result/1"`, `ok`, `owner`, `revision`, `model_status`, `model_calls`, `tool_calls`, `peer_read_attempts`, `peer_read_calls`, `peer_read_bytes`, `peer_read_io_bytes_all_attempts`, `source_stats`, `accepted_artifacts`, exact `continuing_duties`, `returned_file_bytes`, `response_identity`, `per_turn_model_limit`, `run_wide_model_limit_enforced:false`, `elapsed_seconds`, `error`, `scope`, and nullable provider inference/token/price fields.

`peer_read_calls` counts successful reads; `peer_read_bytes` counts their selected decoded artifact bodies only. `peer_read_io_bytes_all_attempts` separately includes checkpoint response/manifest material and partial failed reads. Neither count is the complete model-input or whole-process cost.

The returned state envelope's `accounting` object has exactly nonnegative integer `model_calls`, `tool_calls`, `peer_read_calls` and `peer_read_bytes`. These cumulatively add the corresponding local values over this owner's successful returned turns. They are detached observations supplied for later host acceptance. Failed-turn usage remains in its separate local result. `completed_turns` counts successful model/workbench turns that produced a peer return, including turns whose staged owner status is waiting or continue; it does not assert that the peer owner or whole product completed.

The state envelope's `last_turn` is `{evidence_dir, request_sha256, model_status, scope}`. `request_sha256` identifies canonical JSON of the detached exact request object without a trailing newline. The request's semantic JSON fields are preserved; the harness retains the original stdin request serialization at its own turn directory.


## Delivered model contract

`peer-workbench-contract` contains this complete current contract. `workbench-tool-contract` contains the actual instantiated Workbench tool contract. Each delivered source is bound in `input-identities.json`, which also pins this document and the shared component manifest. A failure to persist a staged peer response yields no successful response.
