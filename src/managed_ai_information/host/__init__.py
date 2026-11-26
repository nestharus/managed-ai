"""Managed AI host adapters: continuing work, material retention, and signal field."""

import importlib
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "ContinuingWorkService": (".continuing_work.contract", "ContinuingWorkService"),
    "HostConfig": (".continuing_work.contract", "HostConfig"),
    "MaterialRetentionService": (".material_retention.config", "MaterialRetentionService"),
    "RetentionLimits": (".material_retention.config", "RetentionLimits"),
    "SignalFieldConfig": (".signal_field.config", "SignalFieldConfig"),
    "SignalFieldService": (".signal_field.config", "SignalFieldService"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    mod_name, attr_name = target
    value = getattr(importlib.import_module(mod_name, __name__), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
