# Cutting a Managed AI Release

Choose the package scope needed by the receiver:

| Release | Contents |
| --- | --- |
| Python wheel / sdist | Standard PEP-517 package built from `pyproject.toml` and `src/managed_ai/` (excluding repo-local `scripts/`) |
| Software bundle | Clean export of `src/managed_ai/`, `docs/`, `examples/`, `pyproject.toml`, `LICENSE`, plus optional initialized stores and deployments |
| External domain | One external domain repository's `domain.json`, `catalogue.json`, sources, and tools exported at an exact Git commit |

## 1. Build a standard Python wheel

```sh
python -B -m pip wheel --no-build-isolation --no-deps . -w /path/to/dist
```

Verify the wheel imports cleanly in isolated mode (`python -I`), excludes maintainer scripts under `scripts/`, and passes `tests/release/test_package_wheel.py`.

## 2. Export clean tracked sources for a bundle archive

Start from a clean checkout:

```sh
git rev-parse HEAD
git status -s
```

Export tracked files (excluding `.git` metadata) into a clean staging directory outside the repository on a filesystem preserving file modes and LF bytes.

To initialize a fresh store and deployment inside a working release directory:

```sh
python -B src/managed_ai/store/manage_store.py --root stores/work init \
  --instance-id work --goal-text "Purpose of this store" \
  --domain /path/to/domain/domain.json

python -B src/managed_ai/deployment/select_deployment.py --deployment deployments/work init \
  --store stores/work --engine src/managed_ai
```

## 3. Verify and build the bundle archive

For existing stores, apply reviewed domain/catalogue updates with native `configure --spec` against the current `system.json` digest and bind each deployment to the qualified engine (see [docs/engine/INSTANCES.md](docs/engine/INSTANCES.md)).

Run the acceptance gate and build the archive outside the staging tree:

```sh
python -B scripts/release/check_current.py
python -B scripts/release/package.py --root /path/to/export --output /path/to/managed-ai.zip --replace
```

`scripts/release/package.py` verifies `src/managed_ai/store/catalogue.json`, `src/managed_ai/CURRENT-RELEASE.json`, store schemas, deployment bindings, and `src/managed_ai/companions/peer/COMPONENTS.json` (stamping exact `bytes` + `sha256` pins into the exported bundle), generates `src/managed_ai/SOURCES.md`, `src/managed_ai/MANIFEST.json`, and root `MANIFEST.json`, and checks ZIP CRCs and member hashes before atomically writing the archive.

## 4. Qualify the extracted archive

Extract the generated ZIP into a fresh directory and verify every deployment and store:

```sh
python -B src/managed_ai/deployment/select_deployment.py --deployment deployments/work status
python -B src/managed_ai/deployment/select_deployment.py --deployment deployments/work system status
```

Record the archive SHA-256, Git commit, engine `code_sha256`, and store/domain digests alongside the receiving verification result. See [docs/engine/release/README.md](docs/engine/release/README.md) for core guidance pin refresh details.
