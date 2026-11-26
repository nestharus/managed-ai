#!/usr/bin/env python3
"""Public facade re-exporting shared deployment selection, identity, isolation, and CLI."""

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    _src_root = str(Path(__file__).resolve().parents[2])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai_information.core.storage import (
    AdmissionContended,
    admission_deadline,
    encode,
    exclusive,
    sha,
    shared,
)
from managed_ai_information.deployment.cli import main, parser
from managed_ai_information.deployment.contract import (
    DeploymentService,
    DeploymentStatus,
    EnginePin,
    StageReceipt,
    StorePin,
)
from managed_ai_information.deployment.identity import (
    CODE_IDENTITY_EXCLUDED_DIRS,
    ENGINE_API,
    ENGINE_ROOT,
    FORMAT,
    NATIVE_CONTRACT,
    NATIVE_ENTRY,
    STORE_FORMAT,
    STORE_SCHEMA,
    VERSION,
    code_identity,
    read,
    read_descriptor,
    read_descriptor_bytes,
    runtime_binding,
)
from managed_ai_information.deployment.identity import (
    HERE as HERE,
)
from managed_ai_information.deployment.identity import (
    NON_SOURCE_SUFFIXES as NON_SOURCE_SUFFIXES,
)
from managed_ai_information.deployment.identity import (
    SRC_ROOT as SRC_ROOT,
)
from managed_ai_information.deployment.identity import (
    _descriptor_bytes as _descriptor_bytes,
)
from managed_ai_information.deployment.identity import (
    _descriptor_object as _descriptor_object,
)
from managed_ai_information.deployment.identity import (
    _engine_directory as _engine_directory,
)
from managed_ai_information.deployment.identity import (
    _engine_record as _engine_record,
)
from managed_ai_information.deployment.identity import (
    _git_provenance as _git_provenance,
)
from managed_ai_information.deployment.identity import (
    _is_test_module_name as _is_test_module_name,
)
from managed_ai_information.deployment.identity import (
    _ordinary_dir as _ordinary_dir,
)
from managed_ai_information.deployment.identity import (
    _ordinary_mode as _ordinary_mode,
)
from managed_ai_information.deployment.identity import (
    _status_payload as _status_payload,
)
from managed_ai_information.deployment.isolation import (
    _ISOLATED_ROOT_PACKAGES as _ISOLATED_ROOT_PACKAGES,
)
from managed_ai_information.deployment.isolation import (
    _attach_deployment_metadata as _attach_deployment_metadata,
)
from managed_ai_information.deployment.isolation import (
    _bind_managed_ai_package as _bind_managed_ai_package,
)
from managed_ai_information.deployment.isolation import (
    _is_package_mounted as _is_package_mounted,
)
from managed_ai_information.deployment.isolation import (
    _isolated_engine as _isolated_engine,
)
from managed_ai_information.deployment.isolation import (
    _load_entrypoint as _load_entrypoint,
)
from managed_ai_information.deployment.isolation import (
    open_admitted_store,
    opened_admitted_store,
)
from managed_ai_information.deployment.selection import (
    ActivationIncomplete,
    Deployment,
    DeploymentAdoptBasisChanged,
    DeploymentSelection,
    observation_guard,
    report_admission,
    validate_deployment_sections,
)
from managed_ai_information.deployment.selection import (
    _check_required_domain as _check_required_domain,
)
from managed_ai_information.deployment.selection import (
    _extra_proof_stores as _extra_proof_stores,
)
from managed_ai_information.deployment.selection import (
    _precheck_local_system_args as _precheck_local_system_args,
)

__all__ = [
    "CODE_IDENTITY_EXCLUDED_DIRS",
    "ENGINE_API",
    "ENGINE_ROOT",
    "FORMAT",
    "NATIVE_CONTRACT",
    "NATIVE_ENTRY",
    "STORE_FORMAT",
    "STORE_SCHEMA",
    "VERSION",
    "ActivationIncomplete",
    "AdmissionContended",
    "Deployment",
    "DeploymentAdoptBasisChanged",
    "DeploymentSelection",
    "DeploymentService",
    "DeploymentStatus",
    "EnginePin",
    "StageReceipt",
    "StorePin",
    "admission_deadline",
    "code_identity",
    "encode",
    "exclusive",
    "main",
    "observation_guard",
    "open_admitted_store",
    "opened_admitted_store",
    "parser",
    "read",
    "read_descriptor",
    "read_descriptor_bytes",
    "report_admission",
    "runtime_binding",
    "sha",
    "shared",
    "validate_deployment_sections",
]

if __name__ == "__main__":
    main()
