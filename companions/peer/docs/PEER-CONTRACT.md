# Peer Workbench Contract (`companions/peer/peer_workbench.py`)

`peer_workbench.py` adapts the shared workbench (`companions/workbench/`) to participate as a single declared owner inside a [Peer Harness v1](PROTOCOL.md) run without introducing a second runtime or scheduler.

## Entrypoint and shared component manifest

```sh
python -B companions/peer/peer_workbench.py --config /path/to/owner-config.json
```

- Shared workbench modules are declared in `companions/peer/COMPONENTS.json` (`schema: peer-workbench-acquired-components/1`) using `companions/`-relative POSIX keys with zero `..`.
- In Git checkouts, `COMPONENTS.json` is declarative (`{"source": "..."}`); `peer_workbench.py` loads strictly from `companions/` inside an isolated private package namespace, verifies `"identity"` (`bytes` + `sha256`) whenever present (e.g., in stamped release bundles produced by `engine/release/package.py`), and always hashes every loaded component live into `input-identities.json` for run provenance.

## Request, turn state, and return handling

1. Reads the exact `peer-request/1` JSON supplied by `peer_harness.py`, consuming `manifest_path`, `return_path`, and `artifacts_dir` directly.
2. Persists per-owner turn state, source observations, and durable workbench checkpoints inside the owner's declared workspace (atomic writes + strict JSON).
3. Exposes bounded read-only library source tools (`SourceService`, guaranteed `close()` in `finally`) and declared artifact proposal tools to the caller-supplied model adapter.
4. Emits a `peer-return/1` JSON at `return_path` containing attributed messages, artifact proposals under `artifacts_dir`, source acquisition accounting, and honest failure/incomplete statuses.

## Accounting and boundaries

- Records exact loaded component digests, contract doc identity (`docs/PEER-CONTRACT.md`), acquired source digests, and model invocation counts.
- Does not schedule other peers, mutate another owner's workspace, or infer semantic acceptance from message delivery.
- For structural representation checking within peer turns, see [../../representation/CONTRACT.md](../../representation/CONTRACT.md).
