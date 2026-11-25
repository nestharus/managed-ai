# Shared AI-system engine

This is the single executable engine for every instance in the package. The native application is version 0.5.0 with portable store bindings, declared payload schemas, selected source catalogues and current-use record retention. The continuation core is version 0.4.2. [Current release identities](CURRENT-RELEASE.json) identify the selected canonical guidance.

The production subsystems live directly under `engine/`. Use [select_deployment.py](deployment/select_deployment.py) to enter a selected deployment, [manage_store.py](store/manage_store.py) for a direct native store, [exchange_packets.py](core/cli/exchange_packets.py) for standalone packet exchange, [track_attention.py](core/cli/track_attention.py) for attention tracking, [collect_returns.py](core/cli/collect_returns.py) for a local carrier and [library/cli.py](library/cli.py) for catalogue or source-library reading. The [deployment controller](deployment/), [native store](store/), [continuation core](core/), [source library](library/) and [operator tools](tools/) own those operations. [Engine tests](../tests/engine/) follow the same boundaries.

[Stores, domains and deployments](INSTANCES.md) defines their actual contract. From the package root, operate the selected store through the same code:

```sh
python3 -B engine/deployment/select_deployment.py --deployment deployments/research system status
python3 -B engine/deployment/select_deployment.py --deployment deployments/novel system status
```

The [native operating contract](store/README.md), [CLI field reference](CLI-FIELD-REFERENCE.md) and [capability map](CAPABILITIES.md) explain source capture, real returns, guarded adoption and current-use interpretation. [Construction](host/CONSTRUCTION.md) supplies general operating guidance. [Peer communication](host/continuing_work/PEER-COMMUNICATION.md) applies when a comparison needs an exposure boundary.

[Material maintenance](host/material_retention/MATERIAL-MAINTENANCE.md) selects current useful content and deliberately selected evidence. Native record replacement also defaults to current-use retention. Schema validation, source custody, semantic adequacy and completion remain distinct judgments.

[SOURCES.md](SOURCES.md) names each canonical operating source. Broad theory, domain methods, human and agent guidance, research state and application material have their own owners under the enclosing package. The engine reads selected sources through verified references; it does not carry another complete source mirror.

The optional [continuing-work host](host/README.md) can attach to a selected existing native store using `native_root`. The [shared companions](../companions/README.md) supply model/tool callbacks and peer adapters; they do not create a second engine implementation.
