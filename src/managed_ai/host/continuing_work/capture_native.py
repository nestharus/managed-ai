#!/usr/bin/env python3
"""Host-owned selected capture for a qualified native implementation.

Canonical engine/store/manage_store.py implementations qualify through:
  1. ordinary unfollowed paths across engine/{deployment,core,store} (no symlinks/junctions),
  2. verified store/system.py ENTRYPOINT contract (engine_api, deployment_api) and required
     public APIs (Attention.inspect_receipt, Exchange.packet_summary, System.assess), and
  3. co-located auto-admission (recording live profile SHA-256 and optional Git HEAD commit),
     OR explicit --expected-profile <sha256> / verified --deployment <dir> for external engines.

Frozen LEGACY_NATIVE_PROFILES remain strictly for historical pre-Wave-4 engine/runtime/ replay.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import hashlib
import importlib
from importlib.machinery import EXTENSION_SUFFIXES
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys

HERE = Path(__file__).resolve().parent
SRC_ROOT = HERE.parents[2]
if __package__ in (None, '') and str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from managed_ai.host.continuing_work.occurrence_files import (
    check_unfollowed_ancestors,
    is_reparse_or_symlink,
    loads_strict,
)

COLOCATED_ENGINE_ROOT = HERE.parents[1]
COLOCATED_ENGINE_ENTRY = COLOCATED_ENGINE_ROOT / 'store/manage_store.py'
EXPECTED_ENGINE_API = 'managed-ai-runtime/1'
EXPECTED_DEPLOYMENT_API = 'agent-system-deployment/2'
EXPECTED_STORE_SCHEMA = 'managed-ai-store/1'
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
_CODE_ID_EXCLUDED_DIRS = frozenset({'tests', 'examples', 'patterns', 'companions', 'optional'})
_NON_SOURCE_SUFFIXES = ('.pyc', '.pyo', '.pyd', '.so', *EXTENSION_SUFFIXES)

# Frozen historical pre-Wave-4 engine/runtime/ profiles kept only for archival replay.
LEGACY_NATIVE_PROFILES = frozenset({
    'f65119d9a2751f7927ecfb502df1f26c07ee1c958c9b219aeb13f6efc1c6565e',  # reviewed
    '65d21e3482ae5ca9a4a9a3060e8d2c60e98bc95e224f6969dbed198d317910aa',  # reorganized
    '7b6176300e8802b9c2714bf9cd9ebdfa8d2741b41d21c680c39f5bf5a32784e3',  # repaired
    'fb535b60e0d22b6fc482f84dcd4482ecce2dda52fa6e06ee5915268a4cc3f621',  # command-routes
    'e92245c09bc33a22e7aa086be2ae141093dd19bf2302f69d53c771e5e8e23cc9',  # body-repairs
    '5f0830029a210dd73fd3299a3a6f5c170a2802dadf85b3076b5640831774d836',  # schema-repairs
    'e031f4e246801e7ea5d41306c7356891872af6444f7fd9b998bacd48ea67945a',  # lifecycle
    '9b879473fc1a7a46b530027d5f7045101b05183b9742027861af15d3f145e6ea',  # outlier-repairs
    '6bb020ad0a1feacaab040b7b8bfe448e9f472525902bad1203344d2ec2f436f4',  # scoped-composition
    '27c5c10a3c1a0016fb5fb9e86454025103aa1a22e9889044ad65bd9f8932316d',  # joined
    'db6b9c8a443483e99932b8970b5abbc506622d1a2f337a1fe9b452a98cec706a',  # packet-lifecycle
    'cc7fb2cdb1c88b5ecff826bac7088e93d11f13a0317fd673f5291778736f1c44',  # bounded-admission
    'e772c4c3a51d8db2937a5cbcb991dc0a5c2433f70082a7b02d32cda5aabdd5e4',  # exponential-admission
    '2eb453170ca260aa32575b628480ec529d71f78d80dcbe052ebe5f807569945f',  # wave1
    'a61df8f59eadde2f1631f7002e062770ae68c577de973717e9f4997748fee1d4',  # wave2
    'dbfaee1c623984cbd60833413de6c4aab1033214d4ddf6300c415b04bfbeefce',  # pre-wave3
    '23c5c06540959fe6cbdd0e9e58e3bb69c328616f879caa6fd4604b22d790cf82',  # wave3
    '82f1d0421b9cb9c134e5d0fb97c14e530407c409ac3716730a7f758e16ddd609',  # wave3.5
})

PROFILE_ERROR = ('Selected native engine does not match the reviewed capture adapter; '
                 'requalify this adapter before capture')
EXTERNAL_PROFILE_ERROR = ('External native engine requires explicit --expected-profile or '
                          'verified --deployment pin before capture')
CONTRACT_ERROR = 'Selected native engine does not satisfy the required capture adapter contract'
RETURN_ERROR = 'Native return already exists; use its retained body or a new current occurrence'


def _is_test_module(name: str) -> bool:
    lower = name.lower()
    return lower.endswith('.py') and (lower.startswith('test_') or lower.endswith('_test.py'))


def _profile_digest(programs: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(programs, sort_keys=True, separators=(',', ':')).encode('utf-8'),
    ).hexdigest()


def _scan_ordinary_programs(base: Path, roots: tuple[Path, ...], *, exclude_tests: bool) -> dict[str, str]:
    programs: dict[str, str] = {}
    for folder in roots:
        if is_reparse_or_symlink(folder):
            raise ValueError(f'Native engine tree must not contain symlinks or reparse points: {folder}')
        if not folder.is_dir():
            continue
        for parent, dirs, files in os.walk(folder, followlinks=False):
            dirs.sort()
            files.sort()
            kept_dirs = []
            for dname in dirs:
                dpath = Path(parent) / dname
                if is_reparse_or_symlink(dpath):
                    raise ValueError(f'Native engine tree must not contain symlinks or reparse points: {dpath}')
                if dname == '__pycache__':
                    continue
                if exclude_tests and dname == 'tests':
                    continue
                kept_dirs.append(dname)
            dirs[:] = kept_dirs
            for fname in files:
                if not fname.endswith('.py'):
                    continue
                fpath = Path(parent) / fname
                if is_reparse_or_symlink(fpath):
                    raise ValueError(f'Native engine tree must not contain symlinks or reparse points: {fpath}')
                if not stat.S_ISREG(fpath.lstat().st_mode):
                    raise ValueError(f'Native engine source must be an ordinary file: {fpath}')
                if exclude_tests and _is_test_module(fname):
                    continue
                programs[fpath.relative_to(base).as_posix()] = hashlib.sha256(fpath.read_bytes()).hexdigest()
    return programs


def _native_sources_with_layout(entry: Path) -> tuple[Path, dict[str, str], str]:
    checked = check_unfollowed_ancestors(entry, 'Native engine entry', include_target=True)
    if not checked.exists() or not stat.S_ISREG(checked.lstat().st_mode):
        raise ValueError(PROFILE_ERROR)
    resolved = checked.resolve(strict=True)
    engine = resolved.parents[1]
    if resolved == engine / 'store/manage_store.py' and (engine / 'store/system.py').is_file():
        native = engine / 'store/system.py'
        if is_reparse_or_symlink(native):
            raise ValueError(f'Native engine entry must not be a symlink or reparse point: {native}')
        roots = (engine / 'deployment', engine / 'core', engine / 'store')
        programs = _scan_ordinary_programs(engine, roots, exclude_tests=True)
        return native, programs, 'canonical'
    if resolved == engine / 'runtime/manage_store.py' and (engine / 'runtime/native/system.py').is_file():
        native = engine / 'runtime/native/system.py'
        if is_reparse_or_symlink(native):
            raise ValueError(f'Native engine entry must not be a symlink or reparse point: {native}')
        roots = (engine / 'runtime',)
        programs = _scan_ordinary_programs(engine, roots, exclude_tests=False)
        return native, programs, 'legacy'
    raise ValueError(PROFILE_ERROR)


def native_sources(entry: Path):
    """Collect the exact executable native engine source map for profile qualification.

    Canonical engines hash only engine/{deployment,core,store} non-test modules.
    Pre-Wave-4 historical engines hash engine/runtime/ to preserve historical profile pins.
    """
    native, programs, _ = _native_sources_with_layout(entry)
    return native, programs


def _engine_code_identity(engine_root: Path) -> str:
    """Compute engine/deployment/controller.py:code_identity() without importing external engine code."""
    engine = check_unfollowed_ancestors(engine_root, 'Deployment pinned engine', include_target=True).resolve(strict=True)
    if not engine.is_dir():
        raise ValueError(f'ordinary shared engine directory required: {engine}')
    paths: list[Path] = []
    pending: list[tuple[Path, bool]] = [(engine, False)]
    while pending:
        directory, in_cache = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = directory / entry.name
                info = entry.stat(follow_symlinks=False)
                if is_reparse_or_symlink(path):
                    raise ValueError(f'shared engine may not use symbolic links or reparse points: {path}')
                mode = info.st_mode
                if stat.S_ISDIR(mode):
                    if entry.name.casefold() in _CODE_ID_EXCLUDED_DIRS:
                        continue
                    pending.append((path, in_cache or entry.name.casefold() == '__pycache__'))
                elif not stat.S_ISREG(mode):
                    raise ValueError(f'ordinary shared engine file or directory required: {path}')
                elif in_cache:
                    if path.suffix.lower() != '.pyc':
                        raise ValueError(f'ordinary bytecode cache files required below __pycache__: {path}')
                elif path.name.lower().endswith(_NON_SOURCE_SUFFIXES):
                    raise ValueError(f'shared engine requires Python source for executable modules: {path}')
                elif path.match('*.py') and not _is_test_module(entry.name):
                    paths.append(path)
    native_entry = Path('store/system.py')
    native_contract = Path('store/selection/domain_selection.py')
    declaration_paths = {native_entry, native_contract}
    rows = []
    declarations: dict[Path, bytes] = {}
    for path in sorted(paths):
        relative = path.relative_to(engine)
        raw = path.read_bytes()
        rows.append({'path': relative.as_posix(), 'sha256': hashlib.sha256(raw).hexdigest()})
        if relative in declaration_paths:
            declarations[relative] = raw
    if declaration_paths != declarations.keys():
        raise ValueError('select the current engine root containing its native entry and API declarations')

    def declared(rel: Path) -> dict:
        result = {}
        try:
            tree = ast.parse(declarations[rel], filename=str(engine / rel))
        except SyntaxError as exc:
            raise ValueError(f'invalid engine API declaration in {engine / rel}: {exc.msg}') from exc
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                try:
                    result[node.targets[0].id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass
        return result

    contract = declared(native_contract)
    if (contract.get('ENGINE_API') != EXPECTED_ENGINE_API
            or contract.get('STORE_SCHEMA') != EXPECTED_STORE_SCHEMA
            or declared(native_entry).get('DEPLOYMENT_API') != EXPECTED_DEPLOYMENT_API):
        raise ValueError('selected engine declares incompatible storage/deployment APIs')
    encoded_rows = (json.dumps(rows, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    return hashlib.sha256(encoded_rows).hexdigest()


def _verify_deployment_pin(deployment_dir: Path, expected_engine_root: Path) -> None:
    dep_root = check_unfollowed_ancestors(deployment_dir, 'Deployment directory', include_target=True).resolve(strict=True)
    if not dep_root.is_dir():
        raise ValueError(f'Deployment directory does not exist: {dep_root}')
    descriptor_path = check_unfollowed_ancestors(dep_root / 'deployment.json', 'Deployment descriptor', include_target=True)
    if not descriptor_path.is_file():
        raise ValueError(f'Deployment descriptor is missing: {descriptor_path}')
    doc = loads_strict(descriptor_path.read_bytes())
    if not isinstance(doc, dict) or doc.get('format') != EXPECTED_DEPLOYMENT_API:
        raise ValueError('Deployment descriptor format is incompatible')
    engine_spec = doc.get('engine')
    if (not isinstance(engine_spec, dict)
            or not isinstance(engine_spec.get('path'), str)
            or not isinstance(engine_spec.get('code_sha256'), str)
            or engine_spec.get('engine_api') != EXPECTED_ENGINE_API):
        raise ValueError('Deployment descriptor lacks a valid engine pin')
    pinned_engine = check_unfollowed_ancestors(dep_root / engine_spec['path'], 'Deployment pinned engine', include_target=True)
    if pinned_engine.resolve(strict=True) != expected_engine_root.resolve(strict=True):
        raise ValueError('Selected native engine path differs from verified deployment pin')
    if _engine_code_identity(pinned_engine) != engine_spec['code_sha256']:
        raise ValueError('Selected native engine code differs from verified deployment pin')


def _git_head_commit(engine_root: Path) -> str | None:
    """Best-effort read of Git HEAD commit SHA without spawning subprocesses."""
    cur = engine_root.resolve()
    git_entry = None
    for candidate in (cur, *cur.parents[:4]):
        probe = candidate / '.git'
        if probe.exists():
            git_entry = probe
            break
    if git_entry is None:
        return None
    try:
        if git_entry.is_file():
            first = git_entry.read_text(encoding='utf-8').strip()
            if not first.startswith('gitdir:'):
                return None
            raw_dir = first.split(':', 1)[1].strip()
            git_dir = Path(raw_dir)
            if not git_dir.is_absolute():
                git_dir = (git_entry.parent / git_dir).resolve()
        elif git_entry.is_dir():
            git_dir = git_entry
        else:
            return None
        head_text = (git_dir / 'HEAD').read_text(encoding='utf-8').strip()
        if re.fullmatch(r'[0-9a-f]{40}', head_text):
            return head_text
        if not head_text.startswith('ref:'):
            return None
        ref_name = head_text.split(':', 1)[1].strip()
        search_dirs = [git_dir]
        commondir_file = git_dir / 'commondir'
        if commondir_file.is_file():
            common_raw = commondir_file.read_text(encoding='utf-8').strip()
            common_dir = Path(common_raw)
            if not common_dir.is_absolute():
                common_dir = (git_dir / common_dir).resolve()
            search_dirs.append(common_dir)
        for base in search_dirs:
            ref_file = base / ref_name
            if ref_file.is_file():
                commit = ref_file.read_text(encoding='utf-8').strip()
                if re.fullmatch(r'[0-9a-f]{40}', commit):
                    return commit
            packed = base / 'packed-refs'
            if packed.is_file():
                for line in packed.read_text(encoding='utf-8').splitlines():
                    if not line or line.startswith(('#', '^')):
                        continue
                    parts = line.split(' ', 1)
                    if len(parts) == 2 and parts[1].strip() == ref_name and re.fullmatch(r'[0-9a-f]{40}', parts[0]):
                        return parts[0]
    except (OSError, UnicodeDecodeError):
        return None
    return None


def _verify_canonical_contract(module) -> None:
    ep = getattr(module, 'ENTRYPOINT', None)
    if (ep is None
            or getattr(ep, 'engine_api', None) != EXPECTED_ENGINE_API
            or getattr(ep, 'deployment_api', None) != EXPECTED_DEPLOYMENT_API):
        raise ValueError(CONTRACT_ERROR)
    system_cls = getattr(module, 'System', None)
    attention_cls = getattr(module, 'Attention', None)
    exchange_cls = getattr(module, 'Exchange', None)
    if (system_cls is None or attention_cls is None or exchange_cls is None
            or not callable(getattr(system_cls, 'assess', None))
            or not callable(getattr(attention_cls, 'inspect_receipt', None))
            or not callable(getattr(exchange_cls, 'packet_summary', None))):
        raise ValueError(CONTRACT_ERROR)


def _is_colocated_engine(resolved_entry: Path) -> bool:
    if not COLOCATED_ENGINE_ENTRY.is_file():
        return False
    try:
        checked = check_unfollowed_ancestors(COLOCATED_ENGINE_ENTRY, 'Co-located engine', include_target=True)
    except ValueError:
        return False
    return resolved_entry == checked.resolve(strict=True)


@contextmanager
def _mount_canonical_module(path: Path, engine_root: Path):
    """Mount store/system.py from a canonical src/managed_ai package (or legacy engine/ layout).

    For external managed_ai package copies, keeps the external managed_ai.* namespace
    mounted in sys.modules for the full context lifetime so lazy function-body imports
    inside System/Attention/Exchange resolve strictly to the qualified external engine,
    then restores the caller's sys.path and sys.modules cleanly on exit.
    """
    resolved_engine = engine_root.resolve()
    init_file = resolved_engine / '__init__.py'
    if init_file.is_file():
        colocated_root = COLOCATED_ENGINE_ROOT.resolve()
        if resolved_engine == colocated_root:
            src_root_str = str(resolved_engine.parent)
            if src_root_str not in sys.path:
                sys.path.insert(0, src_root_str)
            yield importlib.import_module('managed_ai.store.system')
            return
        # External managed_ai package copy (regardless of outer directory name):
        # mount it as 'managed_ai' in an isolated sys.modules snapshot across the context.
        saved_path = list(sys.path)
        saved_modules = {
            name: sys.modules.pop(name)
            for name in list(sys.modules)
            if name == 'managed_ai' or name.startswith('managed_ai.')
        }
        colocated_src = colocated_root.parent
        def _is_colocated_src(entry: str) -> bool:
            try:
                return Path(entry or '.').resolve() == colocated_src
            except OSError:
                return False
        try:
            sys.path[:] = [entry for entry in saved_path if not _is_colocated_src(entry)]
            importlib.invalidate_caches()
            pkg_spec = importlib.util.spec_from_file_location(
                'managed_ai',
                init_file,
                submodule_search_locations=[str(resolved_engine)],
            )
            if pkg_spec is None or pkg_spec.loader is None:
                raise ValueError(PROFILE_ERROR)
            pkg_mod = importlib.util.module_from_spec(pkg_spec)
            sys.modules['managed_ai'] = pkg_mod
            pkg_spec.loader.exec_module(pkg_mod)
            module = importlib.import_module('managed_ai.store.system')
            loaded_file = getattr(module, '__file__', None)
            if not loaded_file or Path(loaded_file).resolve() != path.resolve():
                raise ValueError(PROFILE_ERROR)
            yield module
        finally:
            sys.path[:] = saved_path
            for name in list(sys.modules):
                if name == 'managed_ai' or name.startswith('managed_ai.'):
                    sys.modules.pop(name, None)
            sys.modules.update(saved_modules)
            importlib.invalidate_caches()
        return

    saved_path = list(sys.path)
    saved_legacy = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name in ('core', 'store', 'deployment') or name.startswith(('core.', 'store.', 'deployment.'))
    }
    engine_root_str = str(resolved_engine)
    spec = importlib.util.spec_from_file_location('reviewed_capture_engine', path)
    if spec is None or spec.loader is None:
        sys.modules.update(saved_legacy)
        raise ValueError(PROFILE_ERROR)
    previous = sys.modules.get(spec.name)
    try:
        if engine_root_str in sys.path:
            sys.path.remove(engine_root_str)
        sys.path.insert(0, engine_root_str)
        importlib.invalidate_caches()
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path[:] = saved_path
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
        for name in list(sys.modules):
            if name in ('core', 'store', 'deployment') or name.startswith(('core.', 'store.', 'deployment.')):
                sys.modules.pop(name, None)
        sys.modules.update(saved_legacy)
        importlib.invalidate_caches()


def _load_canonical_module(path: Path, engine_root: Path):
    """Load store/system.py (prefer qualified_engine() context manager for external engines)."""
    with _mount_canonical_module(path, engine_root) as module:
        return module


@contextmanager
def qualified_engine(engine: Path, *, expected_profile: str | None = None,
                     deployment: Path | None = None):
    """Qualify and mount a native implementation for the duration of an operation.

    Refuses unqualified external engines before executing any external code, keeps
    external managed_ai.* packages mounted across lazy imports inside the context,
    and restores sys.modules / sys.path on exit.
    """
    path, programs, layout = _native_sources_with_layout(engine)
    profile = _profile_digest(programs)
    resolved_entry = Path(engine).resolve(strict=True)
    engine_root = path.parents[1]

    if expected_profile is not None:
        if not isinstance(expected_profile, str) or not _SHA256_RE.fullmatch(expected_profile):
            raise ValueError('expected_profile must be a 64-character lowercase SHA-256 hex digest')

    if layout == 'legacy':
        if profile not in LEGACY_NATIVE_PROFILES:
            raise ValueError(PROFILE_ERROR)
        if expected_profile is not None and profile != expected_profile:
            raise ValueError(PROFILE_ERROR)
        if deployment is not None:
            raise ValueError('Historical engine/runtime/ cannot use --deployment qualification')
        admission = 'legacy-pinned'
    else:
        admission_modes = []
        if expected_profile is not None:
            if profile != expected_profile:
                raise ValueError(PROFILE_ERROR)
            admission_modes.append('expected-profile')
        if deployment is not None:
            _verify_deployment_pin(deployment, engine_root)
            admission_modes.append('deployment-pin')
        if admission_modes:
            admission = '+'.join(admission_modes)
        elif _is_colocated_engine(resolved_entry):
            admission = 'colocated'
        else:
            raise ValueError(EXTERNAL_PROFILE_ERROR)

    info = {
        'profile': profile,
        'admission': admission,
        'git_head_commit': _git_head_commit(engine_root),
    }
    if layout == 'canonical':
        with _mount_canonical_module(path, engine_root) as module:
            _verify_canonical_contract(module)
            yield module, info
        return

    spec = importlib.util.spec_from_file_location('reviewed_capture_engine', path)
    if spec is None or spec.loader is None:
        raise ValueError(PROFILE_ERROR)
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module, info
    finally:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous


_qualified_engine = qualified_engine


def qualify_engine(engine: Path, *, expected_profile: str | None = None,
                   deployment: Path | None = None) -> tuple[object, dict]:
    """Qualify and load a native implementation; refuse unqualified external engines before exec.

    Callers that invoke methods with lazy imports on an external managed_ai package should
    use `with qualified_engine(...) as (native, info):` so the external namespace remains mounted.
    """
    with qualified_engine(engine, expected_profile=expected_profile, deployment=deployment) as result:
        return result


def reviewed_engine(engine: Path, *, expected_profile: str | None = None,
                    deployment: Path | None = None):
    """Verify the selected native implementation before loading it."""
    module, info = qualify_engine(engine, expected_profile=expected_profile, deployment=deployment)
    return module, info['profile']


def legacy_packet_summary(exchange, directory, consumer, publications, returned, outcome):
    """The admitted predecessor engines predate Exchange.packet_summary."""
    attempted = exchange.transport.has_attempt(directory)
    transport = 'dispatch-attempted' if attempted else 'prepared'
    if returned:
        transport = 'returned'
    row = {'packet': directory.name, 'consumer': consumer, 'transport': transport,
           'publications': publications, 'semantic_fulfillment': 'not-decided-by-exchange'}
    if outcome is not None:
        row['transport'] = 'terminal-without-return'
        row['transport_outcome'] = {k: outcome[k] for k in ('reason', 'account', 'outcome_sha256')}
    return row


def turn_summary(system, turn: str, observed=None) -> dict:
    """Project only the inspected packet. Current basis observations do not decide transport."""
    try:
        observed = system.ex.inspect(turn) if observed is None else observed
        publications = []
        for observation in observed['publications']:
            if 'record' not in observation:
                raise ValueError(observation['observation_error'])
            record = observation['record']
            publications.append({'path': record['path'], 'phase': record['phase']})
        directory = system.ex.directory(turn)
        returned = observed['return'] is not None
        outcome = observed.get('transport_outcome')
        if hasattr(system.ex, 'packet_summary'):
            return system.ex.packet_summary(directory, observed['consumer'], publications,
                                            returned=returned, outcome=outcome)
        return legacy_packet_summary(system.ex, directory, observed['consumer'], publications, returned, outcome)
    except (ValueError, KeyError, OSError, TypeError) as exc:
        return {'packet': turn, 'transport': 'record-unresolved', 'error': str(exc)}


def _exchange_root(native, root: Path) -> tuple[Path, Path]:
    """Resolve and validate the native root and exchange lock root before locking."""
    resolved_root = native.external(root) if hasattr(native, 'external') else root.expanduser().resolve()
    raw_exchange = native.NativeLayout(resolved_root).exchange if hasattr(native, 'NativeLayout') else resolved_root / 'exchange'
    exchange_root = native.external(raw_exchange) if hasattr(native, 'external') else raw_exchange.expanduser().resolve()
    return resolved_root, exchange_root


def _lookup_attention_receipt(system, raw_id: str) -> tuple[str, dict, str | None]:
    """Inspect a recorded Attention receipt via public API (with pre-Wave-4 historical fallback)."""
    if hasattr(system.attention, 'inspect_receipt'):
        inspected = system.attention.inspect_receipt(raw_id)
        if inspected is None:
            raise ValueError('receipt identity not recorded in native Attention')
        return inspected['object'], inspected['record'], inspected.get('feedback_object')
    # Historical pre-Wave-4 engines predate Attention.inspect_receipt.
    state = system.attention._load()
    current = system.attention._current(state)
    position = current['receipt_ids'].get(raw_id)
    if position is None:
        raise ValueError('receipt identity not recorded in native Attention')
    obj_key = state['receipts'][position]
    record = system.attention._get(obj_key)
    return obj_key, record, state['feedbacks'].get(record.get('feedback'))


def _reconcile_assessment(native, system, turn: str, receipt: Path, producer: str, observed: dict) -> dict:
    """Verify an already-committed native Attention receipt matches this exact capture request."""
    supplied = native.read(receipt)
    if not isinstance(supplied, dict) or set(supplied) != {'id', 'assessments'}:
        raise ValueError('receipt must contain id and assessments')
    raw_id = supplied['id']
    if not isinstance(raw_id, str):
        raise ValueError('receipt identity not recorded in native Attention')
    expected_source_sha = native.sha(native.encode(supplied))
    expected_feedback = native.feedback_id(turn)
    obj_key, record, current_feedback_obj = _lookup_attention_receipt(system, raw_id)
    rid = native.packet_id(raw_id)
    if (record.get('id') != rid
            or record.get('feedback') != expected_feedback
            or record.get('feedback_object') != current_feedback_obj
            or record.get('producer') != producer
            or record.get('source', {}).get('sha256') != expected_source_sha):
        raise ValueError('receipt identity already used')
    attention = {
        'receipt': record['id'],
        'object': obj_key,
        'recorded': [item['item'] for item in record['assessments']],
        'boundary': record['boundary'],
    }
    return {
        'attention': attention,
        'status': {'turns': [turn_summary(system, turn, observed)]},
        'reconciled': True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--engine', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--expected-profile', default=None,
                        help='Explicit 64-hex SHA-256 profile pin for an external native engine')
    parser.add_argument('--deployment', default=None, type=Path,
                        help='Verified deployment directory whose deployment.json pins the external engine')
    commands = parser.add_subparsers(dest='action', required=True)
    capture = commands.add_parser('assess', allow_abbrev=False)
    capture.add_argument('turn')
    capture.add_argument('--receipt', required=True, type=Path)
    capture.add_argument('--producer', required=True)
    reconcile = commands.add_parser('reconcile-assess', allow_abbrev=False,
                                    help='verify and recover an already-committed assessment receipt')
    reconcile.add_argument('turn')
    reconcile.add_argument('--receipt', required=True, type=Path)
    reconcile.add_argument('--producer', required=True)
    inspect = commands.add_parser('inspect-turn', allow_abbrev=False, help='read-only compatibility observation')
    inspect.add_argument('turn')
    args = parser.parse_args()
    try:
        with qualified_engine(
            args.engine,
            expected_profile=args.expected_profile,
            deployment=args.deployment,
        ) as (native, info):
            root, exchange_root = _exchange_root(native, args.root)
            with native.exclusive(exchange_root / 'owner.lock'):
                system = native.System(root)
                if system.ex.root.resolve() != exchange_root.resolve():
                    raise ValueError('native exchange lock root differs from selected system exchange')
                if args.action == 'inspect-turn':
                    result = {'turn': turn_summary(system, args.turn)}
                elif args.action == 'reconcile-assess':
                    observed = system.ex.inspect(args.turn)
                    result = _reconcile_assessment(native, system, args.turn, args.receipt, args.producer, observed)
                else:
                    # Keep the open-turn check and retention in one cooperative
                    # owner invocation. assess only changes Attention, so this
                    # selected transport observation remains applicable afterward.
                    observed = system.ex.inspect(args.turn)
                    if observed['return'] is not None:
                        raise ValueError(RETURN_ERROR)
                    attention = system.assess(args.turn, args.receipt, args.producer)
                    result = {'attention': attention,
                              'status': {'turns': [turn_summary(system, args.turn, observed)]}}
                adapter_meta = {
                    'native_profile_sha256': info['profile'],
                    'admission': info['admission'],
                    'scope': 'Selected native assessment and turn only; no global status computation.',
                }
                if info.get('git_head_commit') is not None:
                    adapter_meta['git_head_commit'] = info['git_head_commit']
                result['capture_adapter'] = adapter_meta
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({'status': 'unresolved', 'operation': args.action,
                          'error': str(exc), **getattr(exc, 'details', {})}, allow_nan=False), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    main()
