# Contributing to Managed AI

## Repository boundaries

- `engine/` holds domain-neutral runtime code (`deployment/`, `core/`, `store/`, `library/`, `host/`, `tools/`, `release/`). Do not hardcode domain names, domain workflows, or private workspace paths into `engine/`.
- `domains/*` are independent Git submodules that supply reusable schemas, catalogues, guidance, and domain operations. Keep domain changes in the owning domain repository and refresh its `catalogue.json` and `domain.json` pins.
- `companions/`, `optional/collective/`, `patterns/`, and `examples/` build on `engine/` through public boundaries.
- Never open sealed source entries (marked `"exposure": "sealed"` in catalogues) as ordinary text sources.

## Development hygiene

- **Python 3.11+ standard library only:** Core engine packages require no third-party dependencies.
- **No bytecode clutter:** Run Python with `-B` (or `PYTHONDONTWRITEBYTECODE=1`).
- **Line endings:** Use LF (`\n`). `.gitattributes` sets `* -text` so catalogue and manifest SHA-256 digests stay byte-identical across Windows and POSIX hosts.
- **Cross-platform safety:** Use POSIX relative paths in descriptors and catalogues, explicit UTF-8 encoding (`encoding="utf-8"`), strict JSON parsing (no duplicate keys or `NaN`/`Infinity`), and unfollowed symlink / NTFS reparse-point checks on file boundaries.
- **Package-time artifacts:** Do not commit `MANIFEST.json`, `engine/MANIFEST.json`, or `engine/SOURCES.md`; `engine/release/package.py` generates them for release archives.

## Running tests

From the repository root:

```sh
python -B -m unittest discover -s tests/engine/deployment
python -B -m unittest discover -s tests/engine/core
python -B -m unittest discover -s tests/engine/store
python -B -m unittest discover -s tests/engine/library
python -B -m unittest discover -s tests/engine/host
python -B -m unittest discover -s tests/engine/release
python -B -m unittest discover -s tests/companions
python -B -m unittest discover -s tests/optional/collective
python -B engine/release/check_current.py
```

For domain submodules (when initialized):

```sh
python -B -m unittest discover -s domains/research/tests
python -B -m unittest discover -s domains/ai-system-building/tests
python -B -m unittest discover -s domains/writing/tests
```

## Updating core guidance pins

After editing any file registered in `engine/release/current_sources.json`, run:

```sh
python -B engine/release/refresh_guidance.py
```
