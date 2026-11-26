# Restartable Workbench and Durability Contract

`resumable_workbench.py` and `durable_agent.py` persist checkpointed turn state, exact source observations, and pending callback custody so a source-backed construction can be inspected and resumed across process restarts without silently repeating uncertain effects.

## Start and supply a model

Configure a fresh `state_dir` and `workspace` outside the repository, validate with `inspect_config.py`, then start:

```sh
python -B src/managed_ai/companions/workbench/core/resumable_workbench.py start --config /path/to/config.json
```

Models are supplied either via a finite command adapter (`argv` JSON) or a host-mediated mailbox reply (`respond`). All persisted state uses strict JSON (rejecting duplicate keys and `NaN`/`Infinity`) and durable atomic writes (`fsync` + atomic replace).

## Inspect and continue

Inspect the saved durable state before resuming or resolving an interrupted turn:

```sh
python -B src/managed_ai/companions/workbench/core/resumable_workbench.py inspect --state-dir /path/to/work/state
python -B src/managed_ai/companions/workbench/core/resumable_workbench.py resume --config /path/to/config.json
```

- `inspect` reports current phase, step count, pending external/model callbacks, captured basis hashes, and source observations without mutating state.
- `resume` verifies that the configured workspace, library selection, and captured basis still match the persisted checkpoint. Any basis, selector, or workspace drift raises `BasisMismatchError` (`WorkbenchBasisMismatch`) and returns a structured `binding_mismatch` result rather than continuing silently on stale inputs.

## Resolve a genuinely unknown outcome

If a model or receiver callback exited before its completion was saved, the occurrence stays `unresolved` in durable state; `resume` will not automatically repeat the side effect. Inspect the original occurrence, determine what actually happened at the external owner, and record an explicit allowlisted resolution payload (`validate_resolution_payload()` rejects unknown fields) via `resolve` before continuing.

## Source observations and scope

- `SourceService` (`source_tools.py`) acquires complete verified source bodies via `src/managed_ai/library/` and records exact `(id, bytes, sha256)` observations alongside search coverage limits.
- Cached or listed search matches do not claim participant comprehension or native store adoption; accepted workspace artifacts must still pass the owning store's revision and basis guards.
- All relative paths reject traversal (`..`), backslashes, NUL bytes, Windows drive specifiers, symlinks, and NTFS reparse points.
