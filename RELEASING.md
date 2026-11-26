# Cutting a Managed AI Release

Choose the package scope needed by the receiver:

| Release | Contents |
| --- | --- |
| Python wheel / sdist | Standard PEP-517 / PEP-639 package (`requires-python >= 3.14`, PEP 561 `py.typed`) built from `pyproject.toml` and `src/managed_ai/` (excluding repo-local `scripts/`, `containers/`, `deploy/`, `infra/`, `gitops/`, `.buildkite/`) |
| Container image | Multi-stage non-root `python:3.15.2-slim` image built from `containers/Containerfile`, tagged immutably (`sha-<12>`, `v<semver>`; never `:latest`) and promoted across `gitops/` channels by `@sha256` digest |
| Optional air-gapped bundle | Clean ZIP export of `src/managed_ai/`, `docs/`, `examples/`, `pyproject.toml`, `LICENSE`, plus optional initialized stores and deployments |
| External domain | One external domain repository's `domain.json`, `catalogue.json`, sources, and tools exported at an exact Git commit |

## Standard Releases (Wheel & Container)

### 1. Build and qualify a Python wheel

Build with `uv` or standard `pip`:

```sh
uv build --wheel --out-dir /path/to/dist
# Or with pip (standard isolated build):
python -B -m pip wheel --no-deps . -w /path/to/dist
```

*(Only pass `--no-build-isolation` in air-gapped environments where `setuptools>=77` and `wheel` are already preinstalled in the active interpreter.)*

Verify the wheel imports cleanly in isolated mode (`python -I`), excludes maintainer scripts under `scripts/`, and passes `python -B -m unittest tests/release/test_package_wheel.py`.

### 2. Build and promote the container image

Build locally from the pinned multi-stage `containers/Containerfile`:

```sh
docker build -f containers/Containerfile -t managed-ai:vX.Y.Z .
```

In CI, Buildkite (`.buildkite/pipeline.yml`) gates container builds behind all fast checks (`- wait`), hashes build inputs (`scripts/ci/image_lifecycle.py context-hash`) to skip redundant rebuilds, and plans immutable tags (`tag-plan --channel release --version X.Y.Z --git-sha <sha>`). Promote releases across `gitops/` channels by pinning the emitted `@sha256` digest—never `:latest`.

Run dry-run registry garbage collection planning regularly (automated in `.buildkite/pipeline-nightly.yml`):

```sh
python -B scripts/ci/image_lifecycle.py plan-gc \
  --catalog /path/to/registry-catalog.json \
  --gitops-dir gitops \
  --keep-main 5 \
  --pr-ttl-days 7
```

`plan-gc` protects every `@sha256` digest pinned in `gitops/`, every SemVer `vX.Y.Z` tag, the newest `keep-main` `sha-*` tags, and unexpired `pr-*` tags, and never deletes a digest shared by any protected tag or pin.

## Optional Air-Gapped Bundle Archive

Use the bundle workflow only when delivering a self-contained ZIP snapshot (optionally bundling pre-initialized stores and deployments) to an offline or air-gapped host.

### 1. Export clean tracked sources

Start from a clean checkout:

```sh
git rev-parse HEAD
git status -s
```

Export tracked files (excluding `.git` metadata) into a clean staging directory outside the repository on a filesystem preserving file modes and LF bytes.

To initialize a fresh store and deployment inside a working release directory:

```sh
managed-ai --root stores/work init \
  --instance-id work --goal-text "Purpose of this store" \
  --domain /path/to/domain/domain.json

managed-ai-deploy --deployment deployments/work init \
  --store stores/work --engine src/managed_ai
```

### 2. Verify and build the bundle archive

For existing stores, apply reviewed domain/catalogue updates with native `configure --spec` against the current `system.json` digest and bind each deployment to the qualified engine (see [docs/engine/INSTANCES.md](docs/engine/INSTANCES.md)).

Run the acceptance gate and build the archive outside the staging tree:

```sh
python -B scripts/release/check_current.py
python -B scripts/release/package.py --root /path/to/export --output /path/to/managed-ai.zip --replace
```

`scripts/release/package.py` verifies `src/managed_ai/store/catalogue.json`, `src/managed_ai/CURRENT-RELEASE.json`, store schemas, deployment bindings, and `src/managed_ai/companions/peer/COMPONENTS.json` (stamping exact `bytes` + `sha256` pins into the exported bundle), generates `src/managed_ai/SOURCES.md`, `src/managed_ai/MANIFEST.json`, and root `MANIFEST.json`, and checks ZIP CRCs and member hashes before atomically writing the archive.

### 3. Qualify the extracted archive

Extract the generated ZIP into a fresh directory and verify every deployment and store:

```sh
python -B src/managed_ai/deployment/select_deployment.py --deployment deployments/work status
python -B src/managed_ai/deployment/select_deployment.py --deployment deployments/work system status
```

Record the archive SHA-256, Git commit, engine `code_sha256`, and store/domain digests alongside the receiving verification result. See [docs/engine/release/README.md](docs/engine/release/README.md) for core guidance pin refresh details.
