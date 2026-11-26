# Cutting a Managed AI Release

Choose the package scope needed by the receiver:

| Release | Contents |
| --- | --- |
| Full | Shared `engine/`, `companions/`, `optional/`, `patterns/`, `guidance/`, selected `domains/`, and initialized stores/deployments |
| Working | Shared `engine/`, required `domains/`, and one initialized or fresh store and deployment |
| Domain | One domain repository's `domain.json`, `catalogue.json`, sources, and tools exported at an exact Git commit |

## 1. Export clean tracked sources

Start from a clean checkout with initialized submodules:

```sh
git submodule update --init --recursive
git rev-parse HEAD
git submodule status --recursive
```

Export tracked files (excluding `.git` metadata) into a clean staging directory outside the repository on a filesystem preserving file modes. Place each domain under `domains/<id>/` and preserve LF bytes and catalogue pins.

To initialize a fresh store and deployment inside a working release directory:

```sh
python -B engine/store/manage_store.py --root stores/work init \
  --instance-id work --goal-text "Purpose of this store" \
  --domain domains/research/domain.json \
  --domain domains/ai-system-building/domain.json

python -B engine/deployment/select_deployment.py --deployment deployments/work init \
  --store stores/work --engine engine
```

## 2. Verify selected sources and build the archive

For existing stores, apply reviewed domain/catalogue updates with native `configure --spec` against the current `system.json` digest and bind each deployment to the qualified engine (see [engine/INSTANCES.md](engine/INSTANCES.md)).

Run the read-only selected-source gate for every included store, then package outside the staging tree:

```sh
python -B domains/ai-system-building/operations/verify_selected_sources.py \
  --bundle . --store stores/work

python -B engine/release/package.py --root . --output /path/to/managed-ai.zip --replace
```

`package.py` verifies `engine/store/catalogue.json`, `engine/CURRENT-RELEASE.json`, store schemas, deployment bindings, and `companions/peer/COMPONENTS.json` (stamping exact `bytes` + `sha256` pins into the exported bundle), generates `engine/SOURCES.md`, `engine/MANIFEST.json`, and root `MANIFEST.json`, and checks ZIP CRCs and member hashes before atomically writing the archive. Compressed research originals (`.json.gz`, `.jsonl.gz`, `.tsv.gz`) are admitted only when bound to completed native import receipts and verified basis objects.

## 3. Qualify the extracted archive

Extract the generated ZIP into a fresh directory and verify every deployment and store:

```sh
python -B engine/deployment/select_deployment.py --deployment deployments/work status
python -B engine/deployment/select_deployment.py --deployment deployments/work system status
```

Record the archive SHA-256, Git commits, `engine` `code_sha256`, and store/domain digests alongside the receiving verification result. See [engine/release/README.md](engine/release/README.md) for core guidance pin refresh details.

## 4. Export a single domain

From the domain repository (`$domain` = domain ID, `$commit` = reviewed commit, `$archive` = output ZIP):

```sh
git -c core.autocrlf=false archive --format=zip --prefix="domains/$domain/" --output="$archive" "$commit"
python -m zipfile -t "$archive"
```

Mount the exported domain in a disposable bundle, run native `configure`, and pass `verify_selected_sources.py`.
