# Collector: admission, returns and uncertainty

## Admission and execution

A job has a caller-selected identity, consumer, argv, existing working directory and UTF-8 originating brief. Admission retains that brief's actual bytes. An identical admission is idempotent; changing the request under the same identity is rejected. Commands are executed without a shell and receive null stdin. The environment is inherited, not snapshotted or isolated.

One owner lock excludes overlapping public CLI operations. A durable dispatch marker is written before requesting a detached worker. A durable started marker precedes child creation. An attempted job is never automatically retried. These choices preserve visible uncertainty after interruption rather than promising exactly-once completion. Use a new identity only when you deliberately authorize another attempt.

`admit` records a job without starting it. `resume` dispatches every admitted job in the selected root that has no recorded attempt. `list` and `receive` inspect actual material; they do not start work. The name `resume` does not promise to reconnect to or restart an interrupted process.

An embedded caller with a known identity can use `views(job_id="known-job")`
to inspect only that job. The exchange uses this route for packet receipt.
An absent or malformed selected record remains an explicit unresolved result;
the requested identity is not presented as a verified stored identity. See the
[API](api.md) for locking and the unscoped list/consumer forms.

## Receiving material

| Reported status | What was observed | Meaning for continuation |
|---|---|---|
| `queued` | Admitted request with no dispatch record | `resume` may make its first attempt |
| `running` | Worker lock held at observation | Later inspection may obtain a terminal return |
| `uncertain` | Attempt record without a terminal result or active worker lock | The worker may be starting or lost; this does not show that the command never ran |
| `completed` | Exit collected and streams bound to the terminal record | Inspect the actual code, signal and output; nonzero and signal exits are included |
| `launch-failed` | The child could not be started | A launch error is retained without an invented child exit |
| `result-integrity-unresolved` | Collected streams do not match the terminal binding | Keep the mismatch visible; do not use the body as an integrity-verified result |
| `record-unresolved` | A request or related record could not be read or validated | The unattributable job remains visible even in a named consumer view |

These states describe transport observations, not task completion. An uncertain or unresolved entry calls for inspection and a current decision; it is not permission to repeat an effect.

Binary streams are returned as base64 with byte counts and hashes; UTF-8 text is also included when decodable. Views without a terminal record are snapshots. The terminal record binds the attempt and request to collected stream hashes. This is integrity checking within trusted local storage, not authentication against a hostile writer who can replace the records and hashes together.

The original brief and current brief are separate. `origin-brief-unchanged` reports equal bytes only; it does not establish unchanged surrounding meaning or permission. Delivery grants no further effect authority and establishes no semantic acceptance.

## Operating limits

Use trusted finite foreground jobs and bounded output on local POSIX storage. There is no automatic timeout, cancellation, process-tree management, arbitrary reattachment, retry, remote execution or hostile-code isolation. Killing a worker can leave a child alive and lose its eventual exit status. Descendant processes retaining stream handles can violate the finite-foreground assumption. Dispatch-before-spawn interruption can leave work permanently uncertain. There is no arbitrary power-loss guarantee.

The caller supplies retention and cleanup policy. Current source identity, permissions and the consequences of running a command remain the caller's responsibilities. Runtime state is an output of this component, separate from the distributable source and from any research account about this component.

The added [continuation exchange](exchange.md) uses this collector unchanged for command transport. Its receiving and publication contract is separate.
