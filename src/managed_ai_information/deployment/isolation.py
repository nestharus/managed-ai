"""External engine module isolation and verified entrypoint mounting."""

import importlib
import importlib.util
import runpy
import sys
from collections.abc import Generator
from contextlib import contextmanager
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import Any

from .identity import (
    ENGINE_API,
    ENGINE_ROOT,
    FORMAT,
    HERE,
    NATIVE_ENTRY,
    SRC_ROOT,
    _engine_directory,
)

_ISOLATED_ROOT_PACKAGES = frozenset(
    {"managed_ai_information", "core", "store", "library", "deployment"}
)


def _is_package_mounted(engine: Path) -> bool:
    loaded = sys.modules.get("managed_ai_information")
    if loaded is None:
        return False

    loaded_file = getattr(loaded, "__file__", None)
    if loaded_file and Path(loaded_file).resolve() == (engine / "__init__.py").resolve():
        return True

    return any(Path(p).resolve() == engine for p in getattr(loaded, "__path__", ()))


def _bind_managed_ai_package(engine: Path, *, force: bool = False) -> None:
    if not force and "managed_ai_information" in sys.modules:
        return

    if not force and engine.name == "managed_ai_information" and (engine / "__init__.py").is_file():
        src = str(engine.parent)
        if src not in sys.path:
            sys.path.insert(0, src)
        return

    init = engine / "__init__.py"
    if init.is_file():
        spec = importlib.util.spec_from_file_location(
            "managed_ai_information",
            str(init),
            submodule_search_locations=[str(engine)],
        )
    else:
        spec = ModuleSpec("managed_ai_information", None, is_package=True)
        spec.submodule_search_locations = [str(engine)]

    if spec is None:
        raise ImportError(f"unable to load managed_ai_information package from {engine}")

    module = importlib.util.module_from_spec(spec)
    sys.modules["managed_ai_information"] = module
    if spec.loader is not None:
        spec.loader.exec_module(module)


@contextmanager
def _isolated_engine(engine: Path) -> Generator[Path]:
    """Mount an external engine cleanly in sys.modules for the context lifetime, restoring on
    exit.
    """
    resolved = _engine_directory(engine)
    is_pkg = (resolved / "__init__.py").is_file()
    if is_pkg and _is_package_mounted(resolved):
        yield resolved
        return

    def selected_name(name: str) -> bool:
        return name.partition(".")[0] in _ISOLATED_ROOT_PACKAGES

    previous = {name: sys.modules.pop(name) for name in tuple(sys.modules) if selected_name(name)}
    search = sys.path[:]

    try:
        excluded = {HERE, ENGINE_ROOT, SRC_ROOT}
        filtered = [p for p in search if Path(p).resolve() not in excluded]
        if is_pkg:
            sys.path[:] = filtered
            _bind_managed_ai_package(resolved, force=True)
        else:
            sys.path[:] = [str(resolved), *filtered]
        importlib.invalidate_caches()
        yield resolved
    finally:
        for name in tuple(sys.modules):
            if selected_name(name):
                del sys.modules[name]
        sys.modules.update(previous)
        sys.path[:] = search
        importlib.invalidate_caches()


def _load_entrypoint(engine: Path) -> Any:
    """Load the verified typed EngineEntrypoint from this engine or an isolated external engine."""
    resolved = Path(engine).resolve(strict=True)
    if resolved == ENGINE_ROOT and _is_package_mounted(ENGINE_ROOT):
        from managed_ai_information.store.system import ENTRYPOINT

        return ENTRYPOINT

    if (resolved / "__init__.py").is_file():
        expected = (resolved / NATIVE_ENTRY).resolve(strict=True)
        mod = importlib.import_module("managed_ai_information.store.system")
        if mod.__file__ is None or Path(mod.__file__).resolve(strict=True) != expected:
            raise ValueError("loaded engine entrypoint differs from selected engine")
        ep = getattr(mod, "ENTRYPOINT", None)
    else:
        api = runpy.run_path(str(resolved / NATIVE_ENTRY))
        ep = api.get("ENTRYPOINT")

    if ep is None:
        raise ValueError("selected engine does not expose a typed EngineEntrypoint")
    return ep


def _attach_deployment_metadata(
    system: Any,
    engine: Path,
    deployment_root: Path | None,
    deployment_config: dict[str, Any] | None,
) -> None:
    if deployment_root is not None and deployment_config is not None:
        system.deployment = {
            "format": FORMAT,
            "selection": deployment_config,
            "engine_root": str(engine),
            "project_root": str(system.work),
            "project_files": [],
            "project_file_count": 0,
            "selection_scope": (
                "Per-store catalogues and domains are selected by the native store descriptor."
            ),
        }
        system.command_prefix = [
            sys.executable,
            "-B",
            str(engine / "deployment/select_deployment.py"),
            "--deployment",
            str(deployment_root),
            "system",
        ]


@contextmanager
def opened_admitted_store(
    engine: Path,
    store_root: Path,
    *,
    deployment_root: Path | None = None,
    deployment_config: dict[str, Any] | None = None,
    selection_override: Any = None,
) -> Generator[Any]:
    """Open an admitted native store with external engine isolation held across the context."""
    resolved_engine = _engine_directory(engine)
    with _isolated_engine(resolved_engine):
        ep = _load_entrypoint(resolved_engine)
        if ep.deployment_api != FORMAT or ep.engine_api != ENGINE_API:
            raise ValueError("selected engine does not implement the declared deployment API")

        system = ep.system_cls(Path(store_root).resolve(), _selection_override=selection_override)
        _attach_deployment_metadata(system, resolved_engine, deployment_root, deployment_config)
        yield system


def open_admitted_store(
    engine: Path,
    store_root: Path,
    *,
    deployment_root: Path | None = None,
    deployment_config: dict[str, Any] | None = None,
    selection_override: Any = None,
) -> Any:
    """Open an already-admitted native store through the verified engine entrypoint."""
    with opened_admitted_store(
        engine,
        store_root,
        deployment_root=deployment_root,
        deployment_config=deployment_config,
        selection_override=selection_override,
    ) as system:
        return system
