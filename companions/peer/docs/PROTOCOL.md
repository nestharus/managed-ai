# Peer Harness v1 — Protocol Contract (`companions/peer/`)

`companions/peer/peer_harness.py` is a cooperative, cross-platform (Windows and POSIX) subprocess, inbox, and checkpoint harness for running caller-configured participant processes with bounded time, output, and artifact custody.

## CLI and configuration

```sh
python -B companions/peer/peer_harness.py --config /path/to/config.json --run-dir /path/to/new-run-dir
```

- `--run-dir` must be a fresh directory outside `engine/` and `companions/` with ordinary (non-symlink, non-reparse) ancestors.
- Config declares participants (`owners`), per-owner `argv`, environment allowlist (minimal OS variables preserved on Windows/POSIX), byte/time limits, and optional artifact receivers.
- Shared protocol helpers live in `companions/peer/peer_protocol.py`. All paths enforce clean relative POSIX syntax (rejecting `..`, backslashes, NUL bytes, Windows drive specifiers, symlinks, and NTFS reparse points).

## Worker request (`peer-request/1`)

For each scheduled turn, the harness writes an immutable `peer-request/1` JSON packet containing:
- Run identity, owner identity, turn number, and inbox messages delivered to that owner.
- Explicit output coordinates: `manifest_path`, `return_path`, and `artifacts_dir` (workers write strictly to these declared coordinates rather than assuming parent-directory layouts).
- Declared bounds (`max_output_bytes`, `timeout_seconds`, allowed artifact target prefixes).

## Worker return (`peer-return/1`) and artifact custody

A worker writes its `peer-return/1` JSON (messages, proposed artifacts, status, account) to `return_path` and proposed files under `artifacts_dir`. On process exit:
- Pipe readers drain stdout/stderr with strict byte caps (threaded pump + `queue.Queue` on both Windows and POSIX) and terminate process groups cleanly on timeout.
- `accept_return()` validates strict JSON, checks every proposed artifact path against `target_path()` and `is_relative_to()` containment guards, hashes accepted bytes, and appends fsynced entries to `events.jsonl`.
- Unresolved timeouts or malformed returns are recorded as explicit failure events and are never silently retried.

See [PEER-CONTRACT.md](PEER-CONTRACT.md) for the source-backed `peer_workbench.py` adapter. Tests: `python -B -m unittest discover -s tests/companions/peer`.
