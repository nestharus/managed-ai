# Contributing to Managed AI

## Repository boundaries

- `src/managed_ai/` holds domain-neutral runtime code (`deployment/`, `core/`, `store/`, `library/`, `host/`, `tools/`, `release/`, `companions/`, `optional/collective/`). Do not hardcode domain names, domain workflows, or private workspace paths into `src/managed_ai/`.
- External domain repositories supply reusable schemas, catalogues, guidance, and domain operations independently (`source_root: "."`). Keep domain changes in the owning domain repository and refresh its `catalogue.json` and `domain.json` pins.
- `src/managed_ai/companions/`, `src/managed_ai/optional/collective/`, and `examples/` build on the core engine through public boundaries (`managed_ai.store.domain_api` and public CLIs).
- Never open sealed source entries (marked `"exposure": "sealed"` in catalogues) as ordinary text sources.

## Development hygiene

- **Python 3.11+ standard library only:** Core packages require zero third-party runtime dependencies.
- **No bytecode clutter:** Run Python with `-B` (or `PYTHONDONTWRITEBYTECODE=1`).
- **Line endings:** Use LF (`\n`). `.gitattributes` sets `* -text` so catalogue and manifest SHA-256 digests stay byte-identical across Windows and POSIX hosts.
- **Cross-platform safety:** Use POSIX relative paths in descriptors and catalogues, explicit UTF-8 encoding (`encoding="utf-8"`), strict JSON parsing (no duplicate keys or `NaN`/`Infinity`), `allow_abbrev=False` on argument parsers, and unfollowed symlink / NTFS reparse-point checks on file boundaries.
- **Package-time artifacts:** Do not commit `MANIFEST.json`, `src/managed_ai/MANIFEST.json`, or `src/managed_ai/SOURCES.md`; `src/managed_ai/release/package.py` generates them for release archives.

## Running tests

From the repository root:

```sh
python -B -m unittest discover -s tests
python -B src/managed_ai/release/check_current.py
```

## Updating core guidance pins

After editing any document registered in `src/managed_ai/release/current_sources.json`, run:

```sh
python -B src/managed_ai/release/refresh_guidance.py
```

## License

By contributing to this repository, you agree that your contributions will be licensed under the MIT License (see [LICENSE](LICENSE)).
