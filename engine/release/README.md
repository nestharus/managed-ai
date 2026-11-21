# Maintain and package the unified system

Edit the canonical maintained files. `current_sources.json` names the core guidance owners; there is no separate library of copied source bodies. `navigation.json` owns routes to domain sources and qualified current accounts.

After a core source change, run:

```sh
python3 -B engine/release/refresh_guidance.py
```

This refreshes canonical source hashes, native catalogue references, the source index and release metadata. It does not manufacture another source tree. Domain and instance catalogues are separately pinned by the stores that select them; use the native `configure` operation to qualify changed selections.

The release owner serializes refresh and packaging. After an interrupted refresh, rerun with valid inputs and compare all four projections before using the recovered release. Each output reserves ordinary directories named `.<output filename>.<temporary suffix>` in its own parent. Both manifests and the transport omit those directories and their contents, including remnants named with filesystem-equivalent spellings. Packaging leaves remnants available for inspection. Selection paths are relative to the engine. Refresh writes native catalogue paths using the spelling of the represented file. The package gate checks that each selected source and its native catalogue entry identify the same exported body. A selection traversing a staging directory is refused. Packaging an engine directory directly checks its selected core bodies and produces a single manifest.

The package gate checks the exact catalogue member path, including the bundle's `engine/` prefix, and reads existing catalogue JSON with the native reader's rules. If the inventory reports a spelling mismatch, correct the named file and directory spelling on disk before running guidance refresh. On a case-insensitive filesystem, a case-only correction may require a temporary name. If the catalogue contains duplicate `body_path` fields, guidance refresh can regenerate the selected references. Repair other invalid JSON or restore a valid catalogue before refreshing if the offending value survives refresh. Rerun packaging after guidance refresh succeeds.

Run the bounded native acceptance cases with:

```sh
python3 -B engine/prototype/agent-system/check_shared_instances.py
```

Export the pinned tracked files and assemble the selected stores as described in [the release procedure](../../RELEASING.md). The producer refuses Git checkout metadata in a package input. From the assembly’s parent or another working directory, run:

```sh
python3 -B /absolute/managed-ai/engine/release/package.py \
  --root /absolute/managed-ai --output /absolute/managed-ai.zip --replace
```

`--refresh-only` verifies source/component identities, store schemas and current deployment bindings and refreshes member manifests without making a ZIP. A missing bundle selection or direct-engine marker can indicate generic source input. If the package command cannot determine the release scope or read its inventory, it reports the error. Output must be outside the source tree. The producer validates ordinary member paths, exact source bytes, ZIP CRCs and archive member hashes. It does not load a predecessor or nest previous transports.
