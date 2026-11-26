# Current capabilities

| Contribution | Route | Boundary |
| --- | --- | --- |
| Capture goals, source context, proposals and acceptance | `src/managed_ai/store/manage_store.py` and [native contract](store/README.md) | Capable participants supply interpretation and permission. |
| Coordinate independent source-bound contributions | `src/managed_ai/collective/store/cli.py` and [Collective contract](../collective/README.md) | Registered source and receiving-owner guards; no mandatory semantic supervisor. |
| Acquire complete selected knowledge | `src/managed_ai/library/cli.py show ID --text` and [source selection](../../scripts/release/current_sources.json) | Exact source bodies; acquisition does not establish applicability. |
| Discover and select sources | `src/managed_ai/library/cli.py discover --query TEXT`, native `discover` | Bounded lexical nominations; no semantic completeness guarantee. |
| Maintain continuing commissioned work | [host](host/README.md), `src/managed_ai/host/continuing_work/continuing_work.py` | Duties, actual captured turns, reminders and guarded receiving. |
| Develop representations and consequences | [construction](host/CONSTRUCTION.md), [representation checker](../companions/representation/CONTRACT.md) | Structural checking supports capable semantic judgment. |
| Maintain useful current material | `src/managed_ai/host/material_retention/managed_material.py` and [contract](host/material_retention/MATERIAL-MAINTENANCE.md) | Current-use default; history requires explicit selection; no automatic raw-store deletion. |
| Run supplied model/tool callbacks, resume, coordinate peers | [shared companions](../companions/workbench/docs/README.md) and [peer harness](../companions/peer/docs/PROTOCOL.md) | Caller supplies actual models/receivers; no provider access bundled. |
| Observe changed publication inputs | [publication tools](../companions/publication_review/docs/PUBLICATION.md) | Exact declared dependencies and receipts, not discovered semantic dependency. |
| Coordinate branch/review/integrate consolidation | [continuous consolidation](../patterns/continuous-consolidation/README.md) | Prompt and inventory protocol; callers supply capable participants and review gates. |
| Exercise a receiving boundary | [local canary example](../../examples/aster-canary/README.md), [receiving example](../../examples/README.md) | Local fixtures and declared identities; no external deployment. |
