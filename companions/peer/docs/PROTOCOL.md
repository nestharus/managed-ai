# Peer harness v1 — frozen implementation contract

This normative interface is recorded before implementation and before root's independent worker qualification. `PROTOCOL.json` supplies illustrative objects; this document specifies required fields and receiving behavior. Any later correction receives a separately retained amendment. The finite configured roster is this run's accounting boundary, not a fixed research ontology. V1 does not add owners during a run. An adapter may arrange other work only if its exposure/resources/cancellation are separately declared; such work is not silently included in core accounting.

## CLI and configuration

```
python /absolute/path/peer_harness.py --config /absolute/config.json --run-dir /absolute/new-run-directory
```

The run directory must not already exist. The CLI prints one concise JSON result pointer on stdout. Exit 0 means structurally admitted submission, exit 1 means an executed run ended without submission, and exit 2 means configuration/CLI/setup rejection. Existing evidence is never overwritten. Creation of `RUN_DIRECTORY/CANCEL`, SIGINT or SIGTERM requests cancellation. No retry occurs within a run.

Required config fields: `schema: "peer-harness-config/1"`, `run_id` (nonempty string), `brief` (any JSON), `workers` (nonempty array), `limits` (object). A worker has required `owner`, `command`, `input`; optional `env` defaults to `{}`. `owner` and send-local message IDs match `[A-Za-z0-9_.-]{1,64}` and cannot be `.` or `..`. Owners are unique. A command is a nonempty array of nonempty strings and is invoked without a shell. Environment contains `PATH: os.defpath` and `LANG: C.UTF-8`, plus the worker's explicit string-to-string `env`. No parent credential environment is inherited or inspected. Worker commands should use absolute executable and script paths.

All limit fields are required: positive integer `parallelism`, `max_turns`, `max_output_bytes`, `max_artifact_bytes`; positive finite numeric `wall_seconds`, `turn_seconds`, `terminate_grace_seconds`. Booleans do not count as numbers. `parallelism` is a maximum; each owner has at most one active turn. `max_turns` counts launched or attempted child invocations across all owners, including failures. `wall_seconds` and `turn_seconds` bound admitted local execution using monotonic time. `max_output_bytes` bounds retained stdout plus stderr per turn; overflow is fatal and the retained prefix is marked truncated. `max_artifact_bytes` bounds decoded files in each accepted owner return and also the final assembled submission. Termination grace and final evidence persistence may extend overall elapsed time and are reported. This interface makes no monetary, model-token, memory or remote-process limit claim.

Additional unknown top-level configuration, worker, limit and return fields are rejected. Opaque JSON fields admit arbitrary JSON values, but nonstandard NaN/Infinity numbers are rejected throughout. Full initial configuration and every generated request are saved. V1 is for cooperative local processes, not filesystem isolation against adversarial children.

## Worker request

One JSON object is supplied through stdin, then EOF. Exactly one JSON object must be written to stdout, allowing surrounding JSON whitespace. Stderr is independent captured diagnostic output. Request fields are:

- `schema: "peer-request/1"`, `run_id`, `owner`.
- `turn`: global invocation integer starting at 1.
- `revision`: this owner's next accepted revision, starting at 1.
- `brief`, `input`: the unchanged configured opaque common and owner inputs.
- `state`: the owner's last accepted opaque state, initially null.
- `inbox`: an array of `{id, from, to, payload}`. Global message `id` is `sender:local-id`. Items unacknowledged by an earlier turn are supplied again with the same ID and payload.
- `peers`: an object keyed by every other owner. Initially each value is `{revision: 0, status: "continue", return_path: null, artifacts_dir: null}`. After an accepted return it supplies that owner's latest revision/status and absolute paths to its preserved `response.json` and decoded `files/` directory. This is a launch-time snapshot; concurrently arriving later returns are not inserted into an already running process's request.
- `workspace`: absolute private owner working directory, used as subprocess cwd and retained through that owner's turns.
- `limits_remaining`: `{wall_seconds, turns, turn_seconds, max_output_bytes, max_artifact_bytes}` at launch. The authoritative deadlines remain host-owned.

The full peer return is available through its exact checkpoint path; the harness does not summarize its semantic content. A worker must not change preserved peer or host evidence. This is an explicit cooperative requirement; OS permissions are not a sandbox boundary.

