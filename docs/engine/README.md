# Shared AI-system engine

This is the single executable engine (`src/managed_ai/`) for every instance in the package. The native application is version 0.5.0 with portable store bindings, declared payload schemas, selected source catalogues and current-use record retention. The continuation core is version 0.4.2. [Current release identities](../../src/managed_ai/CURRENT-RELEASE.json) identify the selected canonical guidance.

The production subsystems live under `src/managed_ai/` on top of the stdlib-only [platform leaf](../../src/managed_ai/platform/). Use [select_deployment.py](../../src/managed_ai/deployment/select_deployment.py) to enter a selected deployment, [manage_store.py](../../src/managed_ai/store/manage_store.py) for a direct native store, [exchange_packets.py](../../src/managed_ai/core/cli/exchange_packets.py) for standalone packet exchange, [track_attention.py](../../src/managed_ai/core/cli/track_attention.py) for attention tracking, [collect_returns.py](../../src/managed_ai/core/cli/collect_returns.py) for a local carrier and [library/cli.py](../../src/managed_ai/library/cli.py) for catalogue or source-library reading. The [platform adapter](../../src/managed_ai/platform/), [deployment controller](../../src/managed_ai/deployment/), [native store](../../src/managed_ai/store/), [continuation core](../../src/managed_ai/core/), [source library](../../src/managed_ai/library/) and [operator tools](../../src/managed_ai/tools/) own those operations. Maintainer and release scripts live under [scripts/](../../scripts/). [Tests](../../tests/) follow the same boundaries.

[Stores, domains and deployments](INSTANCES.md) defines their actual contract. From the package root, operate the selected store through the same code:

```sh
python -B src/managed_ai/deployment/select_deployment.py --deployment /path/to/deployment system status
```

The [native operating contract](store/README.md), [CLI field reference](CLI-FIELD-REFERENCE.md) and [capability map](CAPABILITIES.md) explain source capture, real returns, guarded adoption and current-use interpretation. [Construction](host/CONSTRUCTION.md) supplies general operating guidance. [Peer communication](host/continuing_work/PEER-COMMUNICATION.md) applies when a comparison needs an exposure boundary.

[Material maintenance](host/material_retention/MATERIAL-MAINTENANCE.md) selects current useful content and deliberately selected evidence. Native record replacement also defaults to current-use retention. Schema validation, source custody, semantic adequacy and completion remain distinct judgments.

[Core source selection](../../src/managed_ai/release/current_sources.json) names each canonical operating source (and `scripts/release/package.py` generates `SOURCES.md` for exported bundles). Domain methods, schemas, catalogues, and workspace state have their own external owners; the engine reads selected sources through verified references without mirroring external corpora.

The optional [continuing-work host](host/README.md) can attach to a selected existing native store using `native_root`. The [shared companions](../companions/README.md) supply model/tool callbacks and peer adapters; they do not create a second engine implementation.
