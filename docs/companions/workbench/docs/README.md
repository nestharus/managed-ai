# Shared Model and Tool Workbench (`companions/workbench/`)

An optional, standard-library workbench that connects caller-supplied model and tool adapters to Managed AI's shared source library (`engine/library/`) and native stores (`engine/store/`). No model provider keys or inference daemon are bundled.

## Layout

- `core/`: `inspect_config.py` (preflight), `workbench_agent.py` (single-run assistant), `resumable_workbench.py` & `durable_agent.py` (restartable checkpoint/resume), `source_tools.py` (exact library source reads & bounded search), `model_bridges.py` (command/mailbox bridges), `io_utils.py` & `config.py` (shared strict JSON, atomic writes, unfollowed path checks).
- `controller/`: `agent_runtime.py` & `evidence_support.py` (bounded callable controller loop).
- `configs/`: example configuration templates.

## Preflight and launch

Inspect a configuration before starting any model call:

```sh
python -B companions/workbench/core/inspect_config.py --config /path/to/config.json
```

Preflight validates JSON strictness (no duplicate keys or non-finite numbers), checks that `workspace`, `state_dir`, and `library` are ordinary unfollowed paths outside the engine, verifies declared tool/receiver commands, and confirms the configured source basis without mutating state.

### Single-run assistant (`workbench_agent.py`)

```sh
python -B companions/workbench/core/workbench_agent.py run --config /path/to/config.json
python -B companions/workbench/core/workbench_agent.py respond --workspace /path/to/workspace --reply /path/to/reply.json
```

Supports both command-adapter model bridges and host-mediated mailbox exchanges. The controller bounds tool turns, bytes, and wall time; `SourceService` reads exact complete bodies from the configured library and closes handles in `finally`.

### Restartable construction (`resumable_workbench.py`)

```sh
python -B companions/workbench/core/resumable_workbench.py start --config /path/to/config.json
python -B companions/workbench/core/resumable_workbench.py inspect --state-dir /path/to/work/state
python -B companions/workbench/core/resumable_workbench.py resume --config /path/to/config.json
```

See [RESUME.md](RESUME.md) for durable state inspection, `binding_mismatch` handling, source observation accounting, and resolving interrupted callbacks.

## Boundaries

- Availability, source acquisition, model reply, and native adoption remain distinct.
- Keep mutable state (`state_dir`, `workspace`, outputs) outside `engine/` and `companions/`.
- Tests: `python -B -m unittest discover -s tests/companions/workbench`.
