# Managed AI Information

A domain-neutral, standard-library Python 3.14+ (primary/containerized runtime: Python 3.15.2) engine for persistent goals, schema-validated records, source-pinned catalogues, guarded context/receive/adopt cycles, and cross-store provenance. Loadable domains plug in externally as data; optional companions and collective coordination layer on top through public boundaries.

## Quickstart (Windows and POSIX)

Requires **Python 3.14+** (primary/containerized runtime: **Python 3.15.2**; standard library only; zero third-party runtime dependencies; PEP 561 typed via `py.typed`).

### 1. Clone and install

```sh
git clone https://github.com/nestharus/managed-ai-information.git
cd managed-ai-information
python -m pip install -e .
```

Or with [`uv`](https://docs.astral.sh/uv/):

```sh
uv pip install -e .
# Contributors (installs ruff, ty, mypy dev tools):
uv sync --group dev
```

Installing exposes the public console scripts `managed-ai-information`, `managed-ai-information-deploy`, `managed-ai-information-library`, `managed-ai-information-attend`, `managed-ai-information-work`, and `managed-ai-information-collect` (`g60-attend`, `g60-work`, and `g60-collect` remain available as backward-compatible aliases).

### 2. Initialize a store and run the core workflow

Stores live outside the repository so your data stays independent of engine upgrades:

```sh
managed-ai-information --root ../my-store init \
  --instance-id work \
  --goal-text "Track architecture goals and evidence"

printf '{"id":"note-1","kind":"note","content":{"summary":"Initial architecture baseline"}}\n' > ../note-1.json
managed-ai-information --root ../my-store put --record ../note-1.json --expected 0

managed-ai-information --root ../my-store status --summary
managed-ai-information --root ../my-store reindex
managed-ai-information --root ../my-store graph lint
managed-ai-information --root ../my-store pack-objects --compress --apply
```

*(Pass one or more `--domain /path/to/domain/domain.json` flags to `managed-ai-information init` to attach external domain schemas, source catalogues, and tool contracts as data.)*

### 3. Bind multiple stores to one shared engine (`managed-ai-information-deploy`)

When multiple independent stores share one reviewed engine edition, create a deployment descriptor that pins the engine by path and verified `code_sha256`:

```sh
managed-ai-information-deploy --deployment ../my-workspace init \
  --store ../my-store \
  --engine src/managed_ai_information

managed-ai-information-deploy --deployment ../my-workspace status
managed-ai-information-deploy --deployment ../my-workspace system status --summary
managed-ai-information-deploy --deployment ../my-workspace system catalogue --query composition --limit 5
```

### 4. Run tests, lint, static type checks, and semantic profile

```sh
python -B -m unittest discover -s tests/platform_adapter   # fast inner loop (~2s)
python -B -m unittest discover -s tests                    # full pre-PR suite
uvx ruff check src scripts tests && uvx ruff format --check src scripts tests
uvx ty check src scripts && uvx mypy src scripts
python -B scripts/ci/semantic_profile.py --check-budget --baseline scripts/ci/semantic-baseline.json
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contributor setup (`uv sync --group dev`) and fast per-package test commands.

## Plain-English Glossary

| Term | Meaning |
| --- | --- |
| **store** | A caller-owned directory outside this repo (`system.json`, `work/`, `exchange/`) holding persistent goals, records, objects, and turn receipts. |
| **record** | A canonical JSON document under `work/records/<id>.json` with `id`, `kind`, `content`, `relations`, `basis`, monotonic `revision`, and intrinsic `sha256`. |
| **basis** | Explicit workspace files and source digests pinned to a record or turn so stale assumptions are detected before write adoption. |
| **exchange** | The content-addressed object store (`exchange/objects/`) and packet ledger (`exchange/packets/`) coordinating prepare, dispatch, receive, and adopt steps. |
| **attention** | Explicit tracking of open obligations, active receipts, and review items so unresolved duties are never silently dropped. |
| **carrier** | The subprocess transport layer (`managed-ai-information-collect`) that executes isolated local workers and captures bounded stdout/stderr receipts. |
| **deployment** | A descriptor (`deployment.json`) pinning one store to one reviewed engine edition by `code_sha256`, requiring explicit `stage` + `activate` on engine changes. |
| **domain** | An external data package (`domain.json`, `catalogue.json`, schemas, guidance) loaded by a store without executing domain tools or modifying the engine. |

## Repository layout

| Path | Responsibility |
| --- | --- |
| [`src/managed_ai_information/`](docs/engine/README.md) | Installable Python runtime package: `platform/`, `deployment/`, `core/`, `store/`, `library/`, `host/`, `companions/`, `collective/`, and runtime package data. |
| [`scripts/`](docs/engine/release/README.md) | Maintainer and CI/CD tooling (not shipped in wheels): `scripts/release/{package,check_current,refresh_guidance}.py`, `scripts/ci/{image_lifecycle,validate_deploy,semantic_profile}.py`, and `scripts/publish_library.py`. |
| `containers/`, `deploy/local/` | Multi-stage non-root container image (`containers/Containerfile`) and IP-hiding local Docker Compose stack (`deploy/local/`). |
| `infra/`, `gitops/` | Channel-driven OpenTofu/Vultr infrastructure (`infra/`) and Flux GitOps manifests (`gitops/`) promoting immutable `@sha256` image digests. |
| `.buildkite/` | Staged Buildkite CI/CD pipelines (`pipeline.yml`, `pipeline-nightly.yml`) with semantic budget gates, content-hash-gated builds, and registry GC planning. |
| [`docs/`](docs/engine/README.md) | Engine, companion, collective, and continuous-consolidation documentation. |
| [`examples/`](examples/README.md) | Runnable feedback, receiving, continuing-state, and canary examples. |
| `tests/` | Domain-mirrored unit and integration suites (`tests/{platform_adapter,core,deployment,store,library,host,companions,collective,release,ci}/`). |
| `pyproject.toml` | Standard PEP-517 / PEP-639 build configuration (zero runtime dependencies, MIT). |

## Container, Local Control Surface & Delivery

- **Local Docker stack (`deploy/local/`):** Runs the non-root (`10001:10001`) `managed-ai-information` container (`python:3.15.2-slim`) on an `internal: true` bridge network with zero direct host ports, exposing only a single loopback `127.0.0.1` reverse-proxy control surface. Start locally via `docker compose -f deploy/local/compose.yaml up --build`.
- **Cloud & GitOps substrate (`infra/`, `gitops/`):** OpenTofu/Vultr definitions (`infra/`) and Flux manifests (`gitops/`) separate `local` and `dev` deployment channels and pin container images strictly by immutable `@sha256` digest.
- **Buildkite CI/CD & anti-spam image lifecycle (`.buildkite/`, `scripts/ci/image_lifecycle.py`):**
  - Fast parallel gates (`lint`, `typecheck`, `unit-tests`, `release-check`, `deploy-static`, `semantic-profile`, `wheel-audit`) run before `- wait` so failing PRs build zero images.
  - `scripts/ci/image_lifecycle.py context-hash` skips rebuilds when only docs/tests/infra change, `tag-plan` emits immutable `sha-<12>` / `v<semver>` tags (never `:latest`), and nightly `plan-gc` plans safe registry cleanup while protecting GitOps-pinned digests, SemVer releases, and rollback windows.

## Versioning & State Strategy

- **Packages & container images:** Python distributions use SemVer (`managed_ai_information.__version__`); container images are built from pinned base digests and promoted strictly by immutable `@sha256` digest (`sha-<12>` on `main`, `v<semver>` on releases).
- **Authoritative vs. derived store state:** Authoritative store state (`system.json`, `work/records/`, `exchange/objects/`, `exchange/cold-records/`) carries explicit versioned format identifiers; derived acceleration artifacts (`exchange/index/head-index.*`, packed slab caches) are deterministically rebuildable (`managed-ai-information reindex` / `pack-objects`) without mutating authoritative records.

## Documentation

- [Architecture](ARCHITECTURE.md) — domain-native ownership, `platform/` leaf boundary, schemas, records, and guarded recomposition.
- [Platform architecture](docs/platform-architecture.md) and [Coordination, ingestion & backend contract](docs/coordination-contract.md) — `managed-ai-coordination-contract/1`, `store.ingest`, and selectable `membus-v2` backend wire contract.
- [Engine overview](docs/engine/README.md), [Capabilities](docs/engine/CAPABILITIES.md), and [Instances & domains](docs/engine/INSTANCES.md).
- [CLI field reference](docs/engine/CLI-FIELD-REFERENCE.md) and [Native store contract](docs/engine/store/README.md).
- [Companions](docs/companions/README.md), [Collective coordination](docs/collective/README.md), and [Examples](examples/README.md).
- [Maintenance](MAINTENANCE.md), [Releasing](RELEASING.md), and [Contributing](CONTRIBUTING.md).

## License

MIT License — see [LICENSE](LICENSE) and [src/managed_ai_information/core/NOTICE.md](src/managed_ai_information/core/NOTICE.md).
