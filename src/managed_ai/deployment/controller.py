#!/usr/bin/env python3
"""Select one shared native engine and one schema-governed store by reference.

Deployment metadata never copies an engine, project or store. Native captures
retain the selected occurrence needed by their receiving work.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from importlib.machinery import EXTENSION_SUFFIXES, ModuleSpec
import importlib.util
from pathlib import Path
import argparse
import ast
import io
import json
import os
import runpy
import stat
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ENGINE_ROOT = HERE.parent
SRC_ROOT = ENGINE_ROOT.parent


def _bind_managed_ai_package(engine: Path, *, force: bool = False) -> None:
    if not force and 'managed_ai' in sys.modules:
        return
    if engine.name == 'managed_ai' and (engine / '__init__.py').is_file():
        src = str(engine.parent)
        if src not in sys.path:
            sys.path.insert(0, src)
        return
    init = engine / '__init__.py'
    if init.is_file():
        spec = importlib.util.spec_from_file_location(
            'managed_ai', str(init), submodule_search_locations=[str(engine)],
        )
    else:
        spec = ModuleSpec('managed_ai', None, is_package=True)
        spec.submodule_search_locations = [str(engine)]
    if spec is None:
        raise ImportError(f'unable to load managed_ai package from {engine}')
    module = importlib.util.module_from_spec(spec)
    sys.modules['managed_ai'] = module
    if spec.loader is not None:
        spec.loader.exec_module(module)


_bind_managed_ai_package(ENGINE_ROOT)

from managed_ai.core._platform import admission_deadline
from managed_ai.core.formats import (
    DEPLOYMENT_FORMAT as FORMAT,
    ENGINE_API,
    STORE_FORMAT,
    STORE_SCHEMA,
)
from managed_ai.core.ownership import NativeLayout, external_root, packet_id
from managed_ai.core.storage import (
    AdmissionContended,
    atomic_bytes,
    check_ordinary_ancestors,
    encode,
    exclusive,
    is_ordinary_file_stat,
    is_reparse_or_symlink,
    owner_admission,
    safe_relpath,
    sha,
    shared,
    strict_json_loads,
)

VERSION = '0.3.0'
NATIVE_ENTRY = Path('store/system.py')
NATIVE_CONTRACT = Path('store/selection/domain_selection.py')
NON_SOURCE_SUFFIXES = ('.pyc', '.pyo', '.pyd', '.so', *EXTENSION_SUFFIXES)
CODE_IDENTITY_EXCLUDED_DIRS = frozenset({'tests', 'examples', 'patterns', 'companions', 'optional'})
_ISOLATED_ROOT_PACKAGES = frozenset({'managed_ai', 'core', 'store', 'library', 'deployment'})


def read_descriptor_bytes(path):
    ordinary = check_ordinary_ancestors(Path(path), expect_dir=False, error_prefix='ordinary descriptor required')
    return ordinary.read_bytes()


def read_descriptor(path, raw=None):
    data = read_descriptor_bytes(path) if raw is None else raw
    return strict_json_loads(data, path, require_object=True)


_descriptor_bytes = read_descriptor_bytes
_descriptor_object = lambda path, raw: read_descriptor(path, raw)


def read(path):
    return read_descriptor(path)


def _ordinary_mode(path, info):
    if is_reparse_or_symlink(info):
        raise ValueError(f'shared engine may not use symbolic links or reparse points: {path}')
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError(f'ordinary shared engine file or directory required: {path}')
    return info.st_mode


def _engine_directory(path):
    path = Path(path)
    if not path.is_absolute():
        if path.anchor:
            raise ValueError(f'fully qualified or relative shared engine path required: {path}')
        path = Path.cwd() / path
    current = Path(path.anchor)
    _ordinary_mode(current, current.lstat())
    for part in path.parts[1:]:
        if part == '..':
            current = current.parent
            continue
        current /= part
        if not stat.S_ISDIR(_ordinary_mode(current, current.lstat())):
            raise ValueError(f'ordinary shared engine directory required: {current}')
    return current.resolve(strict=True)


def _is_test_module_name(name: str) -> bool:
    lower = name.lower()
    return lower.endswith('.py') and (lower.startswith('test_') or lower.endswith('_test.py'))


def _git_provenance(engine: Path) -> dict[str, object] | None:
    """Return optional Git commit/clean provenance for a checkout, or None outside Git."""
    probe = engine
    has_git = False
    for candidate in (probe, *probe.parents):
        if (candidate / '.git').exists():
            has_git = True
            break
    if not has_git:
        return None
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    try:
        head = subprocess.run(
            ['git', '-C', str(engine), 'rev-parse', '--verify', 'HEAD'],
            capture_output=True, text=True, encoding='utf-8', env=env, timeout=3.0,
        )
        if head.returncode != 0:
            return None
        commit = head.stdout.strip()
        if len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit.lower()):
            return None
        status = subprocess.run(
            ['git', '-C', str(engine), 'status', '--porcelain', '--', '.'],
            capture_output=True, text=True, encoding='utf-8', env=env, timeout=3.0,
        )
        if status.returncode != 0:
            return None
        return {'git_commit': commit.lower(), 'git_clean': not bool(status.stdout.strip())}
    except (OSError, subprocess.SubprocessError):
        return None


def _engine_record(engine: Path, root: Path, code_sha: str) -> dict[str, object]:
    record: dict[str, object] = {
        'path': safe_relpath(engine, root),
        'code_sha256': code_sha,
        'engine_api': ENGINE_API,
    }
    prov = _git_provenance(engine)
    if prov is not None:
        record.update(prov)
    return record


def _status_payload(root: Path, engine: Path, runtime: Path, config: dict, descriptor_sha256: str) -> dict:
    store = config['store']
    eng = config['engine']
    payload = {
        'format': FORMAT,
        'deployment': str(root),
        'engine': str(engine),
        'engine_code_sha256': eng['code_sha256'],
        'store': str(runtime),
        'instance_id': store['instance_id'],
        'store_schema': store['store_schema'],
        'engine_api': store['engine_api'],
        'descriptor_sha256': descriptor_sha256,
        'scope': 'One shared code tree; explicit store and data-domain selection. No copied runtime.',
    }
    if 'git_commit' in eng:
        payload['engine_git_commit'] = eng['git_commit']
    if 'git_clean' in eng:
        payload['engine_git_clean'] = eng['git_clean']
    return payload


def _ordinary_dir(path: Path, error_prefix: str = 'ordinary deployment, engine and store directories required') -> Path:
    return check_ordinary_ancestors(Path(path), expect_dir=True, error_prefix=error_prefix).resolve(strict=True)


def code_identity(engine):
    """Hash executable Python source modules under the engine root (excluding tests, examples, patterns, companions, optional)."""
    engine = _engine_directory(engine)
    paths: list[Path] = []
    pending: list[tuple[Path, bool]] = [(engine, False)]
    while pending:
        directory, cache = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                name = entry.name
                path = directory / name
                mode = _ordinary_mode(path, entry.stat(follow_symlinks=False))
                if stat.S_ISDIR(mode):
                    lower = name.casefold()
                    if lower in CODE_IDENTITY_EXCLUDED_DIRS:
                        continue
                    pending.append((path, cache or lower == '__pycache__'))
                elif cache:
                    if not name.lower().endswith('.pyc'):
                        raise ValueError(f'ordinary bytecode cache files required below __pycache__: {path}')
                else:
                    lower = name.lower()
                    if lower.endswith(NON_SOURCE_SUFFIXES):
                        raise ValueError(f'shared engine requires Python source for executable modules: {path}')
                    if lower.endswith('.py') and not _is_test_module_name(name):
                        paths.append(path)
    sorted_paths = sorted(paths)
    interfaces = [(NATIVE_ENTRY, NATIVE_CONTRACT)]
    declaration_paths = {path for pair in interfaces for path in pair}

    def hash_chunk(chunk: list[Path]):
        out = []
        for path in chunk:
            relative = path.relative_to(engine)
            raw = path.read_bytes()
            out.append((relative, {'path': relative.as_posix(), 'sha256': sha(raw)},
                        raw if relative in declaration_paths else None))
        return out

    rows, declarations = [], {}
    if len(sorted_paths) >= 32:
        workers = min(8, (len(sorted_paths) + 15) // 16)
        step = (len(sorted_paths) + workers - 1) // workers
        chunks = [sorted_paths[i:i + step] for i in range(0, len(sorted_paths), step)]
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            hashed = [item for sub in pool.map(hash_chunk, chunks) for item in sub]
    else:
        hashed = hash_chunk(sorted_paths)
    for relative, row, raw in hashed:
        rows.append(row)
        if raw is not None:
            declarations[relative] = raw
    if declaration_paths != declarations.keys():
        raise ValueError('select the current engine root containing its native entry and API declarations')

    def declared(path):
        result = {}
        try:
            declaration = ast.parse(declarations[path], filename=str(engine / path))
        except SyntaxError as exc:
            raise ValueError(f'invalid engine API declaration in {engine / path}: {exc.msg}') from exc
        for node in declaration.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                try:
                    result[node.targets[0].id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    pass
        return result

    for entry, schema in interfaces:
        contract = declared(schema)
        if (contract.get('ENGINE_API') != ENGINE_API or contract.get('STORE_SCHEMA') != STORE_SCHEMA or
                declared(entry).get('DEPLOYMENT_API') != FORMAT):
            raise ValueError('selected engine declares incompatible storage/deployment APIs')
    return sha(encode(rows))


def runtime_binding(root):
    config = read(root / 'system.json')
    binding = read(root / 'exchange/workspace.json')
    if (config.get('format') != STORE_FORMAT or
            config.get('store_schema') != STORE_SCHEMA or config.get('engine_api') != ENGINE_API):
        raise ValueError('store/engine schema incompatibility; explicit import or migration required')
    packet_id(config['instance_id'])
    if (type(binding.get('version')) is not int or binding['version'] != 2 or
            binding.get('workspace_relative') != '../work' or
            binding.get('instance_id') != config['instance_id']):
        raise ValueError('portable store binding differs')
    return {'instance_id': config['instance_id'], 'store_schema': STORE_SCHEMA,
            'engine_api': ENGINE_API}


class ActivationIncomplete(RuntimeError):
    def __init__(self, failed_step, descriptor_sha256, cause):
        super().__init__(f'activation committed but {failed_step} failed: {cause}')
        self.failed_step = failed_step
        self.descriptor_sha256 = descriptor_sha256


class DeploymentAdoptBasisChanged(ValueError):
    def __init__(self, message, basis):
        super().__init__(message)
        self.basis = basis


def _load_entrypoint(engine: Path):
    """Load the verified typed EngineEntrypoint from this engine or an isolated external engine."""
    engine = Path(engine).resolve(strict=True)
    if engine == ENGINE_ROOT:
        from managed_ai.store.system import ENTRYPOINT
        return ENTRYPOINT
    api = runpy.run_path(str(engine / NATIVE_ENTRY))
    ep = api.get('ENTRYPOINT')
    if ep is None:
        raise ValueError('selected engine does not expose a typed EngineEntrypoint')
    return ep


def _attach_deployment_metadata(system, engine: Path, deployment_root: Path | None,
                                deployment_config: dict | None) -> None:
    if deployment_root is not None and deployment_config is not None:
        system.deployment = {
            'format': FORMAT,
            'selection': deployment_config,
            'engine_root': str(engine),
            'project_root': str(system.work),
            'project_files': [],
            'project_file_count': 0,
            'selection_scope': 'Per-store catalogues and domains are selected by the native store descriptor.',
        }
        system.command_prefix = [
            sys.executable, '-B', str(engine / 'deployment/select_deployment.py'),
            '--deployment', str(deployment_root), 'system',
        ]


def open_admitted_store(engine: Path, store_root: Path, *,
                        deployment_root: Path | None = None,
                        deployment_config: dict | None = None,
                        selection_override=None):
    """Open an already-admitted native store through the verified engine entrypoint."""
    resolved_engine = _engine_directory(engine)
    ep = _load_entrypoint(resolved_engine)
    if ep.deployment_api != FORMAT or ep.engine_api != ENGINE_API:
        raise ValueError('selected engine does not implement the declared deployment API')
    system = ep.system_cls(Path(store_root).resolve(), _selection_override=selection_override)
    _attach_deployment_metadata(system, resolved_engine, deployment_root, deployment_config)
    return system


class DeploymentSelection:
    """Read or replace deployment metadata, including when the incumbent code has drifted."""

    def __init__(self, root, *, descriptor=None):
        self.root = _ordinary_dir(root)
        path = self.root / 'deployment.json'
        raw = _descriptor_bytes(path) if descriptor is None else descriptor
        self.config = _descriptor_object(path, raw)
        if self.config.get('format') != FORMAT:
            raise ValueError('copied-engine deployment is not supported; create a shared reference deployment')
        self.descriptor_sha256 = sha(raw)

    def _require_current(self):
        if sha(_descriptor_bytes(self.root / 'deployment.json')) != self.descriptor_sha256:
            raise ValueError('deployment selection revision conflict; reopen the deployment')

    def stage(self, edition, engine, *, from_head: bool = False):
        self._require_current()
        engine = _engine_directory(engine)
        if from_head:
            if edition is not None:
                raise ValueError('supply either an explicit edition or --from-head, not both')
            prov = _git_provenance(engine)
            if prov is None:
                raise ValueError('stage --from-head requires an engine inside a Git checkout')
            suffix = '' if prov['git_clean'] else '-dirty'
            edition = f"commit-{prov['git_commit'][:12]}{suffix}"
        if edition is None:
            raise ValueError('edition name or --from-head is required')
        packet_id(edition)
        store = self.config.get('store')
        if isinstance(store, dict) and isinstance(store.get('path'), str):
            store = dict(store, path=store['path'].replace('\\', '/'))
        code_sha = code_identity(engine)
        value = dict(self.config, edition=edition,
                     engine=_engine_record(engine, self.root, code_sha))
        if store is not None:
            value['store'] = store
        encoded = encode(value)
        atomic_bytes(self.root / 'candidate.json', encoded)
        result = {'edition': edition, 'candidate_sha256': sha(encoded), 'engine': str(engine)}
        if 'git_commit' in value['engine']:
            result['engine_git_commit'] = value['engine']['git_commit']
            result['engine_git_clean'] = value['engine']['git_clean']
        return result

    def activate(self, edition, expected, manifest_sha, reason):
        if sha(_descriptor_bytes(self.root / 'deployment.json')) != expected:
            raise ValueError('deployment selection revision conflict')
        candidate_path = self.root / 'candidate.json'
        candidate_raw = _descriptor_bytes(candidate_path)
        candidate = _descriptor_object(candidate_path, candidate_raw)
        if candidate.get('edition') != edition or (
                sha(candidate_raw) != manifest_sha and sha(encode(candidate)) != manifest_sha):
            raise ValueError('staged engine reference differs')
        if not reason.strip():
            raise ValueError('new code selection needs a compatibility/receiving account')
        if candidate.get('format') != FORMAT or candidate['engine'].get('engine_api') != ENGINE_API:
            raise ValueError('candidate declares an incompatible deployment/engine API')
        engine = _engine_directory(self.root / candidate['engine']['path'])
        if code_identity(engine) != candidate['engine']['code_sha256']:
            raise ValueError('staged code changed')
        store_root = _ordinary_dir(self.root / candidate['store']['path'])
        binding = runtime_binding(store_root)
        if any(candidate['store'].get(k) != v for k, v in binding.items()):
            raise ValueError('candidate selects an incompatible or different store')
        candidate['account'] = reason
        descriptor_bytes = encode(candidate)
        descriptor_sha = sha(descriptor_bytes)
        atomic_bytes(self.root / 'deployment.json', descriptor_bytes)
        step = 'candidate_cleanup'
        try:
            (self.root / 'candidate.json').unlink()
            step = 'status_readback'
            readback_raw = _descriptor_bytes(self.root / 'deployment.json')
            readback_cfg = _descriptor_object(self.root / 'deployment.json', readback_raw)
            return _status_payload(self.root, engine, store_root, readback_cfg, sha(readback_raw))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            raise ActivationIncomplete(step, descriptor_sha, exc) from exc


def _check_required_domain(runtime: Path, required_domain: str):
    """Verify the store descriptor pins the required domain by ordinary path and exact SHA-256."""
    sys_doc = read_descriptor(runtime / 'system.json')
    domains = sys_doc.get('domains', ())
    if not isinstance(domains, list):
        raise ValueError('invalid domains declaration in store descriptor')
    matched = False
    for binding in domains:
        if (not isinstance(binding, dict)
                or not isinstance(binding.get('path'), str)
                or not isinstance(binding.get('sha256'), str)):
            raise ValueError('invalid domain binding in store descriptor')
        ordinary_path = check_ordinary_ancestors(
            runtime / binding['path'],
            expect_dir=False,
            error_prefix='ordinary selected domain required',
        )
        raw = ordinary_path.read_bytes()
        if sha(raw) != binding['sha256']:
            raise ValueError('selected domain manifest changed: ' + str(ordinary_path))
        dom_doc = read_descriptor(ordinary_path, raw)
        if dom_doc.get('id') == required_domain:
            matched = True
    if not matched:
        raise ValueError(f'this entrance requires a {required_domain}-domain deployment')


class Deployment(DeploymentSelection):
    """Open the selected engine and store after verifying their declared identities."""

    def __init__(self, root, *, descriptor=None):
        super().__init__(root, descriptor=descriptor)
        self.engine = _engine_directory(self.root / self.config['engine']['path'])
        self.runtime = _ordinary_dir(self.root / self.config['store']['path'])
        self._verify_binding()

    @classmethod
    def initialize(cls, root, runtime, engine=None):
        raw_root = Path(root)
        for existing in (raw_root, *raw_root.parents):
            try:
                info = existing.lstat()
            except FileNotFoundError:
                continue
            if is_reparse_or_symlink(info):
                raise ValueError(f'ordinary deployment, engine and store directories required: {existing}')
        runtime = _ordinary_dir(runtime)
        engine = _engine_directory(engine or ENGINE_ROOT)
        resolved_root = Path(os.path.abspath(raw_root))
        if (resolved_root == runtime or resolved_root.is_relative_to(runtime)
                or resolved_root == engine or resolved_root.is_relative_to(engine)):
            raise ValueError('deployment metadata must be separate from store and engine')
        if (resolved_root / 'deployment.json').exists():
            raise ValueError('deployment already exists; stage and activate an explicit new selection')
        binding = runtime_binding(runtime)
        raw_root.mkdir(parents=True, exist_ok=True)
        root_dir = _ordinary_dir(raw_root)
        code_sha = code_identity(engine)
        value = {'format': FORMAT, 'controller_version': VERSION,
                 'engine': _engine_record(engine, root_dir, code_sha),
                 'store': {'path': safe_relpath(runtime, root_dir), **binding},
                 'account': 'Initial explicit shared engine/store selection.'}
        encoded = encode(value)
        atomic_bytes(root_dir / 'deployment.json', encoded)
        return _status_payload(root_dir, engine, runtime, value, sha(encoded))

    def _verify_engine(self):
        if self.config['engine'].get('engine_api') != ENGINE_API:
            raise ValueError('incompatible engine API')
        if code_identity(self.engine) != self.config['engine']['code_sha256']:
            raise ValueError('shared engine code changed; qualify and activate its current identity')

    def _verify_binding(self):
        binding = runtime_binding(self.runtime)
        if any(self.config['store'].get(k) != v for k, v in binding.items()):
            raise ValueError('deployment selected a different store identity/schema')
        self._verify_engine()
        return binding

    def verify(self):
        self._require_current()
        return self._verify_binding()

    @classmethod
    @contextmanager
    def admitted(cls, root, *, engine=None, instance=None, deployment_sha=None, read_only=True,
                 store_sha=None, extra_stores=(), extra_read_only=False, require_domain=None, deadline=None):
        """Enter a pinned domain operation under shared deployment.lock, then sorted store owner.locks."""
        root = _ordinary_dir(root)
        expected_engine = None if engine is None else _engine_directory(engine)
        extra_resolved = tuple(_ordinary_dir(p) for p in extra_stores)
        store_guard = shared if read_only else exclusive

        def acquire(guards):
            guards.enter_context(shared(root / 'deployment.lock'))
            descriptor = read_descriptor_bytes(root / 'deployment.json')
            selection = DeploymentSelection(root, descriptor=descriptor)
            if deployment_sha is not None and selection.descriptor_sha256 != deployment_sha:
                raise ValueError('deployment descriptor differs')
            actual_engine = _engine_directory(root / selection.config['engine']['path'])
            if expected_engine is not None and actual_engine != expected_engine:
                raise ValueError('selected native engine differs')
            if instance is not None and selection.config['store']['instance_id'] != instance:
                raise ValueError('recipient instance differs')
            runtime = _ordinary_dir(root / selection.config['store']['path'])
            all_stores = sorted({runtime, *extra_resolved}, key=lambda p: str(p))
            for store_root in all_stores:
                if store_root == runtime or not extra_read_only:
                    guards.enter_context(store_guard(NativeLayout(store_root).exchange / 'owner.lock'))
                else:
                    guards.enter_context(shared(NativeLayout(store_root).exchange / 'owner.lock',
                                                allow_readonly_open=True))
            return descriptor, runtime

        with owner_admission(acquire, deadline=admission_deadline(deadline)) as (descriptor, runtime):
            if store_sha is not None and sha(read_descriptor_bytes(runtime / 'system.json')) != store_sha:
                raise ValueError('store descriptor differs')
            selected = cls(root, descriptor=descriptor)
            if selected.runtime != runtime:
                raise ValueError('selected store path differs')
            if require_domain is not None:
                _check_required_domain(selected.runtime, require_domain)
            yield selected

    @classmethod
    def current_status(cls, root):
        selected = cls(root)
        return _status_payload(selected.root, selected.engine, selected.runtime,
                               selected.config, selected.descriptor_sha256)

    def status(self):
        return Deployment.current_status(self.root)

    @classmethod
    def dispatch_current(cls, root, arguments):
        root_dir = _ordinary_dir(root)
        if arguments[:1] == ['explain-context']:
            selected = cls(root_dir)
            return selected._dispatch(arguments, already_admitted=True)
        descriptor = read_descriptor_bytes(root_dir / 'deployment.json')
        selection = DeploymentSelection(root_dir, descriptor=descriptor)
        engine = _engine_directory(root_dir / selection.config['engine']['path'])
        runtime = _ordinary_dir(root_dir / selection.config['store']['path'])
        instance = selection.config['store']['instance_id']
        read_only, extra_stores = _precheck_local_system_args(engine, runtime, arguments)
        with cls.admitted(
                root_dir, engine=engine, instance=instance,
                deployment_sha=selection.descriptor_sha256, read_only=read_only,
                extra_stores=extra_stores, extra_read_only=bool(extra_stores)) as bound:
            return bound._dispatch(arguments, already_admitted=True, proof_store_locked=bool(extra_stores))

    def system(self, arguments):
        self._require_current()
        if arguments[:1] == ['explain-context']:
            self.verify()
            return self._dispatch(arguments, already_admitted=True)
        read_only, extra_stores = _precheck_local_system_args(self.engine, self.runtime, arguments)
        with type(self).admitted(
                self.root, engine=self.engine, instance=self.config['store']['instance_id'],
                deployment_sha=self.descriptor_sha256, read_only=read_only,
                extra_stores=extra_stores, extra_read_only=bool(extra_stores)) as bound:
            return bound._dispatch(arguments, already_admitted=True, proof_store_locked=bool(extra_stores))

    def _dispatch(self, arguments, *, deadline=None, already_admitted=False, proof_store_locked=False):
        if not arguments or any(v == '--root' or v.startswith('--root=') for v in arguments):
            raise ValueError('supply a native command; deployment owns its selected store')
        if self.engine != ENGINE_ROOT:
            return self._selected_system(arguments, deadline, already_admitted=already_admitted,
                                         proof_store_locked=proof_store_locked)
        return self._system_action(arguments, deadline, already_admitted=already_admitted,
                                   proof_store_locked=proof_store_locked)

    def _native_entry(self):
        return self.engine / NATIVE_ENTRY

    def _selected_system(self, arguments, deadline=None, *, already_admitted=False, proof_store_locked=False):
        # Isolate cached engine packages when dispatching into an external verified engine root.
        def selected_name(name):
            return name.partition('.')[0] in _ISOLATED_ROOT_PACKAGES

        previous = {name: sys.modules.pop(name) for name in tuple(sys.modules) if selected_name(name)}
        search = sys.path[:]
        try:
            excluded = {HERE, ENGINE_ROOT, SRC_ROOT}
            filtered = [p for p in search if Path(p).resolve() not in excluded]
            if (self.engine / '__init__.py').is_file():
                if self.engine.name == 'managed_ai':
                    sys.path[:] = [str(self.engine.parent), *filtered]
                else:
                    sys.path[:] = filtered
                    _bind_managed_ai_package(self.engine, force=True)
            else:
                sys.path[:] = [str(self.engine), *filtered]
            return self._system_action(arguments, deadline, already_admitted=already_admitted,
                                       proof_store_locked=proof_store_locked)
        finally:
            for name in tuple(sys.modules):
                if selected_name(name):
                    del sys.modules[name]
            sys.modules.update(previous)
            sys.path[:] = search

    def _system_action(self, arguments, deadline=None, *, already_admitted=False, proof_store_locked=False):
        ep = _load_entrypoint(self.engine)
        if ep.deployment_api != FORMAT or ep.engine_api != ENGINE_API:
            raise ValueError('selected engine does not implement the declared deployment API')
        args = ep.parser().parse_args(['--root', str(self.runtime), *arguments])
        if args.action in ('show', 'catalogue') and arguments[0] != args.action:
            raise ValueError('supply the native store observation as the command')
        if args.action == 'init':
            raise ValueError('initialize a store through the central system entrance before deployment')
        try:
            with self._guarded_system(ep, args, deadline, already_admitted=already_admitted) as system:
                if proof_store_locked:
                    return ep.act(system, args, proof_store_locked=True), args, ep
                return ep.act(system, args), args, ep
        except ValueError as exc:
            if args.action == 'adopt':
                basis = self._adopt_basis(ep, exc)
                if basis is not None:
                    raise DeploymentAdoptBasisChanged(str(exc), basis) from exc
            raise

    @staticmethod
    def _adopt_basis(ep, exc):
        describe = getattr(ep, 'adopt_basis', None)
        if describe is not None:
            try:
                basis = describe(exc)
                if type(basis) is dict:
                    json.dumps(basis, allow_nan=False)
                    return basis
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                pass
        return None

    @contextmanager
    def _guarded_system(self, ep, args, deadline=None, *, already_admitted=False):
        if already_admitted:
            yield self._open_selected_store(ep, args)
            return
        if args.action == 'explain-context':
            self.verify()
            yield self._open_selected_store(ep, args)
            return
        read_only = bool(ep.is_readonly_action(args))
        if args.action in {'show', 'catalogue'}:
            with type(self).admitted(
                    self.root,
                    engine=self.engine,
                    instance=self.config['store']['instance_id'],
                    deployment_sha=self.descriptor_sha256,
                    read_only=read_only,
                    deadline=deadline,
            ) as selected:
                yield selected._open_selected_store(ep, args)
            return
        layout = ep.native_layout(ep.external(self.runtime))
        exchange_root = external_root(layout.exchange)
        guard = shared if read_only else exclusive
        with observation_guard(guard, exchange_root / 'owner.lock', deadline=deadline):
            self.verify()
            yield self._open_selected_store(ep, args)

    def _open_selected_store(self, ep, args):
        override = (ep.configuration_descriptor(self.runtime, read(args.spec))
                    if args.action == 'configure' else None)
        system = ep.system_cls(self.runtime, _selection_override=override)
        _attach_deployment_metadata(system, self.engine, self.root, self.config)
        return system


def observation_guard(guard, path, *, deadline=None):
    return owner_admission(lambda guards: guards.enter_context(guard(path)),
                           deadline=admission_deadline(deadline))


@contextmanager
def report_admission(operation):
    """Emit the CLI disposition only for an exhausted pre-operation admission."""
    try:
        yield
    except AdmissionContended as exc:
        print(json.dumps(exc.result(operation)), file=sys.stderr)
        raise SystemExit(75)


def parser():
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument('--deployment', type=Path, required=True)
    p.add_argument('--expected-descriptor', help='Deployment edition verified by the forwarding entrance')
    p.add_argument('--require-domain', help=argparse.SUPPRESS)
    p.add_argument('--admission-deadline', type=float, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init', allow_abbrev=False); init.add_argument('--runtime', '--store', dest='runtime', required=True, type=Path)
    init.add_argument('--engine', type=Path)
    sub.add_parser('status', allow_abbrev=False); sub.add_parser('verify', allow_abbrev=False)
    stage = sub.add_parser('stage', allow_abbrev=False); stage.add_argument('edition', nargs='?'); stage.add_argument('--engine', required=True, type=Path); stage.add_argument('--from-head', action='store_true', help='Derive edition name from the Git HEAD commit of --engine')
    activate = sub.add_parser('activate', allow_abbrev=False); activate.add_argument('edition'); activate.add_argument('--expected', required=True)
    activate.add_argument('--manifest-sha256', required=True); activate.add_argument('--reason', required=True)
    system = sub.add_parser('system', allow_abbrev=False); system.add_argument('arguments', nargs=argparse.REMAINDER)
    return p


def _extra_proof_stores(runtime: Path, parsed) -> tuple[Path, ...]:
    if getattr(parsed, 'action', None) != 'recompose':
        return ()
    proof = getattr(parsed, 'proof_store', None)
    if proof is None:
        return ()
    try:
        resolved = Path(proof).expanduser().resolve(strict=True)
    except OSError:
        return ()
    if resolved == Path(runtime).resolve():
        return ()
    lock_path = NativeLayout(resolved).exchange / 'owner.lock'
    try:
        if not is_ordinary_file_stat(lock_path.lstat()):
            return ()
    except OSError:
        return ()
    return (resolved,)


def _precheck_local_system_args(engine, runtime, arguments):
    """Parse arguments with THIS trusted controller's parser before locks (zero unverified external execution)."""
    if not arguments or any(v == '--root' or v.startswith('--root=') for v in arguments):
        raise ValueError('supply a native command; deployment owns its selected store')
    from managed_ai.store.cli.command_parser import is_readonly_action, parser as native_parser
    if engine != ENGINE_ROOT:
        # Never execute external engine code prior to code_identity(engine) verification.
        # Classify read-only actions conservatively without leaking argparse usage/help output.
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                parsed = native_parser().parse_args(['--root', str(runtime), *arguments])
        except SystemExit:
            return False, ()
        if parsed.action == 'init':
            raise ValueError('initialize a store through the central system entrance before deployment')
        return is_readonly_action(parsed), _extra_proof_stores(runtime, parsed)
    parsed = native_parser().parse_args(['--root', str(runtime), *arguments])
    if parsed.action in ('show', 'catalogue') and arguments[0] != parsed.action:
        raise ValueError('supply the native store observation as the command')
    if parsed.action == 'init':
        raise ValueError('initialize a store through the central system entrance before deployment')
    return is_readonly_action(parsed), _extra_proof_stores(runtime, parsed)


