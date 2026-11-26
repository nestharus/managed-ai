# Managed AI

A domain-neutral, standard-library Python 3.14+ (primary/containerized runtime: Python 3.15.2) engine for persistent goals, schema-validated records, source-pinned catalogues, guarded context/receive/adopt cycles, and cross-store provenance. Loadable domains plug in externally as data; optional companions and collective coordination layer on top through public boundaries.

## Repository layout

| Path | Responsibility |
| --- | --- |
| [`src/managed_ai/`](docs/engine/README.md) | Installable Python runtime package: `platform/`, `deployment/`, `core/`, `store/`, `library/`, `host/`, `companions/`, `collective/`, and runtime package data. |
| [`scripts/`](docs/engine/release/README.md) | Maintainer and CI/CD tooling (not shipped in wheels): `scripts/release/{package,check_current,refresh_guidance}.py`, `scripts/ci/{image_lifecycle,validate_deploy}.py`, and `scripts/publish_library.py`. |
| `containers/`, `deploy/local/` | Multi-stage non-root container image (`containers/Containerfile`) and IP-hiding local Docker Compose stack (`deploy/local/`). |
| `infra/`, `gitops/` | Channel-driven OpenTofu/Vultr infrastructure (`infra/`) and Flux GitOps manifests (`gitops/`) promoting immutable `@sha256` image digests. |
| `.buildkite/` | Staged Buildkite CI/CD pipelines (`pipeline.yml`, `pipeline-nightly.yml`) with content-hash-gated builds and registry GC planning. |
| [`docs/`](docs/engine/README.md) | Engine, companion, collective, and continuous-consolidation documentation. |
| [`examples/`](examples/README.md) | Runnable feedback, receiving, continuing-state, and canary examples. |
| `tests/` | Domain-mirrored unit and integration suites (`tests/{platform_adapter,core,deployment,store,library,host,companions,collective,release,ci}/`). |
| `pyproject.toml` | Standard PEP-517 build configuration (zero runtime dependencies, MIT). |

## Quickstart (Windows and POSIX)

Requires **Python 3.14+** (primary/containerized runtime: **Python 3.15.2**; standard library only; zero third-party runtime dependencies; PEP 561 typed via `py.typed`).

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

### 4. Run tests, lint, and static type checks

```sh
python -B -m unittest discover -s tests
uvx ruff check src scripts tests && uvx ruff format --check src scripts
uvx ty check src scripts && uvx mypy src scripts
```

## Container, Local Control Surface & Delivery

- **Local Docker stack (`deploy/local/`):** Runs the non-root (`10001:10001`) `managed-ai` container (`python:3.15.2-slim`) on an `internal: true` bridge network with zero direct host ports, exposing only a single loopback `127.0.0.1` reverse-proxy control surface. Start locally via `docker compose -f deploy/local/compose.yaml up --build`.
- **Cloud & GitOps substrate (`infra/`, `gitops/`):** OpenTofu/Vultr definitions (`infra/`) and Flux manifests (`gitops/`) separate `local` and `dev` deployment channels and pin container images strictly by immutable `@sha256` digest.
- **Buildkite CI/CD & anti-spam image lifecycle (`.buildkite/`, `scripts/ci/image_lifecycle.py`):**
  - Fast parallel gates (`lint`, `typecheck`, `unit-tests`, `release-check`, `deploy-static`, `wheel-audit`) run before `- wait` so failing PRs build zero images.
  - `scripts/ci/image_lifecycle.py context-hash` skips rebuilds when only docs/tests/infra change, `tag-plan` emits immutable `sha-<12>` / `v<semver>` tags (never `:latest`), and nightly `plan-gc` plans safe registry cleanup while protecting GitOps-pinned digests, SemVer releases, and rollback windows.

## Versioning & State Strategy

- **Packages & container images:** Python distributions use SemVer (`managed_ai.__version__`); container images are built from pinned base digests and promoted strictly by immutable `@sha256` digest (`sha-<12>` on `main`, `v<semver>` on releases).
- **Authoritative vs. derived store state:** Authoritative store state (`system.json`, `work/records/`, `exchange/objects/`, `exchange/cold-records/`) carries explicit versioned format identifiers; derived acceleration artifacts (`exchange/index/head-index.*`, packed slab caches) are deterministically rebuildable (`manage_store.py reindex` / `pack-objects`) without mutating authoritative records.

## Documentation

- [Architecture](ARCHITECTURE.md) — domain-native ownership, `platform/` leaf boundary, schemas, records, and guarded recomposition.
- [Engine overview](docs/engine/README.md), [Capabilities](docs/engine/CAPABILITIES.md), and [Instances & domains](docs/engine/INSTANCES.md).
- [CLI field reference](docs/engine/CLI-FIELD-REFERENCE.md) and [Native store contract](docs/engine/store/README.md).
- [Companions](docs/companions/README.md), [Collective coordination](docs/collective/README.md), and [Examples](examples/README.md).
- [Maintenance](MAINTENANCE.md), [Releasing](RELEASING.md), and [Contributing](CONTRIBUTING.md).

## License

MIT License — see [LICENSE](LICENSE) and [src/managed_ai/core/NOTICE.md](src/managed_ai/core/NOTICE.md).
