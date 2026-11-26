#!/usr/bin/env python3
"""Public facade re-exporting shared deployment selection, identity, isolation, and CLI."""
from __future__ import annotations

from pathlib import Path
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
    _src_root = str(Path(__file__).resolve().parents[2])
    if _src_root not in sys.path:
        sys.path.insert(0, _src_root)

from managed_ai.core.storage import (
    AdmissionContended,
    admission_deadline,
    encode,
    exclusive,
    sha,
    shared,
)
from managed_ai.deployment.cli import main, parser
from managed_ai.deployment.contract import (
    DeploymentService,
    DeploymentStatus,
    EnginePin,
    StageReceipt,
    StorePin,
)
from managed_ai.deployment.identity import (
    CODE_IDENTITY_EXCLUDED_DIRS,
    ENGINE_API,
    ENGINE_ROOT,
    FORMAT,
    HERE,
    NATIVE_CONTRACT,
    NATIVE_ENTRY,
    NON_SOURCE_SUFFIXES,
    SRC_ROOT,
    STORE_FORMAT,
    STORE_SCHEMA,
    VERSION,
    _descriptor_bytes,
    _descriptor_object,
    _engine_directory,
    _engine_record,
    _git_provenance,
    _is_test_module_name,
    _ordinary_dir,
    _ordinary_mode,
    _status_payload,
    code_identity,
    read,
    read_descriptor,
    read_descriptor_bytes,
    runtime_binding,
)
from managed_ai.deployment.isolation import (
    _ISOLATED_ROOT_PACKAGES,
    _attach_deployment_metadata,
    _bind_managed_ai_package,
    _is_package_mounted,
    _isolated_engine,
    _load_entrypoint,
    open_admitted_store,
    opened_admitted_store,
)
from managed_ai.deployment.selection import (
    ActivationIncomplete,
    Deployment,
    DeploymentAdoptBasisChanged,
    DeploymentSelection,
    _check_required_domain,
    _extra_proof_stores,
    _precheck_local_system_args,
    observation_guard,
    report_admission,
    validate_deployment_sections,
)

__all__ = [
    "ActivationIncomplete",
    "AdmissionContended",
    "CODE_IDENTITY_EXCLUDED_DIRS",
    "Deployment",
    "DeploymentAdoptBasisChanged",
    "DeploymentSelection",
    "DeploymentService",
    "DeploymentStatus",
    "ENGINE_API",
    "ENGINE_ROOT",
    "EnginePin",
    "FORMAT",
    "NATIVE_CONTRACT",
    "NATIVE_ENTRY",
    "STORE_FORMAT",
    "STORE_SCHEMA",
    "StageReceipt",
    "StorePin",
    "VERSION",
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
