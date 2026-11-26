# Managed AI

A domain-neutral, standard-library Python 3.11+ engine for persistent goals, schema-validated records, source-pinned catalogues, guarded context/receive/adopt cycles, and cross-store provenance. Reusable domains load as data via Git submodules; optional companions and collective coordination layer on top without creating a second runtime.

## Repository layout

| Path | Responsibility |
| --- | --- |
| [`engine/`](engine/README.md) | Shared runtime: `deployment/`, `core/`, `store/`, `library/`, `host/`, `tools/`, and `release/`. |
| [`companions/`](companions/README.md) | Optional adapters: `workbench/`, `peer/`, `publication_review/`, and `representation/`. |
| [`optional/collective/`](optional/collective/README.md) | Optional SQLite-backed multi-owner receiving coordination. |
| [`patterns/`](patterns/README.md) | Reusable executable patterns and operating packs (`continuous-consolidation`, `selected-source`, `continuing-state`, `source-admission`, `method-invocation`, `prospective-reception`). |
| `domains/` | Loadable domain submodules: [`research`](domains/research/README.md), [`ai-system-building`](domains/ai-system-building/README.md), and [`writing`](domains/writing/README.md). |
| `guidance/` | Audience guidance registered by domains: [`agents/`](guidance/agents/README.md) and [`humans/`](guidance/humans/README.md). |
| `tests/` | Unified unit and integration suites mirrored by subsystem. |

## Quickstart (Windows and POSIX)

Requires **Python 3.11+** (no third-party packages).

### 1. Clone with domain submodules

```sh
git clone --recursive https://github.com/nestharus/managed-ai.git
cd managed-ai
```

*(If already cloned without `--recursive`, run `git submodule update --init --recursive`.)*

### 2. Initialize an external workspace and deployment

Stores live outside the engine checkout and reference the shared engine by path and verified `code_sha256`:

```sh
python -B engine/store/manage_store.py --root ../my-workspace/stores/work init \
  --instance-id work \
  --goal-text "Track architecture goals and evidence" \
  --domain domains/research/domain.json \
  --domain domains/ai-system-building/domain.json

python -B engine/deployment/select_deployment.py --deployment ../my-workspace init \
  --store ../my-workspace/stores/work \
  --engine engine
```

### 3. Inspect state, query catalogues, and verify selected sources

```sh
python -B engine/deployment/select_deployment.py --deployment ../my-workspace status
python -B engine/deployment/select_deployment.py --deployment ../my-workspace system status
python -B engine/deployment/select_deployment.py --deployment ../my-workspace system catalogue --query method --limit 5
python -B domains/ai-system-building/operations/verify_selected_sources.py \
  --deployment ../my-workspace --engine engine
```

### 4. Run tests

```sh
python -B -m unittest discover -s tests/engine/deployment
python -B -m unittest discover -s tests/engine/core
python -B -m unittest discover -s tests/engine/store
python -B -m unittest discover -s tests/engine/library
python -B -m unittest discover -s tests/engine/host
python -B -m unittest discover -s tests/engine/release
python -B -m unittest discover -s tests/companions
python -B -m unittest discover -s tests/optional/collective
```

## Documentation

- [Architecture](ARCHITECTURE.md) — component ownership, schemas, records, and guarded recomposition.
- [Engine overview](engine/README.md), [Capabilities](engine/CAPABILITIES.md), and [Instances & domains](engine/INSTANCES.md).
- [CLI field reference](engine/CLI-FIELD-REFERENCE.md) and [Native store contract](engine/store/README.md).
- [Agent guidance](guidance/agents/README.md) and [Human guidance](guidance/humans/README.md).
- [Maintenance](MAINTENANCE.md), [Releasing](RELEASING.md), and [Contributing](CONTRIBUTING.md).
