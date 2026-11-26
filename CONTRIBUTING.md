# Contributing to Managed AI

## Repository boundaries

- `src/managed_ai/` holds domain-neutral runtime packages (`platform/`, `deployment/`, `core/`, `store/`, `library/`, `host/`, `companions/`, `collective/`) plus runtime JSON assets (`CURRENT-RELEASE.json`, `store/catalogue.json`, `library/source-map.json`). Do not hardcode domain names, domain workflows, or private workspace paths into `src/managed_ai/`.
- Keep OS/platform branching strictly inside `src/managed_ai/platform/` (zero `os.name`, `sys.platform`, `ctypes.WinDLL`, `msvcrt`, or `fcntl` outside `platform/`). Every domain package owns its own concrete contracts, configs, and storage natively.
- Keep maintainer/CI-CD scripts, container definitions, local compose stacks, IaC, and GitOps manifests under `scripts/`, `containers/`, `deploy/`, `infra/`, `gitops/`, and `.buildkite/` so built wheels ship only runtime packages and runtime data assets.
- Mirror `src/managed_ai/` and `scripts/` under `tests/` by domain capability (`tests/{platform_adapter,core,deployment,store,library,host,companions,collective,release,ci}/`), naming test modules for the behavior they verify rather than sprint or wave numbers.
- External domain repositories supply reusable schemas, catalogues, guidance, and domain operations independently (`source_root: "."`). Keep domain changes in the owning domain repository and refresh its `catalogue.json` and `domain.json` pins.
- Never open sealed source entries (marked `"exposure": "sealed"` in catalogues) as ordinary text sources.

## Development hygiene

- **Python 3.14+ standard library only (primary/containerized runtime: Python 3.15.2):** Core packages require zero third-party runtime dependencies and ship a PEP 561 `src/managed_ai/py.typed` marker.
- **No bytecode clutter:** Run Python with `-B` (or `PYTHONDONTWRITEBYTECODE=1`).
- **Line endings:** Use LF (`\n`). `.gitattributes` sets `* -text` so catalogue and manifest SHA-256 digests stay byte-identical across Windows and POSIX hosts.
- **Cross-platform safety:** Use `managed_ai.platform` for locking, atomic writes, streaming SHA-256, strict JSON parsing, Windows reserved-name validation, and unfollowed symlink / NTFS reparse-point checks. Always set `allow_abbrev=False` on argument parsers and subparsers.
- **Package-time artifacts:** Do not commit `MANIFEST.json`, `src/managed_ai/MANIFEST.json`, or `src/managed_ai/SOURCES.md`; `scripts/release/package.py` generates them for release archives.

## Running tests, linting, and type checks

From the repository root:

```sh
python -B -m unittest discover -s tests
python -B scripts/release/check_current.py
python -B -m unittest tests/ci/test_image_lifecycle.py
uvx ruff check src scripts tests && uvx ruff format --check src scripts
uvx ty check src scripts && uvx mypy src scripts
```

CI pipelines in `.buildkite/pipeline.yml` enforce `ruff`, `ty`, `mypy`, unit tests, `check_current.py`, deployment static checks, and wheel isolation audits before building any container image.

## Updating core guidance pins

After editing any document registered in `scripts/release/current_sources.json`, run:

```sh
python -B scripts/release/refresh_guidance.py
```

## License

By contributing to this repository, you agree that your contributions will be licensed under the MIT License (see [LICENSE](LICENSE)).
