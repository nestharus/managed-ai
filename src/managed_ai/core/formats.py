"""Authoritative wire-format and schema-version registry for the managed-ai engine.

Centralizes versioned format strings across deployment descriptors, store descriptors,
exchange transport, release manifests, source libraries, records, indexes, cold archive,
batch/import receipts, maintenance plans/receipts, turn staging, and packet bindings,
with explicit backward-compatibility helpers for legacy unversioned persisted artifacts.
"""

from typing import Any

# Core API & descriptor contracts
ENGINE_API = "managed-ai-runtime/1"
STORE_SCHEMA = "managed-ai-store/1"
STORE_FORMAT = "g60-agent-system/0.1"
DEPLOYMENT_FORMAT = "agent-system-deployment/2"
DOMAIN_FORMAT = "managed-ai-domain/1"
CATALOGUE_FORMAT = "managed-ai-source-catalogue/1"
INSTALLED_CATALOGUE_FORMAT = "agent-system-catalogue/1"
REVIEWED_CONFIGURE_FORMAT = "managed-ai-reviewed-configure/1"

# Exchange & transport contracts
TRANSPORT_FORMAT = "material-objects-v1"

# Release & packaging manifests
ENGINE_MANIFEST_FORMAT = "managed-ai-engine-manifest/1"
PACKAGE_MANIFEST_FORMAT = "managed-ai-current-package/1"

# Source library formats
KNOWLEDGE_LIBRARY_FORMAT = "knowledge-library/1"
KNOWLEDGE_SELECTION_FORMAT = "knowledge-selection/1"
KNOWLEDGE_INDEX_FORMAT = "knowledge-search-index/1"
KNOWLEDGE_SOURCE_MAP_FORMAT = "knowledge-source-map/1"

# Domain-neutral record payload, discovery & composition contracts
COMPOSITION_FORMAT = "g60-composition/1"
COMPOSITION_VIEW_FORMAT = "g60-composition-view/1"
INTERACTION_MAP_FORMAT = "g60-interaction-map/1"
BOUNDARY_CONTRACT_FORMAT = "g60-boundary-contract/1"
BOUNDARY_OBSERVATIONS_V1 = "g60-boundary-observations/1"
BOUNDARY_OBSERVATIONS_V2 = "g60-boundary-observations/2"
BOUNDARY_VIEW_V1 = "g60-boundary-view/1"
BOUNDARY_VIEW_V2 = "g60-boundary-view/2"
CHANGE_NOTICE_FORMAT = "g60-change-notice/1"
OPPORTUNITY_INVENTORY_FORMAT = "composition-opportunity-inventory/1"
RECOMPOSITION_FORMAT = "g60-recomposition/1"
RECOMPOSITION_VIEW_FORMAT = "g60-recomposition-view/1"
INFORMATION_FORMAT = "g60-information/1"
DISCOVERY_FORMAT = "agent-system-discovery/1"
CONTEXT_EXPLANATION_FORMAT = "agent-system-context-explanation/1"
INCLUSION_ORIGINS_FORMAT = "agent-system-inclusion-origins/1"

# Store index (v2 adds guaranteed graph adjacency & content-link hints; v1 read-compatible)
HEAD_INDEX_FORMAT_V1 = "managed-ai-head-index/1"
HEAD_INDEX_FORMAT = "managed-ai-head-index/2"
HEAD_INDEX_SUPPORTED_FORMATS = frozenset({HEAD_INDEX_FORMAT_V1, HEAD_INDEX_FORMAT})

# Import, transfer & guarded batch receipts
IMPORT_FORMAT = "managed-ai-import/1"
IMPORT_RECEIPT_FORMAT = "managed-ai-import-receipt/1"
TRANSFER_FORMAT = "managed-ai-transfer/1"
SOURCE_OBSERVATION_FORMAT = "managed-ai-source-observation/1"
BATCH_FORMAT = "managed-ai-batch-update/1"
BATCH_RECEIPT_FORMAT = "managed-ai-batch-receipt/1"

# Cold archive & object compaction / audit / GC
COLD_ARCHIVE_MANIFEST_FORMAT = "managed-ai-cold-archive/1"
ARCHIVE_PLAN_FORMAT = "managed-ai-archive-records-plan/1"
ARCHIVE_RECEIPT_FORMAT = "managed-ai-archive-records-receipt/1"
RESTORE_RECEIPT_FORMAT = "managed-ai-restore-records-receipt/1"
DEDUP_PLAN_FORMAT = "managed-ai-deduplicate-originals-plan/1"
DEDUP_RECEIPT_FORMAT = "managed-ai-deduplicate-originals-receipt/1"
COMPACT_PLAN_FORMAT = "managed-ai-compact-objects-plan/1"
COMPACT_RECEIPT_FORMAT = "managed-ai-compact-objects-receipt/1"
OBJECT_AUDIT_FORMAT = "managed-ai-object-audit/1"
OBJECT_GC_RECEIPT_FORMAT = "managed-ai-gc-objects-receipt/1"

# Turn working-copy retirement & graph query / lint
TURN_RETIRE_PLAN_FORMAT = "managed-ai-turn-retirement-plan/1"
TURN_RETIRE_RECEIPT_FORMAT = "managed-ai-turn-retirement-receipt/1"
GRAPH_NEIGHBORS_FORMAT = "managed-ai-graph-neighbors/1"
GRAPH_LINKS_FORMAT = "managed-ai-graph-links/1"
GRAPH_PATH_FORMAT = "managed-ai-graph-path/1"
GRAPH_LINT_FORMAT = "managed-ai-graph-lint/1"