## Worker return

Required fields are `schema: "peer-return/1"`, matching `owner`, matching `revision`, `state` (any JSON), `status`, `obligations` (array of arbitrary JSON values), `ack` (array of unique message ID strings), `send` (array), and `files` (object). Optional `reported_usage` is arbitrary JSON and is preserved as unverified. Fields have these meanings:

- `status` is `continue`, `waiting` or `complete`. Continue requests another turn. Waiting waits for inbox work. Complete declares this owner's selected work complete and requires an empty obligations array. Waiting with no available work can produce a non-submitted `stalled` result. New admitted inbox work schedules even a previously complete owner; no owner is permanently terminal before the run closes.
- `obligations` is the owner's current full declared outstanding-work inventory. The harness preserves and counts it but never interprets, fulfills or deletes its entries itself. A later return replaces it. Truth/completeness of the declaration remains an owner and independent-receiver question.
- `ack` must name only IDs in that exact request's inbox. It removes those IDs from the owner's pending inbox. Delivery into stdin and the return's declared receipt are separate recorded facts; acknowledgement does not establish semantic incorporation. Items arriving while the process ran cannot be acknowledged unseen.
- Each `send` element contains exactly `{id, to, payload}`. `id` is sender-local, `to` names a configured owner (self-send is allowed), and `payload` is arbitrary JSON. A repeated sender-local ID with identical recipient/payload is idempotent even if already acknowledged; it creates no second obligation. Reuse with different recipient/payload is fatal `protocol_error`. Unique send entries and duplicate send attempts remain visible in raw returns. Only messages in a fully validated successful return are admitted. A failed return may describe an attempted send but creates no accepted delivery.
- `files` maps relative POSIX target paths to strict base64 strings. Paths are nonempty normalized relative file paths, with no empty, `.`, `..`, backslash, NUL or absolute component. A path and its child cannot both be files. Values decode to exact submitted bytes; binary is allowed. Each revision is the owner's complete replacement file mapping, not a patch. Earlier revisions remain preserved. Empty mappings are allowed. The host never rewrites file contents.

A return is accepted only after process exit code 0 and complete structural, identity, message and file validation. Its decoded file checkpoint and raw JSON response are written before its state/messages become visible to later turns. Fatal nonzero exit, bad JSON, schema mismatch, invalid identity/path/ack/send, duplicate-ID conflict or file limit ends the run without an admitted submission. There is no automatic repair or retry. Independent later runs must retain the first outcome.

## Scheduling, closure and artifact receiver

The host uses a stable FIFO ready queue. Owners enter it in configured order; new pending work and `continue` requeue an owner after its turn. Active owners never have a second concurrent turn. Returned messages for an active owner stay pending until a later invocation. Completion is tested only when no local turn is active. A full run is eligible to close only when every latest owner status is complete, every owner's obligations array is empty and all admitted messages are acknowledged. Otherwise exhausted execution limits stop the run; an idle run without ready work is stalled.

The host then closes admission to this finite run, mechanically assembles the latest captured mappings into `submitted/`, and writes `result.json`. Any duplicate target path or file/directory collision across owners is `artifact_conflict`, regardless of byte equality. Peers must resolve ownership in their own returns. Earlier superseded files are excluded. The final bytes come from preserved return captures, not mutable owner workspaces. A canonical sorted manifest records each target path, byte count, SHA256, owner and revision. The receiver must use this recorded submitted root/manifest, not an assumed workspace.

This deliberately selects a closed run epoch. It is not a claim that all unrelated work must stop for any useful submission. An authoritative immutable scoped artifact/obligation manifest can permit unrelated work to continue; that alternative is outside this small v1 interface. Neither route needs global semantic agreement or a central semantic merger.

## Results and evidence

`result.json` has `schema: "peer-harness-result/1"`, `run_id`, `submitted` boolean, `done_reason`, `error` (null or explanatory string), `elapsed_seconds`, `total_turns`, `owners`, `messages`, `artifact`, `local_resources`, and `harness_identity`. `owners` records every owner's latest revision, status and declared obligations. `messages` records admitted message count, acknowledged count and pending identities. `artifact` is null unless submitted; otherwise `{root, manifest, manifest_sha256}`. `local_resources` includes actual aggregate child CPU where the platform provides it, recorded local process wall intervals and captured byte totals. Worker `reported_usage` is not added to measured usage or relabeled dollars.

