# Cut a Managed AI release

Choose the package by what its receiver needs.

| Release | Contents |
| --- | --- |
| Full | One shared engine, its companions and guidance, the selected domains, and all intended current stores and deployments |
| Working | The shared engine, the domains needed for a task, and one selected or fresh store and deployment |
| Domain | One domain's descriptor, catalogue, sources and tools at an exact Git commit, with the compatible engine and dependency pins recorded |

## Select and assemble the sources

Start from a clean checkout. The shared repository's submodule entries identify the domain commits.

```sh
git submodule update --init --recursive
git rev-parse HEAD
git submodule status --recursive
```

Export ordinary tracked files from the verified commits into a clean package directory on a filesystem that preserves file modes. Put each domain under `domains/<id>/` and keep shared guidance and maintenance paths in place. Preserve source and catalogue pins. For a full archive, add the stores and retained material selected by their owners. Restore externally retained catalogue bodies from their designated custody.

For a working archive, select one store and its deployment. To start fresh, initialize a store with an appropriate goal and domain selection. The AI-system-building domain requires research. A focused operating release may use a derived research catalogue containing the reusable tools, method and theory needed for the work.

```sh
python3 -B engine/prototype/system.py --root stores/work init \
  --instance-id work --goal-text 'The purpose of this store' \
  --domain domains/research/domain.json \
  --domain domains/ai-system-building/domain.json
python3 -B engine/prototype/deploy.py --deployment deployments/work init \
  --store stores/work --engine engine
```

## Qualify and build the archive

For an existing store, use native `configure --spec` to select the intended domain and catalogue sources against its current descriptor hash. Preserve the instance identity and ordered selection. Use the reviewed configuration form when the receiver must accept an exact prepared edition. Bind the deployment to the reviewed engine. See the [instance contract](engine/INSTANCES.md) for both operations.

Run the selected-source gate with a separate `--store` argument for each included store. Set `verifier` to the path of `domains/ai-system-building/operations/verify_selected_sources.py` in your pinned source checkout so this also works for a working archive that does not select the AI-system-building domain. From the clean package directory, set `archive` to the output ZIP outside that directory.

```sh
python3 -B "$verifier" \
  --bundle . --store stores/work
python3 -B engine/release/package.py --root . --output "$archive"
```

Review the member count and archive digest. Extract into a fresh directory and check the deployment and native store. Repeat for every included deployment.

```sh
python3 -B engine/prototype/deploy.py --deployment deployments/work status
python3 -B engine/prototype/deploy.py --deployment deployments/work system status
```

Record the archive digest, Git commits, selected engine digest, and store and domain identities in the release record. Refer to the [package producer](engine/release/README.md) for maintenance of the canonical registrations.

## Export one domain

From the domain's repository, set `domain` to its declared ID, `commit` to its reviewed commit and `archive` to the output ZIP.

```sh
git archive --format=zip --prefix="domains/$domain/" --output="$archive" "$commit"
python3 -m zipfile -t "$archive"
```

Record the archive, domain descriptor and catalogue digests, engine API, and any required domain commits or external source identities. Mount the domain in a compatible disposable package and run native configuration and the selected-source gate. Publish the receiving result with the release record.
