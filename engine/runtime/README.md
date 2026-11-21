# Production runtime

The complete current native and Collective implementations are here. Keep mutable work, dependencies and generated output outside this distribution.

| Need | Contract |
| --- | --- |
| Persistent goals, open records, captured source context, guarded adoption | [Store implementation and contract](store/README.md) and [store checks](checks/store/) |
| Compose source-bound contributions | [Composition](store/COMPOSITION.md) |
| Shared authority and peer exchange | [Collective work](collective-work/README.md) |
| Exact source acquisition and lexical discovery | [Knowledge system](knowledge-system/README.md) |
| Return collection and source-bound continuation transport | [Continuation package](continuation_support/), [API](docs/api.md) and [continuation checks](checks/continuation/) |

Run `python -B system.py --root /absolute/work init --goal-text "Contribution"` or `python -B collective.py --db /absolute/work/collective.sqlite init` from this directory. The public reader at `../knowledge.py` reads canonical core sources and, with `--store PATH`, that store’s selected domain/source catalogues. No installed mirror of source bodies is needed.

The continuation core is version 0.4.2 and the native application is version 0.5.0. The shared engine supports independent portable stores, data-defined domains and current-use record retention; see [instance contracts](../INSTANCES.md). Optional examples and historical design discussions have their stated scope; a source title alone does not imply a complete external application is installed.
