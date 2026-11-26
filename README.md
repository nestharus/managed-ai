# Managed AI

A domain-neutral, standard-library Python 3.11+ engine for persistent goals, schema-validated records, source-pinned catalogues, guarded context/receive/adopt cycles, and cross-store provenance. Loadable domains plug in externally as data; optional companions and collective coordination layer on top through public boundaries.

## Repository layout

| Path | Responsibility |
| --- | --- |
| [`src/managed_ai/`](docs/engine/README.md) | Installable Python runtime package: `platform/`, `deployment/`, `core/`, `store/`, `library/`, `host/`, `companions/`, `collective/`, and runtime package data. |
| [`scripts/`](docs/engine/release/README.md) | Maintainer and CI/CD tooling (not shipped in wheels): `scripts/release/{package,check_current,refresh_guidance}.py`, `scripts/release/{current_sources,navigation}.json`, and `scripts/publish_library.py`. |
| [`docs/`](docs/engine/README.md) | Engine, companion, collective, and continuous-consolidation documentation. |
| [`examples/`](examples/README.md) | Runnable feedback, receiving, continuing-state, and canary examples. |
| `tests/` | Unified unit and integration suites mirroring `src/managed_ai/` and `scripts/`. |
| `pyproject.toml` | Standard PEP-517 build configuration (zero runtime dependencies, MIT). |

## Quickstart (Windows and POSIX)

Requires **Python 3.11+** (standard library only; zero third-party runtime dependencies).

### 1. Clone and optionally install in editable mode

```sh
git clone https://github.com/nestharus/managed-ai.git
cd managed-ai
python -m pip install -e . --no-build-isolation
```

*(Editable installation exposes `managed-ai`, `managed-ai-deploy`, `managed-ai-library`, `g60-attend`, `g60-work`, and `g60-collect` console scripts. You can also run scripts directly from the checkout with `python -B src/managed_ai/...` without installing.)*

### 2. Initialize an external workspace and deployment

Stores live outside the engine checkout and bind the shared engine by path and verified `code_sha256`:

```sh
python -B src/managed_ai/store/manage_store.py --root ../my-workspace/stores/work init \
  --instance-id work \
  --goal-text "Track architecture goals and evidence"

python -B src/managed_ai/deployment/select_deployment.py --deployment ../my-workspace init \
  --store ../my-workspace/stores/work \
  --engine src/managed_ai
```

*(Pass one or more `--domain /path/to/domain/domain.json` arguments to `manage_store.py init` to load external domain schemas, catalogues, and tool contracts as data.)*

### 3. Inspect state and query catalogues

```sh
python -B src/managed_ai/deployment/select_deployment.py --deployment ../my-workspace status
python -B src/managed_ai/deployment/select_deployment.py --deployment ../my-workspace system status
python -B src/managed_ai/deployment/select_deployment.py --deployment ../my-workspace system catalogue --query composition --limit 5
```

### 4. Run the full test suite

```sh
python -B -m unittest discover -s tests
```

## Documentation

- [Architecture](ARCHITECTURE.md) — domain-native ownership, `platform/` leaf boundary, schemas, records, and guarded recomposition.
- [Engine overview](docs/engine/README.md), [Capabilities](docs/engine/CAPABILITIES.md), and [Instances & domains](docs/engine/INSTANCES.md).
- [CLI field reference](docs/engine/CLI-FIELD-REFERENCE.md) and [Native store contract](docs/engine/store/README.md).
- [Companions](docs/companions/README.md), [Collective coordination](docs/collective/README.md), and [Examples](examples/README.md).
- [Maintenance](MAINTENANCE.md), [Releasing](RELEASING.md), and [Contributing](CONTRIBUTING.md).

## License

MIT License — see [LICENSE](LICENSE) and [src/managed_ai/core/NOTICE.md](src/managed_ai/core/NOTICE.md).
