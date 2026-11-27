"""Curated public store & deployment API for domain plugins (standalone domain repositories).

Domain operations must import runtime primitives exclusively through this module
rather than reaching into private core.* or store.* internals.
"""

import importlib
from typing import Any

from managed_ai_information.core.formats import HEAD_INDEX_FORMAT as HEAD_INDEX_FORMAT
from managed_ai_information.core.ownership import (
    NativeLayout as NativeLayout,
)
from managed_ai_information.core.ownership import (
    external_root as external_root,
)
from managed_ai_information.core.ownership import (
    packet_id as packet_id,
)
from managed_ai_information.core.ownership import (
    relative_path as relative_path,
)
from managed_ai_information.core.storage import (
    MAX_BYTES as MAX_BYTES,
)
from managed_ai_information.core.storage import (
    AdmissionContended as AdmissionContended,
)
from managed_ai_information.core.storage import (
    ObjectStore as ObjectStore,
)
from managed_ai_information.core.storage import (
    OwnerBusy as OwnerBusy,
)
from managed_ai_information.core.storage import (
    admission_deadline as admission_deadline,
)
from managed_ai_information.core.storage import (
    atomic_bytes as atomic_bytes,
)
from managed_ai_information.core.storage import (
    check_ordinary_ancestors as check_ordinary_ancestors,
)
from managed_ai_information.core.storage import (
    encode as encode,
)
from managed_ai_information.core.storage import (
    exclusive as exclusive,
)
from managed_ai_information.core.storage import (
    is_ordinary_file_stat as is_ordinary_file_stat,
)
from managed_ai_information.core.storage import (
    owner_admission as owner_admission,
)
from managed_ai_information.core.storage import (
    safe_relpath as safe_relpath,
)
from managed_ai_information.core.storage import (
    sha as sha,
)
from managed_ai_information.core.storage import (
    shared as shared,
)
from managed_ai_information.core.storage import (
    strict_json_loads as strict_json_loads,
)
from managed_ai_information.store.composition.composition import Composition as Composition
from managed_ai_information.store.composition.composition_opportunities import routes as routes
from managed_ai_information.store.composition.composition_revision import revise as revise
from managed_ai_information.store.governance.feedback import Feedback as Feedback
from managed_ai_information.store.protocol.identities import (
    digest as digest,
)
from managed_ai_information.store.protocol.identities import (
    exact_fields as exact_fields,
)
from managed_ai_information.store.protocol.identities import (
    identity as identity,
)
from managed_ai_information.store.protocol.identities import (
    nonempty as nonempty,
)
from managed_ai_information.store.protocol.identities import (
    record_identity as record_identity,
)
from managed_ai_information.store.protocol.identities import (
    require as require,
)
from managed_ai_information.store.protocol.identities import (
    strict_json as strict_json,
)
from managed_ai_information.store.protocol.inputs import strings as strings
from managed_ai_information.store.query.context_discovery import (
    Discovery as ContextDiscovery,
)
from managed_ai_information.store.query.context_discovery import canonical as canonical
from managed_ai_information.store.query.context_discovery import (
    request as discovery_request,
)
from managed_ai_information.store.query.information import (
    Information as Information,
)
from managed_ai_information.store.query.information import (
    pointer as pointer,
)
from managed_ai_information.store.records.record_import import (
    import_records as import_records,
)
from managed_ai_information.store.records.record_import import (
    read_receipt as read_receipt,
)
from managed_ai_information.store.records.record_scanner import (
    VerifiedIndexView as VerifiedIndexView,
)
from managed_ai_information.store.records.record_scanner import (
    open_verified_index_view as open_verified_index_view,
)
from managed_ai_information.store.records.record_transfer import inspect as inspect_transfer
from managed_ai_information.store.records.records import RecordStore as RecordStore
from managed_ai_information.store.selection.domain_selection import (
    DomainSelection as DomainSelection,
)
from managed_ai_information.store.selection.domain_selection import (
    source_path as source_path,
)
from managed_ai_information.store.selection.installed_catalogue import (
    InstalledCatalogue as InstalledCatalogue,
)
from managed_ai_information.store.system import (
    NativeError as NativeError,
)
from managed_ai_information.store.system import (
    System as System,
)
from managed_ai_information.store.turns.preparation_journal import (
    TurnPreparation as TurnPreparation,
)

_STATIC_EXPORTS = (
    "HEAD_INDEX_FORMAT",
    "MAX_BYTES",
    "AdmissionContended",
    "Composition",
    "ContextDiscovery",
    "DomainSelection",
    "Feedback",
    "Information",
    "InstalledCatalogue",
    "NativeError",
    "NativeLayout",
    "ObjectStore",
    "OwnerBusy",
    "RecordStore",
    "System",
    "TurnPreparation",
    "VerifiedIndexView",
    "admission_deadline",
    "atomic_bytes",
    "canonical",
    "check_ordinary_ancestors",
    "digest",
    "discovery_request",
    "encode",
    "exact_fields",
    "exclusive",
    "external_root",
    "identity",
    "import_records",
    "inspect_transfer",
    "is_ordinary_file_stat",
    "nonempty",
    "open_verified_index_view",
    "owner_admission",
    "packet_id",
    "pointer",
    "read_receipt",
    "record_identity",
    "relative_path",
    "require",
    "revise",
    "routes",
    "safe_relpath",
    "sha",
    "shared",
    "source_path",
    "strict_json",
    "strict_json_loads",
    "strings",
)

_DEPLOYMENT_EXPORTS = frozenset(
    {
        "Deployment",
        "DeploymentSelection",
        "code_identity",
        "open_admitted_store",
        "opened_admitted_store",
        "read_descriptor_bytes",
        "report_admission",
        "runtime_binding",
    }
)

_ALIASED_EXPORTS = (ContextDiscovery, discovery_request, inspect_transfer)

__all__ = sorted([*_STATIC_EXPORTS, *_DEPLOYMENT_EXPORTS])


def __getattr__(name: str) -> Any:
    if name in _DEPLOYMENT_EXPORTS:
        mod = importlib.import_module("managed_ai_information.deployment.controller")
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))