`done_reason` is `submitted`, `cancelled`, `wall_limit`, `turn_limit`, `worker_timeout`, `output_limit`, `worker_failed`, `launch_error`, `protocol_error`, `artifact_conflict`, `artifact_limit`, `stalled`, or `harness_error`. Host cancellation is checked before deadline decisions at each loop; after that, the first detected fatal condition selects the reason and all active process groups are stopped. A turn whose output arrives at or after its enforced deadline is not admitted as timely success. Process stop is cooperative POSIX process-group cleanup with a bounded TERM-to-KILL grace. Escaped descendants or remote model calls are not covered.

Per-turn evidence lives in `turns/000001-owner/` (`request.json`, `stdout.bin`, `stderr.bin`, `process.json`). Accepted checkpoints live in `returns/OWNER/000001/` (`response.json`, `files/`, `manifest.json`). `events.jsonl` records launch, accepted return, message disposition and final status. Config and exact harness identity are retained. Raw first returns and partial failures remain even when no submission is admitted.

Root may supply an optional mailbox process that waits for a separately launched investigator's exact response. Such a wrapper is an external adapter. It must separately record investigator launch/return/interruption and avoid treating the core's local termination as provider cancellation or complete cost accounting. No current success claim is implied by this interface.


# Interface clarification 01 — before implementation and first execution

Root requested exact result/manifest/lifecycle encoding after freezing independent controls. This clarification supplies the previously unspecified representation; it changes no completion or scheduling condition.

`result.artifact.manifest` is an **inline array**, sorted by `path` using Python/Unicode code-point string order. Each entry is exactly `{path, bytes, sha256, owner, revision}`: relative target path, integer byte count, lowercase hex SHA256, owner string and positive integer accepted revision. `result.artifact.root` is the absolute `submitted/` directory. `result.artifact.manifest_sha256` hashes the inline manifest array's canonical JSON bytes. The same array is also saved as `submitted-manifest.json` for convenience.

Canonical JSON bytes throughout the harness are UTF-8 of Python `json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))`, with **no trailing newline**. Human-readable retained JSON files may use indentation plus a newline. Duplicate send identity compares canonical JSON of exactly `{to, payload}`; sender and local ID form its lookup key. Thus numerically distinct serialized JSON types such as integer `1` and float `1.0` are not silently collapsed by Python equality. Invalid Unicode surrogate codepoints are rejected through canonical UTF-8 encoding.

Every accepted owner checkpoint has `manifest.json` containing an inline array of the same five-field entries for that owner's returned files. The final receiver uses `result.artifact.manifest`; no version- or artifact-manifest path string is substituted for the array.

Every attempted turn's `process.json` has these required fields:

- `schema: "peer-process/1"`, `owner`, `turn`, `revision`.
- `pid`, `process_group`: integers after launch, otherwise null.
- `started_at_utc`, `ended_at_utc`: ISO 8601 UTC strings.
- `elapsed_seconds`: nonnegative local monotonic interval from launch attempt through local cleanup.
- `returncode`: process exit code when reaped; null if launch failed or a cleanup failure prevents a measured code.
- `done_reason`: `completed` for a process that exited 0 and whose bytes are available for validation, otherwise the detected local ending reason. Protocol validation may subsequently reject a locally completed process; the run result and events record that distinct rejection.
- `stdout_bytes`, `stderr_bytes`: retained byte counts.
- `observed_output_bytes`: total stdout/stderr bytes actually read, including bytes discarded after the retention bound.
- `output_truncated`: true if any observed output was not retained or open output streams had to be closed by the host.
- `group_cleanup`: `{term_sent, kill_sent, streams_closed_by_host}`, all booleans. These observations do not establish remote cancellation or containment of a deliberately escaped process.
- `error`: null or explanatory string.

The harness records each valid accepted return and each admitted/acknowledged message in `events.jsonl`. The final `messages.pending` is an array of unacknowledged global IDs; `messages.admitted` and `messages.acknowledged` are integer counts. Owner entries in `result.owners` are keyed by owner ID and include `revision`, `status`, `obligations`, `return_path` and `artifacts_dir`. No submitted result can include pending IDs or nonempty owner obligations.
