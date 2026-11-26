# Working in Managed AI

Start with [README.md](README.md), [ARCHITECTURE.md](ARCHITECTURE.md), and the selected store's goal and entry sources. Enter stores through their deployment descriptor (`engine/deployment/select_deployment.py`). Domain names, schemas, and domain operations belong in `domains/*`, not conditionals in `engine/`.

- **Single owner per artifact:** Edit the owning code module, source body, or store record and refresh dependent pins/views. Do not copy `engine/` into a deployment or mirror one domain into another.
- **Distinct claims:** Source availability, body acquisition, receiving another store's record, and effective use are separate judgments. Preserve provenance, scope, and evidence limits on cross-store transfers.
- **Current-use retention:** Retain active goals, obligations, useful arguments, counterexamples, and exact evidence needed by live duties or unresolved turns. Do not accumulate superseded copies without a concrete continuing use.
- **Sealed payloads:** Never open entries marked `"exposure": "sealed"` (including sealed D12 payloads) as ordinary text sources.
- **External workspaces:** Live stores live outside the source checkout. Keep `engine/`, `domains/*`, and workspace storage decoupled.
