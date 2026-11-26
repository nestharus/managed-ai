# Release maintenance and packaging

`engine/release/` refreshes core guidance pins (`current_sources.json`, `engine/store/catalogue.json`, `engine/CURRENT-RELEASE.json`) and produces self-verifying release archives from a clean exported tree.

## Refresh core guidance pins

After editing any core guidance file listed in `engine/release/current_sources.json`, run:

```sh
python -B engine/release/refresh_guidance.py
```

This updates `engine/release/current_sources.json`, `engine/store/catalogue.json`, and `engine/CURRENT-RELEASE.json`. Generated release files (`MANIFEST.json`, `engine/MANIFEST.json`, and `engine/SOURCES.md`) are not tracked in Git; `package.py` generates them dynamically during release packaging. Pass `--write-sources-md` to `refresh_guidance.py` only when you explicitly want `engine/SOURCES.md` written locally.

## Verify and package an exported release

Export tracked files (without `.git` directories) into a clean staging directory outside the repository, then verify and package:

```sh
python -B -m unittest discover -s tests/engine/release
python -B engine/release/check_current.py
python -B engine/release/package.py --root /path/to/export --output /path/to/managed-ai.zip --replace
```

During packaging, `package.py`:
- Refuses `.git` metadata, symlinks, NTFS reparse points/junctions, duplicate JSON keys, non-finite numbers, and nested archives (outside verified research evidence).
- Verifies `engine/store/catalogue.json`, `engine/CURRENT-RELEASE.json`, store schemas, deployment bindings, and `companions/peer/COMPONENTS.json` (accepting declarative entries in Git exports, verifying any existing `identity` pins, and stamping exact `bytes` + `sha256` pins into the exported bundle).
- Generates `engine/SOURCES.md` (when absent), `engine/MANIFEST.json`, and root `MANIFEST.json`, then verifies ZIP CRCs and member hashes before atomically replacing `--output`.
- Use `--refresh-only` on a clean export directory to verify and stamp manifests without creating a ZIP.
