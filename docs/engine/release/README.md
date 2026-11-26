# Release maintenance and packaging

Maintainer release scripts and selection metadata live under `scripts/release/` (outside `src/managed_ai_information/` so built wheels ship only runtime packages and runtime package data). They refresh core guidance pins (`scripts/release/current_sources.json`, `src/managed_ai_information/store/catalogue.json`, `src/managed_ai_information/CURRENT-RELEASE.json`) and produce self-verifying release archives from a clean exported tree.

## Refresh core guidance pins

After editing any core guidance file listed in `scripts/release/current_sources.json`, run:

```sh
python -B scripts/release/refresh_guidance.py
```

This updates `scripts/release/current_sources.json`, `src/managed_ai_information/store/catalogue.json`, and `src/managed_ai_information/CURRENT-RELEASE.json`. Generated release files (`MANIFEST.json`, `src/managed_ai_information/MANIFEST.json`, and `src/managed_ai_information/SOURCES.md`) are not tracked in Git; `scripts/release/package.py` generates them dynamically during release packaging. Pass `--write-sources-md` to `refresh_guidance.py` only when you explicitly want `src/managed_ai_information/SOURCES.md` written locally.

## Verify and package an exported release

Export tracked files (without `.git` directories) into a clean staging directory outside the repository, then verify and package:

```sh
python -B -m unittest discover -s tests/release
python -B scripts/release/check_current.py
python -B scripts/release/package.py --root /path/to/export --output /path/to/managed-ai-information.zip --replace
```

During packaging, `scripts/release/package.py` (backed by `_verify.py`, `_manifest.py`, and `managed_ai_information.platform`):
- Refuses `.git` metadata, symlinks, NTFS reparse points/junctions, duplicate JSON keys, non-finite numbers, and nested archives (outside verified domain-bound originals).
- Verifies `src/managed_ai_information/store/catalogue.json`, `src/managed_ai_information/CURRENT-RELEASE.json`, store schemas, deployment bindings, and `src/managed_ai_information/companions/peer/COMPONENTS.json` (accepting declarative entries in Git exports, verifying any existing `identity` pins, and stamping exact `bytes` + `sha256` pins into the exported bundle).
- Admits root `LICENSE`, `pyproject.toml`, `.gitignore`, `src/managed_ai_information/`, `docs/`, and `examples/`.
- Generates `src/managed_ai_information/SOURCES.md` (when absent), `src/managed_ai_information/MANIFEST.json`, and root `MANIFEST.json`, then verifies ZIP CRCs and member hashes before atomically replacing `--output`.
- Use `--refresh-only` on a clean export directory to verify and stamp manifests without creating a ZIP.
