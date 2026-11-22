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


### Research evidence in a working release

Select the research deployment and its current instance descriptor from the
same receiving cut. Include completed native import receipts, their record
heads, the workspace basis and the retained objects. Keep required catalogue
dependencies under their declared paths. Package verification starts from
the selected research domain and its registered tools.

After extraction, repeat an exact research-origin lookup in the received
workspace and open its head. Check the accepted revision and native digest,
the original source coordinates, and every basis object's bytes against the
selected cut. Reassemble a partitioned original in its declared order and
verify its full digest. For a scope-specific signed evidence request, the
research operation's `verify` command checks the original native receipt.
A retained, hash-addressed JSON, JSON Lines or TSV gzip can be carried at its native basis path when the completed import, current head, retained object and decompressed text all verify. Other nested transports remain excluded.
The scoped research guide describes native discovery and receipt-bound
intake in `domains/research/operations/CURRENT-WORK.md`.

## Accept the exported edition

Record the tested ZIP digest before promotion. Verify its actual membership, modes,
body digests, component registrations and source pins. Extract the same bytes
into a fresh receiving directory and follow the documented entries. For a guide
that draws on an outside source, verify the owner's commit, path and body.
For a retained store example, verify the full release digest, member and body.
Publish the receiving result alongside the digest of the archive it qualifies.

- For a full release, verify every intended store and deployment, the original
  ordered selections and retained evidence. Use current deployment commands to
  inspect a historical capture at the receiving location.
- For a working release, exercise the selected task and dependency domains. If
  the receiver creates the store, package the initialization entry, run it at the
  extracted root and verify the resulting store there. An external pinned verifier
  can qualify a working release whose selected domains omit AI-system-building.
- For the focused operating edition, require one shared engine and the ordered
  research and AI-system-building selections. From the extracted package root
  run `python3 -B start_operation.py`, inspect `system view start` through its
  deployment, and exercise the reading guides, source verifier and companion.
  Check the companion with `--engine engine/prototype`.
- For a domain release, qualify the actual export in the disposable compatible
  receiver described below. Exercise its entry sources and tools at the declared
  domain mount with its exact dependency commits.

## Export one domain

From the domain's repository, set `domain` to its declared ID, `commit` to its reviewed commit and `archive` to the output ZIP.

```sh
git -c core.autocrlf=false archive --format=zip --prefix="domains/$domain/" --output="$archive" "$commit"
python3 -m zipfile -t "$archive"
```

Compare exported file bytes with their pinned Git blobs. The archive uses LF source bytes on hosts configured for automatic CRLF checkout.

Record the archive, domain descriptor and catalogue digests, engine API, and any required domain commits or external source identities. Mount the domain in a compatible disposable package and run native configuration and the selected-source gate. Publish the receiving result with the release record.