# Previously unversioned internal exchange & turn artifacts (versioned on write;
# legacy unversioned files without a 'format' key remain readable for compatibility).
PACKET_BINDING_FORMAT = "managed-ai-packet-binding/1"
TURN_PREPARATION_FORMAT = "managed-ai-turn-preparation/1"
TURN_SELECTION_FORMAT = "managed-ai-turn-selection/1"
TURN_EXPOSURE_FORMAT = "managed-ai-turn-exposure/1"
TURN_VIEW_FORMAT = "managed-ai-turn-view/1"


REGISTRY: dict[str, str] = {
    "engine_api": ENGINE_API,
    "store_schema": STORE_SCHEMA,
    "store": STORE_FORMAT,
    "deployment": DEPLOYMENT_FORMAT,
    "domain": DOMAIN_FORMAT,
    "catalogue": CATALOGUE_FORMAT,
    "installed_catalogue": INSTALLED_CATALOGUE_FORMAT,
    "reviewed_configure": REVIEWED_CONFIGURE_FORMAT,
    "transport": TRANSPORT_FORMAT,
    "engine_manifest": ENGINE_MANIFEST_FORMAT,
    "package_manifest": PACKAGE_MANIFEST_FORMAT,
    "knowledge_library": KNOWLEDGE_LIBRARY_FORMAT,
    "knowledge_selection": KNOWLEDGE_SELECTION_FORMAT,
    "knowledge_index": KNOWLEDGE_INDEX_FORMAT,
    "knowledge_source_map": KNOWLEDGE_SOURCE_MAP_FORMAT,
    "composition": COMPOSITION_FORMAT,
    "composition_view": COMPOSITION_VIEW_FORMAT,
    "interaction_map": INTERACTION_MAP_FORMAT,
    "boundary_contract": BOUNDARY_CONTRACT_FORMAT,
    "boundary_observations_v1": BOUNDARY_OBSERVATIONS_V1,
    "boundary_observations_v2": BOUNDARY_OBSERVATIONS_V2,
    "boundary_view_v1": BOUNDARY_VIEW_V1,
    "boundary_view_v2": BOUNDARY_VIEW_V2,
    "change_notice": CHANGE_NOTICE_FORMAT,
    "opportunity_inventory": OPPORTUNITY_INVENTORY_FORMAT,
    "recomposition": RECOMPOSITION_FORMAT,
    "recomposition_view": RECOMPOSITION_VIEW_FORMAT,
    "information": INFORMATION_FORMAT,
    "discovery": DISCOVERY_FORMAT,
    "context_explanation": CONTEXT_EXPLANATION_FORMAT,
    "inclusion_origins": INCLUSION_ORIGINS_FORMAT,
    "head_index_v1": HEAD_INDEX_FORMAT_V1,
    "head_index": HEAD_INDEX_FORMAT,
    "import": IMPORT_FORMAT,
    "import_receipt": IMPORT_RECEIPT_FORMAT,
    "transfer": TRANSFER_FORMAT,
    "source_observation": SOURCE_OBSERVATION_FORMAT,
    "batch": BATCH_FORMAT,
    "batch_receipt": BATCH_RECEIPT_FORMAT,
    "cold_archive_manifest": COLD_ARCHIVE_MANIFEST_FORMAT,
    "archive_plan": ARCHIVE_PLAN_FORMAT,
    "archive_receipt": ARCHIVE_RECEIPT_FORMAT,
    "restore_receipt": RESTORE_RECEIPT_FORMAT,
    "dedup_plan": DEDUP_PLAN_FORMAT,
    "dedup_receipt": DEDUP_RECEIPT_FORMAT,
    "compact_plan": COMPACT_PLAN_FORMAT,
    "compact_receipt": COMPACT_RECEIPT_FORMAT,
    "object_audit": OBJECT_AUDIT_FORMAT,
    "object_gc_receipt": OBJECT_GC_RECEIPT_FORMAT,
    "turn_retire_plan": TURN_RETIRE_PLAN_FORMAT,
    "turn_retire_receipt": TURN_RETIRE_RECEIPT_FORMAT,
    "graph_neighbors": GRAPH_NEIGHBORS_FORMAT,
    "graph_links": GRAPH_LINKS_FORMAT,
    "graph_path": GRAPH_PATH_FORMAT,
    "graph_lint": GRAPH_LINT_FORMAT,
    "packet_binding": PACKET_BINDING_FORMAT,
    "turn_preparation": TURN_PREPARATION_FORMAT,
    "turn_selection": TURN_SELECTION_FORMAT,
    "turn_exposure": TURN_EXPOSURE_FORMAT,
    "turn_view": TURN_VIEW_FORMAT,
}


def validate_optional_format(doc: Any, expected: str, label: str) -> None:
    """Accept legacy unversioned documents (no 'format' key) or exact matching format."""
    if not isinstance(doc, dict):
        raise ValueError(f"{label} must be a JSON object")
    observed = doc.get("format")
    if observed is not None and observed != expected:
        raise ValueError(f"unsupported {label} format: {observed!r}")
