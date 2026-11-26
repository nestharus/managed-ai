# Working in Managed AI

Start with [README.md](README.md), [ARCHITECTURE.md](ARCHITECTURE.md), and the selected store's goal and entry sources. Enter stores through their deployment descriptor (`src/managed_ai_information/deployment/select_deployment.py`). Domain names, schemas, and domain operations belong in external domain repositories, not conditionals in `src/managed_ai_information/`.

- **Single owner per artifact:** Edit the owning code module, source body, or store record and refresh dependent pins/views (`python -B scripts/release/refresh_guidance.py` for core guidance). Do not copy `src/managed_ai_information/` into a deployment or mirror one domain into another.
- **Platform leaf & domain boundaries:** Keep OS-specific primitives strictly in `src/managed_ai_information/platform/`, keep domain contracts/configs inside their owning packages, and keep maintainer/CI-CD scripts under `scripts/`.
- **Distinct claims:** Source availability, body acquisition, receiving another store's record, and effective use are separate judgments. Preserve provenance, scope, and evidence limits on cross-store transfers.
- **Current-use retention:** Retain active goals, obligations, useful arguments, counterexamples, and exact evidence needed by live duties or unresolved turns. Do not accumulate superseded copies without a concrete continuing use.
- **Sealed payloads:** Never open entries marked `"exposure": "sealed"` in catalogues as ordinary text sources.
- **External workspaces:** Live stores live outside the source checkout. Keep `src/managed_ai_information/`, external domains, and workspace storage decoupled.
