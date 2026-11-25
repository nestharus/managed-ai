"""Curated public store & deployment API for domain plugins (domains/*/operations/**).

Domain operations must import runtime primitives exclusively through this module
rather than reaching into private core.* or store.* internals.
"""
from __future__ import annotations

from core.formats import HEAD_INDEX_FORMAT
from core.ownership import NativeLayout, external_root, packet_id, relative_path
from core.storage import (
    AdmissionContended,
    MAX_BYTES,
    ObjectStore,
    OwnerBusy,
    admission_deadline,
    atomic_bytes,
    check_ordinary_ancestors,
    encode,
    exclusive,
    is_ordinary_file_stat,
    owner_admission,
    safe_relpath,
    sha,
    shared,
    strict_json_loads,
)
from deployment.controller import (
    Deployment,
    DeploymentSelection,
    code_identity,
    open_admitted_store,
    read_descriptor_bytes,
    report_admission,
)
from store.composition.composition import Composition
from store.composition.composition_opportunities import routes
from store.composition.composition_revision import revise
from store.governance.feedback import Feedback
from store.protocol.identities import (
    digest,
    exact_fields,
    identity,
    nonempty,
    record_identity,
    require,
    strict_json,
)
from store.protocol.inputs import strings
from store.query.context_discovery import Discovery as ContextDiscovery, canonical, request as discovery_request
from store.query.information import Information, pointer
from store.records.record_import import import_records, read_receipt
from store.records.record_transfer import inspect as inspect_transfer
from store.records.records import RecordStore
from store.selection.domain_selection import DomainSelection, source_path
from store.selection.installed_catalogue import InstalledCatalogue
from store.system import NativeError, System
from store.turns.preparation_journal import TurnPreparation

__all__ = [
    'AdmissionContended',
    'Composition',
    'ContextDiscovery',
    'Deployment',
    'DeploymentSelection',
    'DomainSelection',
    'Feedback',
    'HEAD_INDEX_FORMAT',
    'Information',
    'InstalledCatalogue',
    'MAX_BYTES',
    'NativeError',
    'NativeLayout',
    'ObjectStore',
    'OwnerBusy',
    'RecordStore',
    'System',
    'TurnPreparation',
    'admission_deadline',
    'atomic_bytes',
    'canonical',
    'check_ordinary_ancestors',
    'code_identity',
    'digest',
    'discovery_request',
    'encode',
    'exact_fields',
    'exclusive',
    'external_root',
    'identity',
    'import_records',
    'inspect_transfer',
    'is_ordinary_file_stat',
    'nonempty',
    'open_admitted_store',
    'owner_admission',
    'packet_id',
    'pointer',
    'read_descriptor_bytes',
    'read_receipt',
    'record_identity',
    'relative_path',
    'report_admission',
    'require',
    'revise',
    'routes',
    'safe_relpath',
    'sha',
    'shared',
    'source_path',
    'strict_json',
    'strict_json_loads',
    'strings',
]
