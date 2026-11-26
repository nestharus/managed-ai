# Contributing to Managed AI

## Repository boundaries

- `src/managed_ai/` holds domain-neutral runtime packages (`platform/`, `deployment/`, `core/`, `store/`, `library/`, `host/`, `companions/`, `collective/`) plus runtime JSON assets (`CURRENT-RELEASE.json`, `store/catalogue.json`, `library/source-map.json`). Do not hardcode domain names, domain workflows, or private workspace paths into `src/managed_ai/`.
- Keep OS/platform branching strictly inside `src/managed_ai/platform/` (zero `os.name`, `sys.platform`, `ctypes.WinDLL`, `msvcrt`, or `fcntl` outside `platform/`). Every domain package owns its own concrete contracts, configs, and storage natively.
- Keep maintainer/CI-CD scripts and selection metadata under `scripts/` (`scripts/release/*`, `scripts/publish_library.py`) so built wheels ship only runtime packages and runtime data assets.
- External domain repositories supply reusable schemas, catalogues, guidance, and domain operations independently (`source_root: "."`). Keep domain changes in the owning domain repository and refresh its `catalogue.json` and `domain.json` pins.
- Never open sealed source entries (marked `"exposure": "sealed"` in catalogues) as ordinary text sources.

## Development hygiene

- **Python 3.11+ standard library only:** Core packages require zero third-party runtime dependencies.
- **No bytecode clutter:** Run Python with `-B` (or `PYTHONDONTWRITEBYTECODE=1`).
- **Line endings:** Use LF (`\n`). `.gitattributes` sets `* -text` so catalogue and manifest SHA-256 digests stay byte-identical across Windows and POSIX hosts.
- **Cross-platform safety:** Use `managed_ai.platform` for locking, atomic writes, streaming SHA-256, strict JSON parsing, Windows reserved-name validation, and unfollowed symlink / NTFS reparse-point checks. Always set `allow_abbrev=False` on argument parsers and subparsers.
- **Package-time artifacts:** Do not commit `MANIFEST.json`, `src/managed_ai/MANIFEST.json`, or `src/managed_ai/SOURCES.md`; `scripts/release/package.py` generates them for release archives.

## Running tests

From the repository root:

```sh
python -B -m unittest discover -s tests
python -B scripts/release/check_current.py
```

## Updating core guidance pins

After editing any document registered in `scripts/release/current_sources.json`, run:

```sh
python -B scripts/release/refresh_guidance.py
```

## License

By contributing to this repository, you agree that your contributions will be licensed under the MIT License (see [LICENSE](LICENSE)).
