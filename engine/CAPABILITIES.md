# Current capabilities

| Contribution | Route | Boundary |
| --- | --- | --- |
| Capture goals, source context, proposals and acceptance | `runtime/manage_store.py` and [native contract](runtime/native/README.md) | Capable participants supply interpretation and permission. |
| Coordinate independent source-bound contributions | `runtime/manage_collective.py` and [Collective contract](runtime/collective/README.md) | Registered source and receiving-owner guards; no mandatory semantic supervisor. |
| Acquire complete selected knowledge | `knowledge.py show ID --text` and [sources](SOURCES.md) | Exact source bodies; acquisition does not establish applicability. |
| Discover and select sources | `knowledge.py discover --query TEXT`, native `discover` | Bounded lexical nominations; no semantic completeness guarantee. |
| Maintain continuing commissioned work | [host](host/README.md), `host/continuing_work/continuing_work.py` | Duties, actual captured turns, reminders and guarded receiving. |
| Develop representations and consequences | [construction](host/CONSTRUCTION.md), [representation checker](companion_representation/CONTRACT.md) | Structural checking supports capable semantic judgment. |
| Maintain useful current material | `host/material_retention/managed_material.py` and [contract](host/material_retention/MATERIAL-MAINTENANCE.md) | Current-use default; history requires explicit selection; no automatic raw-store deletion. |
| Run supplied model/tool callbacks, resume, coordinate peers | [shared companions](../companions/workbench/README.md) and [harness](../companions/harness/PROTOCOL.md) | Caller supplies actual models/receivers; no provider access bundled. |
| Observe changed publication inputs | [publication tools](../companions/workbench/PUBLICATION.md) | Exact declared dependencies and receipts, not discovered semantic dependency. |
| Prepare a qualified external method invocation | [method adapter](runtime/patterns/method-invocation/README.md) | Exact declared external runtime source required. |
| Exercise a receiving boundary | [local canary example](examples/aster-canary/README.md), [receiving example](examples/USAGE.md) | Local fixtures and declared identities; no external deployment. |