def main():
    args = parser().parse_args()
    raw_root = args.deployment
    try:
        if args.action == 'system' and args.arguments[:1] == ['explain-context']:
            # Read frozen evidence without creating deployment or store lock files (single code_identity).
            root = _ordinary_dir(raw_root)
            selected = Deployment(root)
            if args.expected_descriptor is not None and selected.descriptor_sha256 != args.expected_descriptor:
                raise ValueError('deployment descriptor differs')
            if args.require_domain is not None:
                _check_required_domain(selected.runtime, args.require_domain)
            result, app_args, ep = selected._dispatch(args.arguments, already_admitted=True)
            ep.emit(result, app_args)
            return
        if args.action == 'system':
            root = _ordinary_dir(raw_root)
            descriptor = read_descriptor_bytes(root / 'deployment.json')
            selection = DeploymentSelection(root, descriptor=descriptor)
            if args.expected_descriptor is not None and selection.descriptor_sha256 != args.expected_descriptor:
                raise ValueError('deployment descriptor differs')
            engine = _engine_directory(root / selection.config['engine']['path'])
            runtime = _ordinary_dir(root / selection.config['store']['path'])
            instance = selection.config['store']['instance_id']
            read_only, extra_stores = _precheck_local_system_args(engine, runtime, args.arguments)
            with report_admission(args.action), Deployment.admitted(
                    root, engine=engine, instance=instance, deployment_sha=selection.descriptor_sha256,
                    read_only=read_only, extra_stores=extra_stores, extra_read_only=bool(extra_stores),
                    require_domain=args.require_domain, deadline=args.admission_deadline) as selected:
                result, app_args, ep = selected._dispatch(
                    args.arguments, deadline=args.admission_deadline, already_admitted=True,
                    proof_store_locked=bool(extra_stores))
            ep.emit(result, app_args)
            return
        if args.action == 'init':
            for existing in (raw_root, *raw_root.parents):
                try:
                    if is_reparse_or_symlink(existing.lstat()):
                        raise ValueError(f'ordinary deployment, engine and store directories required: {existing}')
                except FileNotFoundError:
                    continue
            raw_root.mkdir(parents=True, exist_ok=True)
        root = _ordinary_dir(raw_root)
        lock = root / 'deployment.lock'
        guard = shared if args.action in {'status', 'verify'} else exclusive
        admission = observation_guard(guard, lock, deadline=args.admission_deadline)
        with report_admission(args.action), admission:
            if args.expected_descriptor is not None and DeploymentSelection(root).descriptor_sha256 != args.expected_descriptor:
                raise ValueError('deployment descriptor differs')
            if args.action == 'init':
                result = Deployment.initialize(root, args.runtime, args.engine)
            elif args.action == 'stage':
                result = DeploymentSelection(root).stage(args.edition, args.engine, from_head=args.from_head)
            elif args.action == 'activate':
                result = DeploymentSelection(root).activate(args.edition, args.expected, args.manifest_sha256, args.reason)
            elif args.action in {'status', 'verify'}:
                selected = Deployment(root)
                if args.require_domain is not None:
                    _check_required_domain(selected.runtime, args.require_domain)
                result = _status_payload(selected.root, selected.engine, selected.runtime,
                                         selected.config, selected.descriptor_sha256)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        failure = {'status': 'unresolved', 'operation': args.action, 'error': str(exc)}
        if isinstance(exc, ActivationIncomplete):
            failure.update(status='committed_incomplete', failed_step=exc.failed_step,
                           descriptor_sha256=exc.descriptor_sha256)
        elif isinstance(exc, DeploymentAdoptBasisChanged):
            failure['publication_basis'] = exc.basis
        print(json.dumps(failure), file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
